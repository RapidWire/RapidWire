from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import staking
from rapidwire.core.currency import burn
from rapidwire.core.errors import CurrencyNotFound, DomainError, InsufficientBalance, InsufficientStake, InvalidAmount, InvalidArgument
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import CurrencyLockEscalation, LedgerTx, run_child, run_operation
from rapidwire.core.types import ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.tables import STAKE_CUSTODY_ACCOUNT_ID, currency, execution, stake, transfer
from rapidwire.db.types import AMOUNT_MAX
from tests.integration.factories import SeededCurrency, balance_of, create_currency, mint, supply_of

T0 = datetime(2026, 6, 1, tzinfo=UTC)
CUR = 41
ALICE = 111
BOB = 222
RATE = 10_000  # 1%/h


@pytest.fixture
async def seeded(db: AsyncEngine) -> SeededCurrency:
    seeded = await create_currency(db, currency_id=CUR, symbol="STK", hourly_rate_ppm=RATE, now=T0)
    await mint(db, CUR, ALICE, 10_000_000, now=T0)
    await mint(db, CUR, BOB, 10_000_000, now=T0)
    return seeded


async def _executions(engine: AsyncEngine, type: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(execution).where(execution.c.type == type).order_by(execution.c.execution_id))).mappings().all()
    return [dict(row) for row in rows]


async def _transfers(engine: AsyncEngine, execution_id: int) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(transfer).where(transfer.c.execution_id == execution_id).order_by(transfer.c.transfer_id))).mappings().all()
    return [dict(row) for row in rows]


async def _stake_row(engine: AsyncEngine, account_id: int, currency_id: int = CUR) -> dict[str, Any] | None:
    async with engine.connect() as conn:
        row = (await conn.execute(select(stake).where(stake.c.account_id == account_id, stake.c.currency_id == currency_id))).mappings().one_or_none()
    return None if row is None else dict(row)


async def _assert_invariants(engine: AsyncEngine) -> None:
    report = await verify_invariants(engine)
    assert report.ok, report.violations


async def test_first_deposit_creates_stake_without_interest(db: AsyncEngine, seeded: SeededCurrency) -> None:
    result = await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000_000, now=T0)
    assert (result.amount, result.stake_amount, result.interest, result.interest_execution_id) == (1_000_000, 1_000_000, 0, None)
    [move] = await _transfers(db, result.execution_id)
    assert (move["from_account"], move["to_account"], move["amount"], move["kind"]) == (ALICE, STAKE_CUSTODY_ACCOUNT_ID, 1_000_000, TransferKind.PRINCIPAL)
    row = await _stake_row(db, ALICE)
    assert row is not None
    assert (row["amount"], row["carry"], row["last_accrued_at"]) == (1_000_000, 0, T0)
    assert await balance_of(db, ALICE, CUR) == 9_000_000
    assert await balance_of(db, STAKE_CUSTODY_ACCOUNT_ID, CUR) == 1_000_000
    assert await _executions(db, ExecutionType.INTEREST) == []
    await _assert_invariants(db)


async def test_deposit_accrues_first_with_its_own_interest_execution(db: AsyncEngine, seeded: SeededCurrency) -> None:
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000_000, now=T0)
    supply_before = await supply_of(db, CUR)
    result = await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=500, now=T0 + timedelta(hours=2))
    # 1,000,000 * 1.01^2 = 1,020,100.
    assert result.interest == 20_100
    assert result.stake_amount == 1_020_100 + 500
    [interest] = await _executions(db, ExecutionType.INTEREST)
    assert interest["execution_id"] == result.interest_execution_id
    assert interest["initiator_id"] == ALICE
    assert interest["status"] == ExecutionStatus.SUCCESS
    assert interest["input"]["trigger"] == "deposit"
    assert interest["input"]["trigger_execution_id"] == str(result.execution_id)
    assert interest["input"]["rate_ppm"] == str(RATE)
    assert interest["input"]["elapsed_microseconds"] == str(2 * 3_600_000_000)
    assert (interest["input"]["old_amount"], interest["input"]["new_amount"]) == ("1000000", "1020100")
    [move] = await _transfers(db, interest["execution_id"])
    assert (move["from_account"], move["to_account"], move["amount"], move["kind"]) == (seeded.issuance_account_id, STAKE_CUSTODY_ACCOUNT_ID, 20_100, TransferKind.INTEREST)
    assert await supply_of(db, CUR) == supply_before + 20_100
    row = await _stake_row(db, ALICE)
    assert row is not None
    assert (row["amount"], row["last_accrued_at"]) == (1_020_600, T0 + timedelta(hours=2))
    await _assert_invariants(db)


async def test_fractional_accrual_keeps_carry(db: AsyncEngine, seeded: SeededCurrency) -> None:
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000_000, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1, now=T0 + timedelta(minutes=30))
    row = await _stake_row(db, ALICE)
    assert row is not None
    expected = staking.compute_accrual(1_000_000, 0, RATE, 1_800_000_000)
    assert (row["amount"], row["carry"]) == (expected.amount + 1, expected.carry)
    assert row["carry"] > 0


async def test_withdraw_accrues_then_moves_back_and_removes_empty_stake(db: AsyncEngine, seeded: SeededCurrency) -> None:
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000_000, now=T0)
    partial = await staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=100, now=T0 + timedelta(hours=1))
    assert partial.interest == 10_000
    assert partial.stake_amount == 1_010_000 - 100
    [move] = await _transfers(db, partial.execution_id)
    assert (move["from_account"], move["to_account"], move["amount"]) == (STAKE_CUSTODY_ACCOUNT_ID, ALICE, 100)
    [interest] = await _executions(db, ExecutionType.INTEREST)
    assert interest["input"]["trigger"] == "withdraw"
    full = await staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=1_009_900, now=T0 + timedelta(hours=1))
    assert full.stake_amount == 0
    assert full.interest_execution_id is None
    assert await _stake_row(db, ALICE) is None
    assert await staking.get_stake(db, ALICE, CUR) is None
    assert await balance_of(db, ALICE, CUR) == 10_000_000 + 10_000
    assert await balance_of(db, STAKE_CUSTODY_ACCOUNT_ID, CUR) == 0
    await _assert_invariants(db)


async def test_failed_withdraw_rolls_back_its_accrual(db: AsyncEngine, seeded: SeededCurrency) -> None:
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000, now=T0)
    with pytest.raises(InsufficientStake) as info:
        await staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=5_000, now=T0 + timedelta(hours=3))
    assert info.value.execution_id is not None
    assert await _executions(db, ExecutionType.INTEREST) == []
    row = await _stake_row(db, ALICE)
    assert row is not None
    assert (row["amount"], row["last_accrued_at"]) == (1_000, T0)
    with pytest.raises(InsufficientStake):
        await staking.withdraw(db, account_id=BOB, currency_id=CUR, amount=1, now=T0)
    with pytest.raises(InsufficientBalance):
        await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=10**12, now=T0)
    with pytest.raises(InvalidAmount):
        await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=0, now=T0)
    with pytest.raises(CurrencyNotFound):
        await staking.deposit(db, account_id=ALICE, currency_id=999, amount=1, now=T0)
    await _assert_invariants(db)


async def test_rate_zero_advances_timestamp_without_execution(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR, symbol="ZERO", hourly_rate_ppm=0, now=T0)
    await mint(db, CUR, ALICE, 1_000, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=100, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=100, now=T0 + timedelta(hours=5))
    row = await _stake_row(db, ALICE)
    assert row is not None
    assert (row["amount"], row["carry"], row["last_accrued_at"]) == (200, 0, T0 + timedelta(hours=5))
    assert await _executions(db, ExecutionType.INTEREST) == []


async def test_idempotent_deposit_replays(db: AsyncEngine, seeded: SeededCurrency) -> None:
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000, now=T0)
    first = await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=10, now=T0 + timedelta(hours=1), idempotency_key="k1")
    again = await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=10, now=T0 + timedelta(hours=2), idempotency_key="k1")
    assert again.replayed and not first.replayed
    assert (again.execution_id, again.stake_amount, again.interest_execution_id) == (first.execution_id, first.stake_amount, first.interest_execution_id)
    row = await _stake_row(db, ALICE)
    assert row is not None
    assert row["amount"] == first.stake_amount


async def test_supply_cap_halts_interest(db: AsyncEngine) -> None:
    seeded = await create_currency(db, currency_id=CUR, symbol="BIG", hourly_rate_ppm=1_000_000, now=T0)
    big = 10**37
    await mint(db, CUR, ALICE, big, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=big, now=T0)
    result = await staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=1, now=T0 + timedelta(hours=4))
    assert result.interest == AMOUNT_MAX - big
    assert await supply_of(db, CUR) == AMOUNT_MAX
    async with db.connect() as conn:
        halted = (await conn.execute(select(currency.c.interest_halted).where(currency.c.currency_id == CUR))).scalar_one()
    assert halted
    [interest] = await _executions(db, ExecutionType.INTEREST)
    assert interest["output"]["halted"] is True
    [move] = await _transfers(db, interest["execution_id"])
    assert (move["from_account"], move["amount"]) == (seeded.issuance_account_id, AMOUNT_MAX - big)
    later = await staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=AMOUNT_MAX - 1, now=T0 + timedelta(hours=10))
    assert (later.interest, later.stake_amount) == (0, 0)
    assert await balance_of(db, ALICE, CUR) == AMOUNT_MAX
    assert len(await _executions(db, ExecutionType.INTEREST)) == 1
    info = await staking.get_stake(db, ALICE, CUR)
    assert info is None
    await _assert_invariants(db)


async def test_accrue_stale_stakes(db: AsyncEngine, seeded: SeededCurrency) -> None:
    await create_currency(db, currency_id=CUR + 1, symbol="FLAT", hourly_rate_ppm=0, now=T0)
    await create_currency(db, currency_id=CUR + 2, symbol="HALT", hourly_rate_ppm=RATE, now=T0)
    await mint(db, CUR + 1, ALICE, 1_000, now=T0)
    await mint(db, CUR + 2, ALICE, 1_000, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000_000, now=T0)
    await staking.deposit(db, account_id=BOB, currency_id=CUR, amount=2_000_000, now=T0 + timedelta(hours=2))
    await staking.deposit(db, account_id=ALICE, currency_id=CUR + 1, amount=1_000, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR + 2, amount=1_000, now=T0)
    async with db.begin() as conn:
        await conn.execute(update(currency).where(currency.c.currency_id == CUR + 2).values(interest_halted=True))
    now = T0 + timedelta(hours=3)
    accrued = await staking.accrue_stale_stakes(db, older_than=timedelta(hours=3), now=now, batch_size=1)
    assert accrued == staking.StaleAccrualResult(credited=1, without_interest=0, failed=0)
    [interest] = await _executions(db, ExecutionType.INTEREST)
    assert interest["input"]["trigger"] == "worker"
    assert interest["initiator_id"] == ALICE
    assert interest["input"]["trigger_execution_id"] is None
    alice = await _stake_row(db, ALICE)
    assert alice is not None
    assert (alice["amount"], alice["last_accrued_at"]) == (1_030_301, now)
    bob = await _stake_row(db, BOB)
    assert bob is not None
    assert bob["last_accrued_at"] == T0 + timedelta(hours=2)
    assert await staking.accrue_stale_stakes(db, older_than=timedelta(hours=3), now=now) == staking.StaleAccrualResult(credited=0, without_interest=0, failed=0)
    assert await staking.accrue_stale_stakes(db, older_than=timedelta(0), now=now + timedelta(hours=1), batch_size=1) == staking.StaleAccrualResult(credited=2, without_interest=0, failed=0)
    await _assert_invariants(db)


async def test_accrue_stale_stakes_counts_stakes_without_interest_apart(db: AsyncEngine, seeded: SeededCurrency) -> None:
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1, now=T0)
    await staking.deposit(db, account_id=BOB, currency_id=CUR, amount=1_000_000, now=T0)
    now = T0 + timedelta(hours=4)
    result = await staking.accrue_stale_stakes(db, older_than=timedelta(hours=3), now=now)
    # 1 * 1.01^4 is below 2, so Alice's stake gains only carry.
    assert result == staking.StaleAccrualResult(credited=1, without_interest=1, failed=0)
    [interest] = await _executions(db, ExecutionType.INTEREST)
    assert interest["initiator_id"] == BOB
    dust = await _stake_row(db, ALICE)
    assert dust is not None
    expected = staking.compute_accrual(1, 0, RATE, 4 * staking.MICROSECONDS_PER_HOUR)
    assert (expected.amount, expected.interest) == (1, 0)
    assert (dust["amount"], dust["carry"], dust["last_accrued_at"]) == (1, expected.carry, now)
    assert dust["carry"] > 0
    await _assert_invariants(db)


async def test_worker_accrual_with_no_room_under_the_cap_halts_without_interest(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR, symbol="FULL", hourly_rate_ppm=RATE, now=T0)
    await mint(db, CUR, ALICE, AMOUNT_MAX, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000_000, now=T0)
    now = T0 + timedelta(hours=4)
    assert await staking.accrue_stale_stakes(db, older_than=timedelta(hours=3), now=now) == staking.StaleAccrualResult(credited=0, without_interest=1, failed=0)
    assert await _executions(db, ExecutionType.INTEREST) == []
    row = await _stake_row(db, ALICE)
    assert row is not None
    assert (row["amount"], row["carry"], row["last_accrued_at"]) == (1_000_000, 0, now)
    async with db.connect() as conn:
        assert (await conn.execute(select(currency.c.interest_halted).where(currency.c.currency_id == CUR))).scalar_one()
    assert await supply_of(db, CUR) == AMOUNT_MAX
    await _assert_invariants(db)


async def test_supply_cap_halt_survives_a_burn_below_the_cap(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR, symbol="BIG", hourly_rate_ppm=1_000_000, now=T0)
    big = 10**37
    await mint(db, CUR, ALICE, big, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=big, now=T0)
    halted = await staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=10**36, now=T0 + timedelta(hours=4))
    assert halted.interest == AMOUNT_MAX - big
    assert await supply_of(db, CUR) == AMOUNT_MAX
    await burn(db, account_id=ALICE, currency_id=CUR, amount=10**36, now=T0 + timedelta(hours=4))
    assert await supply_of(db, CUR) == AMOUNT_MAX - 10**36
    assert await staking.accrue_stale_stakes(db, older_than=timedelta(0), now=T0 + timedelta(hours=10)) == staking.StaleAccrualResult(credited=0, without_interest=0, failed=0)
    later = await staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=1, now=T0 + timedelta(hours=12))
    assert (later.interest, later.interest_execution_id, later.stake_amount) == (0, None, AMOUNT_MAX - 10**36 - 1)
    deposited = await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1, now=T0 + timedelta(hours=20))
    assert (deposited.interest, deposited.interest_execution_id, deposited.stake_amount) == (0, None, AMOUNT_MAX - 10**36)
    assert len(await _executions(db, ExecutionType.INTEREST)) == 1
    assert await supply_of(db, CUR) == AMOUNT_MAX - 10**36
    info = await staking.get_stake(db, ALICE, CUR)
    assert info is not None and info.interest_halted
    assert staking.projected_amount(info, T0 + timedelta(days=30), supply=AMOUNT_MAX - 10**36) == info.amount
    await _assert_invariants(db)


async def test_get_and_list_stakes(db: AsyncEngine) -> None:
    for index in range(17):
        currency_id = 100 + index
        await create_currency(db, currency_id=currency_id, symbol="S" + chr(ord("A") + index), hourly_rate_ppm=index, now=T0)
        await mint(db, currency_id, ALICE, 1_000, now=T0)
        await staking.deposit(db, account_id=ALICE, currency_id=currency_id, amount=10 + index, now=T0)
    first = await staking.list_stakes(db, ALICE, page=1)
    assert (first.total, first.pages, first.per_page, len(first.items), first.total_is_lower_bound) == (17, 2, 15, 15, False)
    assert [item.currency_id for item in first.items] == list(range(100, 115))
    second = await staking.list_stakes(db, ALICE, page=2)
    assert [(item.currency_id, item.amount, item.symbol, item.hourly_rate_ppm) for item in second.items] == [(115, 25, "SP", 15), (116, 26, "SQ", 16)]
    assert (await staking.list_stakes(db, ALICE, page=3)).items == ()
    empty = await staking.list_stakes(db, BOB)
    assert (empty.total, empty.pages, empty.items) == (0, 0, ())
    for page, per_page in ((0, 15), (1, 0), (1, 101)):
        with pytest.raises(InvalidArgument):
            await staking.list_stakes(db, ALICE, page=page, per_page=per_page)
    info = await staking.get_stake(db, ALICE, 101)
    assert info is not None
    assert (info.amount, info.carry, info.last_accrued_at, info.name) == (11, 0, T0, "Coin")
    assert staking.projected_amount(info, T0) == 11


async def test_parallel_deposits_and_withdrawals_on_one_stake(db: AsyncEngine, seeded: SeededCurrency) -> None:
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000_000, now=T0)
    rng = random.Random(5)  # noqa: S311
    expected_failures = (InsufficientStake, InsufficientBalance)
    deposited: list[int] = []
    withdrawn: list[int] = []

    async def op(index: int) -> None:
        at = T0 + timedelta(minutes=index)
        amount = rng.randrange(1, 400_000)
        try:
            if index % 2:
                await staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=amount, now=at)
                withdrawn.append(amount)
            else:
                await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=amount, now=at)
                deposited.append(amount)
        except expected_failures:
            pass

    await asyncio.gather(*(op(index) for index in range(30)))
    assert deposited and withdrawn
    async with db.connect() as conn:
        interest_rows = (await conn.execute(select(transfer.c.amount).where(transfer.c.currency_id == CUR, transfer.c.kind == TransferKind.INTEREST))).scalars().all()
    expected = 1_000_000 + sum(deposited) - sum(withdrawn) + sum(int(amount) for amount in interest_rows)
    row = await _stake_row(db, ALICE)
    assert (0 if row is None else row["amount"]) == expected
    assert row is None or row["amount"] > 0
    assert await balance_of(db, STAKE_CUSTODY_ACCOUNT_ID, CUR) == expected
    assert await balance_of(db, ALICE, CUR) >= 0
    await _assert_invariants(db)


async def test_failed_first_deposit_leaves_no_placeholder(db: AsyncEngine, seeded: SeededCurrency) -> None:
    carol = 333
    with pytest.raises(InsufficientBalance):
        await staking.deposit(db, account_id=carol, currency_id=CUR, amount=5, now=T0)
    assert await _stake_row(db, carol) is None
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=5, now=T0)
    await staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=5, now=T0)
    async with db.connect() as conn:
        assert (await conn.execute(select(stake).where(stake.c.amount == 0))).first() is None
    await _assert_invariants(db)


async def test_worker_counts_and_logs_failures(db: AsyncEngine, seeded: SeededCurrency, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    for account_id in (ALICE, BOB):
        await staking.deposit(db, account_id=account_id, currency_id=CUR, amount=1_000, now=T0)
    original = staking._accrue_one_stale

    async def flaky(engine: AsyncEngine, account_id: int, currency_id: int, older_than: timedelta, now: datetime | None) -> staking.StakeAccrual | None:
        if account_id == ALICE:
            raise RuntimeError("boom")
        return await original(engine, account_id, currency_id, older_than, now)

    monkeypatch.setattr(staking, "_accrue_one_stale", flaky)
    with caplog.at_level("ERROR", logger="rapidwire.core.staking"):
        result = await staking.accrue_stale_stakes(db, older_than=timedelta(hours=3), now=T0 + timedelta(hours=4))
    assert result == staking.StaleAccrualResult(credited=1, without_interest=0, failed=1)
    assert f"stake accrual failed for account {ALICE} in currency {CUR}" in caplog.text
    await _assert_invariants(db)


async def _cap_currency(db: AsyncEngine, holders: tuple[int, ...]) -> None:
    await create_currency(db, currency_id=CUR, symbol="CAP", hourly_rate_ppm=1_000_000, now=T0)
    for holder in holders:
        await mint(db, CUR, holder, 10**37 + 10, now=T0)
        await staking.deposit(db, account_id=holder, currency_id=CUR, amount=10**37, now=T0)


async def test_concurrent_operations_at_the_cap_escalate_once_each(db: AsyncEngine) -> None:
    await _cap_currency(db, (ALICE, BOB))
    at = T0 + timedelta(hours=4)
    first, second = await asyncio.gather(
        staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=1, now=at),
        staking.deposit(db, account_id=BOB, currency_id=CUR, amount=1, now=at),
    )
    assert len(await _executions(db, ExecutionType.INTEREST)) == 1
    assert first.interest + second.interest > 0
    assert len(await _executions(db, ExecutionType.STAKE_WITHDRAW)) == 1
    assert [row["status"] for row in await _executions(db, ExecutionType.STAKE_DEPOSIT)] == [ExecutionStatus.SUCCESS] * 3
    async with db.connect() as conn:
        assert (await conn.execute(select(currency.c.interest_halted).where(currency.c.currency_id == CUR))).scalar_one()
    assert await supply_of(db, CUR) == AMOUNT_MAX
    await _assert_invariants(db)


async def test_escalated_deposit_with_idempotency_key_runs_once(db: AsyncEngine) -> None:
    await _cap_currency(db, (ALICE,))
    at = T0 + timedelta(hours=4)
    first = await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=5, now=at, idempotency_key="cap")
    again = await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=5, now=at, idempotency_key="cap")
    assert (first.replayed, again.replayed) == (False, True)
    assert again == staking.StakeChanged(**{**first.__dict__, "replayed": True})
    async with db.connect() as conn:
        keyed = (await conn.execute(select(execution.c.execution_id).where(execution.c.idempotency_key == "cap"))).scalars().all()
    assert keyed == [first.execution_id]
    assert len(await _executions(db, ExecutionType.INTEREST)) == 1
    await _assert_invariants(db)


async def test_withdraw_racing_worker_accrual(db: AsyncEngine, seeded: SeededCurrency) -> None:
    for account_id in (ALICE, BOB):
        await staking.deposit(db, account_id=account_id, currency_id=CUR, amount=1_000_000, now=T0)
    now = T0 + timedelta(hours=4)
    errors: list[DomainError] = []

    async def withdraw_all(account_id: int) -> None:
        try:
            await staking.withdraw(db, account_id=account_id, currency_id=CUR, amount=staking.compute_accrual(1_000_000, 0, RATE, 4 * 3_600_000_000).amount, now=now)
        except DomainError as error:
            errors.append(error)

    _, first, _, second = await asyncio.gather(
        withdraw_all(ALICE),
        staking.accrue_stale_stakes(db, older_than=timedelta(hours=3), now=now),
        withdraw_all(BOB),
        staking.accrue_stale_stakes(db, older_than=timedelta(hours=3), now=now),
    )
    assert errors == []
    assert first.failed == second.failed == 0
    assert first.credited + second.credited <= 2
    assert first.without_interest == second.without_interest == 0
    assert await _stake_row(db, ALICE) is None
    assert await _stake_row(db, BOB) is None
    assert len(await _executions(db, ExecutionType.INTEREST)) == 2
    assert await balance_of(db, STAKE_CUSTODY_ACCOUNT_ID, CUR) == 0
    await _assert_invariants(db)


async def test_worker_skips_deleted_currency(db: AsyncEngine, seeded: SeededCurrency) -> None:
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000, now=T0)
    async with db.begin() as conn:
        await conn.execute(update(currency).where(currency.c.currency_id == CUR).values(deleted_at=T0))
    assert await staking.accrue_stale_stakes(db, older_than=timedelta(hours=3), now=T0 + timedelta(hours=5)) == staking.StaleAccrualResult(credited=0, without_interest=0, failed=0)
    row = await _stake_row(db, ALICE)
    assert row is not None and row["last_accrued_at"] == T0


async def test_clock_earlier_than_last_accrual(db: AsyncEngine, seeded: SeededCurrency) -> None:
    later = T0 + timedelta(hours=2)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=1_000_000, now=later)
    deposited = await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=10, now=T0)
    withdrawn = await staking.withdraw(db, account_id=ALICE, currency_id=CUR, amount=10, now=T0 + timedelta(hours=1))
    assert (deposited.interest, withdrawn.interest) == (0, 0)
    row = await _stake_row(db, ALICE)
    assert row is not None
    assert (row["amount"], row["carry"], row["last_accrued_at"]) == (1_000_000, 0, later)
    assert await _executions(db, ExecutionType.INTEREST) == []
    await _assert_invariants(db)


async def test_supply_cap_escalates_the_currency_lock_once(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR, symbol="CAP", hourly_rate_ppm=1_000_000, now=T0)
    big = 10**37
    await mint(db, CUR, ALICE, big + 10, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=big, now=T0)
    result = await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=10, now=T0 + timedelta(hours=4))
    # The supply already includes the 10 units in Alice's wallet, so the stake is capped 10 below AMOUNT_MAX before the deposit adds them.
    assert result.stake_amount == AMOUNT_MAX
    assert len(await _executions(db, ExecutionType.STAKE_DEPOSIT)) == 2
    assert len(await _executions(db, ExecutionType.INTEREST)) == 1
    await _assert_invariants(db)


async def test_worker_escalates_at_the_supply_cap(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR, symbol="CAP", hourly_rate_ppm=1_000_000, now=T0)
    big = 10**37
    await mint(db, CUR, ALICE, big, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=big, now=T0)
    assert await staking.accrue_stale_stakes(db, older_than=timedelta(hours=3), now=T0 + timedelta(hours=4)) == staking.StaleAccrualResult(credited=1, without_interest=0, failed=0)
    assert await supply_of(db, CUR) == AMOUNT_MAX
    async with db.connect() as conn:
        assert (await conn.execute(select(currency.c.interest_halted).where(currency.c.currency_id == CUR))).scalar_one()
    assert await staking.accrue_stale_stakes(db, older_than=timedelta(0), now=T0 + timedelta(hours=9)) == staking.StaleAccrualResult(credited=0, without_interest=0, failed=0)
    await _assert_invariants(db)


async def test_accrual_under_share_lock_signals_escalation(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR, symbol="CAP", hourly_rate_ppm=1_000_000, now=T0)
    big = 10**37
    await mint(db, CUR, ALICE, big, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=big, now=T0)
    async with db.connect() as conn, conn.begin():
        tx = LedgerTx(conn, T0 + timedelta(hours=4))
        row = (await tx.lock_currencies([CUR]))[CUR]
        with pytest.raises(CurrencyLockEscalation) as escalation:
            await staking.accrue_stake_in_tx(tx, None, currency_row=row, account_id=ALICE, trigger=staking.AccrualTrigger.WORKER)
        assert escalation.value.currency_id == CUR
        await conn.rollback()
    async with db.connect() as conn, conn.begin():
        tx = LedgerTx(conn, T0 + timedelta(hours=4), exclusive_currencies={CUR})
        row = (await tx.lock_currencies([CUR]))[CUR]
        assert tx.currency_lock(CUR) == "update"
        accrual = await staking.accrue_stake_in_tx(tx, None, currency_row=row, account_id=ALICE, trigger=staking.AccrualTrigger.WORKER)
        assert accrual is not None and accrual.halted
        await conn.rollback()
    await _assert_invariants(db)


@pytest.mark.parametrize("lock_in_prepare", [False, True])
async def test_escalation_reruns_a_composed_operation_with_the_currency_exclusive(db: AsyncEngine, lock_in_prepare: bool) -> None:
    await create_currency(db, currency_id=CUR, symbol="CAP", hourly_rate_ppm=1_000_000, now=T0)
    await create_currency(db, currency_id=CUR + 1, symbol="FEE", now=T0)
    big = 10**37
    await mint(db, CUR, ALICE, big + 10, now=T0)
    await mint(db, CUR + 1, ALICE, 10, now=T0)
    await staking.deposit(db, account_id=ALICE, currency_id=CUR, amount=big, now=T0)
    runs: list[str | None] = []

    async def prepare(tx: LedgerTx, execution_id: int) -> None:
        await tx.lock_currencies([CUR, CUR + 1] if lock_in_prepare else [CUR + 1])
        runs.append(tx.currency_lock(CUR))

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        async def child(child_tx: LedgerTx, child_id: int) -> dict[str, Any]:
            return (await staking.deposit_in_tx(child_tx, child_id, account_id=ALICE, currency_id=CUR, amount=10)).to_output()

        await tx.lock_balances([(ALICE, CUR + 1)])
        result = await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.STAKE_DEPOSIT, initiator_id=ALICE, input={}, body=child)
        return {"child": result.execution_id}

    before_calls: list[None] = []

    async def before() -> None:
        before_calls.append(None)

    op = await run_operation(db, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, prepare=prepare, before=before, now=T0 + timedelta(hours=4))
    assert runs == (["share", "update"] if lock_in_prepare else [None, None])
    assert len(before_calls) == 1
    assert (await staking.get_stake(db, ALICE, CUR)) is not None
    assert await supply_of(db, CUR) == AMOUNT_MAX
    assert len(await _executions(db, ExecutionType.STAKE_DEPOSIT)) == 2
    assert op.int_field("child") > op.execution_id
    await _assert_invariants(db)
