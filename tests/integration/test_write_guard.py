from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import Response
from sqlalchemy import func, select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.api.auth import Principal, Writer
from rapidwire.core.errors import InsufficientBalance, Unauthorized
from rapidwire.core.ledger import LedgerTx, RerunTransaction, failure_output, is_replayed_error, run_operation, set_failure_output, write_guard
from rapidwire.core.tokens import SecretToken, issue_token, revoke_tokens
from rapidwire.core.types import ExecutionType, TransferKind
from rapidwire.db.tables import account, execution
from tests.integration.factories import balance_of, create_currency, mint

NOW = datetime(2026, 5, 1, tzinfo=UTC)
CUR = 77
ALICE = 3101
BOB = 3102
NEWCOMER = 3199


class Recorder:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[int, bool, int]] = []
        self.error = error

    async def __call__(self, conn: AsyncConnection, initiator_id: int) -> None:
        rows = (await conn.execute(select(func.count()).select_from(execution).where(execution.c.initiator_id == initiator_id, execution.c.type == ExecutionType.TRANSFER))).scalar_one()
        self.calls.append((initiator_id, conn.in_transaction(), int(rows)))
        if self.error is not None:
            raise self.error


async def _send(engine: AsyncEngine, amount: int, *, recipient: int = BOB, key: str | None = None) -> Any:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (recipient, CUR)])
        return {"transfer_id": await tx.move(execution_id, CUR, ALICE, recipient, amount, TransferKind.PRINCIPAL)}

    return await run_operation(engine, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={"amount": amount, "to": recipient}, body=body, idempotency_key=key, user_accounts=[recipient], now=NOW)


async def _count(engine: AsyncEngine, table: Any, *where: Any) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(table).where(*where))).scalar_one())


@pytest.fixture
async def funded(db: AsyncEngine) -> AsyncEngine:
    await create_currency(db, currency_id=CUR, symbol="WG")
    await mint(db, CUR, ALICE, 100, now=NOW)
    return db


async def test_without_a_guard_operations_are_unchanged(funded: AsyncEngine) -> None:
    result = await _send(funded, 5)
    assert not result.replayed
    assert await balance_of(funded, BOB, CUR) == 5


async def test_guard_runs_inside_the_transaction_before_the_execution(funded: AsyncEngine) -> None:
    guard = Recorder()
    with write_guard(guard):
        await _send(funded, 5)
    assert guard.calls == [(ALICE, True, 0)]
    await _send(funded, 5)
    assert len(guard.calls) == 1


async def test_failing_guard_records_nothing(funded: AsyncEngine) -> None:
    guard = Recorder(Unauthorized())
    with write_guard(guard), pytest.raises(Unauthorized) as caught:
        await _send(funded, 5, recipient=NEWCOMER)
    assert caught.value.execution_id is None
    assert await _count(funded, execution, execution.c.type == ExecutionType.TRANSFER) == 0
    assert await _count(funded, account, account.c.account_id == NEWCOMER) == 0
    assert await balance_of(funded, ALICE, CUR) == 100


async def test_guard_is_not_consulted_for_a_replay(funded: AsyncEngine) -> None:
    await _send(funded, 5, key="k1")
    guard = Recorder(Unauthorized())
    with write_guard(guard):
        again = await _send(funded, 5, key="k1")
    assert again.replayed
    assert guard.calls == []


async def test_guard_runs_again_on_every_attempt(funded: AsyncEngine) -> None:
    attempts: list[str] = []

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        attempts.append("body")
        if len(attempts) == 1:
            raise OperationalError("UPDATE balance", {}, Exception(1213, "Deadlock found when trying to get lock"))
        if len(attempts) == 2:
            raise RerunTransaction("plan changed")
        return {}

    guard = Recorder()
    with write_guard(guard):
        result = await run_operation(funded, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={}, body=body, now=NOW)
    assert not result.replayed
    assert attempts == ["body"] * 3
    assert guard.calls == [(ALICE, True, 0)] * 3


async def test_guard_refusal_on_a_retry_records_nothing(funded: AsyncEngine) -> None:
    calls: list[int] = []

    async def guard(conn: AsyncConnection, initiator_id: int) -> None:
        calls.append(initiator_id)
        if len(calls) > 1:
            raise Unauthorized()

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        raise RerunTransaction("plan changed")

    with write_guard(guard), pytest.raises(Unauthorized):
        await run_operation(funded, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={}, body=body, now=NOW)
    assert calls == [ALICE, ALICE]
    assert await _count(funded, execution, execution.c.type == ExecutionType.TRANSFER) == 0


async def test_revoke_started_during_a_write_waits_for_its_commit(funded: AsyncEngine) -> None:
    issued = await issue_token(funded, account_id=ALICE)
    holding = asyncio.Event()
    release = asyncio.Event()
    guard = Writer(Principal(account_id=ALICE, token=SecretToken(issued.token)), None, Response()).guard

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR)])
        transfer_id = await tx.move(execution_id, CUR, ALICE, BOB, 7, TransferKind.PRINCIPAL)
        holding.set()
        await release.wait()
        return {"transfer_id": transfer_id}

    async def write() -> Any:
        with write_guard(guard):
            return await run_operation(funded, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={"amount": 7}, body=body, user_accounts=[BOB], now=NOW)

    writing = asyncio.create_task(write())
    await holding.wait()
    revoking = asyncio.create_task(revoke_tokens(funded, account_id=ALICE))
    await _wait_for_lock_wait(funded)
    assert not revoking.done()
    release.set()
    written = await writing
    revoked = await revoking
    assert revoked.revoked_token_ids == (issued.token_id,)
    assert await balance_of(funded, BOB, CUR) == 7
    async with funded.connect() as conn:
        rows = (await conn.execute(select(execution.c.execution_id, execution.c.type).order_by(execution.c.execution_id))).all()
    order = [row.type for row in rows if row.execution_id >= written.execution_id]
    assert order == [ExecutionType.TRANSFER, ExecutionType.TOKEN_REVOKE]
    with write_guard(guard), pytest.raises(Unauthorized):
        await _send(funded, 1)


async def _wait_for_lock_wait(engine: AsyncEngine) -> None:
    """Return once some transaction is waiting for a row lock."""
    async with engine.connect() as conn:
        for _ in range(200):
            waiting = (await conn.execute(text("SELECT COUNT(*) FROM performance_schema.data_lock_waits"))).scalar_one()
            await conn.rollback()
            if waiting:
                return
            await asyncio.sleep(0.02)
    raise AssertionError("no transaction waited for a lock")


async def test_replayed_failures_carry_their_stored_fields(funded: AsyncEngine) -> None:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        error = InsufficientBalance()
        set_failure_output(error, {"kind": "SETTLEMENT", "gas_used": 4})
        raise error

    async def attempt() -> InsufficientBalance:
        with pytest.raises(InsufficientBalance) as caught:
            await run_operation(funded, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={}, body=body, idempotency_key="fail-1", now=NOW)
        return caught.value

    first = await attempt()
    assert not is_replayed_error(first)
    again = await attempt()
    assert is_replayed_error(again)
    assert again.execution_id == first.execution_id
    assert failure_output(again) == {"kind": "SETTLEMENT", "gas_used": "4"}
