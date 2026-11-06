"""execution.contract_id on real contract executions: top-level calls, nested children (successful, failed and refused by the VM), and the management executions of a contract, as recorded and as listed by list_executions(contract_id=)."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.values import Account
from rapidwire.core import contracts
from rapidwire.core.contract_calls import ContractCallResult, call_contract
from rapidwire.core.errors import ContractNotFound
from rapidwire.core.queries import list_executions
from rapidwire.core.types import ExecutionType
from tests.integration.contract_runtime_helpers import CALLER, NOW, config, deploy, executions, setup_gas
from tests.integration.test_contract_nested import CALLEE_SOURCE, CALLER_SOURCE

A = 8101
B = 8102
NOBODY = 8105


async def _setup(engine: AsyncEngine) -> None:
    await setup_gas(engine, {CALLER: 1_000_000, A: 1_000})
    await deploy(engine, A, CALLER_SOURCE)
    await deploy(engine, B, CALLEE_SOURCE)


async def _call(engine: AsyncEngine, function: str, args: list[Any]) -> ContractCallResult:
    return await call_contract(engine, None, caller_id=CALLER, contract_id=A, function=function, args=args, gas_limit=20_000, config=config(), now=NOW)


async def _rows(engine: AsyncEngine) -> dict[int, dict[str, Any]]:
    return {row["execution_id"]: row for row in await executions(engine)}


async def test_calls_and_children_name_the_called_contract(db: AsyncEngine) -> None:
    await _setup(db)
    strict = await _call(db, "strict", [Account(B)])
    missing = await _call(db, "relay", [Account(NOBODY), "ok", 300])
    unknown = await _call(db, "relay", [Account(B), "missing", 300])
    reentrant = await _call(db, "relay", [Account(A), "noop", 300])
    refused = await _call(db, "relay_many", [Account(B), 17])
    rows = await _rows(db)
    expected = {strict.execution_id: B, missing.execution_id: NOBODY, unknown.execution_id: B, reentrant.execution_id: A, refused.execution_id: B}
    child_of: dict[int, int] = {}
    for parent, target in expected.items():
        assert rows[parent]["contract_id"] == A
        [child] = [row for row in rows.values() if row["parent_execution_id"] == parent]
        assert (child["type"], child["contract_id"]) == (ExecutionType.CONTRACT_CALL, target), parent
        child_of[parent] = child["execution_id"]

    calls_of_b = await list_executions(db, contract_id=B, execution_type=ExecutionType.CONTRACT_CALL)
    assert {item.execution_id for item in calls_of_b.items} == {child_of[parent] for parent in (strict.execution_id, unknown.execution_id, refused.execution_id)}
    assert all(item.parent_execution_id is not None for item in calls_of_b.items)
    calls_of_a = await list_executions(db, contract_id=A, execution_type=ExecutionType.CONTRACT_CALL)
    assert {item.execution_id for item in calls_of_a.items} == set(expected) | {child_of[reentrant.execution_id]}


async def test_a_failed_top_level_call_names_its_target(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 1_000_000})
    with pytest.raises(ContractNotFound) as info:
        await call_contract(db, None, caller_id=CALLER, contract_id=NOBODY, function="ok", args=[], gas_limit=1_000, config=config(), now=NOW)
    assert info.value.execution_id is not None
    assert (await _rows(db))[info.value.execution_id]["contract_id"] == NOBODY


async def test_management_executions_name_their_contract(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 1_000_000})
    await deploy(db, B, CALLEE_SOURCE)
    paused = await contracts.pause_contract(db, account_id=B, now=NOW)
    resumed = await contracts.resume_contract(db, account_id=B, now=NOW)
    deleted = await contracts.delete_contract(db, account_id=B, now=NOW)
    listed = await list_executions(db, contract_id=B)
    assert [(item.type, item.contract_id) for item in listed.items] == [
        (ExecutionType.CONTRACT_DELETE, B),
        (ExecutionType.CONTRACT_RESUME, B),
        (ExecutionType.CONTRACT_PAUSE, B),
        (ExecutionType.CONTRACT_UPDATE, B),
    ]
    assert [item.execution_id for item in listed.items[:3]] == [deleted.execution_id, resumed.execution_id, paused.execution_id]
