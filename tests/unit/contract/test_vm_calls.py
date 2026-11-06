from __future__ import annotations

import pytest

from rapidwire.contract.errors import CallFailed, CallFailure, ContractRevert, ContractRuntimeError, ErrorKind, HostProtocolError, LimitExceeded, OutOfGas, call_result_error, try_call_catches
from rapidwire.contract.limits import Limits
from rapidwire.contract.types import ACCOUNT, INT
from rapidwire.contract.values import Account, Attachment
from rapidwire.contract.vm import check_args, function_label, is_function_name
from tests.unit.contract.helpers import RAPID, SELF, SENDER, FakeHost, context, program, run

OTHER = Account(3003)

CALLEE = """
.function echo public params=int returns=int
    LOAD 0
    PUSH_INT 1
    ADD
    RET
.end
.function who public returns=int
    CALL_BUILTIN sender 0
    TO_INT
    CALL_BUILTIN paid 0
    GET_FIELD Paid amount
    ADD
    RET
.end
.function fail public returns=int
    PUSH_CONST "callee says no"
    REVERT
.end
.function crash public returns=int
    PUSH_INT 1
    PUSH_INT 0
    FLOORDIV
    RET
.end
.function spin public returns=int
spin:
    PUSH_TRUE
    JUMP_IF_TRUE spin
    PUSH_INT 0
    RET
.end
.function roll public returns=int
    PUSH_INT 1
    PUSH_INT 100
    CALL_BUILTIN random 2
    RET
.end
.function back public returns=int
    PUSH_INT 1001
    CALL_BUILTIN Account 1
    PUSH_CONST "echo"
    PUSH_INT 1
    CALL_CONTRACT 0 1
    CHECK_TYPE int
    RET
.end
.function save public returns=int
    PUSH_CONST "k"
    PUSH_INT 5
    CALL_BUILTIN storage.set 2
    PUSH_CONST "x"
    REVERT
.end
"""


def caller(body: str, returns: str = "int") -> str:
    return f".function main public returns={returns}\n    PUSH_INT {OTHER.id}\n    CALL_BUILTIN Account 1\n{body}\n.end\n"


def host_with_callee() -> FakeHost:
    return FakeHost(contracts={OTHER: program(CALLEE)}, balances={(SELF, RAPID): 1000})


async def test_call_returns_the_callee_value() -> None:
    host = host_with_callee()
    outcome = await run(caller('PUSH_CONST "echo"\nPUSH_INT 41\nCALL_CONTRACT 0 1\nCHECK_TYPE int\nRET'), host=host)
    assert outcome.value == 42
    assert outcome.gas_used == 1 + 15 + 1 + 1


async def test_args_as_list_attach_and_sender() -> None:
    host = host_with_callee()
    body = f'PUSH_CONST "who"\nBUILD_LIST 0\nPUSH_INT {RAPID.id}\nCALL_BUILTIN Currency 1\nPUSH_INT 250\nCALL_CONTRACT 5 0\nCHECK_TYPE int\nRET'
    assert (await run(caller(body), host=host)).value == SELF.id + 250
    assert host.calls[0] == ("call_contract", (OTHER, "who", (), Attachment(RAPID, 250)))


async def test_call_failure_propagates() -> None:
    host = host_with_callee()
    with pytest.raises(ContractRevert) as info:
        await run(caller('PUSH_CONST "fail"\nCALL_CONTRACT 0 0\nCHECK_TYPE int\nRET'), host=host)
    assert info.value.message == "callee says no"
    assert info.value.function == "fail"


def try_body(function: str, flags: int = 0, extra: str = "") -> str:
    return f'PUSH_CONST "{function}"\n{extra}TRY_CALL {flags} 0\nSTORE 0\nLOAD 0\nGET_FIELD CallResult ok\nTO_STR\nLOAD 0\nGET_FIELD CallResult error\nCONCAT\nPUSH_CONST ":"\nCONCAT\nLOAD 0\nGET_FIELD CallResult message\nCONCAT\nRET'


async def test_try_call_success_and_failure_kinds() -> None:
    host = host_with_callee()
    text = ".function main public returns=str locals=1\n    PUSH_INT 3003\n    CALL_BUILTIN Account 1\n{body}\n.end\n"
    assert (await run(text.format(body=try_body("who")), host=host)).value == "True:"
    assert (await run(text.format(body=try_body("fail")), host=host)).value == "FalseREVERTED:callee says no"
    assert (await run(text.format(body=try_body("crash")), host=host)).value == "FalseRUNTIME:division by zero"
    assert str((await run(text.format(body=try_body("missing")), host=host)).value).startswith("FalseCALL_FAILED:")
    assert str((await run(text.format(body=try_body("spin", 2, "PUSH_INT 50\n")), host=host)).value).startswith("FalseOUT_OF_GAS:")


async def test_try_call_restores_state_and_the_caller_continues() -> None:
    host = host_with_callee()
    text = (
        f".function main public returns=int locals=1\n    PUSH_INT {OTHER.id}\n    CALL_BUILTIN Account 1\n"
        + 'PUSH_CONST "save"\nTRY_CALL 0 0\nGET_FIELD CallResult ok\nJUMP_IF_TRUE bad\nPUSH_INT 7\nRET\nbad:\nPUSH_INT 0\nRET\n.end\n'
    )
    assert (await run(text, host=host)).value == 7
    assert OTHER not in host.storage or "k" not in host.storage[OTHER]


async def test_reading_the_value_of_a_failed_call_is_a_runtime_error() -> None:
    host = host_with_callee()
    with pytest.raises(ContractRuntimeError, match="not available"):
        await run(caller('PUSH_CONST "fail"\nTRY_CALL 0 0\nGET_FIELD CallResult value\nCHECK_TYPE int\nRET'), host=host)


async def test_sub_limit_exhaustion_is_catchable_and_consumes_the_sub_limit() -> None:
    host = host_with_callee()
    ctx = context(gas=1000)
    text = caller('PUSH_CONST "spin"\nPUSH_INT 100\nTRY_CALL 2 0\nGET_FIELD CallResult error\nPUSH_CONST "OUT_OF_GAS"\nEQ\nJUMP_IF_FALSE no\nPUSH_INT 1\nRET\nno:\nPUSH_INT 0\nRET')
    outcome = await run(text, host=host, ctx=ctx)
    assert outcome.value == 1
    assert outcome.gas_used == 1 + 15 + 100 + 1 + 1
    assert ctx.gas.level_count == 1


async def test_exhausting_the_top_level_limit_is_not_catchable() -> None:
    host = host_with_callee()
    without_gas = caller('PUSH_CONST "spin"\nTRY_CALL 0 0\nPOP\nPUSH_INT 1\nRET')
    with pytest.raises(OutOfGas) as info:
        await run(without_gas, host=host, ctx=context(gas=500))
    assert info.value.level == 0
    larger_than_remaining = caller('PUSH_CONST "spin"\nPUSH_INT 100000\nTRY_CALL 2 0\nPOP\nPUSH_INT 1\nRET')
    with pytest.raises(OutOfGas) as info:
        await run(larger_than_remaining, host=host, ctx=context(gas=500))
    assert not info.value.sublimit


async def test_an_enclosing_sub_limit_is_caught_only_by_its_own_try_call() -> None:
    middle = Account(4004)
    middle_source = f'.function relay public returns=int\nPUSH_INT {OTHER.id}\nCALL_BUILTIN Account 1\nPUSH_CONST "spin"\nPUSH_INT 100000\nTRY_CALL 2 0\nPOP\nPUSH_INT 5\nRET\n.end\n'
    host = FakeHost(contracts={OTHER: program(CALLEE), middle: program(middle_source)})
    text = f'.function main public returns=str\nPUSH_INT {middle.id}\nCALL_BUILTIN Account 1\nPUSH_CONST "relay"\nPUSH_INT 200\nTRY_CALL 2 0\nGET_FIELD CallResult error\nRET\n.end\n'
    assert (await run(text, host=host, ctx=context(gas=10_000))).value == "OUT_OF_GAS"


async def test_reentrancy_is_rejected() -> None:
    host = FakeHost(contracts={OTHER: program(CALLEE), SELF: program(CALLEE)})
    with pytest.raises(CallFailed) as info:
        await run(caller('PUSH_CONST "back"\nCALL_CONTRACT 0 0\nCHECK_TYPE int\nRET'), host=host)
    assert info.value.reason is CallFailure.REENTRANCY
    self_call = '.function main public returns=str\nCALL_BUILTIN self 0\nPUSH_CONST "echo"\nPUSH_INT 1\nTRY_CALL 0 1\nGET_FIELD CallResult error\nRET\n.end\n'
    assert (await run(self_call, host=host)).value == "CALL_FAILED"
    assert host.calls == [
        ("call_contract", (OTHER, "back", (), None)),
        ("reject_call", (SELF, "echo", 1, None, CallFailure.REENTRANCY)),
        ("reject_call", (SELF, "echo", 1, None, CallFailure.REENTRANCY)),
    ]


async def test_contract_call_depth_limit() -> None:
    accounts = [Account(5000 + i) for i in range(8)]
    contracts = {}
    for index, account in enumerate(accounts):
        if index + 1 < len(accounts):
            body = f'PUSH_INT {accounts[index + 1].id}\nCALL_BUILTIN Account 1\nPUSH_CONST "go"\nCALL_CONTRACT 0 0\nCHECK_TYPE int\nPUSH_INT 1\nADD\nRET'
        else:
            body = "PUSH_INT 0\nRET"
        contracts[account] = program(f".function go public returns=int\n{body}\n.end\n")
    host = FakeHost(contracts=contracts)
    ctx = context()
    with pytest.raises(CallFailed) as info:
        await run(contracts[accounts[0]], "go", host=host, ctx=ctx)
    assert info.value.reason is CallFailure.DEPTH
    assert sum(1 for name, _ in host.calls if name == "call_contract") == 5
    assert host.calls[-1] == ("reject_call", (accounts[6], "go", 0, None, CallFailure.DEPTH))
    shallow = FakeHost(contracts=contracts)
    assert (await run(contracts[accounts[0]], "go", host=shallow, ctx=context(limits=Limits(max_contract_depth=8)))).value == 7


async def test_random_values_are_logged_per_execution() -> None:
    host = host_with_callee()
    seen: list[list[int]] = []
    original = host.call_contract

    async def recording(ctx, target, function, args, attach):  # type: ignore[no-untyped-def]
        try:
            return await original(ctx, target, function, args, attach)
        finally:
            seen.append(list(ctx.random_log))

    host.call_contract = recording  # type: ignore[method-assign]
    text = caller('PUSH_CONST "roll"\nCALL_CONTRACT 0 0\nCHECK_TYPE int\nPUSH_INT 5\nPUSH_INT 5\nCALL_BUILTIN random 2\nADD\nRET')
    outcome = await run(text, host=host)
    assert outcome.random_values == (5,)
    assert len(seen) == 1 and len(seen[0]) == 1
    assert outcome.value == seen[0][0] + 5


async def test_child_context_fields() -> None:
    captured = []

    class Capture(FakeHost):
        async def call_contract(self, ctx, target, function, args, attach):  # type: ignore[no-untyped-def]
            captured.append(ctx)
            return 1

    ctx = context()
    await run(caller('PUSH_CONST "x"\nCALL_CONTRACT 0 0\nCHECK_TYPE int\nRET'), host=Capture(), ctx=ctx)
    child = captured[0]
    assert child.self_account == OTHER
    assert child.sender == SELF
    assert child.depth == 2
    assert child.call_stack == (SELF, OTHER)
    assert child.parent is ctx
    assert child.gas is ctx.gas and child.memory is ctx.memory and child.deadline is ctx.deadline
    assert child.now == ctx.now
    assert child.random_log is not ctx.random_log
    assert ctx.sender == SENDER


async def test_bad_call_operands_fail_in_the_caller() -> None:
    host = host_with_callee()
    with pytest.raises(ContractRuntimeError, match="call target must be an Account"):
        await run('.function main public returns=int\nPUSH_INT 1\nPUSH_CONST "echo"\nTRY_CALL 0 0\nPOP\nPUSH_INT 0\nRET\n.end\n', host=host)
    with pytest.raises(ContractRuntimeError, match="call arguments must be a list"):
        await run(caller('PUSH_CONST "echo"\nPUSH_INT 1\nTRY_CALL 4 0\nPOP\nPUSH_INT 0\nRET'), host=host)
    with pytest.raises(ContractRuntimeError, match="gas must be a positive int"):
        await run(caller('PUSH_CONST "echo"\nPUSH_INT 0\nTRY_CALL 2 0\nPOP\nPUSH_INT 0\nRET'), host=host)
    with pytest.raises(ContractRuntimeError, match="attached amount must be positive"):
        await run(caller(f'PUSH_CONST "echo"\nPUSH_INT {RAPID.id}\nCALL_BUILTIN Currency 1\nPUSH_INT 0\nTRY_CALL 1 0\nPOP\nPUSH_INT 0\nRET'), host=host)
    assert host.calls == []


@pytest.mark.parametrize(
    "arguments",
    [
        "BUILD_LIST 0\nBUILD_LIST 1",
        "BUILD_LIST 0\nBUILD_LIST 0\nBUILD_LIST 2",
        "\n".join(["PUSH_INT 1"] * 17) + "\nBUILD_LIST 17",
    ],
    ids=["nested-list", "two-lists", "too-many"],
)
async def test_bad_argument_contents_are_a_catchable_call_failure(arguments: str) -> None:
    host = host_with_callee()
    text = caller(f'PUSH_CONST "echo"\n{arguments}\nTRY_CALL 4 0\nGET_FIELD CallResult error\nRET', returns="str")
    assert (await run(text, host=host)).value == "CALL_FAILED"
    with pytest.raises(CallFailed) as info:
        await run(caller(f'PUSH_CONST "echo"\n{arguments}\nCALL_CONTRACT 4 0\nCHECK_TYPE int\nRET'), host=host)
    assert info.value.reason is CallFailure.BAD_ARGUMENTS
    stack_form = caller('PUSH_CONST "echo"\nBUILD_LIST 0\nTRY_CALL 0 1\nGET_FIELD CallResult error\nRET', returns="str")
    assert (await run(stack_form, host=host)).value == "CALL_FAILED"
    assert [name for name, _ in host.calls] == ["reject_call"] * 3
    assert {entry[4] for _, entry in host.calls} == {CallFailure.BAD_ARGUMENTS}


@pytest.mark.parametrize("name", ["not a name", "x" * 65, "naïve", "", "9lives"])
async def test_unusable_function_names_are_refused_by_the_vm(name: str) -> None:
    host = host_with_callee()
    text = caller(f'PUSH_CONST "{name}"\nPUSH_INT 1\nTRY_CALL 0 1\nGET_FIELD CallResult message\nRET', returns="str")
    assert (await run(text, host=host)).value == f"no public function {function_label(name)}"
    with pytest.raises(CallFailed) as info:
        await run(caller(f'PUSH_CONST "{name}"\nPUSH_INT 1\nCALL_CONTRACT 0 1\nCHECK_TYPE int\nRET'), host=host)
    assert info.value.reason is CallFailure.NO_FUNCTION
    assert host.calls == [("reject_call", (OTHER, name, 1, None, CallFailure.NO_FUNCTION))] * 2


def test_function_label_is_bounded_and_never_an_identifier() -> None:
    assert function_label("buy") == "buy"
    assert function_label("x" * 64) == "x" * 64
    assert function_label("not a name") == "'not a name'"
    assert function_label("it's") == '"it\'s"'
    assert function_label("naïve") == "'na\\xefve'"
    assert function_label("x" * 65) == "'" + "x" * 59 + "'..."
    assert function_label("é" * 40) == "'" + "\\xe9" * 14 + "'..."
    assert function_label("x" * 40, 16) == "'" + "x" * 11 + "'..."
    for name in ("x" * 65, "a b", "é" * 4096, "'" * 100, ""):
        label = function_label(name)
        assert len(label) <= 64 and label.isascii() and not is_function_name(label)


async def test_a_refused_call_costs_the_same_gas_as_a_call_the_host_fails() -> None:
    refused = caller('PUSH_CONST "not a name"\nPUSH_INT 300\nTRY_CALL 2 0\nPOP\nPUSH_INT 0\nRET')
    missing = caller('PUSH_CONST "not_a_name"\nPUSH_INT 300\nTRY_CALL 2 0\nPOP\nPUSH_INT 0\nRET')
    refused_ctx, missing_ctx = context(gas=5000), context(gas=5000)
    await run(refused, host=host_with_callee(), ctx=refused_ctx)
    await run(missing, host=FakeHost(), ctx=missing_ctx)
    assert refused_ctx.gas.used == missing_ctx.gas.used > 0
    assert refused_ctx.gas.level_count == 1 and refused_ctx.gas.ceiling == 5000


async def test_a_host_whose_reject_call_returns_is_a_protocol_error() -> None:
    class Returning(FakeHost):
        async def reject_call(self, ctx, target, function, args, attach, failure):  # type: ignore[no-untyped-def]
            return None

    with pytest.raises(HostProtocolError, match="reject_call"):
        await run(caller('PUSH_CONST "not a name"\nTRY_CALL 0 0\nPOP\nPUSH_INT 0\nRET'), host=Returning(contracts={OTHER: program(CALLEE)}))


async def test_limit_exceeded_in_a_callee_is_not_catchable() -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 0.01
        return now[0]

    host = host_with_callee()
    with pytest.raises(LimitExceeded):
        await run(caller('PUSH_CONST "spin"\nPUSH_INT 1000000000\nTRY_CALL 2 0\nPOP\nPUSH_INT 0\nRET'), host=host, ctx=context(gas=10**12, clock=clock))


def test_error_kind_mapping() -> None:
    assert call_result_error(ContractRevert("x")) is ErrorKind.REVERTED
    assert call_result_error(ContractRuntimeError("x")) is ErrorKind.RUNTIME
    assert call_result_error(OutOfGas("x", level=1)) is ErrorKind.OUT_OF_GAS
    assert call_result_error(CallFailed(CallFailure.NO_CONTRACT, "x")) is ErrorKind.CALL_FAILED
    assert try_call_catches(OutOfGas("x", level=2), 2)
    assert not try_call_catches(OutOfGas("x", level=1), 2)
    assert not try_call_catches(LimitExceeded.__new__(LimitExceeded), 1)


class RerunRequested(Exception):
    pass


async def test_host_exceptions_pass_through_try_call_unchanged() -> None:
    error = RerunRequested("lock escalation")
    host = host_with_callee()
    host.fail["call_contract"] = error
    with pytest.raises(RerunRequested) as info:
        await run(caller('PUSH_CONST "echo"\nPUSH_INT 1\nTRY_CALL 0 1\nPOP\nPUSH_INT 0\nRET'), host=host)
    assert info.value is error
    nested = host_with_callee()
    nested.fail["storage_set"] = error
    with pytest.raises(RerunRequested):
        await run(caller('PUSH_CONST "save"\nTRY_CALL 0 0\nPOP\nPUSH_INT 0\nRET'), host=nested)


async def test_gas_meter_is_balanced_after_a_failed_call() -> None:
    host = host_with_callee()
    ctx = context(gas=5000)
    text = caller('PUSH_CONST "crash"\nPUSH_INT 300\nTRY_CALL 2 0\nPOP\nPUSH_INT 0\nRET')
    await run(text, host=host, ctx=ctx)
    assert ctx.gas.level_count == 1
    assert ctx.gas.ceiling == 5000


def test_check_args() -> None:
    assert check_args((INT, ACCOUNT), [1, SELF]) is None
    assert check_args((INT,), [True]) == "argument 0 must be int"
    assert check_args((INT,), []) == "expected 1 arguments, got 0"
