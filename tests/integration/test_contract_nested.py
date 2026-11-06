"""Nested contract calls: call() and try_call() as child executions in savepoints, every catchable error kind, every CALL_FAILED reason, what a failed callee leaves behind, and failures no try_call can catch."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.limits import Limits
from rapidwire.contract.runtime import contract_error, vm_failure
from rapidwire.contract.values import Account
from rapidwire.core import contracts, transfers
from rapidwire.core.contract_calls import ContractCallResult, call_contract
from rapidwire.core.errors import (
    ContractCallFailed,
    ContractLimitExceeded,
    ContractOutOfGas,
    ContractReverted,
    ContractRuntimeFailure,
    DomainError,
    RevertError,
)
from rapidwire.core.ledger import CurrencyLockEscalation, RerunTransaction
from rapidwire.core.types import ExecutionStatus, TransferKind
from tests.integration.contract_runtime_helpers import CALLER, NOW, RAPID, assert_clean, config, deploy, executions, setup_gas, stored, transfers_of
from tests.integration.factories import balance_of, mint

A = 8001
B = 8002
PAUSED = 8003
DELETED = 8004
NOBODY = 8005
SINK = 7100

CALLER_SOURCE = """
@public
def relay(target: Account, fn: str, budget: int) -> str:
    storage["before"] = 1
    transfer(Account(7100), currency("RAPID"), 1)
    r = try_call(target, fn, [], gas=budget)
    if r.ok:
        return "ok:" + str(r.value)
    return r.error + ":" + r.message

@public
def relay_all(target: Account, fn: str) -> str:
    storage["before"] = 1
    r = try_call(target, fn, [])
    if r.ok:
        return "ok:" + str(r.value)
    return r.error + ":" + r.message

@public
def relay_pay(target: Account, fn: str, amount: int) -> str:
    transfer(Account(7100), currency("RAPID"), 1)
    r = try_call(target, fn, [], attach=(currency("RAPID"), amount))
    if r.ok:
        return "ok:" + str(r.value)
    return r.error + ":" + r.message

@public
def strict(target: Account) -> int:
    storage["before"] = 1
    v: int = call(target, "ok", [21])
    return v

@public
def strict_revert(target: Account) -> int:
    storage["before"] = 1
    v: int = call(target, "fail_revert", [])
    return v

@public
def noop() -> int:
    return 0

@public
def relay_many(target: Account, count: int) -> str:
    items: list[int] = []
    while len(items) < count:
        items.append(1)
    r = try_call(target, "ok", items, gas=300)
    return r.error + ":" + r.message

@public
def relay_value(target: Account) -> str:
    v: Value = [1]
    r = try_call(target, "ok", [v], gas=300)
    return r.error + ":" + r.message

@public
def strict_fn(target: Account, fn: str) -> int:
    storage["before"] = 1
    transfer(Account(7100), currency("RAPID"), 1)
    v: int = call(target, fn, [21])
    return v

@public
def strict_many(target: Account) -> int:
    storage["before"] = 1
    transfer(Account(7100), currency("RAPID"), 1)
    items: list[int] = []
    while len(items) < 17:
        items.append(1)
    v: int = call(target, "ok", items)
    return v

@public
def strict_value(target: Account) -> int:
    storage["before"] = 1
    transfer(Account(7100), currency("RAPID"), 1)
    w: Value = [1]
    v: int = call(target, "ok", [w])
    return v
"""

CALLEE_SOURCE = """
@public
def ok(x: int) -> int:
    storage["seen"] = x
    return x * 2

@public
def fail_revert() -> int:
    storage["x"] = 1
    revert("callee says no")

@public
def fail_runtime() -> int:
    z: int = storage.get("zero", 0)
    return 1 // z

@public
def fail_builtin() -> int:
    transfer(sender, currency("RAPID"), 1000000000)
    return 0

@public
def spin() -> int:
    i = 0
    while i >= 0:
        i = i + 1
    return i

@public
def take() -> int:
    r = random(1, 1000000)
    storage["paid"] = paid.amount
    return r

@public
def take_then_fail() -> int:
    r = random(1, 1000000)
    storage["paid"] = paid.amount
    transfer(Account(7100), currency("RAPID"), 1)
    revert("after " + str(r))

@public
def back() -> str:
    r = try_call(sender, "noop", [])
    return r.error + ":" + r.message

@public
def hog() -> int:
    items: list[int] = []
    i = 0
    while i < 3000:
        items.append(i)
        i = i + 1
    return len(items)
"""


async def _setup(engine: AsyncEngine) -> None:
    await setup_gas(engine, {CALLER: 1_000_000, A: 1_000})
    await deploy(engine, A, CALLER_SOURCE)
    await deploy(engine, B, CALLEE_SOURCE)
    await deploy(engine, PAUSED, CALLEE_SOURCE)
    await contracts.pause_contract(engine, account_id=PAUSED, now=NOW)
    await deploy(engine, DELETED, CALLEE_SOURCE)
    await contracts.delete_contract(engine, account_id=DELETED, now=NOW)


async def _call(engine: AsyncEngine, function: str, args: list[Any], *, gas_limit: int = 20_000, **kwargs: Any) -> ContractCallResult:
    return await call_contract(engine, None, caller_id=CALLER, contract_id=A, function=function, args=args, gas_limit=gas_limit, config=config(**kwargs), now=NOW)


async def _children(engine: AsyncEngine, parent_id: int) -> list[dict[str, Any]]:
    return [row for row in await executions(engine) if row["parent_execution_id"] == parent_id]


async def test_call_success_records_a_child_execution(db: AsyncEngine) -> None:
    await _setup(db)
    result = await _call(db, "strict", [Account(B)])
    assert result.value == 42
    [child] = await _children(db, result.execution_id)
    assert (child["status"], child["initiator_id"], child["contract_function"]) == (ExecutionStatus.SUCCESS, A, "ok")
    assert child["input"] == {"contract_id": str(B), "function": "ok", "args": [{"type": "int", "value": "21"}], "attach": None, "gas_limit": None}
    assert child["output"]["value"] == "42"
    assert 0 < int(child["output"]["gas_used"]) < result.gas_used
    assert await stored(db, B, "seen") == 21
    assert await stored(db, A, "before") == 1
    [gas] = await transfers_of(db, result.execution_id)
    assert (gas["kind"], gas["amount"]) == (TransferKind.GAS, result.gas_used)
    assert await transfers_of(db, child["execution_id"]) == []
    await assert_clean(db)


async def test_call_failure_fails_the_whole_call_and_keeps_no_child(db: AsyncEngine) -> None:
    await _setup(db)
    with pytest.raises(ContractReverted) as info:
        await _call(db, "strict_revert", [Account(B)])
    [row] = await executions(db)
    assert row["execution_id"] == info.value.execution_id
    assert row["status"] == ExecutionStatus.REVERTED
    assert row["output"]["error"]["message"] == "callee says no"
    assert row["output"]["error"]["child"] == {"type": "contract_call", "contract_function": "fail_revert", "code": "contract_reverted", "message": "callee says no"}
    assert row["output"]["location"]["function"] == "fail_revert"
    assert await stored(db, A, "before") is None
    assert await stored(db, B, "x") is None
    [gas] = await transfers_of(db, row["execution_id"])
    assert gas["kind"] == TransferKind.GAS
    await assert_clean(db)


@pytest.mark.parametrize(
    ("fn", "expected", "status"),
    [
        ("fail_revert", "REVERTED:callee says no", ExecutionStatus.REVERTED),
        ("fail_runtime", "RUNTIME:division by zero", ExecutionStatus.FAILED),
        ("fail_builtin", "RUNTIME:transfer failed: insufficient_balance: The balance is insufficient.", ExecutionStatus.FAILED),
        ("spin", "OUT_OF_GAS:out of gas: the gas limit given to this call is used up", ExecutionStatus.FAILED),
    ],
)
async def test_try_call_catches_callee_failures(db: AsyncEngine, fn: str, expected: str, status: ExecutionStatus) -> None:
    await _setup(db)
    result = await _call(db, "relay", [Account(B), fn, 300])
    assert result.value == expected
    [child] = await _children(db, result.execution_id)
    assert child["status"] == status
    assert child["output"]["kind"] == expected.split(":", 1)[0]
    used = int(child["output"]["gas_used"])
    assert 0 < used <= 300
    if fn == "spin":
        assert used == 300
    assert result.gas_used > used
    assert await transfers_of(db, child["execution_id"]) == []
    assert await stored(db, B, "x") is None
    assert await stored(db, A, "before") == 1
    kinds = [(row["kind"], row["from_account"], row["to_account"], row["amount"]) for row in await transfers_of(db, result.execution_id)]
    assert kinds[0] == (TransferKind.PRINCIPAL, A, SINK, 1)
    assert kinds[1][0] == TransferKind.GAS
    assert kinds[1][3] == result.gas_used
    await assert_clean(db)


def _assert_call_failed_child(child: dict[str, Any], *, initiator: int, target: int, function: str, args: list[Any], gas_limit: str | None, reason: str, message: str) -> None:
    assert (child["status"], child["initiator_id"], child["contract_function"]) == (ExecutionStatus.FAILED, initiator, function)
    assert child["input"] == {"contract_id": str(target), "function": function, "args": args, "attach": None, "gas_limit": gas_limit}
    output = child["output"]
    assert (output["kind"], output["reason"], output["gas_used"], output["gas_limit"], output["random"], output["random_tree"]) == ("CALL_FAILED", reason, "0", gas_limit, [], [])
    assert (output["error"]["code"], output["error"]["message"]) == ("contract_call_failed", message)


LONG_NAME = "x" * 70


@pytest.mark.parametrize(
    ("target", "fn", "message", "reason", "recorded_fn"),
    [
        (NOBODY, "ok", f"account {NOBODY} has no contract", "NO_CONTRACT", "ok"),
        (PAUSED, "ok", f"the contract of account {PAUSED} is paused", "INACTIVE", "ok"),
        (DELETED, "ok", f"account {DELETED} has no contract", "NO_CONTRACT", "ok"),
        (B, "missing", "no public function 'missing'", "NO_FUNCTION", "missing"),
        (B, "ok", "ok: expected 1 arguments, got 0", "BAD_ARGUMENTS", "ok"),
        (B, "not-a-name", "no public function 'not-a-name'", "NO_FUNCTION", "'not-a-name'"),
        (B, LONG_NAME, "no public function '" + "x" * 59 + "'...", "NO_FUNCTION", "'" + "x" * 59 + "'..."),
        (B, "naïve", "no public function 'na\\xefve'", "NO_FUNCTION", "'na\\xefve'"),
        (A, "noop", f"contract {A} is already executing", "REENTRANCY", "noop"),
    ],
    ids=["no-contract", "paused", "deleted", "no-function", "bad-arguments", "invalid-name", "long-name", "non-ascii-name", "reentrancy"],
)
async def test_try_call_records_every_call_failed(db: AsyncEngine, target: int, fn: str, message: str, reason: str, recorded_fn: str) -> None:
    await _setup(db)
    result = await _call(db, "relay", [Account(target), fn, 300])
    assert result.value == "CALL_FAILED:" + message
    [child] = await _children(db, result.execution_id)
    _assert_call_failed_child(child, initiator=A, target=target, function=recorded_fn, args=[], gas_limit="300", reason=reason, message=message)
    assert await transfers_of(db, child["execution_id"]) == []
    assert await stored(db, A, "before") == 1
    await assert_clean(db)


@pytest.mark.parametrize(
    ("function", "args", "message"),
    [
        ("relay_many", [Account(B), 17], "17 call arguments, more than 16"),
        ("relay_value", [Account(B)], "call argument 0 must be int, str, bool, Account or Currency, not list"),
    ],
    ids=["too-many", "not-scalar"],
)
async def test_try_call_records_a_refused_argument_list_without_its_arguments(db: AsyncEngine, function: str, args: list[Any], message: str) -> None:
    await _setup(db)
    result = await _call(db, function, args)
    assert result.value == "CALL_FAILED:" + message
    [child] = await _children(db, result.execution_id)
    _assert_call_failed_child(child, initiator=A, target=B, function="ok", args=[], gas_limit="300", reason="BAD_ARGUMENTS", message=message)
    await assert_clean(db)


async def test_sixteen_arguments_reach_the_callee_and_are_stored(db: AsyncEngine) -> None:
    await _setup(db)
    result = await _call(db, "relay_many", [Account(B), 16])
    assert result.value == "CALL_FAILED:ok: expected 1 arguments, got 16"
    [child] = await _children(db, result.execution_id)
    stored_args = [{"type": "int", "value": "1"}] * 16
    _assert_call_failed_child(child, initiator=A, target=B, function="ok", args=stored_args, gas_limit="300", reason="BAD_ARGUMENTS", message="ok: expected 1 arguments, got 16")


async def test_reentrancy_from_a_callee_is_call_failed(db: AsyncEngine) -> None:
    await _setup(db)
    result = await _call(db, "relay", [Account(B), "back", 300])
    message = f"contract {A} is already executing"
    assert result.value == f"ok:CALL_FAILED:{message}"
    [child] = await _children(db, result.execution_id)
    assert child["status"] == ExecutionStatus.SUCCESS
    [grandchild] = await _children(db, child["execution_id"])
    _assert_call_failed_child(grandchild, initiator=B, target=A, function="noop", args=[], gas_limit=None, reason="REENTRANCY", message=message)
    await assert_clean(db)


@pytest.mark.parametrize(
    ("function", "args", "recorded_fn", "reason", "message"),
    [
        ("strict_fn", [Account(A), "noop"], "noop", "REENTRANCY", f"contract {A} is already executing"),
        ("strict_fn", [Account(B), "not-a-name"], "'not-a-name'", "NO_FUNCTION", "no public function 'not-a-name'"),
        ("strict_many", [Account(B)], "ok", "BAD_ARGUMENTS", "17 call arguments, more than 16"),
        ("strict_value", [Account(B)], "ok", "BAD_ARGUMENTS", "call argument 0 must be int, str, bool, Account or Currency, not list"),
        ("strict_fn", [Account(NOBODY), "ok"], "ok", "NO_CONTRACT", f"account {NOBODY} has no contract"),
    ],
    ids=["reentrancy", "invalid-name", "too-many", "not-scalar", "no-contract"],
)
async def test_a_failed_call_under_call_fails_the_caller_and_keeps_no_child(db: AsyncEngine, function: str, args: list[Any], recorded_fn: str, reason: str, message: str) -> None:
    await _setup(db)
    with pytest.raises(ContractCallFailed) as info:
        await _call(db, function, args)
    [row] = await executions(db)
    assert (row["execution_id"], row["status"], row["parent_execution_id"]) == (info.value.execution_id, ExecutionStatus.FAILED, None)
    assert (row["output"]["kind"], row["output"]["reason"], row["output"]["error"]["message"]) == ("CALL_FAILED", reason, message)
    assert row["output"]["error"]["child"] == {"type": "contract_call", "contract_function": recorded_fn, "code": "contract_call_failed", "message": message}
    assert await stored(db, A, "before") is None
    assert await balance_of(db, SINK, RAPID) == 0
    [gas] = await transfers_of(db, row["execution_id"])
    assert (gas["kind"], gas["amount"]) == (TransferKind.GAS, int(row["output"]["gas_used"]))
    await assert_clean(db)


async def test_unpayable_attachment_is_call_failed_and_recorded(db: AsyncEngine) -> None:
    await _setup(db)
    result = await _call(db, "relay_pay", [Account(B), "take", 5_000])
    assert result.value.startswith("CALL_FAILED:the attachment cannot be paid")  # type: ignore[union-attr]
    [child] = await _children(db, result.execution_id)
    assert (child["status"], child["output"]["reason"]) == (ExecutionStatus.FAILED, "INSUFFICIENT_BALANCE")
    assert await balance_of(db, B, RAPID) == 0


async def test_attachment_and_random_values_belong_to_the_child(db: AsyncEngine) -> None:
    await _setup(db)
    result = await _call(db, "relay_pay", [Account(B), "take", 50])
    assert isinstance(result.value, str)
    drawn = int(result.value.removeprefix("ok:"))
    [child] = await _children(db, result.execution_id)
    assert child["output"]["random"] == [str(drawn)]
    assert child["input"]["attach"] == {"currency_id": str(RAPID), "amount": "50"}
    assert result.random_values == ()
    [moved] = await transfers_of(db, child["execution_id"])
    assert (moved["kind"], moved["from_account"], moved["to_account"], moved["amount"]) == (TransferKind.PRINCIPAL, A, B, 50)
    assert await stored(db, B, "paid") == 50
    assert await balance_of(db, B, RAPID) == 50
    assert await balance_of(db, A, RAPID) == 1_000 - 1 - 50
    await assert_clean(db)


async def test_failed_callee_rolls_back_its_attachment_and_keeps_the_callers_work(db: AsyncEngine) -> None:
    await _setup(db)
    result = await _call(db, "relay_pay", [Account(B), "take_then_fail", 50])
    assert isinstance(result.value, str)
    assert result.value.startswith("REVERTED:after ")
    drawn = result.value.removeprefix("REVERTED:after ")
    [child] = await _children(db, result.execution_id)
    assert child["status"] == ExecutionStatus.REVERTED
    assert child["output"]["random"] == [drawn]
    assert await transfers_of(db, child["execution_id"]) == []
    assert await stored(db, B, "paid") is None
    assert await balance_of(db, B, RAPID) == 0
    assert await balance_of(db, A, RAPID) == 1_000 - 1
    assert await balance_of(db, SINK, RAPID) == 1
    await assert_clean(db)


async def test_top_level_gas_running_out_in_a_callee_is_not_caught(db: AsyncEngine) -> None:
    await _setup(db)
    with pytest.raises(ContractOutOfGas) as info:
        await _call(db, "relay_all", [Account(B), "spin"], gas_limit=500)
    [row] = await executions(db)
    assert (row["execution_id"], row["output"]["gas_used"], row["output"]["kind"]) == (info.value.execution_id, "500", "OUT_OF_GAS")
    assert row["output"]["error"]["child"]["contract_function"] == "spin"
    assert await stored(db, A, "before") is None
    [gas] = await transfers_of(db, row["execution_id"])
    assert gas["amount"] == 500
    await assert_clean(db)


async def test_memory_limit_in_a_callee_is_not_caught(db: AsyncEngine) -> None:
    await _setup(db)
    with pytest.raises(ContractLimitExceeded) as info:
        await _call(db, "relay_all", [Account(B), "hog"], gas_limit=50_000, limits=Limits(memory_max_bytes=20_000))
    [row] = await executions(db)
    assert row["execution_id"] == info.value.execution_id
    assert row["output"]["kind"] == "MEMORY_LIMIT"
    assert row["output"]["error"]["code"] == "contract_limit_exceeded"
    assert int(row["output"]["gas_used"]) > 0
    await assert_clean(db)


def _chain_source(next_account: int | None) -> str:
    if next_account is None:
        return '@public\ndef down() -> str:\n    return "bottom"\n'
    return f"""
@public
def down() -> str:
    r = try_call(Account({next_account}), "down", [])
    if r.ok:
        return "ok>" + str(r.value)
    return r.error + ":" + r.message
"""


async def test_depth_limit_stops_the_seventh_contract(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 100_000})
    chain = list(range(8101, 8108))
    for index, account_id in enumerate(chain):
        await deploy(db, account_id, _chain_source(chain[index + 1] if index + 1 < len(chain) else None))
    result = await call_contract(db, None, caller_id=CALLER, contract_id=chain[0], function="down", args=[], gas_limit=5_000, config=config(), now=NOW)
    assert result.value == "ok>" * 5 + "CALL_FAILED:contract call depth limit of 6 reached"
    parent = result.execution_id
    for account_id in chain[1:6]:
        [child] = await _children(db, parent)
        assert (child["initiator_id"], child["input"]["contract_id"], child["status"]) == (account_id - 1, str(account_id), ExecutionStatus.SUCCESS)
        parent = child["execution_id"]
    [refused] = await _children(db, parent)
    _assert_call_failed_child(refused, initiator=chain[5], target=chain[6], function="down", args=[], gas_limit=None, reason="DEPTH", message="contract call depth limit of 6 reached")
    assert await _children(db, refused["execution_id"]) == []
    await assert_clean(db)


def _strict_chain_source(next_account: int | None) -> str:
    if next_account is None:
        return '@public\ndef down() -> str:\n    return "bottom"\n'
    return f'@public\ndef down() -> str:\n    storage["seen"] = 1\n    return "ok>" + str(call(Account({next_account}), "down", []))\n'


async def test_depth_limit_under_call_fails_the_whole_chain(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 100_000})
    chain = list(range(8201, 8208))
    for index, account_id in enumerate(chain):
        await deploy(db, account_id, _strict_chain_source(chain[index + 1] if index + 1 < len(chain) else None))
    message = "contract call depth limit of 6 reached"
    with pytest.raises(ContractCallFailed) as info:
        await call_contract(db, None, caller_id=CALLER, contract_id=chain[0], function="down", args=[], gas_limit=5_000, config=config(), now=NOW)
    assert info.value.message == message
    [row] = await executions(db)
    assert (row["output"]["reason"], row["output"]["error"]["child"]["contract_function"]) == ("DEPTH", "down")
    for account_id in chain:
        assert await stored(db, account_id, "seen") is None
    await assert_clean(db)


def test_error_mapping_round_trips_every_failure_kind() -> None:
    from rapidwire.contract.errors import CallFailed, CallFailure, ContractRevert, ContractRuntimeError, LimitExceeded, LimitResource, OutOfGas

    cases: list[tuple[Any, type[DomainError], ExecutionStatus]] = [
        (ContractRevert("r"), ContractReverted, ExecutionStatus.REVERTED),
        (ContractRuntimeError("x"), ContractRuntimeFailure, ExecutionStatus.FAILED),
        (OutOfGas("g", level=1), ContractOutOfGas, ExecutionStatus.FAILED),
        (CallFailed(CallFailure.DEPTH, "d"), ContractCallFailed, ExecutionStatus.FAILED),
        (LimitExceeded(LimitResource.MEMORY, "m"), ContractLimitExceeded, ExecutionStatus.FAILED),
    ]
    for failure, error_type, status in cases:
        error = contract_error(failure)
        assert type(error) is error_type
        assert error.status is status
        assert error.message == failure.message
        assert vm_failure(error) is failure
        assert isinstance(error, RevertError) == (status is ExecutionStatus.REVERTED)
    plain = vm_failure(DomainError("other"))
    assert isinstance(plain, ContractRuntimeError)
    assert plain.message == "other"


LOTTERY = 8010
PEEKER = 8011
OUTER = 8012

LOTTERY_SOURCE = """
@public
def draw() -> int:
    return random(0, 1000000)
"""

PEEKER_SOURCE = """
@public
def play() -> int:
    mine: int = random(5, 5)
    v: int = call(Account(8010), "draw", [])
    revert("did not like " + str(v))
"""

OUTER_SOURCE = """
@public
def shield() -> str:
    r = try_call(Account(8011), "play", [])
    return r.error + ":" + r.message
"""


def _draws(rows: list[dict[str, Any]]) -> list[tuple[str, str, str, list[str]]]:
    return [(row["order"], row["depth"], row["contract_id"], row["values"]) for row in rows]


async def test_draws_of_a_rolled_back_callee_stay_on_the_failed_top_level_row(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 1_000_000})
    await deploy(db, LOTTERY, LOTTERY_SOURCE)
    await deploy(db, PEEKER, PEEKER_SOURCE)
    with pytest.raises(ContractReverted) as info:
        await call_contract(db, None, caller_id=CALLER, contract_id=PEEKER, function="play", args=[], gas_limit=2_000, config=config(), now=NOW)
    drawn = info.value.message.removeprefix("did not like ")
    [row] = await executions(db)
    assert row["output"]["random"] == ["5"]
    assert _draws(row["output"]["random_tree"]) == [("0", "1", str(PEEKER), ["5"]), ("1", "2", str(LOTTERY), [drawn])]
    assert row["output"]["random_tree"][1]["function"] == "draw"


async def test_draws_of_a_caught_subtree_stay_on_the_failed_child_row(db: AsyncEngine) -> None:
    await setup_gas(db, {CALLER: 1_000_000})
    await deploy(db, LOTTERY, LOTTERY_SOURCE)
    await deploy(db, PEEKER, PEEKER_SOURCE)
    await deploy(db, OUTER, OUTER_SOURCE)
    result = await call_contract(db, None, caller_id=CALLER, contract_id=OUTER, function="shield", args=[], gas_limit=2_000, config=config(), now=NOW)
    assert isinstance(result.value, str)
    drawn = result.value.removeprefix("REVERTED:did not like ")
    rows = await executions(db)
    assert [row["parent_execution_id"] for row in rows] == [None, result.execution_id]
    child = rows[1]
    assert (child["status"], child["input"]["contract_id"]) == (ExecutionStatus.REVERTED, str(PEEKER))
    assert _draws(child["output"]["random_tree"]) == [("1", "2", str(PEEKER), ["5"]), ("2", "3", str(LOTTERY), [drawn])]
    assert rows[0]["output"]["random"] == []


async def test_child_input_records_the_gas_sub_limit(db: AsyncEngine) -> None:
    await _setup(db)
    limited = await _call(db, "relay", [Account(B), "fail_revert", 300])
    [child] = await _children(db, limited.execution_id)
    assert (child["input"]["gas_limit"], child["output"]["gas_limit"]) == ("300", "300")
    unlimited = await _call(db, "strict", [Account(B)])
    [child] = await _children(db, unlimited.execution_id)
    assert child["input"]["gas_limit"] is None


async def test_attachment_in_a_missing_currency_is_no_currency(db: AsyncEngine) -> None:
    await _setup(db)
    await deploy(db, 8020, MISSING_CURRENCY_SOURCE)
    result = await call_contract(db, None, caller_id=CALLER, contract_id=8020, function="pay_ghost", args=[], gas_limit=2_000, config=config(), now=NOW)
    assert isinstance(result.value, str)
    assert result.value.startswith("CALL_FAILED:the attachment's currency 424242 was not found")
    [child] = await _children(db, result.execution_id)
    assert child["output"]["reason"] == "NO_CURRENCY"


MISSING_CURRENCY_SOURCE = """
@public
def pay_ghost() -> str:
    r = try_call(Account(8002), "take", [], attach=(Currency(424242), 5))
    return r.error + ":" + r.message
"""


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("signal", ["rerun", "escalation"])
async def test_rerun_requests_pass_through_builtins_unchanged(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch, nested: bool, signal: str) -> None:
    await _setup(db)
    original = transfers.transfer_in_tx
    raised: list[bool] = []

    async def once(*args: Any, **kwargs: Any) -> Any:
        if not raised and kwargs["sender_id"] == (B if nested else A):
            raised.append(True)
            raise RerunTransaction("test rerun") if signal == "rerun" else CurrencyLockEscalation(RAPID)
        return await original(*args, **kwargs)

    monkeypatch.setattr(transfers, "transfer_in_tx", once)
    if nested:
        result = await _call(db, "relay_pay", [Account(B), "take_then_fail", 50])
        assert isinstance(result.value, str)
        assert result.value.startswith("REVERTED:")
    else:
        result = await _call(db, "relay", [Account(B), "ok", 300])
    assert raised == [True]
    assert len([row for row in await executions(db) if row["parent_execution_id"] is None]) == 1
    assert await balance_of(db, SINK, RAPID) == 1
    await assert_clean(db)


def test_the_host_exposes_no_minting_or_currency_management() -> None:
    from rapidwire.contract.runtime import LedgerHost
    from rapidwire.contract.vm import Host

    forbidden = ("mint", "burn", "renounce", "rate", "delegat", "delete_currency", "finalize", "permission", "token", "create_currency", "create_pool")
    for cls in (LedgerHost, Host):
        names = [name for name in dir(cls) if not name.startswith("__")]
        assert [name for name in names if any(word in name for word in forbidden)] == []


async def test_children_of_a_successful_call_survive_and_belong_to_it(db: AsyncEngine) -> None:
    await _setup(db)
    await mint(db, RAPID, A, 10, now=NOW)
    first = await _call(db, "relay", [Account(B), "fail_revert", 300])
    second = await _call(db, "strict", [Account(B)])
    rows = await executions(db)
    assert [row["parent_execution_id"] for row in rows] == [None, first.execution_id, None, second.execution_id]
