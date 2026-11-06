from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.errors import IdempotencyConflict, InsufficientBalance, InvalidArgument
from rapidwire.core.ledger import LedgerTx, OperationResult, run_operation
from rapidwire.core.types import ExecutionType, TransferKind
from rapidwire.db.tables import execution, transfer
from tests.integration.factories import balance_of, create_currency, mint

NOW = datetime(2026, 4, 1, tzinfo=UTC)
CUR = 5
ALICE = 111
BOB = 222


class Counter:
    def __init__(self) -> None:
        self.calls = 0


async def _send(engine: AsyncEngine, amount: int, key: str | None, counter: Counter, *, delay: float = 0.0, initiator: int = ALICE, type: str = ExecutionType.TRANSFER) -> OperationResult:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        counter.calls += 1
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(initiator, CUR), (BOB, CUR)])
        if delay:
            await asyncio.sleep(delay)
        return {"transfer_id": await tx.move(execution_id, CUR, initiator, BOB, amount, TransferKind.PRINCIPAL), "amount": amount}

    return await run_operation(engine, type=type, initiator_id=initiator, input={"to": BOB, "amount": amount}, body=body, idempotency_key=key, now=NOW)


async def _count(engine: AsyncEngine) -> tuple[int, int]:
    async with engine.connect() as conn:
        executions = (await conn.execute(select(func.count()).select_from(execution).where(execution.c.type == "transfer"))).scalar_one()
        transfers = (await conn.execute(select(func.count()).select_from(transfer).where(transfer.c.to_account == BOB))).scalar_one()
    return executions, transfers


@pytest.fixture
async def funded(db: AsyncEngine) -> AsyncEngine:
    await create_currency(db, currency_id=CUR, symbol="IDM")
    await mint(db, CUR, ALICE, 100, now=NOW)
    return db


async def test_first_request_runs_once_and_replay_returns_stored_result(funded: AsyncEngine) -> None:
    counter = Counter()
    first = await _send(funded, 10, "key-1", counter)
    again = await _send(funded, 10, "key-1", counter)
    assert counter.calls == 1
    assert not first.replayed
    assert again.replayed
    assert again.execution_id == first.execution_id
    assert again.output == first.output
    assert again.int_field("amount") == 10
    assert await _count(funded) == (1, 1)
    assert await balance_of(funded, BOB, CUR) == 10


async def test_replay_of_failure_reraises_the_same_error(funded: AsyncEngine) -> None:
    counter = Counter()
    with pytest.raises(InsufficientBalance) as first:
        await _send(funded, 1_000, "key-2", counter)
    with pytest.raises(InsufficientBalance) as again:
        await _send(funded, 1_000, "key-2", counter)
    assert counter.calls == 1
    assert first.value.execution_id is not None
    assert again.value.execution_id == first.value.execution_id
    assert again.value.message == first.value.message
    assert await _count(funded) == (1, 0)


async def test_replay_with_different_request_conflicts(funded: AsyncEngine) -> None:
    counter = Counter()
    await _send(funded, 10, "key-3", counter)
    with pytest.raises(IdempotencyConflict):
        await _send(funded, 11, "key-3", counter)
    with pytest.raises(IdempotencyConflict):
        await _send(funded, 10, "key-3", counter, type=ExecutionType.TRANSFER_FROM)
    assert counter.calls == 1
    assert await _count(funded) == (1, 1)


async def test_keys_are_scoped_per_initiator(funded: AsyncEngine) -> None:
    await mint(funded, CUR, 333, 50, now=NOW)
    counter = Counter()
    first = await _send(funded, 10, "shared", counter)
    second = await _send(funded, 10, "shared", counter, initiator=333)
    assert first.execution_id != second.execution_id
    assert counter.calls == 2


async def test_concurrent_requests_with_same_key_execute_once(funded: AsyncEngine) -> None:
    counter = Counter()
    results = await asyncio.gather(_send(funded, 10, "key-4", counter, delay=0.3), _send(funded, 10, "key-4", counter, delay=0.3))
    assert counter.calls == 1
    assert results[0].execution_id == results[1].execution_id
    assert sorted(result.replayed for result in results) == [False, True]
    assert await _count(funded) == (1, 1)
    assert await balance_of(funded, BOB, CUR) == 10


async def test_second_request_runs_when_the_first_holder_rolls_back(funded: AsyncEngine) -> None:
    calls: list[str] = []
    first_inside = asyncio.Event()

    async def crashing(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        calls.append("first")
        first_inside.set()
        await asyncio.sleep(0.3)
        raise RuntimeError("worker crashed")

    async def working(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        calls.append("second")
        return {"done": True}

    async def second() -> OperationResult:
        await first_inside.wait()
        return await run_operation(funded, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={"x": 1}, body=working, idempotency_key="key-5", now=NOW)

    first_task = asyncio.create_task(run_operation(funded, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={"x": 1}, body=crashing, idempotency_key="key-5", now=NOW))
    result = await second()
    with pytest.raises(RuntimeError, match="worker crashed"):
        await first_task
    assert calls == ["first", "second"]
    assert not result.replayed
    assert result.output == {"done": True}
    async with funded.connect() as conn:
        stored = (await conn.execute(select(execution.c.execution_id).where(execution.c.idempotency_key == "key-5"))).scalars().all()
    assert stored == [result.execution_id]


async def test_key_length_is_validated(funded: AsyncEngine) -> None:
    counter = Counter()
    with pytest.raises(InvalidArgument):
        await _send(funded, 10, "", counter)
    with pytest.raises(InvalidArgument):
        await _send(funded, 10, "k" * 256, counter)
    await _send(funded, 10, "k" * 255, counter)
    assert counter.calls == 1
