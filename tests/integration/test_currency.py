from __future__ import annotations

import asyncio
from collections.abc import Coroutine, Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.core import currency as cur
from rapidwire.core import staking
from rapidwire.core.accounts import create_pool_custody_account
from rapidwire.core.allowances import approve, get_allowance
from rapidwire.core.claims import create_claim, get_claim
from rapidwire.core.errors import (
    AlreadyExists,
    AmountOverflow,
    CurrencyNotFound,
    CurrencyRenounced,
    DomainError,
    GuildUnavailable,
    InsufficientBalance,
    InvalidArgument,
    InvalidState,
    NoPendingRequest,
    PermissionDenied,
    RequestExpired,
    TimelockActive,
)
from rapidwire.core.hooks import NoopPoolDissolver, PoolDissolution, get_pool_dissolver, register_pool_dissolver
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import MAX_RERUNS, LedgerTx, LedgerUsageError, run_operation
from rapidwire.core.permissions import InMemoryGuildInfo, snapshot_guild
from rapidwire.core.pools import DexPoolDissolver
from rapidwire.core.transfers import transfer
from rapidwire.core.types import ClaimStatus, ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.tables import FEE_TIERS_BPS, MINIMUM_LIQUIDITY, STAKE_CUSTODY_ACCOUNT_ID, allowance_history, claim, currency, execution, pool, stake
from rapidwire.db.tables import transfer as transfer_table
from rapidwire.db.types import AMOUNT_MAX
from tests.integration.factories import balance_of, supply_of

T0 = datetime(2026, 7, 1, tzinfo=UTC)
GUILD = 5001
OTHER_GUILD = 5002
OWNER = 101
ADMIN = 102
MEMBER = 103
NEW_OWNER = 104
HOLDER = 105
DAY = timedelta(days=1)


@pytest.fixture
def guilds() -> InMemoryGuildInfo:
    info = InMemoryGuildInfo()
    info.add_guild(GUILD, OWNER)
    info.add_admin(GUILD, ADMIN)
    info.add_guild(OTHER_GUILD, NEW_OWNER)
    return info


async def _create(engine: AsyncEngine, guilds: InMemoryGuildInfo, **overrides: Any) -> cur.CurrencyCreated:
    args: dict[str, Any] = {"guild_id": GUILD, "user_id": OWNER, "name": "Gold", "symbol": "GLD", "initial_supply": 1_000_000, "hourly_rate_ppm": 10_000, "now": T0}
    args.update(overrides)
    return await cur.create_currency(engine, guilds, **args)


async def _execution(engine: AsyncEngine, execution_id: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        return dict((await conn.execute(select(execution).where(execution.c.execution_id == execution_id))).mappings().one())


async def _executions(engine: AsyncEngine, type: str) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(execution).where(execution.c.type == type).order_by(execution.c.execution_id))).mappings().all()
    return [dict(row) for row in rows]


async def _transfers(engine: AsyncEngine, execution_id: int) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(transfer_table).where(transfer_table.c.execution_id == execution_id).order_by(transfer_table.c.transfer_id))).mappings().all()
    return [dict(row) for row in rows]


async def _currency_row(engine: AsyncEngine, currency_id: int = GUILD) -> dict[str, Any]:
    async with engine.connect() as conn:
        return dict((await conn.execute(select(currency).where(currency.c.currency_id == currency_id))).mappings().one())


async def _assert_invariants(engine: AsyncEngine) -> None:
    report = await verify_invariants(engine)
    assert report.ok, report.violations


async def test_create_mints_initial_supply_to_owner(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    created = await _create(db, guilds)
    assert (created.currency_id, created.name, created.symbol, created.hourly_rate_ppm, created.initial_supply, created.revived) == (GUILD, "Gold", "GLD", 10_000, 1_000_000, False)
    row = await _execution(db, created.execution_id)
    assert (row["type"], row["initiator_id"], row["status"]) == (ExecutionType.CURRENCY_CREATE, OWNER, ExecutionStatus.SUCCESS)
    [move] = await _transfers(db, created.execution_id)
    assert move["transfer_id"] == created.transfer_id
    assert (move["to_account"], move["amount"], move["kind"]) == (OWNER, 1_000_000, TransferKind.PRINCIPAL)
    info = await cur.get_currency(db, GUILD)
    assert info is not None
    assert (info.supply, info.created_by, info.delegation_enabled, info.minting_renounced, info.interest_halted) == (1_000_000, OWNER, False, False, False)
    assert info.pending_rate_ppm is None and info.rate_change_apply_after is None and info.delete_requested_at is None
    assert await cur.get_currency_by_symbol(db, "GLD") == info
    assert await cur.get_currency_by_symbol(db, "gld") is None
    await _assert_invariants(db)


async def test_create_with_zero_supply(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    created = await _create(db, guilds, initial_supply=0)
    assert created.transfer_id is None
    assert await _transfers(db, created.execution_id) == []
    assert await supply_of(db, GUILD) == 0


async def test_create_rules(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    with pytest.raises(PermissionDenied):
        await _create(db, guilds, user_id=ADMIN)
    with pytest.raises(GuildUnavailable):
        await _create(db, guilds, guild_id=9999)
    with pytest.raises(InvalidArgument, match="consecutive underscores") as info:
        await _create(db, guilds, name="a__b")
    assert info.value.execution_id is not None
    assert (await _execution(db, info.value.execution_id))["status"] == ExecutionStatus.FAILED
    with pytest.raises(InvalidArgument):
        await _create(db, guilds, symbol="gld")
    with pytest.raises(InvalidArgument):
        await _create(db, guilds, hourly_rate_ppm=1_000_001)
    await _create(db, guilds, hourly_rate_ppm=1_000_000)
    with pytest.raises(AlreadyExists, match="already exists for this server"):
        await _create(db, guilds, symbol="OTHER")
    with pytest.raises(AlreadyExists, match="symbol GLD"):
        await _create(db, guilds, guild_id=OTHER_GUILD, user_id=NEW_OWNER)
    await _create(db, guilds, guild_id=OTHER_GUILD, user_id=NEW_OWNER, symbol="SLV", initial_supply=0)
    await _assert_invariants(db)


async def test_concurrent_creates_with_one_symbol(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    results = await asyncio.gather(_create(db, guilds), _create(db, guilds, guild_id=OTHER_GUILD, user_id=NEW_OWNER), return_exceptions=True)
    assert sum(isinstance(result, cur.CurrencyCreated) for result in results) == 1
    assert sum(isinstance(result, AlreadyExists) for result in results) == 1
    await _assert_invariants(db)


async def test_management_permissions(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds)
    await cur.mint(db, guilds, currency_id=GUILD, user_id=OWNER, amount=10, now=T0)
    with pytest.raises(PermissionDenied):
        await cur.mint(db, guilds, currency_id=GUILD, user_id=ADMIN, amount=10, now=T0)
    with pytest.raises(PermissionDenied):
        await cur.enable_delegation(db, guilds, currency_id=GUILD, user_id=ADMIN, now=T0)
    enabled = await cur.enable_delegation(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    assert (enabled.enabled, enabled.changed) == (True, True)
    assert (await _execution(db, enabled.execution_id))["type"] == ExecutionType.DELEGATION_ENABLE
    assert not (await cur.enable_delegation(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)).changed
    minted = await cur.mint(db, guilds, currency_id=GUILD, user_id=ADMIN, amount=7, now=T0)
    assert (await _execution(db, minted.execution_id))["initiator_id"] == ADMIN
    assert await balance_of(db, ADMIN, GUILD) == 7
    await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=ADMIN, new_rate_ppm=5, now=T0)
    with pytest.raises(PermissionDenied):
        await cur.mint(db, guilds, currency_id=GUILD, user_id=MEMBER, amount=1, now=T0)
    with pytest.raises(PermissionDenied):
        await cur.disable_delegation(db, guilds, currency_id=GUILD, user_id=ADMIN, now=T0)
    guilds.remove_admin(GUILD, ADMIN)
    with pytest.raises(PermissionDenied):
        await cur.mint(db, guilds, currency_id=GUILD, user_id=ADMIN, amount=1, now=T0)
    guilds.add_admin(GUILD, ADMIN)
    disabled = await cur.disable_delegation(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    assert (await _execution(db, disabled.execution_id))["type"] == ExecutionType.DELEGATION_DISABLE
    with pytest.raises(PermissionDenied):
        await cur.mint(db, guilds, currency_id=GUILD, user_id=ADMIN, amount=1, now=T0)

    guilds.set_owner(GUILD, NEW_OWNER)
    with pytest.raises(PermissionDenied):
        await cur.mint(db, guilds, currency_id=GUILD, user_id=OWNER, amount=1, now=T0)
    await cur.mint(db, guilds, currency_id=GUILD, user_id=NEW_OWNER, amount=3, now=T0)
    assert await balance_of(db, NEW_OWNER, GUILD) == 3

    guilds.remove_guild(GUILD)
    for call in (
        cur.mint(db, guilds, currency_id=GUILD, user_id=NEW_OWNER, amount=1, now=T0),
        cur.renounce(db, guilds, currency_id=GUILD, user_id=NEW_OWNER, now=T0),
        cur.apply_rate_change(db, guilds, currency_id=GUILD, user_id=NEW_OWNER, now=T0 + 8 * DAY),
        cur.request_delete(db, guilds, currency_id=GUILD, user_id=NEW_OWNER, now=T0),
        cur.enable_delegation(db, guilds, currency_id=GUILD, user_id=NEW_OWNER, now=T0),
    ):
        with pytest.raises(GuildUnavailable):
            await call
    burned = await cur.burn(db, account_id=OWNER, currency_id=GUILD, amount=5, now=T0)
    assert (await _execution(db, burned.execution_id))["type"] == ExecutionType.BURN
    await transfer(db, sender_id=OWNER, recipient_id=HOLDER, currency_id=GUILD, amount=5, now=T0)
    await staking.deposit(db, account_id=HOLDER, currency_id=GUILD, amount=5, now=T0)
    await _assert_invariants(db)


async def test_mint_burn_and_supply(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds, initial_supply=100)
    minted = await cur.mint(db, guilds, currency_id=GUILD, user_id=OWNER, amount=50, now=T0)
    [move] = await _transfers(db, minted.execution_id)
    assert move["from_account"] == (await _currency_row(db))["issuance_account_id"]
    assert (minted.amount, minted.account_id, minted.transfer_id) == (50, OWNER, move["transfer_id"])
    assert await supply_of(db, GUILD) == 150
    await transfer(db, sender_id=OWNER, recipient_id=HOLDER, currency_id=GUILD, amount=30, now=T0)
    burned = await cur.burn(db, account_id=HOLDER, currency_id=GUILD, amount=20, now=T0)
    [move] = await _transfers(db, burned.execution_id)
    assert (move["from_account"], move["to_account"], move["amount"]) == (HOLDER, created_issuance := move["to_account"], 20)
    assert created_issuance < -1
    assert await supply_of(db, GUILD) == 130
    with pytest.raises(InsufficientBalance):
        await cur.burn(db, account_id=HOLDER, currency_id=GUILD, amount=11, now=T0)
    with pytest.raises(AmountOverflow):
        await cur.mint(db, guilds, currency_id=GUILD, user_id=OWNER, amount=AMOUNT_MAX - 129, now=T0)
    await cur.mint(db, guilds, currency_id=GUILD, user_id=OWNER, amount=AMOUNT_MAX - 130, now=T0)
    assert await supply_of(db, GUILD) == AMOUNT_MAX
    info = await cur.get_currency(db, GUILD)
    assert info is not None and info.supply == AMOUNT_MAX
    replay_first = await cur.burn(db, account_id=OWNER, currency_id=GUILD, amount=1, now=T0, idempotency_key="b")
    replay = await cur.burn(db, account_id=OWNER, currency_id=GUILD, amount=1, now=T0, idempotency_key="b")
    assert replay.replayed and replay.execution_id == replay_first.execution_id
    assert await supply_of(db, GUILD) == AMOUNT_MAX - 1
    await _assert_invariants(db)


async def test_renounce_blocks_mint_and_rate_changes(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds)
    await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, new_rate_ppm=1, now=T0)
    renounced = await cur.renounce(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    assert renounced == cur.MintingRenounced(execution_id=renounced.execution_id, currency_id=GUILD)
    assert (await _execution(db, renounced.execution_id))["output"] == {"currency_id": str(GUILD)}
    info = await cur.get_currency(db, GUILD)
    assert info is not None and info.minting_renounced and info.pending_rate_ppm is None
    with pytest.raises(CurrencyRenounced):
        await cur.mint(db, guilds, currency_id=GUILD, user_id=OWNER, amount=1, now=T0)
    with pytest.raises(CurrencyRenounced):
        await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, new_rate_ppm=1, now=T0)
    with pytest.raises(CurrencyRenounced):
        await cur.apply_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 8 * DAY)
    with pytest.raises(CurrencyRenounced, match="already been renounced"):
        await cur.renounce(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    await cur.burn(db, account_id=OWNER, currency_id=GUILD, amount=1, now=T0)


async def test_rate_change_timelock_and_accrual_before_switch(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds, hourly_rate_ppm=10_000)
    await staking.deposit(db, account_id=OWNER, currency_id=GUILD, amount=1_000_000, now=T0)
    with pytest.raises(NoPendingRequest):
        await cur.apply_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    with pytest.raises(InvalidArgument):
        await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, new_rate_ppm=1_000_001, now=T0)
    requested = await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, new_rate_ppm=20_000, now=T0)
    assert (requested.new_rate_ppm, requested.requested_at, requested.apply_after) == (20_000, T0, T0 + 7 * DAY)
    info = await cur.get_currency(db, GUILD)
    assert info is not None and (info.pending_rate_ppm, info.rate_change_apply_after) == (20_000, T0 + 7 * DAY)
    with pytest.raises(InvalidState):
        await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, new_rate_ppm=30_000, now=T0)
    with pytest.raises(TimelockActive):
        await cur.apply_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 7 * DAY - timedelta(microseconds=1))
    apply_at = T0 + 7 * DAY
    applied = await cur.apply_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, now=apply_at)
    assert (applied.old_rate_ppm, applied.new_rate_ppm, applied.stakes_accrued) == (10_000, 20_000, 1)
    [interest_id] = applied.interest_execution_ids
    interest = await _execution(db, interest_id)
    assert interest["input"]["trigger"] == "rate_apply"
    assert interest["input"]["rate_ppm"] == "10000"
    at_apply = staking.compute_accrual(1_000_000, 0, 10_000, 7 * 24 * 3_600_000_000)
    row = await staking.get_stake(db, OWNER, GUILD)
    assert row is not None
    assert (row.amount, row.carry, row.last_accrued_at, row.hourly_rate_ppm) == (at_apply.amount, at_apply.carry, apply_at, 20_000)
    later = await staking.withdraw(db, account_id=OWNER, currency_id=GUILD, amount=1, now=apply_at + timedelta(hours=1))
    after = staking.compute_accrual(at_apply.amount, at_apply.carry, 20_000, 3_600_000_000)
    assert later.interest == after.interest
    info = await cur.get_currency(db, GUILD)
    assert info is not None and (info.hourly_rate_ppm, info.pending_rate_ppm, info.rate_change_requested_at) == (20_000, None, None)
    with pytest.raises(NoPendingRequest):
        await cur.apply_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, now=apply_at)
    # Requests never expire.
    await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, new_rate_ppm=0, now=apply_at)
    await cur.apply_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, now=apply_at + 400 * DAY)
    await _assert_invariants(db)


async def test_delete_request_window(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds)
    with pytest.raises(NoPendingRequest):
        await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    requested = await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    assert (requested.requested_at, requested.finalize_from, requested.finalize_until) == (T0, T0 + 7 * DAY, T0 + 10 * DAY)
    with pytest.raises(InvalidState):
        await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 10 * DAY)
    with pytest.raises(TimelockActive, match="seconds remaining"):
        await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 7 * DAY - timedelta(seconds=1))
    info = await cur.get_currency(db, GUILD, now=T0 + 10 * DAY)
    assert info is not None and (info.delete_requested_at, info.delete_finalize_from, info.delete_finalize_until) == (T0, T0 + 7 * DAY, T0 + 10 * DAY)
    assert not info.delete_request_lapsed
    lapsed = await cur.get_currency(db, GUILD, now=T0 + 10 * DAY + timedelta(seconds=1))
    assert lapsed is not None and (lapsed.delete_requested_at, lapsed.delete_request_lapsed) == (None, True)
    assert lapsed.delete_finalize_from is None

    with pytest.raises(RequestExpired) as expired:
        await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 10 * DAY + timedelta(seconds=1))
    failed = await _execution(db, expired.value.execution_id or 0)
    assert failed["status"] == ExecutionStatus.FAILED
    assert failed["output"]["error"]["code"] == "request_expired"
    assert (await _currency_row(db))["delete_requested_at"] is None
    cleared = await cur.get_currency(db, GUILD, now=T0 + 11 * DAY)
    assert cleared is not None and (cleared.delete_requested_at, cleared.delete_request_lapsed) == (None, False)
    with pytest.raises(NoPendingRequest):
        await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 11 * DAY)

    second = T0 + 20 * DAY
    await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=second)
    # A lapsed request that was never finalized is replaced by a new one.
    third = second + 11 * DAY
    replaced = await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=third)
    assert replaced.requested_at == third
    deleted = await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=third + 10 * DAY)
    assert deleted.burned_amount == 1_000_000
    assert await cur.get_currency(db, GUILD) is None
    with pytest.raises(CurrencyNotFound):
        await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=third + 10 * DAY)
    await _assert_invariants(db)


async def test_clearing_a_lapsed_delete_request_never_clears_a_fresh_one(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds)
    at = T0 + 11 * DAY
    # Request first: the finalization then finds the fresh request too early and leaves it.
    await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=at)
    with pytest.raises(TimelockActive):
        await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=at)
    assert (await _currency_row(db))["delete_requested_at"] == at
    for round_index in range(8):
        lapsed_at = T0 + round_index * 30 * DAY
        now = lapsed_at + 11 * DAY
        async with db.begin() as conn:
            await conn.execute(update(currency).where(currency.c.currency_id == GUILD).values(delete_requested_at=lapsed_at))
        calls: list[Coroutine[Any, Any, Any]] = [
            cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=now),
            cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=now),
        ]
        if round_index % 2:
            calls.reverse()
        outcomes = await asyncio.gather(*calls, return_exceptions=True)
        finalized = outcomes[1] if round_index % 2 else outcomes[0]
        requested = outcomes[0] if round_index % 2 else outcomes[1]
        # Whichever runs first, the finalization fails (lapsed or too early) and the new request stands.
        assert isinstance(finalized, RequestExpired | TimelockActive), finalized
        assert isinstance(requested, cur.DeleteRequested), requested
        assert (await _currency_row(db))["delete_requested_at"] == now
        info = await cur.get_currency(db, GUILD, now=now)
        assert info is not None and (info.delete_requested_at, info.delete_request_lapsed) == (now, False)
    await _assert_invariants(db)


class RecordingDissolver:
    """A fake DEX with one pool of the deleted currency and a partner, holding reserves in pool custody and owing the partner reserve to one provider."""

    def __init__(self) -> None:
        self.pool_account: int | None = None
        self.partner = OTHER_GUILD
        self.provider = MEMBER
        self.events: list[str] = []
        self.calls: list[tuple[int, int, str]] = []

    def keys(self, currency_id: int) -> set[tuple[int, int]]:
        if self.pool_account is None:
            return set()
        return {(self.pool_account, currency_id), (self.pool_account, self.partner), (self.provider, self.partner)}

    async def partner_currencies(self, conn: AsyncConnection, currency_id: int) -> set[int]:
        self.events.append("partners")
        return set() if self.pool_account is None else {self.partner}

    async def balance_keys(self, tx: LedgerTx, currency_id: int) -> set[tuple[int, int]]:
        self.events.append("keys")
        assert tx.currency_lock(currency_id) == "update"
        if self.pool_account is not None:
            assert tx.currency_lock(self.partner) == "share"
        pending = (await tx.conn.execute(select(claim.c.claim_id).where(claim.c.currency_id == currency_id, claim.c.status == ClaimStatus.PENDING))).first()
        assert pending is None
        assert not any(tx.is_locked(*key) for key in self.keys(currency_id))
        return self.keys(currency_id)

    async def dissolve_pools_for_currency(self, tx: LedgerTx, execution_id: int, currency_id: int) -> PoolDissolution:
        self.events.append("dissolve")
        assert all(tx.is_locked(*key) for key in self.keys(currency_id))
        row = (await tx.conn.execute(select(execution.c.type).where(execution.c.execution_id == execution_id))).one()
        self.calls.append((execution_id, currency_id, row.type))
        if self.pool_account is None:
            return PoolDissolution()
        reserve = tx.balance(self.pool_account, self.partner)
        if reserve > 0:
            await tx.move(execution_id, self.partner, self.pool_account, self.provider, reserve, TransferKind.PRINCIPAL)
        # The deleted currency's reserve stays in custody for the cascade to burn and count, and the pool row is closed as the real dissolver closes it.
        closed = {"reserve_a": 0, "reserve_b": 0, "total_shares": 0, "locked_shares": 0, "dissolved_at": tx.now, "dissolved_execution_id": execution_id}
        await tx.conn.execute(update(pool).where(pool.c.currency_a.in_([currency_id, self.partner]), pool.c.currency_b.in_([currency_id, self.partner])).values(**closed))
        return PoolDissolution(pools_dissolved=1)


class FailingDissolver(NoopPoolDissolver):
    async def dissolve_pools_for_currency(self, tx: LedgerTx, execution_id: int, currency_id: int) -> PoolDissolution:
        raise InvalidState("pool dissolution failed")


class ShiftingDissolver(NoopPoolDissolver):
    """Reports the partner sets in answers, cycling, as if pools appeared or vanished between the unlocked read and the locked one."""

    def __init__(self, answers: list[set[int]]) -> None:
        self.answers = answers
        self.reads = 0

    async def partner_currencies(self, conn: AsyncConnection, currency_id: int) -> set[int]:
        self.reads += 1
        return self.answers[(self.reads - 1) % len(self.answers)]


class GrowingDissolver(NoopPoolDissolver):
    """Reports one partner more on each run's locked read than on its unlocked read, as if a pool with a new currency appeared during every run."""

    def __init__(self, partners: list[int]) -> None:
        self.partners = partners
        self.reads = 0

    async def partner_currencies(self, conn: AsyncConnection, currency_id: int) -> set[int]:
        self.reads += 1
        run, locked_read = divmod(self.reads - 1, 2)
        return set(self.partners[: run + locked_read])


async def _create_pool_account(engine: AsyncEngine, pool_id: int, currency_ids: tuple[int, int], creator: int) -> int:
    """Insert an active pool row with an explicit ID for the two currencies and return its new custody account; the pool row only anchors the account, since the stub dissolver works on custody balances."""
    currency_a, currency_b = sorted(currency_ids)
    async with engine.begin() as conn:
        created = await conn.execute(
            insert(execution).values(type=ExecutionType.POOL_CREATE, initiator_id=creator, status=ExecutionStatus.SUCCESS, input={}, output={"pool_id": str(pool_id)}, created_at=T0)
        )
        assert created.inserted_primary_key is not None
        row = {"pool_id": pool_id, "currency_a": currency_a, "currency_b": currency_b, "fee_bps": FEE_TIERS_BPS[0], "created_by": creator, "created_execution_id": created.inserted_primary_key[0]}
        await conn.execute(insert(pool).values(created_at=T0, reserve_a=1, reserve_b=1, total_shares=MINIMUM_LIQUIDITY, locked_shares=MINIMUM_LIQUIDITY, **row))
        return await create_pool_custody_account(conn, pool_id, now=T0)


async def _fund_pool(engine: AsyncEngine, pool_account: int, currency_id: int, sender: int, amount: int) -> None:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([currency_id])
        await tx.lock_balances([(sender, currency_id), (pool_account, currency_id)])
        return {"transfer_id": await tx.move(execution_id, currency_id, sender, pool_account, amount, TransferKind.PRINCIPAL)}

    await run_operation(engine, type=ExecutionType.LIQUIDITY_ADD, initiator_id=sender, input={"currency_id": currency_id, "amount": amount}, body=body, now=T0)


@pytest.fixture
def dissolver() -> Iterator[RecordingDissolver]:
    recording = RecordingDissolver()
    previous = register_pool_dissolver(recording)
    yield recording
    register_pool_dissolver(previous)


def test_default_dissolver_is_the_dex() -> None:
    assert isinstance(get_pool_dissolver(), DexPoolDissolver)


async def test_finalize_cascade(db: AsyncEngine, guilds: InMemoryGuildInfo, dissolver: RecordingDissolver) -> None:
    await _create(db, guilds, initial_supply=1_000_000)
    await _create(db, guilds, guild_id=OTHER_GUILD, user_id=NEW_OWNER, symbol="SLV", initial_supply=500)
    await transfer(db, sender_id=OWNER, recipient_id=HOLDER, currency_id=GUILD, amount=300_000, now=T0)
    await staking.deposit(db, account_id=HOLDER, currency_id=GUILD, amount=100_000, now=T0)
    await staking.deposit(db, account_id=OWNER, currency_id=GUILD, amount=200_000, now=T0)
    await staking.deposit(db, account_id=NEW_OWNER, currency_id=OTHER_GUILD, amount=100, now=T0)
    claim_a = await create_claim(db, claimant_id=HOLDER, payer_id=OWNER, currency_id=GUILD, amount=5, now=T0)
    claim_other = await create_claim(db, claimant_id=HOLDER, payer_id=NEW_OWNER, currency_id=OTHER_GUILD, amount=5, now=T0)
    await approve(db, owner_id=OWNER, spender_id=HOLDER, currency_id=GUILD, amount=77, now=T0)
    await approve(db, owner_id=HOLDER, spender_id=MEMBER, currency_id=GUILD, amount=0, now=T0)
    await approve(db, owner_id=NEW_OWNER, spender_id=HOLDER, currency_id=OTHER_GUILD, amount=9, now=T0)
    pool_account = await _create_pool_account(db, 1, (GUILD, OTHER_GUILD), OWNER)
    await _fund_pool(db, pool_account, GUILD, OWNER, 1_000)
    await _fund_pool(db, pool_account, OTHER_GUILD, NEW_OWNER, 300)
    dissolver.pool_account = pool_account
    await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)

    previous = register_pool_dissolver(FailingDissolver())
    try:
        with pytest.raises(InvalidState, match="pool dissolution failed") as failed:
            await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 8 * DAY)
        assert [row["execution_id"] for row in await _executions(db, ExecutionType.CURRENCY_DELETE_FINALIZE)] == [failed.value.execution_id]
    finally:
        register_pool_dissolver(previous)
    assert await cur.get_currency(db, GUILD, now=T0 + 8 * DAY) is not None
    assert await supply_of(db, GUILD) > 0

    at = T0 + 8 * DAY
    deleted = await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=at)
    assert dissolver.events == ["partners", "partners", "keys", "dissolve"]
    assert dissolver.calls == [(deleted.execution_id, GUILD, ExecutionType.CURRENCY_DELETE_FINALIZE)]
    assert (deleted.stakes_burned, deleted.claims_canceled, deleted.allowances_deleted) == (2, 1, 2)
    assert (deleted.balances_burned, deleted.pools_dissolved) == (4, 1)  # owner wallet, holder wallet, stake custody, pool custody
    assert await supply_of(db, GUILD) == 0
    for account_id in (OWNER, HOLDER, STAKE_CUSTODY_ACCOUNT_ID, pool_account):
        assert await balance_of(db, account_id, GUILD) == 0
    assert await balance_of(db, pool_account, OTHER_GUILD) == 0
    assert await balance_of(db, MEMBER, OTHER_GUILD) == 300
    moves = await _transfers(db, deleted.execution_id)
    issuance = (await _currency_row(db))["issuance_account_id"]
    burns = [move for move in moves if move["currency_id"] == GUILD]
    assert {move["to_account"] for move in burns} == {issuance}
    assert sum(move["amount"] for move in burns) == deleted.burned_amount
    assert [(move["from_account"], move["to_account"], move["amount"]) for move in moves if move["currency_id"] == OTHER_GUILD] == [(pool_account, MEMBER, 300)]
    async with db.connect() as conn:
        assert (await conn.execute(select(stake).where(stake.c.currency_id == GUILD))).first() is None
        history = (await conn.execute(select(allowance_history).where(allowance_history.c.execution_id == deleted.execution_id))).mappings().all()
    assert sorted((row["owner_id"], row["spender_id"], row["old_amount"], row["new_amount"]) for row in history) == [(OWNER, HOLDER, 77, 0), (HOLDER, MEMBER, 0, 0)]
    assert await get_allowance(db, OWNER, HOLDER, GUILD) == 0
    canceled = await get_claim(db, claim_a.claim_id)
    assert (canceled.status, canceled.resolved_execution_id) == (ClaimStatus.CANCELED, deleted.execution_id)
    assert (await get_claim(db, claim_other.claim_id)).status == ClaimStatus.PENDING
    assert await get_allowance(db, NEW_OWNER, HOLDER, OTHER_GUILD) == 9
    assert await staking.get_stake(db, NEW_OWNER, OTHER_GUILD) is not None
    row = await _currency_row(db)
    assert row["deleted_at"] == at and row["delete_requested_at"] is None
    gone = await cur.get_currency(db, GUILD, include_deleted=True)
    assert gone is not None and gone.deleted_at == at and gone.supply == 0
    assert await cur.get_currency_by_symbol(db, "GLD") is None
    with pytest.raises(CurrencyNotFound):
        await transfer(db, sender_id=OWNER, recipient_id=HOLDER, currency_id=GUILD, amount=1, now=at)
    await _assert_invariants(db)

    # The symbol is free again, and the same guild revives its row with fresh attributes.
    guilds.add_guild(7000, MEMBER)
    await cur.create_currency(db, guilds, guild_id=7000, user_id=MEMBER, name="Reuse", symbol="GLD", initial_supply=1, now=at)
    guilds.set_owner(GUILD, NEW_OWNER)
    revived = await cur.create_currency(db, guilds, guild_id=GUILD, user_id=NEW_OWNER, name="Gold2", symbol="GLDB", initial_supply=42, hourly_rate_ppm=7, now=at + DAY)
    assert revived.revived
    info = await cur.get_currency(db, GUILD)
    assert info is not None
    assert (info.name, info.symbol, info.created_by, info.created_at, info.supply, info.hourly_rate_ppm) == ("Gold2", "GLDB", NEW_OWNER, at + DAY, 42, 7)
    assert (info.minting_renounced, info.delegation_enabled, info.interest_halted, info.pending_rate_ppm, info.delete_requested_at, info.deleted_at) == (False, False, False, None, None, None)
    assert info.issuance_account_id == issuance
    await _assert_invariants(db)


async def test_finalize_racing_transfers_and_stakes(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds, initial_supply=1_000_000)
    await transfer(db, sender_id=OWNER, recipient_id=HOLDER, currency_id=GUILD, amount=500_000, now=T0)
    await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    at = T0 + 8 * DAY

    async def noise(index: int) -> None:
        try:
            if index % 3 == 0:
                await staking.deposit(db, account_id=HOLDER, currency_id=GUILD, amount=10, now=at)
            elif index % 3 == 1:
                await transfer(db, sender_id=HOLDER, recipient_id=OWNER, currency_id=GUILD, amount=10, now=at)
            else:
                await transfer(db, sender_id=OWNER, recipient_id=HOLDER, currency_id=GUILD, amount=10, now=at)
        except (CurrencyNotFound, InsufficientBalance):
            pass

    tasks: list[Coroutine[Any, Any, object]] = [noise(index) for index in range(12)]
    tasks.insert(6, cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=at))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert not [result for result in results if isinstance(result, BaseException)]
    assert await supply_of(db, GUILD) == 0
    async with db.connect() as conn:
        assert (await conn.execute(select(stake).where(stake.c.currency_id == GUILD))).first() is None
    await _assert_invariants(db)


async def test_failed_operations_record_executions(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds)
    with pytest.raises(DomainError) as info:
        await cur.mint(db, guilds, currency_id=GUILD, user_id=MEMBER, amount=1, now=T0)
    failed = await _execution(db, info.value.execution_id or 0)
    assert (failed["type"], failed["status"], failed["initiator_id"]) == (ExecutionType.MINT, ExecutionStatus.FAILED, MEMBER)
    assert failed["output"]["error"]["code"] == "permission_denied"
    assert len(await _executions(db, ExecutionType.MINT)) == 1


async def test_administrator_manages_under_delegation(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds)
    await cur.enable_delegation(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=ADMIN, new_rate_ppm=0, now=T0)
    applied = await cur.apply_rate_change(db, guilds, currency_id=GUILD, user_id=ADMIN, now=T0 + 7 * DAY)
    assert (await _execution(db, applied.execution_id))["initiator_id"] == ADMIN
    renounced = await cur.renounce(db, guilds, currency_id=GUILD, user_id=ADMIN, now=T0 + 7 * DAY)
    assert (await _execution(db, renounced.execution_id))["initiator_id"] == ADMIN
    requested = await cur.request_delete(db, guilds, currency_id=GUILD, user_id=ADMIN, now=T0 + 7 * DAY)
    deleted = await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=ADMIN, now=requested.finalize_from)
    assert (await _execution(db, deleted.execution_id))["initiator_id"] == ADMIN
    assert await cur.get_currency(db, GUILD) is None
    await _assert_invariants(db)


async def test_finalize_clears_pending_rate_change(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds)
    await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, new_rate_ppm=5, now=T0)
    await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 7 * DAY)
    row = await _currency_row(db)
    assert (row["pending_rate_ppm"], row["rate_change_requested_at"], row["delete_requested_at"]) == (None, None, None)
    assert row["deleted_at"] == T0 + 7 * DAY


async def test_finalize_locks_a_lower_partner_first(db: AsyncEngine, guilds: InMemoryGuildInfo, dissolver: RecordingDissolver) -> None:
    lower = GUILD - 1
    guilds.add_guild(lower, NEW_OWNER)
    await _create(db, guilds)
    await _create(db, guilds, guild_id=lower, user_id=NEW_OWNER, symbol="LOW", initial_supply=50)
    pool_account = await _create_pool_account(db, 2, (lower, GUILD), OWNER)
    await _fund_pool(db, pool_account, lower, NEW_OWNER, 50)
    await _fund_pool(db, pool_account, GUILD, OWNER, 70)
    dissolver.pool_account, dissolver.partner = pool_account, lower
    await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    deleted = await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 7 * DAY)
    assert dissolver.events == ["partners", "partners", "keys", "dissolve"]
    assert deleted.balances_burned == 2
    assert await balance_of(db, MEMBER, lower) == 50
    assert await supply_of(db, GUILD) == 0
    await _assert_invariants(db)


async def test_rate_apply_hitting_supply_cap_halts_interest(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    big = 10**37
    await _create(db, guilds, initial_supply=big, hourly_rate_ppm=1_000_000)
    await staking.deposit(db, account_id=OWNER, currency_id=GUILD, amount=big, now=T0)
    await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, new_rate_ppm=0, now=T0)
    applied = await cur.apply_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 7 * DAY)
    assert len(applied.interest_execution_ids) == 1
    info = await cur.get_currency(db, GUILD)
    assert info is not None
    assert (info.supply, info.interest_halted, info.hourly_rate_ppm) == (AMOUNT_MAX, True, 0)
    stake_info = await staking.get_stake(db, OWNER, GUILD)
    assert stake_info is not None and stake_info.amount == AMOUNT_MAX
    await staking.withdraw(db, account_id=OWNER, currency_id=GUILD, amount=AMOUNT_MAX, now=T0 + 8 * DAY)
    await _assert_invariants(db)


async def test_rate_apply_racing_deposits_and_worker(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds, initial_supply=10_000_000, hourly_rate_ppm=10_000)
    for holder in (HOLDER, MEMBER, ADMIN):
        await transfer(db, sender_id=OWNER, recipient_id=holder, currency_id=GUILD, amount=1_000_000, now=T0)
        await staking.deposit(db, account_id=holder, currency_id=GUILD, amount=500_000, now=T0)
    await cur.request_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, new_rate_ppm=20_000, now=T0)
    at = T0 + 7 * DAY
    results = await asyncio.gather(
        staking.deposit(db, account_id=HOLDER, currency_id=GUILD, amount=10, now=at),
        cur.apply_rate_change(db, guilds, currency_id=GUILD, user_id=OWNER, now=at),
        staking.accrue_stale_stakes(db, older_than=timedelta(hours=3), now=at),
        staking.withdraw(db, account_id=MEMBER, currency_id=GUILD, amount=10, now=at),
        staking.deposit(db, account_id=ADMIN, currency_id=GUILD, amount=10, now=at),
        return_exceptions=True,
    )
    assert not [result for result in results if isinstance(result, BaseException)]
    # Every stake was accrued exactly once at the old rate for the whole week, whichever transaction got there first.
    week = staking.compute_accrual(500_000, 0, 10_000, 7 * 24 * staking.MICROSECONDS_PER_HOUR)
    assert len(await _executions(db, ExecutionType.INTEREST)) == 3
    for holder, delta in ((HOLDER, 10), (MEMBER, -10), (ADMIN, 10)):
        info = await staking.get_stake(db, holder, GUILD)
        assert info is not None
        assert (info.amount, info.carry, info.last_accrued_at) == (week.amount + delta, week.carry, at)
    currency_info = await cur.get_currency(db, GUILD)
    assert currency_info is not None and currency_info.hourly_rate_ppm == 20_000
    await _assert_invariants(db)


@pytest.mark.parametrize(
    ("answers", "reads"),
    [
        # A partner appears once under the lock: the first run rolls back and the rerun locks it and succeeds.
        ([set(), {OTHER_GUILD}, {OTHER_GUILD}, {OTHER_GUILD}], 4),
        # A partner disappears under the lock: the locked set still covers it, so the first run succeeds.
        ([{OTHER_GUILD}, set()], 2),
        # A partner flickers between the reads of every run: the rerun pre-locks it, so the second run succeeds.
        ([set(), {OTHER_GUILD}], 4),
    ],
)
async def test_finalize_partner_set_changes(db: AsyncEngine, guilds: InMemoryGuildInfo, answers: list[set[int]], reads: int) -> None:
    await _create(db, guilds)
    await _create(db, guilds, guild_id=OTHER_GUILD, user_id=NEW_OWNER, symbol="SLV", initial_supply=1)
    await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    shifting = ShiftingDissolver(answers)
    previous = register_pool_dissolver(shifting)
    try:
        deleted = await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 7 * DAY)
    finally:
        register_pool_dissolver(previous)
    assert shifting.reads == reads
    assert [row["execution_id"] for row in await _executions(db, ExecutionType.CURRENCY_DELETE_FINALIZE)] == [deleted.execution_id]
    assert await cur.get_currency(db, GUILD) is None
    await _assert_invariants(db)


async def test_finalize_gives_up_after_bounded_reruns(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds)
    partners = [OTHER_GUILD, OTHER_GUILD + 1, OTHER_GUILD + 2]
    for index, partner in enumerate(partners):
        guilds.add_guild(partner, NEW_OWNER)
        await _create(db, guilds, guild_id=partner, user_id=NEW_OWNER, symbol="SL" + "ABC"[index], initial_supply=1)
    await cur.request_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0)
    growing = GrowingDissolver(partners)
    previous = register_pool_dissolver(growing)
    try:
        with pytest.raises(InvalidState, match="pools of this currency changed") as changed:
            await cur.finalize_delete(db, guilds, currency_id=GUILD, user_id=OWNER, now=T0 + 7 * DAY)
    finally:
        register_pool_dissolver(previous)
    assert growing.reads == 2 * (MAX_RERUNS + 1)
    # The reruns rolled back without a trace; only the last run is recorded, as a failure.
    assert [row["execution_id"] for row in await _executions(db, ExecutionType.CURRENCY_DELETE_FINALIZE)] == [changed.value.execution_id]
    assert await cur.get_currency(db, GUILD) is not None
    await _assert_invariants(db)


class CountingGuildInfo(InMemoryGuildInfo):
    def __init__(self) -> None:
        super().__init__()
        self.lookups = 0

    async def guild_owner(self, guild_id: int) -> int | None:
        self.lookups += 1
        return await super().guild_owner(guild_id)


async def test_guild_is_consulted_once_per_call(db: AsyncEngine) -> None:
    counting = CountingGuildInfo()
    counting.add_guild(GUILD, OWNER)
    await _create(db, counting)
    counting.lookups = 0
    await cur.mint(db, counting, currency_id=GUILD, user_id=OWNER, amount=1, now=T0)
    assert counting.lookups == 1


async def test_in_tx_rejects_a_snapshot_of_another_subject(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _create(db, guilds)
    snapshot = await snapshot_guild(guilds, GUILD, OWNER)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await cur.mint_in_tx(tx, execution_id, snapshot, currency_id=GUILD, user_id=ADMIN, amount=1)).to_output()

    with pytest.raises(LedgerUsageError):
        await run_operation(db, type=ExecutionType.MINT, initiator_id=ADMIN, input={}, body=body, now=T0)
    assert await balance_of(db, ADMIN, GUILD) == 0
