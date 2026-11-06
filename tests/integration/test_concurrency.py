"""Cross-module concurrency tests: storms of transfers, claim payments, stake changes, a currency deletion and idempotent retries run on separate pooled connections must never pay twice, never leave a negative balance or stake, and keep every ledger invariant."""

from __future__ import annotations

import asyncio
import random
import sys
from collections.abc import AsyncIterator, Awaitable, Iterable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import URL, func, select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import claims, ledger, staking
from rapidwire.core import currency as cur
from rapidwire.core.allowances import approve, get_allowance
from rapidwire.core.claims import CancelClaimResult, PayClaimResult, cancel_claim, create_claim, get_claim, pay_claim
from rapidwire.core.errors import CurrencyNotFound, DomainError, InsufficientAllowance, InsufficientBalance, InsufficientStake, InvalidState
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.transfers import TransferFromResult, TransferResult, transfer, transfer_from
from rapidwire.core.types import ClaimStatus, ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.engine import create_engine
from rapidwire.db.tables import STAKE_CUSTODY_ACCOUNT_ID, allowance, balance, claim, execution, stake
from rapidwire.db.tables import transfer as transfer_table
from tests.integration.factories import balance_of, supply_of
from tests.integration.opmodel import assert_failed_execution

T0 = datetime(2026, 8, 1, tzinfo=UTC)
STORM_TIMEOUT_SECONDS = 60


@pytest_asyncio.fixture(loop_scope="session")
async def wide(db: AsyncEngine, migrated_url: URL) -> AsyncIterator[AsyncEngine]:
    """An engine on the emptied test database with a pool large enough for every storm to hold its own connections."""
    eng = create_engine(migrated_url, pool_size=40, max_overflow=40)
    try:
        yield eng
    finally:
        await eng.dispose()


@pytest.fixture
def retries(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every deadlock or lock wait timeout retry: retry_delay() is replaced by a counting wrapper in each rapidwire module that imported it."""
    counted: list[int] = []
    original = ledger.retry_delay

    def counting(attempt: int) -> float:
        counted.append(attempt)
        return original(attempt)

    patched = [module for name, module in list(sys.modules.items()) if name.startswith("rapidwire.") and getattr(module, "retry_delay", None) is original]
    assert ledger in patched
    for module in patched:
        monkeypatch.setattr(module, "retry_delay", counting)
    return counted


class _LockSignal:
    """Sets locked when finalize_delete_in_tx() starts canceling the currency's pending claims, which it does inside its transaction while holding the currency FOR UPDATE (checked through tx.currency_lock())."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.locked = asyncio.Event()
        original = claims.cancel_pending_claims_for_currency

        async def signalling(tx: ledger.LedgerTx, execution_id: int, *, currency_id: int) -> int:
            assert tx.currency_lock(currency_id) == "update"
            self.locked.set()
            return await original(tx, execution_id, currency_id=currency_id)

        monkeypatch.setattr(claims, "cancel_pending_claims_for_currency", signalling)


async def _storm(calls: Iterable[Awaitable[Any]]) -> list[Any]:
    """Run the calls concurrently and return each result or DomainError; any other exception fails the test."""
    async with asyncio.timeout(STORM_TIMEOUT_SECONDS):
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(outcome, DomainError):
            raise outcome
    return outcomes


async def _check_failures(engine: AsyncEngine, outcomes: Iterable[Any], allowed: tuple[type[DomainError], ...]) -> None:
    for outcome in outcomes:
        if isinstance(outcome, DomainError):
            assert isinstance(outcome, allowed), f"unexpected {type(outcome).__name__}: {outcome.message}"
            await assert_failed_execution(engine, outcome)


async def _assert_ledger_ok(engine: AsyncEngine) -> None:
    report = await verify_invariants(engine)
    assert report.ok, report.violations
    async with engine.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(balance).where(balance.c.account_id > 0, balance.c.amount < 0))).scalar_one() == 0
        assert (await conn.execute(select(func.count()).select_from(stake).where(stake.c.amount <= 0))).scalar_one() == 0


async def _row_lock_waits(engine: AsyncEngine) -> int:
    """The server-wide count of row lock waits since start."""
    async with engine.connect() as conn:
        row = (await conn.execute(text("SHOW GLOBAL STATUS LIKE 'Innodb_row_lock_waits'"))).one()
    return int(row[1])


async def _new_currency(engine: AsyncEngine, guild_id: int, owner: int, symbol: str, *, supply: int, rate: int = 0) -> InMemoryGuildInfo:
    guilds = InMemoryGuildInfo()
    guilds.add_guild(guild_id, owner)
    await cur.create_currency(engine, guilds, guild_id=guild_id, user_id=owner, name=symbol.capitalize(), symbol=symbol, initial_supply=supply, hourly_rate_ppm=rate, now=T0)
    return guilds


async def _fund(engine: AsyncEngine, owner: int, currency_id: int, amounts: dict[int, int]) -> None:
    for user_id, amount in amounts.items():
        await transfer(engine, sender_id=owner, recipient_id=user_id, currency_id=currency_id, amount=amount, now=T0)


async def _execution_ids(engine: AsyncEngine, type: ExecutionType) -> list[int]:
    async with engine.connect() as conn:
        return list((await conn.execute(select(execution.c.execution_id).where(execution.c.type == type.value).order_by(execution.c.execution_id))).scalars())


async def _transfer_count(engine: AsyncEngine, execution_ids: Iterable[int]) -> int:
    ids = list(execution_ids)
    if not ids:
        return 0
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(transfer_table).where(transfer_table.c.execution_id.in_(ids)))).scalar_one())


async def _interest_of(engine: AsyncEngine, account_id: int, currency_id: int) -> int:
    """The interest minted to the account's stake: the transfers of interest executions it initiated."""
    stmt = (
        select(transfer_table.c.amount)
        .join(execution, execution.c.execution_id == transfer_table.c.execution_id)
        .where(execution.c.type == ExecutionType.INTEREST.value, execution.c.initiator_id == account_id, transfer_table.c.currency_id == currency_id)
    )
    async with engine.connect() as conn:
        return sum(int(amount) for amount in (await conn.execute(stmt)).scalars())


async def test_ring_and_hot_account_transfers_conserve_money(wide: AsyncEngine, retries: list[int]) -> None:
    guild, owner, hot = 8001, 20, 30
    ring = list(range(21, 29))
    await _new_currency(wide, guild, owner, "RING", supply=100_000)
    await _fund(wide, owner, guild, {**dict.fromkeys(ring, 1_000), hot: 500})
    start = {user_id: await balance_of(wide, user_id, guild) for user_id in [owner, hot, *ring]}
    rng = random.Random(8001)  # noqa: S311
    planned = [(sender, ring[(index + 1) % len(ring)], rng.randrange(1, 400)) for _ in range(5) for index, sender in enumerate(ring)]
    planned += [(hot, ring[index % len(ring)], 50) for index in range(30)]
    rng.shuffle(planned)
    waits_before = await _row_lock_waits(wide)

    outcomes = await _storm(transfer(wide, sender_id=sender, recipient_id=recipient, currency_id=guild, amount=amount, now=T0) for sender, recipient, amount in planned)

    assert await _row_lock_waits(wide) > waits_before
    assert retries == []
    await _check_failures(wide, outcomes, (InsufficientBalance,))
    expected = dict(start)
    succeeded = [(plan, outcome) for plan, outcome in zip(planned, outcomes, strict=True) if isinstance(outcome, TransferResult)]
    for (sender, recipient, amount), _ in succeeded:
        expected[sender] -= amount
        expected[recipient] += amount
    for user_id, amount in expected.items():
        assert await balance_of(wide, user_id, guild) == amount
    assert sum(1 for (sender, _, _), _ in succeeded if sender == hot) == 10
    assert sum(expected.values()) == await supply_of(wide, guild) == 100_000
    assert await _transfer_count(wide, (outcome.execution_id for _, outcome in succeeded)) == len(succeeded)
    assert len({outcome.transfer_id for _, outcome in succeeded}) == len(succeeded)
    await _assert_ledger_ok(wide)


async def test_claim_double_pay_storm_pays_each_claim_at_most_once(wide: AsyncEngine, retries: list[int]) -> None:
    guild, owner, payer = 8002, 40, 41
    claimants = list(range(42, 48))
    await _new_currency(wide, guild, owner, "CLAIM", supply=10_000)
    await _fund(wide, owner, guild, {payer: 350})
    created = [await create_claim(wide, claimant_id=claimant, payer_id=payer, currency_id=guild, amount=100, now=T0) for claimant in claimants]
    planned: list[tuple[str, int, int]] = []
    for entry in created:
        planned += [("pay", entry.claim_id, payer)] * 6
        planned += [("cancel", entry.claim_id, entry.claimant_id), ("cancel", entry.claim_id, payer)]
    random.Random(8002).shuffle(planned)  # noqa: S311

    def call(action: str, claim_id: int, user_id: int) -> Awaitable[Any]:
        if action == "pay":
            return pay_claim(wide, claim_id=claim_id, payer_id=user_id, now=T0)
        return cancel_claim(wide, claim_id=claim_id, user_id=user_id, now=T0)

    outcomes = await _storm(call(*plan) for plan in planned)

    assert retries == []
    await _check_failures(wide, outcomes, (InvalidState, InsufficientBalance))
    paid = 0
    for entry in created:
        mine = [outcome for (_, claim_id, _), outcome in zip(planned, outcomes, strict=True) if claim_id == entry.claim_id]
        pays = [outcome for outcome in mine if isinstance(outcome, PayClaimResult)]
        cancels = [outcome for outcome in mine if isinstance(outcome, CancelClaimResult)]
        assert len(pays) + len(cancels) == 1
        stored = await get_claim(wide, entry.claim_id)
        winner = (pays + cancels)[0]
        assert stored.resolved_execution_id == winner.execution_id
        assert stored.status is (ClaimStatus.PAID if pays else ClaimStatus.CANCELED)
        assert await balance_of(wide, entry.claimant_id, guild) == (100 if pays else 0)
        assert await _transfer_count(wide, (outcome.execution_id for outcome in pays)) == len(pays)
        paid += len(pays)
    assert paid <= 3
    assert await balance_of(wide, payer, guild) == 350 - 100 * paid
    assert await _transfer_count(wide, await _execution_ids(wide, ExecutionType.CLAIM_PAY)) == paid
    await _assert_ledger_ok(wide)


async def test_stake_storm_with_accruals_and_rate_change(wide: AsyncEngine) -> None:
    guild, owner, alice, bob = 8003, 50, 51, 52
    funding = 1_000_000
    guilds = await _new_currency(wide, guild, owner, "STAKE", supply=10_000_000, rate=10_000)
    await _fund(wide, owner, guild, {alice: funding, bob: funding})
    for user_id in (alice, bob):
        await staking.deposit(wide, account_id=user_id, currency_id=guild, amount=500_000, now=T0)
    await cur.request_rate_change(wide, guilds, currency_id=guild, user_id=owner, new_rate_ppm=20_000, now=T0)
    start = T0 + timedelta(days=8)
    rng = random.Random(8003)  # noqa: S311
    calls: list[Awaitable[Any]] = []
    for _ in range(30):
        operation = rng.choice((staking.deposit, staking.withdraw))
        calls.append(operation(wide, account_id=rng.choice((alice, bob)), currency_id=guild, amount=rng.randrange(1, 400_000), now=start + timedelta(minutes=rng.randrange(0, 120))))
    calls.append(staking.withdraw(wide, account_id=alice, currency_id=guild, amount=10**12, now=start))
    calls += [staking.accrue_stale_stakes(wide, older_than=timedelta(0), now=start + timedelta(minutes=30 * index)) for index in range(4)]
    calls.append(cur.apply_rate_change(wide, guilds, currency_id=guild, user_id=owner, now=start + timedelta(minutes=60)))
    rng.shuffle(calls)

    outcomes = await _storm(calls)

    await _check_failures(wide, outcomes, (InsufficientStake, InsufficientBalance))
    assert any(isinstance(outcome, staking.StakeChanged) for outcome in outcomes)
    assert sum(1 for outcome in outcomes if isinstance(outcome, InsufficientStake | InsufficientBalance)) >= 1
    sweeps = [outcome for outcome in outcomes if isinstance(outcome, staking.StaleAccrualResult)]
    assert len(sweeps) == 4
    assert all(sweep.failed == 0 for sweep in sweeps)
    applied = [outcome for outcome in outcomes if isinstance(outcome, cur.RateChangeApplied)]
    assert len(applied) == 1
    info = await cur.get_currency(wide, guild)
    assert info is not None
    assert (info.hourly_rate_ppm, info.pending_rate_ppm) == (20_000, None)
    interest_ids = await _execution_ids(wide, ExecutionType.INTEREST)
    assert interest_ids
    async with wide.connect() as conn:
        rows = (await conn.execute(select(execution.c.execution_id, execution.c.status).where(execution.c.execution_id.in_(interest_ids)))).all()
        moves = (await conn.execute(select(transfer_table).where(transfer_table.c.execution_id.in_(interest_ids)))).mappings().all()
    assert {row.status for row in rows} == {ExecutionStatus.SUCCESS.value}
    assert sorted(move["execution_id"] for move in moves) == interest_ids
    assert all(move["kind"] == TransferKind.INTEREST.value and move["to_account"] == STAKE_CUSTODY_ACCOUNT_ID and move["from_account"] == info.issuance_account_id for move in moves)
    referenced = {outcome.interest_execution_id for outcome in outcomes if isinstance(outcome, staking.StakeChanged) and outcome.interest_execution_id is not None}
    referenced |= {execution_id for outcome in applied for execution_id in outcome.interest_execution_ids}
    assert referenced <= set(interest_ids)
    stakes = 0
    for user_id in (alice, bob):
        entry = await staking.get_stake(wide, user_id, guild)
        staked = 0 if entry is None else entry.amount
        stakes += staked
        assert await balance_of(wide, user_id, guild) + staked == funding + await _interest_of(wide, user_id, guild)
    assert await balance_of(wide, STAKE_CUSTODY_ACCOUNT_ID, guild) == stakes
    assert await supply_of(wide, guild) == 10_000_000 + sum(int(move["amount"]) for move in moves)
    await _assert_ledger_ok(wide)


async def test_delete_finalize_racing_holder_operations(wide: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    guild, owner = 8004, 60
    holders = list(range(61, 66))
    supply = 100_000
    guilds = await _new_currency(wide, guild, owner, "DOOM", supply=supply)
    await _fund(wide, owner, guild, dict.fromkeys(holders, 10_000))
    await approve(wide, owner_id=61, spender_id=62, currency_id=guild, amount=5_000, now=T0)
    await approve(wide, owner_id=63, spender_id=64, currency_id=guild, amount=5_000, now=T0)
    created = [await create_claim(wide, claimant_id=62, payer_id=61, currency_id=guild, amount=100, now=T0) for _ in range(3)]
    for user_id in (61, 62, 63):
        await staking.deposit(wide, account_id=user_id, currency_id=guild, amount=1_000, now=T0)
    await cur.request_delete(wide, guilds, currency_id=guild, user_id=owner, now=T0)
    at = T0 + timedelta(days=8)
    rng = random.Random(8004)  # noqa: S311
    signal = _LockSignal(monkeypatch)
    first_success = asyncio.Event()

    def holder_call(index: int) -> Awaitable[Any]:
        kind = index % 6
        user_id = holders[index % len(holders)]
        if kind == 0:
            return transfer(wide, sender_id=user_id, recipient_id=holders[(index + 1) % len(holders)], currency_id=guild, amount=rng.randrange(10, 100), now=at)
        if kind == 1:
            spender, owner_id = (62, 61) if index % 2 else (64, 63)
            return transfer_from(wide, spender_id=spender, owner_id=owner_id, recipient_id=65, currency_id=guild, amount=100, now=at)
        if kind == 2:
            return pay_claim(wide, claim_id=created[index % len(created)].claim_id, payer_id=61, now=at)
        if kind == 3:
            return staking.deposit(wide, account_id=61 + index % 3, currency_id=guild, amount=200, now=at)
        if kind == 4:
            return staking.withdraw(wide, account_id=61 + index % 3, currency_id=guild, amount=300, now=at)
        return cur.burn(wide, account_id=user_id, currency_id=guild, amount=rng.randrange(1, 50), now=at)

    async def before(index: int) -> Any:
        result = await holder_call(index)
        first_success.set()
        return result

    async def finalize() -> cur.CurrencyDeleted:
        await first_success.wait()
        return await cur.finalize_delete(wide, guilds, currency_id=guild, user_id=owner, now=at)

    async def after(index: int) -> Any:
        await signal.locked.wait()
        return await holder_call(index)

    early, late = range(24), range(24, 48)
    outcomes = await _storm([finalize(), *(before(index) for index in early), *(after(index) for index in late)])

    deleted, early_outcomes, late_outcomes = outcomes[0], outcomes[1 : 1 + len(early)], outcomes[1 + len(early) :]
    assert isinstance(deleted, cur.CurrencyDeleted)
    await _check_failures(wide, early_outcomes, (CurrencyNotFound, InsufficientBalance, InsufficientStake, InvalidState))
    await _check_failures(wide, late_outcomes, (CurrencyNotFound, InvalidState))
    assert any(not isinstance(outcome, DomainError) for outcome in early_outcomes)
    for index, outcome in zip(late, late_outcomes, strict=True):
        assert isinstance(outcome, InvalidState if index % 6 == 2 else CurrencyNotFound)
    burned_before = sum(outcome.amount for outcome in early_outcomes if isinstance(outcome, cur.SupplyChanged))
    assert deleted.burned_amount == supply - burned_before
    info = await cur.get_currency(wide, guild, include_deleted=True)
    assert info is not None
    assert info.deleted_at is not None
    assert await cur.get_currency(wide, guild) is None
    assert await supply_of(wide, guild) == 0
    async with wide.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(balance).where(balance.c.currency_id == guild, balance.c.amount != 0))).scalar_one() == 0
        assert (await conn.execute(select(func.count()).select_from(stake).where(stake.c.currency_id == guild))).scalar_one() == 0
        assert (await conn.execute(select(func.count()).select_from(allowance).where(allowance.c.currency_id == guild))).scalar_one() == 0
        statuses = {row.claim_id: (row.status, row.resolved_execution_id) for row in await conn.execute(select(claim).where(claim.c.currency_id == guild))}
    pays = {outcome.claim_id: outcome.execution_id for outcome in early_outcomes if isinstance(outcome, PayClaimResult)}
    for entry in created:
        if entry.claim_id in pays:
            assert statuses[entry.claim_id] == (ClaimStatus.PAID.value, pays[entry.claim_id])
        else:
            assert statuses[entry.claim_id] == (ClaimStatus.CANCELED.value, deleted.execution_id)
    assert deleted.claims_canceled == len(created) - len(pays)
    assert deleted.allowances_deleted == 2
    await _assert_ledger_ok(wide)


async def test_idempotency_key_storms_execute_once(wide: AsyncEngine) -> None:
    guild, owner, payer, payee = 8005, 70, 71, 72
    await _new_currency(wide, guild, owner, "ONCE", supply=10_000)
    await _fund(wide, owner, guild, {payer: 1_000})
    bill = await create_claim(wide, claimant_id=payee, payer_id=payer, currency_id=guild, amount=100, now=T0)

    sent = await _storm(transfer(wide, sender_id=payer, recipient_id=payee, currency_id=guild, amount=10, now=T0, idempotency_key="storm-transfer") for _ in range(25))
    assert all(isinstance(outcome, TransferResult) for outcome in sent)
    assert len({outcome.execution_id for outcome in sent}) == 1
    assert sum(1 for outcome in sent if not outcome.replayed) == 1

    refused = await _storm(transfer(wide, sender_id=payer, recipient_id=payee, currency_id=guild, amount=10**6, now=T0, idempotency_key="storm-refused") for _ in range(25))
    assert all(isinstance(outcome, InsufficientBalance) for outcome in refused)
    assert len({outcome.execution_id for outcome in refused}) == 1
    await _check_failures(wide, refused, (InsufficientBalance,))

    paid = await _storm(pay_claim(wide, claim_id=bill.claim_id, payer_id=payer, now=T0, idempotency_key="storm-pay") for _ in range(25))
    assert all(isinstance(outcome, PayClaimResult) for outcome in paid)
    assert len({outcome.execution_id for outcome in paid}) == 1

    await approve(wide, owner_id=payer, spender_id=payee, currency_id=guild, amount=3, now=T0)
    keys = [f"storm-mixed-{index}" for index in range(5)]
    mixed = await _storm(transfer_from(wide, spender_id=payee, owner_id=payer, recipient_id=owner, currency_id=guild, amount=1, now=T0, idempotency_key=key) for key in keys for _ in range(5))
    await _check_failures(wide, mixed, (InsufficientAllowance,))
    by_key = [mixed[index * 5 : index * 5 + 5] for index in range(len(keys))]
    assert all(len({type(outcome) for outcome in group}) == 1 and len({outcome.execution_id for outcome in group}) == 1 for group in by_key)
    assert sum(1 for group in by_key if isinstance(group[0], TransferFromResult)) == 3
    assert await get_allowance(wide, payer, payee, guild) == 0

    async with wide.connect() as conn:
        keyed = (await conn.execute(select(execution.c.idempotency_key, func.count()).where(execution.c.idempotency_key.is_not(None)).group_by(execution.c.idempotency_key))).all()
    assert dict(keyed) == dict.fromkeys(["storm-transfer", "storm-refused", "storm-pay", *keys], 1)
    assert await _transfer_count(wide, [sent[0].execution_id, paid[0].execution_id]) == 2
    assert await balance_of(wide, payer, guild) == 1_000 - 10 - 100 - 3
    assert await balance_of(wide, payee, guild) == 10 + 100
    await _assert_ledger_ok(wide)
