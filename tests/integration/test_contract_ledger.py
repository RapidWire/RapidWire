"""The ledger features contract calls rely on: an on_failure hook locking balances of held currencies under late_currency_locks, and run_operation() with rollback_inspect, which never commits."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.errors import DomainError, InsufficientBalance
from rapidwire.core.ledger import LedgerTx, LedgerUsageError, run_operation
from rapidwire.core.types import ExecutionType, TransferKind
from rapidwire.db.tables import execution, transfer
from tests.integration.contract_runtime_helpers import NOW, RAPID, setup_gas
from tests.integration.factories import balance_of, create_currency

USER = 7001
OTHER = 7002
GEM = 900_002


async def _count(engine: AsyncEngine, table: Any) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(table))).scalar_one())


def _operation(issuance: int, *, lock_other_currency: bool = False) -> dict[str, Any]:
    async def prepare(tx: LedgerTx, execution_id: int) -> None:
        await tx.lock_currencies([RAPID])
        await tx.lock_balances([(USER, RAPID)])

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        raise InsufficientBalance("body failed")

    async def on_failure(tx: LedgerTx, execution_id: int, error: DomainError) -> None:
        if lock_other_currency:
            await tx.lock_balances([(USER, GEM)])
        await tx.lock_balances([(issuance, RAPID)])
        await tx.move(execution_id, RAPID, USER, issuance, 7, TransferKind.GAS)

    return {"prepare": prepare, "body": body, "on_failure": on_failure}


async def test_on_failure_may_lock_balances_of_held_currencies_with_late_locks(db: AsyncEngine) -> None:
    issuance = await setup_gas(db, {USER: 100})
    with pytest.raises(InsufficientBalance) as info:
        await run_operation(db, type=ExecutionType.CONTRACT_CALL, initiator_id=USER, input={}, late_currency_locks=True, now=NOW, **_operation(issuance))
    assert info.value.execution_id is not None
    assert await balance_of(db, USER, RAPID) == 93
    assert await balance_of(db, issuance, RAPID) == -93


async def test_on_failure_still_cannot_lock_without_late_locks_or_for_other_currencies(db: AsyncEngine) -> None:
    issuance = await setup_gas(db, {USER: 100})
    await create_currency(db, currency_id=GEM, symbol="GEM", now=NOW)
    with pytest.raises(LedgerUsageError, match="on_failure"):
        await run_operation(db, type=ExecutionType.CONTRACT_CALL, initiator_id=USER, input={}, now=NOW, **_operation(issuance))
    with pytest.raises(LedgerUsageError, match="must be locked with lock_currencies"):
        await run_operation(db, type=ExecutionType.CONTRACT_CALL, initiator_id=USER, input={}, late_currency_locks=True, now=NOW, **_operation(issuance, lock_other_currency=True))
    assert await balance_of(db, USER, RAPID) == 100


async def test_rollback_inspect_sees_the_writes_and_commits_nothing(db: AsyncEngine) -> None:
    issuance = await setup_gas(db, {USER: 100})
    executions_before, transfers_before = await _count(db, execution), await _count(db, transfer)
    seen: list[int] = []

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([RAPID])
        await tx.lock_balances([(USER, RAPID), (OTHER, RAPID)])
        await tx.move(execution_id, RAPID, USER, OTHER, 30, TransferKind.PRINCIPAL)
        return {"moved": 30}

    async def inspect(tx: LedgerTx, execution_id: int) -> None:
        seen.append(int((await tx.conn.execute(select(func.count()).select_from(transfer).where(transfer.c.execution_id == execution_id))).scalar_one()))

    result = await run_operation(db, type=ExecutionType.TRANSFER, initiator_id=USER, input={}, body=body, now=NOW, rollback_inspect=inspect)
    assert result.output == {"moved": "30"}
    assert seen == [1]
    with pytest.raises(InsufficientBalance) as info:
        await run_operation(db, type=ExecutionType.CONTRACT_CALL, initiator_id=USER, input={}, late_currency_locks=True, now=NOW, rollback_inspect=inspect, **_operation(issuance))
    assert info.value.execution_id is None
    assert seen == [1, 1]
    assert (await _count(db, execution), await _count(db, transfer)) == (executions_before, transfers_before)
    assert await balance_of(db, USER, RAPID) == 100
    with pytest.raises(LedgerUsageError, match="idempotency key"):
        await run_operation(db, type=ExecutionType.TRANSFER, initiator_id=USER, input={}, body=body, now=NOW, idempotency_key="k", rollback_inspect=inspect)
