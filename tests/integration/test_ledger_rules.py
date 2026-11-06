from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import currency as cur
from rapidwire.core import db as db_module
from rapidwire.core.accounts import account_kinds
from rapidwire.core.claims import cancel_claim, pay_claim
from rapidwire.core.db import consistent_snapshot
from rapidwire.core.discord_permissions import allow_discord
from rapidwire.core.errors import AccountNotFound, InsufficientBalance, InvalidArgument, InvalidState, PermissionDenied
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import MAX_RERUNS, LedgerTx, LedgerUsageError, RerunTransaction, run_child, run_operation
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.tokens import issue_token
from rapidwire.core.transfers import transfer
from rapidwire.core.types import AccountKind, ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.tables import account, execution
from tests.integration.factories import balance_of, create_currency, mint

NOW = datetime(2026, 6, 1, tzinfo=UTC)
CUR = 91
OTHER = 92
ALICE = 111
BOB = 222

type Call = Callable[[AsyncEngine], Awaitable[Any]]


@pytest.fixture
async def funded(db: AsyncEngine) -> AsyncEngine:
    await create_currency(db, currency_id=CUR, symbol="RUL", now=NOW)
    await create_currency(db, currency_id=OTHER, symbol="RUX", now=NOW)
    await mint(db, CUR, ALICE, 100, now=NOW)
    await mint(db, OTHER, ALICE, 100, now=NOW)
    return db


async def _count(engine: AsyncEngine, table: Any) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(table))).scalar_one())


class RaisingGuildInfo(InMemoryGuildInfo):
    async def guild_owner(self, guild_id: int) -> int | None:
        raise RuntimeError("GuildInfo was consulted")

    async def is_administrator(self, guild_id: int, user_id: int) -> bool:
        raise RuntimeError("GuildInfo was consulted")


SYSTEM_INITIATORS: dict[str, Call] = {
    "transfer sender -1": lambda engine: transfer(engine, sender_id=-1, recipient_id=BOB, currency_id=CUR, amount=1, now=NOW),
    "transfer sender 0": lambda engine: transfer(engine, sender_id=0, recipient_id=BOB, currency_id=CUR, amount=1, now=NOW),
    "pay_claim payer -1": lambda engine: pay_claim(engine, claim_id=1, payer_id=-1, now=NOW),
    "cancel_claim user -2": lambda engine: cancel_claim(engine, claim_id=1, user_id=-2, now=NOW),
    "issue_token -1": lambda engine: issue_token(engine, account_id=-1, now=NOW),
}


@pytest.mark.parametrize("call", SYSTEM_INITIATORS.values(), ids=SYSTEM_INITIATORS.keys())
async def test_non_user_initiator_is_rejected_before_any_write(funded: AsyncEngine, call: Call) -> None:
    executions, accounts = await _count(funded, execution), await _count(funded, account)
    with pytest.raises(InvalidArgument) as caught:
        await call(funded)
    assert caught.value.execution_id is None
    assert (await _count(funded, execution), await _count(funded, account)) == (executions, accounts)


async def test_run_child_rejects_a_non_user_initiator(funded: AsyncEngine) -> None:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        async def child(child_tx: LedgerTx, child_id: int) -> dict[str, Any]:
            return {}

        await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.CONTRACT_CALL, initiator_id=-1, input={}, body=child)
        return {}

    with pytest.raises(InvalidArgument) as caught:
        await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, now=NOW)
    async with funded.connect() as conn:
        rows = (await conn.execute(select(execution.c.status, execution.c.parent_execution_id).where(execution.c.type == ExecutionType.CONTRACT_CALL))).all()
    assert [(row.status, row.parent_execution_id) for row in rows] == [(ExecutionStatus.FAILED, None)]
    assert caught.value.execution_id is not None


async def test_new_currency_after_balances_is_a_usage_error(funded: AsyncEngine) -> None:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR)])
        await tx.lock_currencies([CUR])
        await tx.lock_currencies([OTHER])
        return {}

    executions = await _count(funded, execution)
    with pytest.raises(LedgerUsageError, match="after balance rows"):
        await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, now=NOW)
    assert await _count(funded, execution) == executions


async def test_run_child_may_lock_a_currency_after_balances(funded: AsyncEngine) -> None:
    async def child(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([OTHER])
        await tx.lock_balances([(ALICE, OTHER), (BOB, OTHER)])
        return {"transfer_id": await tx.move(execution_id, OTHER, ALICE, BOB, 5, TransferKind.PRINCIPAL)}

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR)])
        await tx.move(execution_id, CUR, ALICE, BOB, 7, TransferKind.PRINCIPAL)
        await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={}, body=child)
        return {}

    await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, now=NOW)
    assert (await balance_of(funded, BOB, CUR), await balance_of(funded, BOB, OTHER)) == (7, 5)
    assert (await verify_invariants(funded)).ok


async def test_late_currency_locks_let_a_top_level_body_lock_after_balances(funded: AsyncEngine) -> None:
    async def prepare(tx: LedgerTx, execution_id: int) -> None:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR)])

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([OTHER])
        await tx.lock_balances([(ALICE, OTHER), (BOB, OTHER)])
        return {"transfer_id": await tx.move(execution_id, OTHER, ALICE, BOB, 3, TransferKind.PRINCIPAL)}

    with pytest.raises(LedgerUsageError, match="after balance rows"):
        await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, prepare=prepare, now=NOW)
    await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, prepare=prepare, now=NOW, late_currency_locks=True)
    assert await balance_of(funded, BOB, OTHER) == 3


async def test_failed_balance_lock_in_a_child_does_not_block_later_currency_locks(funded: AsyncEngine) -> None:
    async def child(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (-999, CUR)])
        return {}

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        with pytest.raises(AccountNotFound):
            await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=child)
        await tx.lock_currencies([OTHER])
        return {}

    await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, now=NOW)


async def test_run_child_passes_a_rerun_signal_through_unchanged(funded: AsyncEngine) -> None:
    signal = RerunTransaction("rerun", exclusive={CUR}, pre_lock={OTHER})
    runs: list[tuple[str | None, str | None, bool]] = []

    async def child(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        raise signal

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        runs.append((tx.currency_lock(CUR), tx.currency_lock(OTHER), tx.rerun_allowed))
        if len(runs) == 1:
            with pytest.raises(RerunTransaction) as caught:
                await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=child)
            assert caught.value is signal
            raise caught.value
        await tx.lock_currencies([CUR])
        return {}

    executions = await _count(funded, execution)
    await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, now=NOW)
    assert runs == [(None, None, True), (None, "share", True)]
    assert await _count(funded, execution) == executions + 1


async def test_rerun_is_bounded(funded: AsyncEngine) -> None:
    allowed: list[bool] = []

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        allowed.append(tx.rerun_allowed)
        raise RerunTransaction("again")

    with pytest.raises(RerunTransaction):
        await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, now=NOW)
    assert allowed == [True] * MAX_RERUNS + [False]


class Counter:
    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


async def test_before_is_skipped_by_keyed_replays(funded: AsyncEngine) -> None:
    before = Counter()

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return {"ok": True}

    async def failing(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        raise InvalidState("no")

    first = await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, now=NOW, idempotency_key="ok", before=before)
    again = await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, now=NOW, idempotency_key="ok", before=before)
    assert (again.execution_id, again.replayed, before.calls) == (first.execution_id, True, 1)

    stored = Counter()
    with pytest.raises(InvalidState) as failed:
        await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=failing, now=NOW, idempotency_key="bad", before=stored)
    with pytest.raises(InvalidState) as replayed:
        await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=failing, now=NOW, idempotency_key="bad", before=stored)
    assert replayed.value.execution_id == failed.value.execution_id is not None
    assert stored.calls == 1


async def test_before_runs_once_across_deadlock_retries(funded: AsyncEngine) -> None:
    before = Counter()
    attempts = 0

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DBAPIError("SELECT 1", {}, Exception(1213, "Deadlock found when trying to get lock"))
        return {}

    await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, now=NOW, before=before)
    assert (before.calls, attempts) == (1, 2)


async def test_concurrent_same_key_calls_run_the_body_once(funded: AsyncEngine) -> None:
    before = Counter()
    bodies = 0

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        nonlocal bodies
        bodies += 1
        await asyncio.sleep(0.2)
        return {}

    first, second = await asyncio.gather(
        *(run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, now=NOW, idempotency_key="same", before=before) for _ in range(2))
    )
    assert bodies == 1
    assert first.execution_id == second.execution_id
    assert sorted((first.replayed, second.replayed)) == [False, True]
    assert before.calls in (1, 2)


async def test_user_accounts_persist_when_the_body_fails(funded: AsyncEngine) -> None:
    with pytest.raises(InsufficientBalance) as caught:
        await transfer(funded, sender_id=5_001, recipient_id=5_002, currency_id=CUR, amount=1, now=NOW)
    assert caught.value.execution_id is not None
    async with funded.connect() as conn:
        assert await account_kinds(conn, [5_001, 5_002]) == {5_001: AccountKind.USER, 5_002: AccountKind.USER}


async def test_failed_snapshot_start_surfaces_the_database_error(funded: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(db_module, "_START_SNAPSHOT", "START TRANSACTION WITH NO SUCH OPTION")
    async with funded.connect() as conn:
        with pytest.raises(DBAPIError):
            async with consistent_snapshot(conn):
                pass
        assert not conn.in_transaction()
        assert (await conn.execute(text("SELECT @@SESSION.transaction_isolation"))).scalar_one() == "READ-COMMITTED"
    with pytest.raises(DBAPIError):
        async with consistent_snapshot(funded):
            pass


REPLAYS: dict[str, Callable[[AsyncEngine, InMemoryGuildInfo], Awaitable[Any]]] = {
    "mint": lambda engine, guilds: cur.mint(engine, guilds, currency_id=CUR, user_id=ALICE, amount=5, now=NOW, idempotency_key="k"),
    "enable_delegation": lambda engine, guilds: cur.enable_delegation(engine, guilds, currency_id=CUR, user_id=ALICE, now=NOW, idempotency_key="k"),
    "allow_discord": lambda engine, guilds: allow_discord(engine, guilds, guild_id=CUR, account_id=BOB, acting_user_id=ALICE, now=NOW, idempotency_key="k"),
}


@pytest.mark.parametrize("call", REPLAYS.values(), ids=REPLAYS.keys())
async def test_replay_does_not_consult_guild_info(funded: AsyncEngine, call: Callable[[AsyncEngine, InMemoryGuildInfo], Awaitable[Any]]) -> None:
    guilds = InMemoryGuildInfo()
    guilds.add_guild(CUR, ALICE)
    first = await call(funded, guilds)
    again = await call(funded, RaisingGuildInfo())
    assert again == first
    assert again.replayed and not first.replayed


async def test_replayed_failure_does_not_consult_guild_info(funded: AsyncEngine) -> None:
    guilds = InMemoryGuildInfo()
    guilds.add_guild(CUR, ALICE)
    with pytest.raises(PermissionDenied) as first:
        await cur.mint(funded, guilds, currency_id=CUR, user_id=BOB, amount=5, now=NOW, idempotency_key="k")
    with pytest.raises(PermissionDenied) as again:
        await cur.mint(funded, RaisingGuildInfo(), currency_id=CUR, user_id=BOB, amount=5, now=NOW, idempotency_key="k")
    assert again.value.execution_id == first.value.execution_id is not None


async def test_guild_info_is_consulted_only_for_valid_requests(funded: AsyncEngine) -> None:
    with pytest.raises(InvalidArgument):
        await cur.mint(funded, RaisingGuildInfo(), currency_id=CUR, user_id=-1, amount=5, now=NOW)
    with pytest.raises(RuntimeError, match="consulted"):
        await cur.mint(funded, RaisingGuildInfo(), currency_id=CUR, user_id=ALICE, amount=5, now=NOW, idempotency_key="fresh")
