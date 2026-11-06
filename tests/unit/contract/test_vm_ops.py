from __future__ import annotations

from typing import Any

import pytest

from rapidwire.contract.errors import CallFailed, CallFailure, ContractRevert, ContractRuntimeError
from rapidwire.contract.types import Kind
from rapidwire.contract.values import Account, Currency, Record
from tests.unit.contract.helpers import SELF, SENDER, run, value


def fn(body: str, *, params: str = "", returns: str = "int", locals_: int | None = None, extra: str = "") -> str:
    header = ".function main public"
    if params:
        header += f" params={params}"
    if returns:
        header += f" returns={returns}"
    if locals_ is not None:
        header += f" locals={locals_}"
    return f"{header}\n{body}\n.end\n{extra}"


async def binop(op: str, a: str, b: str, returns: str = "int") -> Any:
    return await value(fn(f"PUSH_CONST {a}\nPUSH_CONST {b}\n{op}\nRET", returns=returns))


@pytest.mark.parametrize(
    ("op", "a", "b", "expected"),
    [
        ("ADD", "7", "5", 12),
        ("SUB", "7", "-5", 12),
        ("MUL", "-7", "5", -35),
        ("FLOORDIV", "-7", "2", -4),
        ("MOD", "-7", "2", 1),
        ("MOD", "7", "-2", -1),
    ],
)
async def test_arithmetic(op: str, a: str, b: str, expected: int) -> None:
    assert await binop(op, a, b) == expected


@pytest.mark.parametrize("op", ["FLOORDIV", "MOD"])
async def test_division_by_zero(op: str) -> None:
    with pytest.raises(ContractRuntimeError, match="division by zero"):
        await binop(op, "1", "0")


async def test_neg_and_not() -> None:
    assert await value(fn("PUSH_INT 5\nNEG\nRET")) == -5
    assert await value(fn("PUSH_TRUE\nNOT\nRET", returns="bool")) is False


@pytest.mark.parametrize(
    ("op", "a", "b", "expected"),
    [
        ("EQ", "3", "3", True),
        ("NE", "3", "3", False),
        ("LT", "2", "3", True),
        ("LE", "3", "3", True),
        ("GT", "2", "3", False),
        ("GE", "3", "3", True),
        ("LT", '"a"', '"b"', True),
        ("EQ", '"a"', '"a"', True),
    ],
)
async def test_comparisons(op: str, a: str, b: str, expected: bool) -> None:
    assert await binop(op, a, b, returns="bool") is expected


async def test_eq_on_accounts_and_type_strictness() -> None:
    body = "CALL_BUILTIN sender 0\nCALL_BUILTIN sender 0\nEQ\nRET"
    assert await value(fn(body, returns="bool")) is True
    with pytest.raises(ContractRuntimeError, match="unsupported operand types for EQ"):
        await value(fn("PUSH_INT 1\nPUSH_TRUE\nEQ\nRET", returns="bool"))
    with pytest.raises(ContractRuntimeError, match="unsupported operand types for LT"):
        await value(fn('PUSH_INT 1\nPUSH_CONST "1"\nLT\nRET', returns="bool"))
    with pytest.raises(ContractRuntimeError):
        await value(fn("PUSH_INT 0\nCALL_BUILTIN Account 1\nPUSH_INT 0\nEQ\nRET", returns="bool"))


@pytest.mark.parametrize(("op", "a", "b"), [("ADD", "PUSH_TRUE", "PUSH_INT 1"), ("SUB", 'PUSH_CONST "x"', "PUSH_INT 1"), ("MUL", "PUSH_INT 1", "PUSH_FALSE")])
async def test_arithmetic_rejects_non_ints(op: str, a: str, b: str) -> None:
    with pytest.raises(ContractRuntimeError, match=f"unsupported operand types for {op}"):
        await value(fn(f"{a}\n{b}\n{op}\nRET"))


async def test_int_limit_on_results() -> None:
    big = str(2**255)
    with pytest.raises(ContractRuntimeError, match="int limit"):
        await value(fn(f"PUSH_CONST {big}\nPUSH_CONST {big}\nADD\nRET"))
    with pytest.raises(ContractRuntimeError, match="int limit"):
        await value(fn(f"PUSH_CONST {big}\nPUSH_INT 2\nMUL\nRET"))
    assert await value(fn(f"PUSH_CONST {big}\nPUSH_INT 1\nSUB\nRET")) == 2**255 - 1


async def test_stack_ops_and_locals() -> None:
    body = "PUSH_INT 1\nPUSH_INT 2\nSWAP\nSUB\nDUP\nADD\nSTORE 0\nPUSH_INT 9\nPOP\nLOAD 0\nRET"
    assert await value(fn(body)) == 2


async def test_unassigned_local_is_a_runtime_error() -> None:
    with pytest.raises(ContractRuntimeError, match="read before it is assigned"):
        await value(fn("LOAD 0\nRET", locals_=1))


async def test_jumps_and_branches() -> None:
    body = "PUSH_FALSE\nJUMP_IF_FALSE a\nPUSH_INT 1\nRET\na:\nPUSH_TRUE\nJUMP_IF_TRUE b\nPUSH_INT 2\nRET\nb:\nJUMP c\nPUSH_INT 3\nRET\nc:\nPUSH_INT 4\nRET"
    assert await value(fn(body)) == 4
    with pytest.raises(ContractRuntimeError, match="condition must be bool"):
        await value(fn("PUSH_INT 1\nJUMP_IF_FALSE a\na:\nPUSH_INT 0\nRET"))


async def test_short_circuit_or_lowering() -> None:
    # a or b lowered as DUP; JUMP_IF_TRUE end; POP; <b>; end:
    body = "LOAD 0\nDUP\nJUMP_IF_TRUE end\nPOP\nLOAD 1\nend:\nRET"
    assert await value(fn(body, params="bool,bool", returns="bool"), args=[False, True]) is True
    assert await value(fn(body, params="bool,bool", returns="bool"), args=[True, False]) is True
    assert await value(fn(body, params="bool,bool", returns="bool"), args=[False, False]) is False


RANGE_SUM = fn(
    "PUSH_INT 0\nSTORE 1\nLOAD 0\nSTORE 5\nPUSH_INT 0\nSTORE 2\nLOAD 6\nSTORE 3\nloop:\nFOR_RANGE 2 5 3 4 done\nLOAD 1\nLOAD 4\nADD\nSTORE 1\nJUMP loop\ndone:\nLOAD 1\nRET",
    params="int,int,int,int,int,int,int",
)


async def test_for_range() -> None:
    # counter starts at 0, stop = param 0, step = param 6; sums the loop variable.
    outcome = await run(RANGE_SUM, args=[10, 0, 0, 0, 0, 0, 1])
    assert outcome.value == 45
    # 11 FOR_RANGE steps, 10 ADDs, and 1 for the setup's run of eight loads and stores.
    assert outcome.gas_used == 11 + 10 + 1
    assert (await run(RANGE_SUM, args=[-10, 0, 0, 0, 0, 0, -3])).value == 0 + -3 + -6 + -9
    with pytest.raises(ContractRuntimeError, match="step must not be zero"):
        await run(RANGE_SUM, args=[10, 0, 0, 0, 0, 0, 0])


async def test_for_range_near_the_int_limit_terminates() -> None:
    bound = 2**256
    body = "PUSH_INT 0\nSTORE 2\nPUSH_INT 0\nSTORE 0\nPUSH_CONST {stop}\nSTORE 1\nPUSH_CONST {step}\nSTORE 3\nloop:\nFOR_RANGE 0 1 3 4 done\nLOAD 2\nPUSH_INT 1\nADD\nSTORE 2\nJUMP loop\ndone:\nLOAD 2\nRET"
    assert await value(fn(body.format(stop=bound - 1, step=2**255), locals_=5)) == 2


async def test_for_each_over_list_and_str() -> None:
    body = (
        "PUSH_INT 1\nPUSH_INT 2\nPUSH_INT 3\nBUILD_LIST 3\nSTORE 0\nPUSH_INT 0\nSTORE 1\nPUSH_INT 0\nSTORE 3\nloop:\nFOR_EACH 0 1 2 done\nLOAD 3\nLOAD 2\nADD\nSTORE 3\nJUMP loop\ndone:\nLOAD 3\nRET"
    )
    assert await value(fn(body, locals_=4)) == 6
    text_body = '.const "abc"\n' + fn(
        'PUSH_CONST #0\nSTORE 0\nPUSH_INT 0\nSTORE 1\nPUSH_CONST ""\nSTORE 3\nloop:\nFOR_EACH 0 1 2 done\nLOAD 2\nLOAD 3\nCONCAT\nSTORE 3\nJUMP loop\ndone:\nLOAD 3\nRET',
        returns="str",
        locals_=4,
    )
    assert await value(text_body) == "cba"


async def test_for_each_sees_appends_during_iteration() -> None:
    body = (
        "PUSH_INT 1\nBUILD_LIST 1\nSTORE 0\nPUSH_INT 0\nSTORE 1\n"
        "loop:\nFOR_EACH 0 1 2 done\nLOAD 2\nPUSH_INT 3\nLT\nJUMP_IF_FALSE loop\nLOAD 0\nLOAD 2\nPUSH_INT 1\nADD\nAPPEND\nJUMP loop\n"
        "done:\nLOAD 0\nLEN\nRET"
    )
    assert await value(fn(body, locals_=3)) == 3


async def test_internal_calls_and_recursion() -> None:
    text = fn(
        "LOAD 0\nCALL fact\nRET",
        params="int",
        extra=".function fact params=int returns=int\nLOAD 0\nPUSH_INT 1\nLE\nJUMP_IF_FALSE rec\nPUSH_INT 1\nRET\nrec:\nLOAD 0\nLOAD 0\nPUSH_INT 1\nSUB\nCALL fact\nMUL\nRET\n.end\n",
    )
    assert await value(text, args=[10]) == 3628800


async def test_none_returning_internal_function() -> None:
    text = fn(
        "BUILD_LIST 0\nDUP\nSTORE 0\nCALL push\nLOAD 0\nLEN\nRET",
        locals_=1,
        extra=".function push params=list[int]\nLOAD 0\nPUSH_INT 5\nAPPEND\nRET\n.end\n",
    )
    assert await value(text) == 1


async def test_internal_call_argument_and_return_types_are_checked() -> None:
    text = fn("PUSH_TRUE\nCALL f\nRET", extra=".function f params=int returns=int\nLOAD 0\nRET\n.end\n")
    with pytest.raises(ContractRuntimeError, match="argument must be int"):
        await value(text)
    text = fn("PUSH_INT 1\nCALL f\nRET", extra='.function f params=int returns=int\nPUSH_CONST "x"\nRET\n.end\n')
    with pytest.raises(ContractRuntimeError, match="must return int"):
        await value(text)


async def test_revert_and_assert() -> None:
    with pytest.raises(ContractRevert) as info:
        await value(fn('PUSH_CONST "nope"\nREVERT'))
    assert info.value.message == "nope"
    assert info.value.function == "main"
    assert info.value.offset == 2
    with pytest.raises(ContractRevert, match="bad"):
        await value(fn('PUSH_FALSE\nASSERT "bad"\nPUSH_INT 1\nRET'))
    assert await value(fn('PUSH_TRUE\nASSERT "bad"\nPUSH_INT 1\nRET')) == 1
    with pytest.raises(ContractRuntimeError, match="revert message must be a str"):
        await value(fn("PUSH_INT 1\nREVERT"))


async def test_failure_location_is_the_failing_instruction() -> None:
    text = fn("PUSH_INT 1\nCALL f\nRET", extra=".function f params=int returns=int\nLOAD 0\nPUSH_INT 0\nFLOORDIV\nRET\n.end\n")
    with pytest.raises(ContractRuntimeError) as info:
        await value(text)
    assert (info.value.function, info.value.offset) == ("f", 4)


async def test_strings() -> None:
    assert await value(fn('PUSH_CONST "ab"\nPUSH_CONST "cd"\nCONCAT\nRET', returns="str")) == "abcd"
    assert await value(fn('PUSH_CONST "hello"\nPUSH_INT 1\nPUSH_INT -1\nSLICE 3\nRET', returns="str")) == "ell"
    assert await value(fn('PUSH_CONST "hello"\nPUSH_INT 3\nSLICE 1\nRET', returns="str")) == "lo"
    assert await value(fn('PUSH_CONST "hello"\nPUSH_INT 2\nSLICE 2\nRET', returns="str")) == "he"
    assert await value(fn('PUSH_CONST "hello"\nSLICE 0\nRET', returns="str")) == "hello"
    assert await value(fn('PUSH_CONST "hello"\nPUSH_INT -1\nINDEX_GET\nRET', returns="str")) == "o"
    assert await value(fn('PUSH_CONST "hello"\nLEN\nRET')) == 5
    assert await value(fn('PUSH_CONST "ell"\nPUSH_CONST "hello"\nCONTAINS\nRET', returns="bool")) is True
    with pytest.raises(ContractRuntimeError, match="index out of range"):
        await value(fn('PUSH_CONST "hello"\nPUSH_INT 5\nINDEX_GET\nRET', returns="str"))


async def test_slice_with_huge_bounds() -> None:
    assert await value(fn(f'PUSH_CONST "hello"\nPUSH_CONST {-(2**255)}\nPUSH_CONST {2**255}\nSLICE 3\nRET', returns="str")) == "hello"


async def test_lists() -> None:
    body = (
        "PUSH_INT 1\nPUSH_INT 2\nBUILD_LIST 2\nSTORE 0\n"
        "LOAD 0\nPUSH_INT 3\nAPPEND\n"
        "LOAD 0\nPUSH_INT 0\nPUSH_INT 10\nINDEX_SET\n"
        "LOAD 0\nPUSH_INT 1\nDELETE_ITEM\n"
        "LOAD 0\nLIST_POP 0\nLOAD 0\nPUSH_INT 0\nINDEX_GET\nADD\n"
        "LOAD 0\nLEN\nADD\nRET"
    )
    assert await value(fn(body, locals_=1)) == 3 + 10 + 1
    assert await value(fn("PUSH_INT 1\nPUSH_INT 2\nBUILD_LIST 2\nPUSH_INT 0\nLIST_POP 1\nRET")) == 1
    concat = "PUSH_INT 1\nBUILD_LIST 1\nPUSH_INT 2\nPUSH_INT 3\nBUILD_LIST 2\nCONCAT\nPUSH_INT 1\nSLICE 1\nDUP\nLEN\nSWAP\nPUSH_INT 0\nINDEX_GET\nADD\nRET"
    assert await value(fn(concat)) == 2 + 2
    assert await value(fn("PUSH_INT 2\nPUSH_INT 1\nPUSH_INT 2\nBUILD_LIST 2\nCONTAINS\nRET", returns="bool")) is True
    with pytest.raises(ContractRuntimeError, match="pop from an empty list"):
        await value(fn("BUILD_LIST 0\nLIST_POP 0\nRET"))
    with pytest.raises(ContractRuntimeError, match="index out of range"):
        await value(fn("BUILD_LIST 0\nPUSH_INT 0\nPUSH_INT 1\nINDEX_SET\nPUSH_INT 0\nRET"))
    with pytest.raises(ContractRuntimeError, match="unsupported operand types for in"):
        await value(fn("BUILD_LIST 0\nBUILD_LIST 0\nCONTAINS\nRET", returns="bool"))


async def test_dicts() -> None:
    body = (
        'PUSH_CONST "a"\nPUSH_INT 1\nPUSH_CONST "b"\nPUSH_INT 2\nBUILD_DICT 2\nSTORE 0\n'
        'LOAD 0\nPUSH_CONST "c"\nPUSH_INT 3\nINDEX_SET\n'
        'LOAD 0\nPUSH_CONST "a"\nDELETE_ITEM\n'
        'LOAD 0\nPUSH_CONST "c"\nINDEX_GET\n'
        "LOAD 0\nLEN\nADD\n"
        'LOAD 0\nPUSH_CONST "zz"\nPUSH_INT 100\nCALL_BUILTIN dict.get 3\nADD\n'
        "LOAD 0\nCALL_BUILTIN dict.keys 1\nLEN\nADD\n"
        "LOAD 0\nCALL_BUILTIN dict.values 1\nPUSH_INT 0\nINDEX_GET\nADD\nRET"
    )
    assert await value(fn(body, locals_=1)) == 3 + 2 + 100 + 2 + 2
    with pytest.raises(ContractRuntimeError, match="not found"):
        await value(fn('BUILD_DICT 0\nPUSH_CONST "x"\nINDEX_GET\nRET'))
    with pytest.raises(ContractRuntimeError, match="not found"):
        await value(fn('BUILD_DICT 0\nPUSH_CONST "x"\nDELETE_ITEM\nPUSH_INT 0\nRET'))
    with pytest.raises(ContractRuntimeError, match="dict keys must be"):
        await value(fn("BUILD_LIST 0\nPUSH_INT 1\nBUILD_DICT 1\nLEN\nRET"))
    with pytest.raises(ContractRuntimeError):
        await value(fn("BUILD_DICT 0\nBUILD_LIST 0\nPUSH_INT 1\nINDEX_SET\nPUSH_INT 0\nRET"))
    assert await value(fn('PUSH_CONST "k"\nPUSH_CONST "k"\nPUSH_INT 1\nBUILD_DICT 1\nCONTAINS\nRET', returns="bool")) is True


async def test_dict_keys_keep_account_and_int_apart() -> None:
    body = "CALL_BUILTIN sender 0\nPUSH_INT 1\nPUSH_INT 2002\nPUSH_INT 2\nBUILD_DICT 2\nLEN\nRET"
    assert await value(fn(body)) == 2


@pytest.mark.parametrize(
    "body",
    [
        "PUSH_TRUE\nPUSH_INT 1\nBUILD_DICT 1\nLEN\nRET",
        "PUSH_INT 1\nPUSH_INT 10\nBUILD_DICT 1\nPUSH_TRUE\nPUSH_INT 20\nINDEX_SET\nPUSH_INT 0\nRET",
        "PUSH_INT 1\nPUSH_INT 10\nBUILD_DICT 1\nPUSH_TRUE\nINDEX_GET\nRET",
        "PUSH_INT 1\nPUSH_INT 1\nBUILD_DICT 1\nPUSH_TRUE\nPUSH_INT 0\nCALL_BUILTIN dict.get 3\nCHECK_TYPE int\nRET",
        "PUSH_TRUE\nPUSH_INT 1\nPUSH_INT 1\nBUILD_DICT 1\nCONTAINS\nTO_INT\nRET",
        "PUSH_INT 1\nPUSH_INT 1\nBUILD_DICT 1\nPUSH_TRUE\nDELETE_ITEM\nPUSH_INT 0\nRET",
    ],
    ids=["build", "set", "get", "dict.get", "in", "del"],
)
async def test_bool_is_not_a_dict_key(body: str) -> None:
    with pytest.raises(ContractRuntimeError, match="dict keys must be a 64-bit int, str, Account or Currency, not bool"):
        await value(fn(body))


@pytest.mark.parametrize(
    ("needle", "items", "expected"),
    [
        ("PUSH_TRUE", "PUSH_INT 1", False),
        ("PUSH_FALSE", "PUSH_INT 0", False),
        ("PUSH_INT 1", "PUSH_TRUE", False),
        ("PUSH_INT 0", "PUSH_FALSE", False),
        ("PUSH_INT 1", "PUSH_TRUE\nPUSH_INT 1", True),
        ("PUSH_TRUE", "PUSH_INT 1\nPUSH_TRUE", True),
        ("PUSH_INT 7", "PUSH_INT 7", True),
    ],
)
async def test_list_membership_is_type_strict(needle: str, items: str, expected: bool) -> None:
    count = items.count("\n") + 1
    assert await value(fn(f"{needle}\n{items}\nBUILD_LIST {count}\nCONTAINS\nRET", returns="bool")) is expected


async def test_records_and_fields() -> None:
    assert await value(fn("CALL_BUILTIN paid 0\nGET_FIELD Paid amount\nRET")) == 0
    body = "CALL_BUILTIN paid 0\nGET_FIELD Paid currency\nCALL_BUILTIN self 0\nPOP\nTO_INT\nRET"
    assert await value(fn(body)) == 0
    with pytest.raises(ContractRuntimeError, match="expected a Quote record"):
        await value(fn("CALL_BUILTIN paid 0\nGET_FIELD Quote amount_in\nRET"))


async def test_conversions() -> None:
    assert await value(fn('PUSH_CONST "-0042"\nTO_INT\nRET')) == -42
    assert await value(fn("PUSH_TRUE\nTO_INT\nRET")) == 1
    assert await value(fn("CALL_BUILTIN sender 0\nTO_INT\nRET")) == SENDER.id
    assert await value(fn("PUSH_INT -17\nTO_STR\nRET", returns="str")) == "-17"
    assert await value(fn("PUSH_FALSE\nTO_STR\nRET", returns="str")) == "False"
    assert await value(fn("CALL_BUILTIN self 0\nTO_STR\nRET", returns="str")) == str(SELF.id)
    for bad in ['" 1"', '"1.0"', '"+1"', '""', '"1_000"', '"\\u0661"', '"' + "9" * 100 + '"']:
        with pytest.raises(ContractRuntimeError, match="cannot parse"):
            await value(fn(f"PUSH_CONST {bad}\nTO_INT\nRET"))
    with pytest.raises(ContractRuntimeError, match="int limit"):
        await value(fn(f'PUSH_CONST "{2**256}"\nTO_INT\nRET'))
    with pytest.raises(ContractRuntimeError):
        await value(fn("BUILD_LIST 0\nTO_STR\nRET", returns="str"))


async def test_check_type() -> None:
    assert await value(fn("PUSH_INT 5\nCHECK_TYPE int\nRET")) == 5
    with pytest.raises(ContractRuntimeError, match="expected str, got int"):
        await value(fn("PUSH_INT 5\nCHECK_TYPE str\nRET", returns="str"))
    nested = "PUSH_INT 1\nBUILD_LIST 1\nPUSH_CONST {inner}\nBUILD_LIST 1\nBUILD_LIST 2\nCHECK_TYPE list[list[int]]\nLEN\nRET"
    assert await value(fn(nested.format(inner="2"))) == 2
    with pytest.raises(ContractRuntimeError, match=r"expected list\[list\[int\]\]"):
        await value(fn(nested.format(inner='"x"')))
    dict_check = 'PUSH_CONST "a"\nPUSH_INT 1\nBUILD_DICT 1\nCHECK_TYPE dict[str,int]\nCHECK_TYPE Value\nLEN\nRET'
    assert await value(fn(dict_check)) == 1


async def test_check_type_on_a_cyclic_list_terminates() -> None:
    body = "BUILD_LIST 0\nSTORE 0\nLOAD 0\nLOAD 0\nAPPEND\nLOAD 0\nCHECK_TYPE list[list[int]]\nLEN\nRET"
    with pytest.raises(ContractRuntimeError, match="expected"):
        await value(fn(body, locals_=1))


async def test_public_entry_checks() -> None:
    text = fn("LOAD 0\nRET", params="int")
    with pytest.raises(CallFailed) as info:
        await value(text, args=[True])
    assert info.value.reason is CallFailure.BAD_ARGUMENTS
    with pytest.raises(CallFailed) as info:
        await value(text, args=[1, 2])
    assert info.value.reason is CallFailure.BAD_ARGUMENTS
    with pytest.raises(CallFailed) as info:
        await value(text, args=[2**300])
    assert info.value.reason is CallFailure.BAD_ARGUMENTS
    with pytest.raises(CallFailed) as info:
        await value(text, "other", args=[1])
    assert info.value.reason is CallFailure.NO_FUNCTION
    private = ".function main public returns=int\nPUSH_INT 0\nRET\n.end\n.function hidden returns=int\nPUSH_INT 1\nRET\n.end\n"
    with pytest.raises(CallFailed) as info:
        await value(private, "hidden")
    assert info.value.reason is CallFailure.NO_FUNCTION
    assert await value(fn("LOAD 0\nTO_INT\nRET", params="Account"), args=[Account(9)]) == 9
    assert await value(fn("LOAD 0\nTO_INT\nRET", params="Currency"), args=[Currency(8)]) == 8
    with pytest.raises(CallFailed):
        await value(fn("LOAD 0\nTO_INT\nRET", params="Currency"), args=[Account(8)])
    with pytest.raises(CallFailed):
        await value(fn("LOAD 0\nLEN\nRET", params="str"), args=["\ud800"])


def test_record_values_are_read_only() -> None:
    record = Record(Kind.PAID, (Currency(1), 2))
    with pytest.raises(AttributeError):
        record.kind = Kind.QUOTE
    assert record.field("amount") == 2
    twin = Record(Kind.PAID, (Currency(1), 2))
    assert record.fields_equal(twin)
    assert record != twin
    assert not record.fields_equal(Record(Kind.PAID, (Currency(1), True)))


def test_accounts_and_currencies_are_distinct_values_with_c_level_equality() -> None:
    assert Account(5) == Account(5)
    assert hash(Account(5)) == hash(Account(5))
    other_kinds: list[object] = [Currency(5), 5, (2, 5)]
    assert all(Account(5) != other for other in other_kinds)
    assert Account(5).id == 5
    assert Account(id=7).id == 7
    assert type(Account(5)).__eq__ is tuple.__eq__
    assert {Account(1): "a", Currency(1): "c", 1: "i"}[Currency(1)] == "c"
    assert repr(Currency(9)) == "Currency(9)"
    with pytest.raises(ValueError):
        Account(2**63)
    with pytest.raises(ValueError):
        Currency(True)


@pytest.mark.parametrize(
    "body",
    [
        f"PUSH_CONST {2**63}\nPUSH_INT 1\nBUILD_DICT 1\nLEN\nRET",
        f"BUILD_DICT 0\nPUSH_CONST {-(2**63) - 1}\nPUSH_INT 1\nINDEX_SET\nPUSH_INT 0\nRET",
        f"BUILD_DICT 0\nPUSH_CONST {2**64}\nINDEX_GET\nRET",
        f"PUSH_CONST {2**63}\nBUILD_DICT 0\nCONTAINS\nTO_INT\nRET",
    ],
    ids=["build", "set", "get", "in"],
)
async def test_int_dict_keys_are_signed_64_bit(body: str) -> None:
    with pytest.raises(ContractRuntimeError, match="dict keys must be a 64-bit int"):
        await value(fn(body))
    limit = f"PUSH_CONST {2**63 - 1}\nPUSH_INT 1\nPUSH_CONST {-(2**63)}\nPUSH_INT 2\nBUILD_DICT 2\nLEN\nRET"
    assert await value(fn(limit)) == 2
