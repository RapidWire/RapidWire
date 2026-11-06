from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from rapidwire.contract.builtins import BUILTINS_V1, BUILTINS_V1_BY_NAME, Builtin, BuiltinKind
from rapidwire.contract.costs import BUILTIN_GAS, storage_write_gas
from rapidwire.contract.errors import ContractRuntimeError, HostProtocolError
from rapidwire.contract.types import Kind, Type
from rapidwire.contract.values import Account, Attachment, Currency, StorageValueError, Value, decode_value, encode_value, encode_value_counted
from rapidwire.contract.vm import _PURE_BUILTINS
from tests.unit.contract.helpers import NOW, RAPID, SELF, SENDER, FakeHost, context, program, run, straight_line_gas, value


def push_arg(t: Type, account_local: int, currency_local: int) -> str:
    """Instructions pushing a sample argument of type t; Account and Currency come from locals so the setup costs no gas apart from containers."""
    kind = t.kind
    if kind is Kind.INT:
        return "PUSH_INT 3"
    if kind is Kind.STR:
        return 'PUSH_CONST "key"'
    if kind is Kind.ACCOUNT:
        return f"LOAD {account_local}"
    if kind is Kind.CURRENCY:
        return f"LOAD {currency_local}"
    if kind is Kind.LIST:
        return "BUILD_LIST 0"
    if kind is Kind.DICT:
        return "BUILD_DICT 0"
    return 'PUSH_CONST "key"'


def call_program(builtin: Builtin) -> tuple[str, int]:
    lines = [push_arg(param.type, 0, 1) for param in builtin.params]
    setup = sum(1 for param in builtin.params if param.type.kind in (Kind.LIST, Kind.DICT))
    lines.append(f"CALL_BUILTIN {builtin.name} {builtin.arity}")
    if builtin.pushes:
        lines.append("POP")
    lines += ["PUSH_INT 0", "RET"]
    text = ".function main public params=Account,Currency returns=int\n" + "\n".join(lines) + "\n.end\n"
    return text, setup


def test_registry_ids_are_positions_and_names_unique() -> None:
    assert [b.id for b in BUILTINS_V1] == list(range(len(BUILTINS_V1)))
    assert len(BUILTINS_V1_BY_NAME) == len(BUILTINS_V1)
    assert not any(b.name.startswith("__") for b in BUILTINS_V1)


def test_every_non_host_builtin_is_implemented_by_the_vm() -> None:
    assert {b.name for b in BUILTINS_V1 if b.kind is not BuiltinKind.HOST} == set(_PURE_BUILTINS)


def test_every_host_builtin_names_a_host_method() -> None:
    from rapidwire.contract.vm import Host

    for builtin in BUILTINS_V1:
        if builtin.kind is BuiltinKind.HOST:
            assert builtin.host_method is not None
            assert callable(getattr(Host, builtin.host_method))


@pytest.mark.parametrize("builtin", BUILTINS_V1, ids=lambda b: b.name)
async def test_each_builtin_runs_and_costs_its_table_gas(builtin: Builtin) -> None:
    host = FakeHost(balances={(SELF, RAPID): 100})
    text, _ = call_program(builtin)
    verified = program(text)
    outcome = await run(verified, args=[SENDER, RAPID], host=host)
    assert outcome.value == 0
    size_extra = storage_write_gas(len("key") + len(encode_value(3)), 1) - builtin.gas if builtin.name == "storage.set" else 0
    assert outcome.gas_used == straight_line_gas(verified) + size_extra
    if builtin.kind is BuiltinKind.HOST:
        assert [name for name, _ in host.calls] == [builtin.host_method]
    else:
        assert host.calls == []


async def test_host_receives_typed_arguments() -> None:
    host = FakeHost(balances={(SELF, RAPID): 100})
    text = ".function main public params=Account,Currency returns=int\nLOAD 0\nLOAD 1\nPUSH_INT 30\nCALL_BUILTIN transfer 3\nCALL_BUILTIN self 0\nLOAD 1\nCALL_BUILTIN balance 2\nRET\n.end\n"
    assert await value(text, args=[SENDER, RAPID], host=host) == 70
    assert host.calls[0] == ("transfer", (SENDER, RAPID, 30))
    assert host.balances[(SENDER, RAPID)] == 30


async def test_builtin_argument_types_are_checked_before_the_host_is_called() -> None:
    host = FakeHost()
    text = ".function main public returns=int\nPUSH_INT 1\nPUSH_INT 77\nCALL_BUILTIN balance 2\nRET\n.end\n"
    with pytest.raises(ContractRuntimeError, match="argument account must be Account"):
        await value(text, host=host)
    assert host.calls == []


async def test_host_error_becomes_a_runtime_error() -> None:
    text = ".function main public returns=int\nCALL_BUILTIN sender 0\nPUSH_INT 77\nCALL_BUILTIN Currency 1\nPUSH_INT 5\nCALL_BUILTIN transfer 3\nPUSH_INT 0\nRET\n.end\n"
    with pytest.raises(ContractRuntimeError) as info:
        await value(text)
    assert info.value.host_code == "InsufficientBalance"
    assert "transfer failed" in info.value.message


class RerunRequested(Exception):
    pass


@pytest.mark.parametrize("exc", [RerunRequested("rerun"), RuntimeError("db down"), asyncio.CancelledError(), KeyboardInterrupt()], ids=type)
async def test_other_host_exceptions_propagate_unchanged(exc: BaseException) -> None:
    host = FakeHost(fail={"balance": exc})
    text = ".function main public params=Account,Currency returns=int\nLOAD 0\nLOAD 1\nCALL_BUILTIN balance 2\nRET\n.end\n"
    with pytest.raises(type(exc)) as info:
        await value(text, args=[SENDER, RAPID], host=host)
    assert info.value is exc


async def test_wrong_host_result_type_is_a_protocol_error() -> None:
    class BadHost(FakeHost):
        async def balance(self, ctx: object, account: Account, currency: Currency) -> int:
            return True

    text = ".function main public params=Account,Currency returns=int\nLOAD 0\nLOAD 1\nCALL_BUILTIN balance 2\nRET\n.end\n"
    with pytest.raises(HostProtocolError):
        await value(text, args=[SENDER, RAPID], host=BadHost())


async def test_context_builtins() -> None:
    text = (
        ".function main public returns=int\n"
        "CALL_BUILTIN now 0\nCALL_BUILTIN paid 0\nGET_FIELD Paid amount\nADD\n"
        "CALL_BUILTIN paid 0\nGET_FIELD Paid currency\nTO_INT\nADD\n"
        "CALL_BUILTIN self 0\nTO_INT\nADD\nCALL_BUILTIN sender 0\nTO_INT\nADD\nRET\n.end\n"
    )
    ctx = context(paid=Attachment(RAPID, 500))
    assert await value(text, ctx=ctx) == NOW + 500 + RAPID.id + SELF.id + SENDER.id


async def test_records_from_host() -> None:
    text = (
        ".function main public params=Currency returns=str\n"
        "LOAD 0\nCALL_BUILTIN currency_info 1\nGET_FIELD CurrencyInfo symbol\n"
        "PUSH_INT 9\nCALL_BUILTIN claim_info 1\nGET_FIELD ClaimInfo status\nCONCAT\n"
        "LOAD 0\nLOAD 0\nPUSH_INT 10\nCALL_BUILTIN quote 3\nGET_FIELD Quote amount_out\nTO_STR\nCONCAT\nRET\n.end\n"
    )
    assert await value(text, args=[RAPID]) == "RAPIDpending5"


async def test_remove_liquidity_returns_a_list() -> None:
    text = ".function main public params=Currency returns=int\nLOAD 0\nLOAD 0\nPUSH_INT 10\nPUSH_INT 4\nPUSH_INT 0\nPUSH_INT 0\nCALL_BUILTIN remove_liquidity 6\nPUSH_INT 1\nINDEX_GET\nRET\n.end\n"
    assert await value(text, args=[RAPID]) == 8


async def test_storage_round_trip_and_gas() -> None:
    host = FakeHost()
    setter = (
        ".function main public returns=int\n"
        'PUSH_CONST "k"\nPUSH_CONST "a"\nPUSH_INT 1\nPUSH_CONST "b"\nCALL_BUILTIN sender 0\nBUILD_LIST 1\nBUILD_DICT 2\nCALL_BUILTIN storage.set 2\n'
        "PUSH_INT 0\nRET\n.end\n"
    )
    verified = program(setter)
    outcome = await run(verified, host=host)
    stored = host.storage[SELF]["k"]
    assert decode_value(stored) == {"a": 1, "b": [SENDER]}
    assert encode_value_counted({"a": 1, "b": [SENDER]}) == (stored, 6)
    assert outcome.gas_used == straight_line_gas(verified) - BUILTIN_GAS["storage.set"] + storage_write_gas(1 + len(stored), 6)
    getter = '.function main public returns=int\nPUSH_CONST "k"\nPUSH_INT 0\nCALL_BUILTIN storage.get 2\nCHECK_TYPE dict[str,Value]\nPUSH_CONST "a"\nINDEX_GET\nCHECK_TYPE int\nRET\n.end\n'
    assert await value(getter, host=host) == 1
    default = '.function main public returns=int\nPUSH_CONST "missing"\nPUSH_INT 7\nCALL_BUILTIN storage.get 2\nCHECK_TYPE int\nRET\n.end\n'
    assert await value(default, host=host) == 7
    other = '.function main public params=Account returns=int\nLOAD 0\nPUSH_CONST "k"\nPUSH_INT 0\nCALL_BUILTIN read_storage 3\nCHECK_TYPE dict[str,Value]\nLEN\nRET\n.end\n'
    assert await value(other, args=[SELF], host=host) == 2
    deleter = '.function main public returns=int\nPUSH_CONST "k"\nCALL_BUILTIN storage.delete 1\nPUSH_CONST "k"\nPUSH_INT -1\nCALL_BUILTIN storage.get 2\nCHECK_TYPE int\nRET\n.end\n'
    assert await value(deleter, host=host) == -1


async def test_storage_reads_without_a_default() -> None:
    host = FakeHost()
    await value('.function main public returns=int\nPUSH_CONST "k"\nPUSH_INT 41\nCALL_BUILTIN storage.set 2\nPUSH_INT 0\nRET\n.end\n', host=host)
    own = ".function main public returns=int\nPUSH_CONST {key}\nCALL_BUILTIN storage.get 1\nCHECK_TYPE int\nRET\n.end\n"
    assert await value(own.format(key='"k"'), host=host) == 41
    with pytest.raises(ContractRuntimeError, match='storage key "nope" not found'):
        await value(own.format(key='"nope"'), host=host)
    other = ".function main public params=Account returns=int\nLOAD 0\nPUSH_CONST {key}\nCALL_BUILTIN read_storage 2\nCHECK_TYPE int\nRET\n.end\n"
    assert await value(other.format(key='"k"'), args=[SELF], host=host) == 41
    with pytest.raises(ContractRuntimeError, match="read_storage\\(\\): storage key"):
        await value(other.format(key='"nope"'), args=[SELF], host=host)
    assert BUILTINS_V1_BY_NAME["storage.get"].required == 1
    assert BUILTINS_V1_BY_NAME["read_storage"].required == 2
    assert BUILTINS_V1_BY_NAME["storage.get"].signature() == "storage.get(key: str[, default: Value]) -> Value"
    assert all(b.required == b.arity for b in BUILTINS_V1 if b.name not in ("storage.get", "read_storage"))


@pytest.mark.parametrize(
    ("args", "bad"),
    [("PUSH_INT 0\nCALL_BUILTIN sender 0\nPUSH_INT 1", "guild"), (f"PUSH_INT 1\nCALL_BUILTIN sender 0\nPUSH_CONST {2**63}", "role")],
)
async def test_discord_ids_must_be_snowflakes(args: str, bad: str) -> None:
    host = FakeHost()
    with pytest.raises(ContractRuntimeError, match=f"{bad} must be a Discord id"):
        await value(f".function main public returns=bool\n{args}\nCALL_BUILTIN discord.has_role 3\nRET\n.end\n", host=host)
    assert host.calls == []
    with pytest.raises(ContractRuntimeError, match="channel must be a Discord id"):
        await value('.function main public returns=int\nPUSH_INT 1\nPUSH_INT -5\nPUSH_CONST "hi"\nCALL_BUILTIN discord.send 3\nPUSH_INT 0\nRET\n.end\n', host=host)
    assert await value('.function main public returns=int\nPUSH_INT 1\nPUSH_INT 2\nPUSH_CONST "hi"\nCALL_BUILTIN discord.send 3\nPUSH_INT 0\nRET\n.end\n', host=host) == 0


async def test_host_lists_are_copied() -> None:
    shared = [1, 2]

    class SharingHost(FakeHost):
        async def remove_liquidity(self, ctx: object, *args: object) -> list[int]:
            return shared

    text = ".function main public params=Currency returns=int\nLOAD 0\nLOAD 0\nPUSH_INT 10\nPUSH_INT 4\nPUSH_INT 0\nPUSH_INT 0\nCALL_BUILTIN remove_liquidity 6\nDUP\nPUSH_INT 99\nAPPEND\nLEN\nRET\n.end\n"
    assert await value(text, args=[RAPID], host=SharingHost()) == 3
    assert shared == [1, 2]


async def test_storage_rejects_bad_keys_and_values() -> None:
    long_key = json.dumps("k" * 65)
    with pytest.raises(ContractRuntimeError, match="storage keys"):
        await value(f".function main public returns=int\nPUSH_CONST {long_key}\nPUSH_INT 1\nCALL_BUILTIN storage.set 2\nPUSH_INT 0\nRET\n.end\n")
    with pytest.raises(ContractRuntimeError, match="storage keys"):
        await value('.function main public returns=int\nPUSH_CONST ""\nPUSH_INT 1\nCALL_BUILTIN storage.get 2\nCHECK_TYPE int\nRET\n.end\n')
    with pytest.raises(ContractRuntimeError, match="cannot be stored"):
        await value('.function main public returns=int\nPUSH_CONST "k"\nCALL_BUILTIN paid 0\nCALL_BUILTIN storage.set 2\nPUSH_INT 0\nRET\n.end\n')
    big = json.dumps("x" * 4096)
    with pytest.raises(ContractRuntimeError, match="larger than 4096 bytes"):
        await value(f'.function main public returns=int\nPUSH_CONST "k"\nPUSH_CONST {big}\nCALL_BUILTIN storage.set 2\nPUSH_INT 0\nRET\n.end\n')


async def test_a_failing_storage_set_pays_for_what_it_encoded() -> None:
    tree = "PUSH_TRUE\nBUILD_LIST 1\n" + "DUP\nBUILD_LIST 2\n" * 12
    text = f'.function main public returns=int\n{tree}STORE 0\nPUSH_CONST "k"\nLOAD 0\nCALL_BUILTIN storage.set 2\nPUSH_INT 0\nRET\n.end\n'
    ctx = context(gas=100_000)
    host = FakeHost()
    with pytest.raises(ContractRuntimeError, match="larger than 4096 bytes"):
        await run(text, ctx=ctx, host=host)
    assert host.calls == []
    with pytest.raises(StorageValueError) as info:
        encode_value_counted(_tree(12))
    setup = 1 + 12  # BUILD_LIST 1 and twelve BUILD_LIST 2; every free run in between stays below the allowance
    assert ctx.gas.used == setup + storage_write_gas(1 + info.value.encoded_bytes, info.value.values)
    assert info.value.values > 2000
    record_tail = '.function main public returns=int\nPUSH_CONST "k"\nPUSH_TRUE\nPUSH_TRUE\nCALL_BUILTIN paid 0\nBUILD_LIST 3\nCALL_BUILTIN storage.set 2\nPUSH_INT 0\nRET\n.end\n'
    ctx = context(gas=100_000)
    with pytest.raises(ContractRuntimeError, match="cannot be stored"):
        await run(record_tail, ctx=ctx)
    assert ctx.gas.used == 1 + storage_write_gas(1 + 4, 4)


def _tree(depth: int) -> Value:
    node: Value = [True]
    for _ in range(depth):
        node = [node, node]
    return node


async def test_an_oversized_stored_value_is_a_runtime_error() -> None:
    host = FakeHost()
    host.storage[SELF] = {"k": b"\x02" + bytes(5000)}
    with pytest.raises(ContractRuntimeError, match="larger than 4096 bytes"):
        await value('.function main public returns=int\nPUSH_CONST "k"\nCALL_BUILTIN storage.get 1\nCHECK_TYPE int\nRET\n.end\n', host=host)


async def test_random_is_logged_and_uses_the_host_hook() -> None:
    host = FakeHost()
    text = ".function main public returns=int\nPUSH_INT 1\nPUSH_INT 6\nCALL_BUILTIN random 2\nPUSH_INT 10\nPUSH_INT 10\nCALL_BUILTIN random 2\nADD\nRET\n.end\n"
    ctx = context()
    outcome = await run(text, host=host, ctx=ctx)
    assert len(outcome.random_values) == 2
    assert outcome.random_values[1] == 10
    assert 1 <= outcome.random_values[0] <= 6
    assert outcome.value == sum(outcome.random_values)
    assert ctx.random_log == list(outcome.random_values)
    with pytest.raises(ContractRuntimeError, match="low must not be greater"):
        await value(".function main public returns=int\nPUSH_INT 2\nPUSH_INT 1\nCALL_BUILTIN random 2\nRET\n.end\n")

    class BadRandom(FakeHost):
        def random_below(self, n: int) -> int:
            return n

    with pytest.raises(HostProtocolError):
        await value(text, host=BadRandom())


async def str_call(name: str, *args: str, returns: str = "str") -> object:
    pushes = "\n".join(f"PUSH_CONST {json.dumps(arg)}" for arg in args)
    return await value(f".function main public returns={returns}\n{pushes}\nCALL_BUILTIN {name} {len(args)}\nRET\n.end\n")


async def test_pure_string_builtins() -> None:
    assert await str_call("sha256", "abc") == hashlib.sha256(b"abc").hexdigest()
    assert await str_call("sha256", chr(0x3042)) == hashlib.sha256(chr(0x3042).encode()).hexdigest()
    assert await str_call("str.find", "hello", "l", returns="int") == 2
    assert await str_call("str.startswith", "hello", "he", returns="bool") is True
    assert await str_call("str.endswith", "hello", "lo", returns="bool") is True
    assert await str_call("str.replace", "a-b-c", "-", "+") == "a+b+c"
    assert await str_call("str.upper", "abc") == "ABC"
    assert await str_call("str.lower", "ABC") == "abc"
    assert await str_call("str.strip", "  x ") == "x"
    split = '.function main public returns=int\nPUSH_CONST "a,b,,c"\nPUSH_CONST ","\nCALL_BUILTIN str.split 2\nLEN\nRET\n.end\n'
    assert await value(split) == 4
    join = '.function main public returns=str\nPUSH_CONST "-"\nPUSH_CONST "a"\nPUSH_CONST "b"\nBUILD_LIST 2\nCALL_BUILTIN str.join 2\nRET\n.end\n'
    assert await value(join) == "a-b"
    with pytest.raises(ContractRuntimeError, match="empty separator"):
        await str_call("str.split", "abc", "", returns="int")


async def test_string_builtins_respect_the_length_limit() -> None:
    with pytest.raises(ContractRuntimeError, match="longer than 4096"):
        await str_call("str.replace", "a" * 100, "a", "b" * 50)
    with pytest.raises(ContractRuntimeError, match="longer than 4096"):
        await str_call("str.replace", "x" * 2048, "", "y")
    with pytest.raises(ContractRuntimeError, match="longer than 4096"):
        await str_call("str.upper", chr(0xDF) * 3000)
    many = ",".join(["a"] * 1100)
    with pytest.raises(ContractRuntimeError, match="1024 elements"):
        await str_call("str.split", many, ",", returns="int")
    join = '.function main public returns=str\nPUSH_CONST "-"\nPUSH_INT 1\nBUILD_LIST 1\nCALL_BUILTIN str.join 2\nRET\n.end\n'
    with pytest.raises(ContractRuntimeError, match="items must be str"):
        await value(join)


async def test_numeric_builtins() -> None:
    def call(name: str, *args: int) -> str:
        pushes = "\n".join(f"PUSH_INT {a}" for a in args)
        return f".function main public returns=int\n{pushes}\nCALL_BUILTIN {name} {len(args)}\nRET\n.end\n"

    assert await value(call("min", 3, -2)) == -2
    assert await value(call("max", 3, -2)) == 3
    assert await value(call("abs", -9)) == 9
    to_int = ".function main public returns=int\nPUSH_INT 5\nCALL_BUILTIN Account 1\nTO_INT\nRET\n.end\n"
    assert await value(to_int) == 5
    for bad in (0, -1):
        with pytest.raises(ContractRuntimeError, match="Account"):
            await value(f".function main public returns=int\nPUSH_INT {bad}\nCALL_BUILTIN Account 1\nTO_INT\nRET\n.end\n")
        with pytest.raises(ContractRuntimeError, match="Currency"):
            await value(f".function main public returns=int\nPUSH_INT {bad}\nCALL_BUILTIN Currency 1\nTO_INT\nRET\n.end\n")
    with pytest.raises(ContractRuntimeError, match="Account"):
        await value(f".function main public returns=int\nPUSH_CONST {2**63}\nCALL_BUILTIN Account 1\nTO_INT\nRET\n.end\n")


def test_fake_host_satisfies_the_protocol() -> None:
    from rapidwire.contract.vm import Host

    host: Host = FakeHost()
    assert host.random_below(5) in range(5)
