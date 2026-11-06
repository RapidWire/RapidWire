"""Top-level contract calls through call_contract(): the recorded execution and its output, the attachment, storage, and every failure that happens before or inside the VM."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.values import Account, Attachment, Currency
from rapidwire.core import contracts
from rapidwire.core.contract_calls import ContractCallResult, call_contract
from rapidwire.core.errors import (
    AmountOverflow,
    ContractCallFailed,
    ContractNotFound,
    ContractPaused,
    ContractReverted,
    ContractRuntimeFailure,
    CurrencyNotFound,
    DomainError,
    IdempotencyConflict,
    InsufficientBalance,
    InvalidAmount,
    InvalidArgument,
    RevertError,
)
from rapidwire.core.types import ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.tables import account, balance
from tests.integration.contract_runtime_helpers import (
    CALLER,
    CONTRACT,
    NOW,
    RAPID,
    assert_clean,
    config,
    deploy,
    executions,
    setup_gas,
    stored,
    transfers_of,
)
from tests.integration.factories import balance_of, create_currency, mint, supply_of

COUNTER = """
@public
def bump(step: int) -> int:
    n: int = storage.get("n", 0) + step
    storage["n"] = n
    return n

@public
def ping(x: int) -> int:
    return x + 1

@public
def echo(a: int, b: str, c: bool, d: Account, e: Currency) -> str:
    return str(a) + b + str(int(c)) + str(int(d)) + str(int(e))

@public
def flag() -> bool:
    return True

@public
def boom(x: int) -> int:
    storage["before"] = 1
    return 10 // x

def helper() -> int:
    return 3
"""

SHOP = """
@public
def buy(price: int) -> str:
    if paid.amount < price:
        revert("Send at least the price")
    if paid.amount > price:
        transfer(sender, paid.currency, paid.amount - price)
    return "OK"

@public
def overspend(amount: int) -> str:
    transfer(sender, paid.currency, amount)
    return "never"
"""


async def _call(engine: AsyncEngine, function: str, args: list[Any] | None = None, **kwargs: Any) -> ContractCallResult:
    return await call_contract(engine, None, caller_id=CALLER, contract_id=CONTRACT, function=function, args=args or [], config=config(), now=NOW, **kwargs)


async def _failure(engine: AsyncEngine, function: str, args: list[Any] | None = None, **kwargs: Any) -> tuple[DomainError, dict[str, Any]]:
    with pytest.raises(DomainError) as info:
        await _call(engine, function, args, **kwargs)
    error = info.value
    assert error.execution_id is not None
    [row] = [row for row in await executions(engine) if row["execution_id"] == error.execution_id]
    return error, row


async def test_success_returns_and_records_the_value(db: AsyncEngine) -> None:
    issuance = await setup_gas(db)
    await deploy(db, CONTRACT, COUNTER)
    result = await _call(db, "bump", [5])
    assert (result.value, result.contract_id, result.function, result.gas_limit, result.random_values, result.replayed) == (5, CONTRACT, "bump", 128, (), False)
    [row] = await executions(db)
    assert row["execution_id"] == result.execution_id
    assert (row["type"], row["status"], row["initiator_id"], row["contract_function"], row["parent_execution_id"]) == (ExecutionType.CONTRACT_CALL, "success", CALLER, "bump", None)
    assert row["input"] == {"contract_id": str(CONTRACT), "function": "bump", "args": [{"type": "int", "value": "5"}], "attach": None, "gas_limit": "128"}
    assert row["output"] == {"value": "5", "value_type": "int", "gas_used": str(result.gas_used), "gas_limit": "128", "gas_currency_id": str(RAPID), "gas_price": "1", "random": []}
    assert (result.gas_currency_id, result.gas_price, result.gas_cost) == (RAPID, 1, result.gas_used)
    assert await stored(db, CONTRACT, "n") == 5
    [gas] = await transfers_of(db, result.execution_id)
    assert (gas["kind"], gas["from_account"], gas["to_account"], gas["amount"], gas["currency_id"]) == (TransferKind.GAS, CALLER, issuance, result.gas_used, RAPID)
    second = await _call(db, "bump", [2])
    assert second.value == 7
    assert await stored(db, CONTRACT, "n") == 7
    await assert_clean(db)


async def test_typed_arguments_and_results(db: AsyncEngine) -> None:
    await setup_gas(db)
    await deploy(db, CONTRACT, COUNTER)
    result = await _call(db, "echo", [12, "x", True, Account(34), Currency(56)])
    assert result.value == "12x13456"
    [row] = await executions(db)
    assert row["input"]["args"] == [
        {"type": "int", "value": "12"},
        {"type": "str", "value": "x"},
        {"type": "bool", "value": True},
        {"type": "Account", "value": "34"},
        {"type": "Currency", "value": "56"},
    ]
    assert row["output"]["value_type"] == "str"
    flag = await _call(db, "flag")
    assert flag.value is True
    assert (await executions(db))[-1]["output"]["value"] is True


async def test_idempotent_replay_returns_the_stored_result(db: AsyncEngine) -> None:
    await setup_gas(db)
    await deploy(db, CONTRACT, COUNTER)
    first = await _call(db, "bump", [1], idempotency_key="k1")
    again = await _call(db, "bump", [1], idempotency_key="k1")
    assert again == first
    assert again.replayed
    assert await stored(db, CONTRACT, "n") == 1


async def test_attachment_moves_to_the_contract_and_change_is_refunded(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 10_000})
    await deploy(db, CONTRACT, SHOP)
    result = await _call(db, "buy", [300], attach=Attachment(Currency(RAPID), 500))
    assert result.value == "OK"
    rows = await transfers_of(db, result.execution_id)
    moved = [(row["kind"], row["from_account"], row["to_account"], row["amount"]) for row in rows[:2]]
    assert moved == [(TransferKind.PRINCIPAL, CALLER, CONTRACT, 500), (TransferKind.PRINCIPAL, CONTRACT, CALLER, 200)]
    assert rows[2]["kind"] == TransferKind.GAS
    assert await balance_of(db, CONTRACT, RAPID) == 300
    assert await balance_of(db, CALLER, RAPID) == 10_000 - 300 - result.gas_used
    [row] = await executions(db)
    assert row["input"]["attach"] == {"currency_id": str(RAPID), "amount": "500"}
    await assert_clean(db)


async def test_revert_returns_the_attachment_and_collects_used_gas(db: AsyncEngine) -> None:
    issuance = await setup_gas(db, {CALLER: 10_000})
    await deploy(db, CONTRACT, SHOP)
    error, row = await _failure(db, "buy", [300], attach=Attachment(Currency(RAPID), 100))
    assert isinstance(error, ContractReverted)
    assert isinstance(error, RevertError)
    assert row["status"] == ExecutionStatus.REVERTED
    output = row["output"]
    assert output["error"] == {"code": "contract_reverted", "message": "Send at least the price"}
    assert output["kind"] == "REVERTED"
    assert output["location"]["function"] == "buy"
    used = int(output["gas_used"])
    assert used > 0
    [gas] = await transfers_of(db, row["execution_id"])
    assert (gas["kind"], gas["from_account"], gas["to_account"], gas["amount"]) == (TransferKind.GAS, CALLER, issuance, used)
    assert await balance_of(db, CONTRACT, RAPID) == 0
    assert await balance_of(db, CALLER, RAPID) == 10_000 - used
    await assert_clean(db)


async def test_builtin_failure_is_a_runtime_error_and_rolls_back_the_body(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 10_000})
    await deploy(db, CONTRACT, SHOP)
    error, row = await _failure(db, "overspend", [10**6], attach=Attachment(Currency(RAPID), 100))
    assert isinstance(error, ContractRuntimeFailure)
    assert row["status"] == ExecutionStatus.FAILED
    assert row["output"]["kind"] == "RUNTIME"
    assert row["output"]["host_code"] == "insufficient_balance"
    assert [transfer["kind"] for transfer in await transfers_of(db, row["execution_id"])] == [TransferKind.GAS]
    assert await balance_of(db, CONTRACT, RAPID) == 0
    await assert_clean(db)


async def test_runtime_error_rolls_back_storage(db: AsyncEngine) -> None:
    await setup_gas(db)
    await deploy(db, CONTRACT, COUNTER)
    error, row = await _failure(db, "boom", [0])
    assert isinstance(error, ContractRuntimeFailure)
    assert row["output"]["error"]["code"] == "contract_runtime_error"
    assert "division by zero" in row["output"]["error"]["message"]
    assert await stored(db, CONTRACT, "before") is None
    info = await contracts.get_contract(db, CONTRACT)
    assert info is not None
    assert info.storage_keys == 0
    await assert_clean(db)


async def _assert_not_called(db: AsyncEngine, row: dict[str, Any], reason: str | None) -> None:
    assert row["status"] == ExecutionStatus.FAILED
    assert row["output"]["kind"] == "CALL_FAILED"
    assert row["output"].get("reason") == reason
    assert row["output"]["gas_used"] == "0"
    assert await transfers_of(db, row["execution_id"]) == []
    await assert_clean(db)


async def test_missing_contract_is_a_recorded_failure_without_gas(db: AsyncEngine) -> None:
    await setup_gas(db)
    error, row = await _failure(db, "ping", [1], attach=Attachment(Currency(RAPID), 5))
    assert isinstance(error, ContractNotFound)
    await _assert_not_called(db, row, "NO_CONTRACT")
    async with db.connect() as conn:
        assert (await conn.execute(select(account.c.account_id).where(account.c.account_id == CONTRACT))).first() is None
        assert (await conn.execute(select(balance.c.account_id).where(balance.c.account_id == CONTRACT))).first() is None


async def test_replaying_a_failed_call_reraises_without_collecting_again(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 10_000})
    await deploy(db, CONTRACT, SHOP)
    with pytest.raises(ContractReverted) as first:
        await _call(db, "buy", [300], idempotency_key="k")
    with pytest.raises(ContractReverted) as again:
        await _call(db, "buy", [300], idempotency_key="k")
    assert again.value.execution_id == first.value.execution_id is not None
    assert again.value.message == first.value.message
    assert len(await executions(db)) == 1
    assert len(await transfers_of(db, first.value.execution_id)) == 1
    with pytest.raises(IdempotencyConflict):
        await _call(db, "buy", [301], idempotency_key="k")
    assert len(await executions(db)) == 1


async def test_paused_contract_is_a_recorded_failure(db: AsyncEngine) -> None:
    await setup_gas(db)
    await deploy(db, CONTRACT, COUNTER)
    await contracts.pause_contract(db, account_id=CONTRACT, now=NOW)
    error, row = await _failure(db, "ping", [1])
    assert isinstance(error, ContractPaused)
    await _assert_not_called(db, row, "INACTIVE")


async def test_deleted_contract_is_a_recorded_failure(db: AsyncEngine) -> None:
    await setup_gas(db)
    await deploy(db, CONTRACT, COUNTER)
    await contracts.delete_contract(db, account_id=CONTRACT, now=NOW)
    error, row = await _failure(db, "ping", [1])
    assert isinstance(error, ContractNotFound)
    await _assert_not_called(db, row, "NO_CONTRACT")


@pytest.mark.parametrize("function", ["missing", "helper", "not-an-identifier"])
async def test_unknown_or_private_function_is_a_recorded_failure(db: AsyncEngine, function: str) -> None:
    await setup_gas(db)
    await deploy(db, CONTRACT, COUNTER)
    error, row = await _failure(db, function, [])
    assert isinstance(error, ContractCallFailed)
    assert row["contract_function"] == function
    await _assert_not_called(db, row, "NO_FUNCTION")


@pytest.mark.parametrize("args", [[], [1, 2], ["1"], [True], [Account(1)]])
async def test_bad_arguments_are_a_recorded_failure(db: AsyncEngine, args: list[Any]) -> None:
    await setup_gas(db)
    await deploy(db, CONTRACT, COUNTER)
    error, row = await _failure(db, "ping", args)
    assert isinstance(error, ContractCallFailed)
    await _assert_not_called(db, row, "BAD_ARGUMENTS")


async def test_attachment_in_a_missing_currency_or_unaffordable_is_a_recorded_failure(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 1_000})
    await create_currency(db, currency_id=900_002, symbol="GEM", now=NOW)
    await deploy(db, CONTRACT, SHOP)
    error, row = await _failure(db, "buy", [1], attach=Attachment(Currency(123_456), 5))
    assert isinstance(error, CurrencyNotFound)
    await _assert_not_called(db, row, "NO_CURRENCY")
    error, row = await _failure(db, "buy", [1], attach=Attachment(Currency(900_002), 5))
    assert isinstance(error, InsufficientBalance)
    await _assert_not_called(db, row, "INSUFFICIENT_BALANCE")
    await mint(db, 900_002, CALLER, 5, now=NOW)
    result = await _call(db, "buy", [5], attach=Attachment(Currency(900_002), 5))
    assert await balance_of(db, CONTRACT, 900_002) == 5
    assert await supply_of(db, RAPID) == 1_000 - result.gas_used


@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        ({"function": ""}, InvalidArgument),
        ({"function": "f" * 65}, InvalidArgument),
        ({"args": [1.5]}, InvalidArgument),
        ({"args": [None]}, InvalidArgument),
        ({"args": [2**300]}, InvalidArgument),
        ({"args": list(range(17))}, InvalidArgument),
        ({"attach": Attachment(Currency(RAPID), 0)}, InvalidAmount),
        ({"attach": Attachment(Currency(RAPID), 10**38)}, AmountOverflow),
        ({"attach": Attachment(Currency(0), 5)}, InvalidArgument),
        ({"gas_limit": 0}, InvalidArgument),
        ({"gas_limit": 100_001}, InvalidArgument),
        ({"contract_id": -5}, InvalidArgument),
        ({"caller_id": 0}, InvalidArgument),
    ],
)
async def test_malformed_requests_record_nothing(db: AsyncEngine, kwargs: dict[str, Any], expected: type[DomainError]) -> None:
    await setup_gas(db)
    await deploy(db, CONTRACT, COUNTER)
    request: dict[str, Any] = {"caller_id": CALLER, "contract_id": CONTRACT, "function": "ping", "args": [1]}
    request.update(kwargs)
    with pytest.raises(expected):
        await call_contract(db, None, config=config(), now=NOW, **request)
    assert await executions(db) == []


async def test_attaching_to_your_own_contract_is_rejected(db: AsyncEngine) -> None:
    await setup_gas(db, {CONTRACT: 1_000})
    await deploy(db, CONTRACT, SHOP)
    with pytest.raises(InvalidArgument):
        await call_contract(db, None, caller_id=CONTRACT, contract_id=CONTRACT, function="buy", args=[1], attach=Attachment(Currency(RAPID), 1), config=config(), now=NOW)
    result = await call_contract(db, None, caller_id=CONTRACT, contract_id=CONTRACT, function="buy", args=[0], config=config(), now=NOW)
    assert result.value == "OK"
