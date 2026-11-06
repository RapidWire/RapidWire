from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from rapidwire.contract.costs import (
    BUILTIN_GAS,
    FREE_BUILTIN_WEIGHTS,
    FREE_INSTRUCTION_WEIGHTS,
    FREE_RUN_ALLOWANCE,
    OPCODE_GAS,
    ROUTE_GAS_PER_EXPANSION,
    STORAGE_READ_GAS_PER_VALUE,
    STORAGE_WRITE_GAS_PER_VALUE,
    elements_gas,
    free_instruction_weight,
    free_run_gas,
    memory_walk_gas,
    route_search_gas,
    storage_read_gas,
    storage_write_gas,
    str_gas,
)
from rapidwire.contract.errors import ContractRevert, ContractRuntimeError, LimitExceeded, LimitResource, OutOfGas, VerificationError
from rapidwire.contract.limits import DEFAULT_LIMITS, MESSAGE_MAX_LENGTH, Limits
from rapidwire.contract.opcodes import INSTRUCTION_SET_V1, Op
from rapidwire.contract.values import Account
from rapidwire.contract.vm import GasMeter, MemoryMeter
from tests.unit.contract.helpers import RAPID, SELF, FakeHost, checking_context, context, program, run, straight_line_gas, value


def main(body: str, returns: str = "int", locals_: int | None = None) -> str:
    extra = f" locals={locals_}" if locals_ is not None else ""
    return f".function main public returns={returns}{extra}\n{body}\n.end\n"


def test_cost_table_covers_every_opcode_and_builtin() -> None:
    assert set(OPCODE_GAS) == {op.name for op in Op}
    assert {info.mnemonic: info.gas for info in INSTRUCTION_SET_V1.ops.values()} == dict(OPCODE_GAS)
    assert set(BUILTIN_GAS) == {b.name for b in INSTRUCTION_SET_V1.builtins}
    assert all(b.gas == BUILTIN_GAS[b.name] for b in INSTRUCTION_SET_V1.builtins)


def test_auxiliary_opcodes_are_free_and_operations_cost_one() -> None:
    free = {"PUSH_INT", "PUSH_CONST", "PUSH_TRUE", "PUSH_FALSE", "POP", "DUP", "SWAP", "LOAD", "STORE", "JUMP", "RET", "REVERT", "GET_FIELD", "CALL_BUILTIN"}
    assert {name for name, gas in OPCODE_GAS.items() if gas == 0} == free
    assert OPCODE_GAS["CALL_CONTRACT"] == OPCODE_GAS["TRY_CALL"] == 15
    assert {gas for name, gas in OPCODE_GAS.items() if name not in free and name not in {"CALL_CONTRACT", "TRY_CALL"}} == {1}


def test_size_surcharges() -> None:
    assert str_gas(255) == 0
    assert str_gas(4096) == 16
    assert elements_gas(63) == 0
    assert elements_gas(1024) == 16
    assert storage_write_gas(0) == 3
    assert storage_write_gas(1) == 4
    assert storage_write_gas(32) == 4
    assert storage_write_gas(33) == 5
    assert storage_write_gas(32, 8) == 4 + 8 * STORAGE_WRITE_GAS_PER_VALUE
    assert storage_read_gas(127) == 1
    assert storage_read_gas(128) == 2
    assert storage_read_gas(128, 9) == 2 + 9 * STORAGE_READ_GAS_PER_VALUE
    assert route_search_gas(0) == 0
    assert route_search_gas(10, 3) == 10 * ROUTE_GAS_PER_EXPANSION + 5
    assert free_run_gas([1] * (FREE_RUN_ALLOWANCE - 1)) == 0
    assert free_run_gas([1] * FREE_RUN_ALLOWANCE) == 1
    assert free_run_gas([3, 3, 3, 3, 3, 3]) == 2
    assert all(0 < weight <= FREE_RUN_ALLOWANCE for weight in [*FREE_INSTRUCTION_WEIGHTS.values(), *FREE_BUILTIN_WEIGHTS.values()])
    assert set(FREE_BUILTIN_WEIGHTS) == {b.name for b in INSTRUCTION_SET_V1.builtins if b.gas == 0}
    assert memory_walk_gas(0) == 0
    assert memory_walk_gas(1) == 1


async def test_exact_gas_of_a_small_program() -> None:
    outcome = await run(main("PUSH_INT 2\nPUSH_INT 3\nMUL\nPUSH_INT 1\nLT\nJUMP_IF_FALSE a\na:\nPUSH_INT 7\nRET"))
    assert outcome.gas_used == 3


async def test_short_runs_of_free_instructions_cost_nothing() -> None:
    body = "PUSH_INT 1\nSTORE 0\nJUMP a\na:\nLOAD 0\nRET"
    verified = program(main(body, locals_=1))
    assert sum(-ins[1] for ins in verified.functions[0].code) < FREE_RUN_ALLOWANCE
    assert (await run(verified)).gas_used == 0


async def test_the_free_run_counter_charges_one_gas_per_allowance() -> None:
    weights = [1] + [1, 1] * FREE_RUN_ALLOWANCE + [free_instruction_weight("RET")]
    body = "PUSH_INT 0\n" + "DUP\nPOP\n" * FREE_RUN_ALLOWANCE + "RET"
    assert (await run(main(body))).gas_used == free_run_gas(weights) == 2
    # A charged instruction sets the counter back to 0, so two runs just below the allowance cost only the operation between them.
    split = "PUSH_INT 0\n" + "DUP\nPOP\n" * 3 + "NEG\n" + "DUP\nPOP\n" * 2 + "RET"
    assert (await run(main(split))).gas_used == 1
    weighted = 'PUSH_CONST "x"\nPOP\n' * 3 + "CALL_BUILTIN paid 0\nPOP\nPUSH_INT 0\nRET"
    verified = program(main(weighted))
    assert (await run(verified)).gas_used == straight_line_gas(verified) == free_run_gas([2, 1, 2, 1, 2, 1, 3, 1, 1, 3])


async def test_a_function_costs_the_same_in_every_context() -> None:
    body = (
        "PUSH_INT 1\nPUSH_INT 2\nADD\nPOP\nCALL_BUILTIN sender 0\nPOP\nCALL_BUILTIN paid 0\nGET_FIELD Paid amount\nPOP\nCALL_BUILTIN self 0\nPOP\n"
        + "PUSH_INT 3\nPOP\n" * 5
        + "PUSH_INT 3\nPUSH_INT 4\nADD\nRET"
    )
    source = f".function f public returns=int\n{body}\n.end\n.function viacall public returns=int\nCALL f\nRET\n.end\n"
    verified = program(source)
    alone = (await run(verified, "f")).gas_used
    assert alone == straight_line_gas(verified, "f")
    assert alone > 2
    viacall = (await run(verified, "viacall")).gas_used
    assert viacall == 1 + alone
    peer = Account(3003)
    caller = f'.function main public returns=int\nPUSH_INT {peer.id}\nCALL_BUILTIN Account 1\nPUSH_CONST "f"\nCALL_CONTRACT 0 0\nCHECK_TYPE int\nRET\n.end\n'
    nested = await run(caller, host=FakeHost(contracts={peer: verified}))
    assert nested.gas_used == 1 + 15 + 1 + alone


async def test_ret_resets_the_counter_so_the_caller_continues_from_zero() -> None:
    callee = ".function f returns=int\n" + "PUSH_INT 0\nPOP\n" * 3 + "PUSH_INT 1\nRET\n.end\n"
    after_call = "CALL f\nPOP\n" + "PUSH_INT 0\nPOP\n" * 3 + "PUSH_INT 0\nRET"
    # Callee: 7 + RET 3 = 10 units, so 1 gas; caller after RET: 8 units, so 1 gas; CALL itself 1.
    assert (await run(main(after_call) + callee)).gas_used == 3


def test_loops_of_free_instructions_are_rejected() -> None:
    with pytest.raises(VerificationError, match="a loop must contain an instruction with a gas cost"):
        program(main("a:\nJUMP a\nPUSH_INT 0\nRET"))
    with pytest.raises(VerificationError, match="a loop must contain"):
        program(main("PUSH_INT 0\nSTORE 0\na:\nLOAD 0\nPOP\nJUMP a", locals_=1))
    program(main("PUSH_INT 0\nRET\na:\nJUMP a"))


async def test_a_while_loop_costs_its_branch_per_iteration() -> None:
    # while flag: flag = False  -> the condition branch is the only charged instruction per iteration
    body = "PUSH_TRUE\nSTORE 0\nloop:\nLOAD 0\nJUMP_IF_FALSE end\nPUSH_FALSE\nSTORE 0\nJUMP loop\nend:\nPUSH_INT 0\nRET"
    assert (await run(main(body, locals_=1))).gas_used == 2
    loop = main("a:\nPUSH_TRUE\nJUMP_IF_TRUE a\nPUSH_INT 0\nRET")
    ctx = context(gas=1000)
    with pytest.raises(OutOfGas):
        await run(loop, ctx=ctx)
    assert ctx.gas.used == 1000


async def test_concat_surcharge() -> None:
    long = json.dumps("x" * 2048)
    outcome = await run(main(f"PUSH_CONST {long}\nPUSH_CONST {long}\nCONCAT\nLEN\nRET"))
    assert outcome.gas_used == 1 + 16 + 1


async def test_out_of_gas_before_an_operation_runs() -> None:
    host = FakeHost(balances={(SELF, RAPID): 100})
    text = main("CALL_BUILTIN sender 0\nPUSH_INT 77\nCALL_BUILTIN Currency 1\nPUSH_INT 5\nCALL_BUILTIN transfer 3\nPUSH_INT 0\nRET")
    ctx = context(gas=10)
    with pytest.raises(OutOfGas) as info:
        await run(text, host=host, ctx=ctx)
    assert host.calls == []
    assert info.value.level == 0
    assert info.value.sublimit is False
    assert ctx.gas.used == 10
    ctx = context(gas=11)
    assert (await run(text, host=host, ctx=ctx)).gas_used == 11


def test_gas_meter_sub_limits() -> None:
    meter = GasMeter(100)
    meter.charge(10)
    level = meter.push_limit(20)
    assert level == 1
    assert meter.remaining == 20
    inner = meter.push_limit(500)
    assert meter.remaining == 20
    with pytest.raises(OutOfGas) as info:
        meter.charge(21)
    assert info.value.level == 1
    meter.pop_limit(inner)
    meter.pop_limit(level)
    assert meter.used == 30
    assert meter.remaining == 70
    top = meter.push_limit(1000)
    with pytest.raises(OutOfGas) as info:
        meter.charge(71)
    assert info.value.level == 0
    assert not info.value.sublimit
    meter.pop_limit(top)
    with pytest.raises(RuntimeError):
        meter.pop_limit(0)


async def test_str_limit() -> None:
    half = json.dumps("x" * 2048)
    assert await value(main(f"PUSH_CONST {half}\nPUSH_CONST {half}\nCONCAT\nLEN\nRET")) == 4096
    with pytest.raises(ContractRuntimeError, match="longer than 4096"):
        await value(main(f'PUSH_CONST {half}\nPUSH_CONST {half}\nCONCAT\nPUSH_CONST "y"\nCONCAT\nLEN\nRET'))


async def test_list_and_dict_limits() -> None:
    limits = Limits(collection_max_length=3)
    appends = "BUILD_LIST 0\nSTORE 0\n" + "LOAD 0\nPUSH_INT 1\nAPPEND\n" * 3 + "LOAD 0\nLEN\nRET"
    assert await value(main(appends, locals_=1), limits=limits) == 3
    with pytest.raises(ContractRuntimeError, match="longer than 3"):
        await value(main(appends.replace("LOAD 0\nLEN", "LOAD 0\nPUSH_INT 1\nAPPEND\nLOAD 0\nLEN"), locals_=1), limits=limits)
    sets = "BUILD_DICT 0\nSTORE 0\n" + "".join(f"LOAD 0\nPUSH_INT {i}\nPUSH_INT 1\nINDEX_SET\n" for i in range(4)) + "LOAD 0\nLEN\nRET"
    with pytest.raises(ContractRuntimeError, match="larger than 3"):
        await value(main(sets, locals_=1), limits=limits)
    with pytest.raises(ContractRuntimeError, match="longer than 3"):
        await value(main("PUSH_INT 1\nPUSH_INT 1\nPUSH_INT 1\nPUSH_INT 1\nBUILD_LIST 4\nLEN\nRET"), limits=limits)
    with pytest.raises(ContractRuntimeError, match="longer than 3"):
        await value(main("PUSH_INT 1\nPUSH_INT 1\nBUILD_LIST 2\nDUP\nCONCAT\nLEN\nRET"), limits=limits)


async def test_memory_limit() -> None:
    limits = Limits(memory_max_bytes=64 * 1024)
    chunk = json.dumps("z" * 4000)
    body = 'BUILD_LIST 0\nSTORE 0\nPUSH_INT 0\nSTORE 1\nPUSH_INT 100\nSTORE 2\nPUSH_INT 1\nSTORE 3\nloop:\nFOR_RANGE 1 2 3 4 done\nLOAD 0\nPUSH_CONST {chunk}\nPUSH_CONST "!"\nCONCAT\nAPPEND\nJUMP loop\ndone:\nLOAD 0\nLEN\nRET'
    ctx = context(limits=limits, gas=100_000)
    with pytest.raises(LimitExceeded) as info:
        await run(main(body.replace("{chunk}", chunk), locals_=5), limits=limits, ctx=ctx)
    assert info.value.resource is LimitResource.MEMORY
    assert info.value.catchable is False
    assert info.value.code == "MEMORY_LIMIT"
    small = context(limits=limits, gas=100_000)
    assert (await run(main(body.replace("{chunk}", json.dumps("z" * 100)), locals_=5), limits=limits, ctx=small)).value == 100


APPEND_CONSTANT = """.function main public returns=int locals=5
BUILD_LIST 0
STORE 0
PUSH_INT 0
STORE 1
PUSH_INT {count}
STORE 2
PUSH_INT 1
STORE 3
l:
FOR_RANGE 1 2 3 4 d
LOAD 0
PUSH_CONST {chunk}
APPEND
JUMP l
d:
LOAD 0
LEN
RET
.end
"""


async def test_references_to_one_constant_string_count_per_reference() -> None:
    chunk = json.dumps("z" * 4000)
    ctx = checking_context()
    with pytest.raises(LimitExceeded):
        await run(APPEND_CONSTANT.format(count=1024, chunk=chunk), ctx=ctx)
    entries = (ctx.memory.limit - 4096) // (8 + 16 + 4000)
    within = checking_context()
    assert (await run(APPEND_CONSTANT.format(count=entries, chunk=chunk), ctx=within)).value == entries


async def test_the_limit_fails_at_the_allocation_that_crosses_it() -> None:
    limits = Limits(memory_max_bytes=40_000)
    chunk = json.dumps("z" * 984)
    ctx = checking_context(limits=limits)
    with pytest.raises(LimitExceeded):
        await run(APPEND_CONSTANT.format(count=1000, chunk=chunk), ctx=ctx, limits=limits)
    exact, _ = ctx.memory.measure(10**12)
    assert exact <= limits.memory_max_bytes


async def test_memory_released_values_do_not_count() -> None:
    limits = Limits(memory_max_bytes=64 * 1024)
    chunk = json.dumps("z" * 4000)
    body = f'PUSH_INT 0\nSTORE 1\nPUSH_INT 200\nSTORE 2\nPUSH_INT 1\nSTORE 3\nloop:\nFOR_RANGE 1 2 3 4 done\nPUSH_CONST {chunk}\nPUSH_CONST "!"\nCONCAT\nSTORE 0\nJUMP loop\ndone:\nLOAD 0\nLEN\nRET'
    ctx = context(limits=limits, gas=100_000)
    assert (await run(main(body, locals_=5), limits=limits, ctx=ctx)).value == 4001
    assert ctx.memory.walks > 0


async def test_walks_are_charged_and_bounded() -> None:
    limits = Limits(memory_max_bytes=256 * 1024)
    body = (
        "BUILD_LIST 0\nSTORE 0\nPUSH_INT 0\nSTORE 1\nPUSH_INT 1000\nSTORE 2\nPUSH_INT 1\nSTORE 3\n"
        "l1:\nFOR_RANGE 1 2 3 4 d1\nLOAD 0\nPUSH_INT 1\nBUILD_LIST 1\nAPPEND\nJUMP l1\nd1:\n"
        "PUSH_INT 0\nSTORE 1\nPUSH_INT 400\nSTORE 2\nl2:\nFOR_RANGE 1 2 3 4 d2\nLOAD 0\nPUSH_INT 0\nPUSH_INT 64\nSLICE 3\nPOP\nJUMP l2\nd2:\nPUSH_INT 0\nRET"
    )
    ctx = context(limits=limits, gas=1_000_000)
    await run(main(body, locals_=5), limits=limits, ctx=ctx)
    meter = ctx.memory
    assert ctx.gas.used >= memory_walk_gas(meter.visited)
    assert meter.walks <= 2 + ctx.gas.used // 50


def fake_frame(stack: list[object], local_count: int = 0, max_stack: int = 0) -> Any:
    from rapidwire.contract.vm import _Frame

    frame = _Frame.__new__(_Frame)
    frame.function = SimpleNamespace(local_count=local_count, max_stack=max_stack)  # type: ignore[assignment]
    frame.stack = stack
    frame.locals = []
    return frame


def test_memory_model_counts_shared_containers_once_and_strings_per_reference() -> None:
    shared: list[object] = [1, "xy"]
    meter = MemoryMeter(10**6)
    meter.attach(SimpleNamespace(frames=[fake_frame([shared, shared, "ab", 5], local_count=1, max_stack=4)]))
    # frame 64 + 24 * 5 slots, str slot 16 + 2, list once: 32 + 2 refs of 8 + int 16 + str 16 + 2
    assert meter.measure() == (64 + 120 + 18 + 32 + 16 + 16 + 18, 6)
    cyclic: list[object] = []
    cyclic.append(cyclic)
    meter = MemoryMeter(10**6)
    meter.attach(SimpleNamespace(frames=[fake_frame([cyclic])]))
    assert meter.measure() == (64 + 32 + 8, 2)


async def test_callee_memory_counts_caller_frames_and_is_not_catchable() -> None:
    limits = Limits(memory_max_bytes=64 * 1024)
    big = json.dumps("q" * 4000)
    callee = (
        ".function grow public returns=int locals=5\nBUILD_LIST 0\nSTORE 0\nPUSH_INT 0\nSTORE 1\nPUSH_INT 10\nSTORE 2\nPUSH_INT 1\nSTORE 3\n"
        f"l:\nFOR_RANGE 1 2 3 4 d\nLOAD 0\nPUSH_CONST {big}\nAPPEND\nJUMP l\nd:\nPUSH_INT 1\nRET\n.end\n"
    )
    peer = Account(3003)
    host = FakeHost(contracts={peer: program(callee, limits=limits)})
    hold = "".join(f"PUSH_CONST {big}\nSTORE {i}\n" for i in range(8))
    caller = main(f'{hold}PUSH_INT {peer.id}\nCALL_BUILTIN Account 1\nPUSH_CONST "grow"\nTRY_CALL 0 0\nGET_FIELD CallResult ok\nJUMP_IF_FALSE no\nPUSH_INT 1\nRET\nno:\nPUSH_INT 0\nRET', locals_=8)
    with pytest.raises(LimitExceeded):
        await run(caller, host=host, ctx=context(limits=limits, gas=100_000), limits=limits)
    alone = main(f'PUSH_INT {peer.id}\nCALL_BUILTIN Account 1\nPUSH_CONST "grow"\nTRY_CALL 0 0\nGET_FIELD CallResult ok\nJUMP_IF_FALSE no\nPUSH_INT 1\nRET\nno:\nPUSH_INT 0\nRET')
    assert (await run(alone, host=host, ctx=context(limits=limits, gas=100_000), limits=limits)).value == 1


async def test_random_draws_are_capped_per_execution() -> None:
    limits = Limits(max_random_draws=3)
    draw = "PUSH_INT 1\nPUSH_INT 6\nCALL_BUILTIN random 2\nPOP\n"
    assert (await run(main(draw * 3 + "PUSH_INT 0\nRET"), limits=limits)).random_values.__len__() == 3
    with pytest.raises(ContractRuntimeError, match="at most 3 draws"):
        await run(main(draw * 4 + "PUSH_INT 0\nRET"), limits=limits)
    assert DEFAULT_LIMITS.max_random_draws == 256


async def test_internal_frame_depth_limit() -> None:
    text = main("PUSH_INT 0\nCALL f\nRET") + ".function f params=int returns=int\nLOAD 0\nPUSH_INT 1\nADD\nCALL f\nRET\n.end\n"
    with pytest.raises(ContractRuntimeError, match="internal call depth limit of 64"):
        await value(text)
    countdown = main("PUSH_INT {n}\nCALL f\nRET") + ".function f params=int returns=int\nLOAD 0\nPUSH_INT 0\nEQ\nJUMP_IF_FALSE rec\nPUSH_INT 0\nRET\nrec:\nLOAD 0\nPUSH_INT 1\nSUB\nCALL f\nRET\n.end\n"
    assert await value(countdown.replace("{n}", "62")) == 0
    with pytest.raises(ContractRuntimeError, match="depth limit"):
        await value(countdown.replace("{n}", "63"))


async def test_wall_clock_limit_with_a_fake_clock() -> None:
    now = [0.0]

    def clock() -> float:
        now[0] += 0.001
        return now[0]

    ctx = context(gas=10**9, clock=clock)
    with pytest.raises(LimitExceeded) as info:
        await run(main("a:\nPUSH_TRUE\nJUMP_IF_TRUE a\nPUSH_INT 0\nRET"), ctx=ctx)
    assert info.value.resource is LimitResource.WALL_CLOCK
    assert info.value.code == "WALL_CLOCK_LIMIT"


async def test_wall_clock_is_checked_on_every_host_call() -> None:
    now = [0.0]
    host = FakeHost()

    def advance(name: str) -> None:
        now[0] += 1.5

    host.on_call = advance
    ctx = context(clock=lambda: now[0])
    text = main("CALL_BUILTIN sender 0\nPUSH_INT 1\nCALL_BUILTIN Currency 1\nCALL_BUILTIN balance 2\nCALL_BUILTIN sender 0\nPUSH_INT 1\nCALL_BUILTIN Currency 1\nCALL_BUILTIN balance 2\nADD\nRET")
    with pytest.raises(LimitExceeded):
        await run(text, host=host, ctx=ctx)
    assert len(host.calls) == 2


async def test_failure_messages_are_bounded_and_ascii_safe() -> None:
    message = json.dumps("\u3042" * 1000)
    with pytest.raises(ContractRevert) as info:
        await value(main(f"PUSH_CONST {message}\nREVERT"))
    assert len(info.value.message) == MESSAGE_MAX_LENGTH
    assert info.value.message.endswith("...")
    assert str(info.value).isascii()
    limits = Limits(message_max_length=20)
    with pytest.raises(ContractRevert) as info:
        await value(main(f"PUSH_CONST {message}\nREVERT"), limits=limits)
    assert len(info.value.message) == 20
    assert len(str(info.value)) == 6 * 17 + 3


def test_limits_validation() -> None:
    assert DEFAULT_LIMITS.int_bound == 2**256
    assert DEFAULT_LIMITS.int_ok(2**256 - 1)
    assert not DEFAULT_LIMITS.int_ok(-(2**256))
    with pytest.raises(ValueError):
        Limits(int_bits=32)
    with pytest.raises(ValueError):
        Limits(int_bits=10_000)
    with pytest.raises(ValueError):
        Limits(str_max_length=0)
    with pytest.raises(ValueError):
        Limits(wall_clock_seconds=0)
    with pytest.raises(ValueError):
        Limits(max_locals=4, max_params=8)
    with pytest.raises(ValueError):
        Limits(max_random_draws=0)
