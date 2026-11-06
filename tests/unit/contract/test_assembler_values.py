from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rapidwire.contract.assembler import Assembler, assemble, disassemble, parse
from rapidwire.contract.errors import ContractRuntimeError
from rapidwire.contract.limits import Limits
from rapidwire.contract.types import INT
from rapidwire.contract.values import Account, Currency, StorageDecodeError, Value, decode_value, encode_value
from rapidwire.contract.verifier import verify

SAMPLE = """
.const "unused"
.const type list[int]
.function main public params=int,Account returns=str locals=4
    LOAD 0
    PUSH_INT 1
    ADD
    STORE 2
    PUSH_CONST 123456789012345678901234567890
    POP
loop:
    LOAD 2
    PUSH_INT 0
    GT
    JUMP_IF_FALSE done
    LOAD 2
    PUSH_INT 1
    SUB
    STORE 2
    JUMP loop
done:
    LOAD 1
    PUSH_CONST "echo"
    PUSH_INT 1
    TRY_CALL 0 1
    GET_FIELD CallResult message
    POP
    BUILD_LIST 0
    CHECK_TYPE list[int]
    POP
    LOAD 1
    CALL helper
    RET
.end
.function helper params=Account returns=str
    LOAD 0
    TO_STR
    PUSH_CONST "a,b"
    PUSH_CONST ","
    CALL_BUILTIN str.split 2
    LEN
    TO_STR
    CONCAT
    RET
.end
"""


def test_disassemble_round_trips() -> None:
    data = assemble(SAMPLE)
    verified = verify(data, max_size=10_000)
    listing = disassemble(verified)
    assert "CALL_BUILTIN str.split 2" in listing
    assert "GET_FIELD CallResult message" in listing
    assert "L" in listing and ".function helper params=Account returns=str locals=1" in listing
    assert listing.isascii()
    assert assemble(listing) == data


def test_disassemble_keeps_an_explicit_name_constant() -> None:
    asm = Assembler()
    asm.add_const("main")
    asm.add_const("main")
    asm.function("main", public=True, returns=INT, name_index=1).emit("PUSH_INT", 0).emit("RET")
    data = asm.assemble()
    listing = disassemble(verify(data, max_size=1000))
    assert "name=#1" in listing
    assert assemble(listing) == data


def test_builder_resolves_labels_names_and_constants() -> None:
    asm = Assembler()
    f = asm.function("main", public=True, returns=INT)
    f.emit("PUSH_INT", 5).emit("CALL", "double").emit("JUMP", "end").label("end").emit("RET")
    g = asm.function("double", params=[INT], returns=INT)
    g.emit("LOAD", 0).emit("DUP").emit("ADD").emit("RET")
    verified = verify(asm.assemble(), max_size=1000)
    assert [fn.name for fn in verified.functions] == ["main", "double"]
    assert verified.functions[1].local_count == 1
    assert asm.const("main") == 0


@pytest.mark.parametrize(
    ("text", "message"),
    [
        (".function main\nNOPE\n.end\n", "line 2"),
        (".function main\nPUSH_INT\n.end\n", "takes 1 operands"),
        (".function main\nPUSH_INT 1\n", "missing .end"),
        ("PUSH_INT 1\n", "outside a function"),
        (".function main bogus\n.end\n", "unknown .function option"),
        (".const\n", "bad .const"),
        (".function main\n.const 1\n.end\n", "inside a function"),
    ],
)
def test_parse_errors(text: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        parse(text)


def test_program_errors() -> None:
    with pytest.raises(ValueError, match="unknown function"):
        assemble(".function main\nCALL nowhere\nRET\n.end\n")
    with pytest.raises(ValueError, match="unknown builtin"):
        assemble(".function main\nCALL_BUILTIN nothing 0\nRET\n.end\n")
    with pytest.raises(ValueError, match="undefined labels"):
        assemble(".function main\nJUMP nowhere\nRET\n.end\n")
    with pytest.raises(ValueError, match="unknown record type"):
        assemble(".function main\nGET_FIELD Nope x\nRET\n.end\n")


storable: st.SearchStrategy[Value] = st.recursive(
    st.one_of(
        st.integers(min_value=-(2**255), max_value=2**255),
        st.text(max_size=20),
        st.booleans(),
        st.integers(min_value=-(2**63), max_value=2**63 - 1).map(Account),
        st.integers(min_value=-(2**63), max_value=2**63 - 1).map(Currency),
    ),
    lambda inner: st.one_of(
        st.lists(inner, max_size=5),
        st.dictionaries(st.one_of(st.integers(min_value=-5, max_value=5), st.text(max_size=5), st.integers(min_value=1, max_value=5).map(Account)), inner, max_size=5),
    ),
    max_leaves=20,
)


@settings(max_examples=200)
@given(storable)
def test_storage_encoding_round_trips(value: Value) -> None:
    data = encode_value(value, limits=Limits(storage_value_max_bytes=100_000))
    decoded = decode_value(data, limits=Limits(storage_value_max_bytes=100_000))
    assert decoded == value
    assert _types(decoded) == _types(value)


def _types(value: object) -> object:
    if isinstance(value, list):
        return [_types(item) for item in value]
    if isinstance(value, dict):
        return {(type(k), k): _types(v) for k, v in value.items()}
    return type(value)


def test_storage_encoding_limits() -> None:
    shared: list[Value] = list(range(100))
    alias: list[Value] = [shared] * 1000
    with pytest.raises(ContractRuntimeError, match="larger than 4096 bytes"):
        encode_value(alias)
    cyclic: list[Value] = []
    cyclic.append(cyclic)
    with pytest.raises(ContractRuntimeError, match="nested deeper than 16"):
        encode_value(cyclic)
    nested: Value = 1
    for _ in range(16):
        nested = [nested]
    assert decode_value(encode_value(nested)) == nested
    with pytest.raises(ContractRuntimeError, match="nested deeper"):
        encode_value([nested])


@pytest.mark.parametrize(
    "data",
    [
        b"",
        b"\x09",
        b"\x01",
        b"\x01\x02\x01",
        b"\x01\x02\x00\x00",
        b"\x02\x05ab",
        b"\x02\x01\xff",
        b"\x07\x02\x03",
        b"\x08\x01\x07\x00\x03",
        b"\x08\x02\x03\x03\x03\x03",
        b"\x03\x03",
        b"\x07\x80\x80\x01",
    ],
)
def test_storage_decode_rejects_corrupt_bytes(data: bytes) -> None:
    with pytest.raises(StorageDecodeError):
        decode_value(data)


def test_storage_decode_enforces_value_limits() -> None:
    with pytest.raises(StorageDecodeError):
        decode_value(encode_value("x" * 30, limits=Limits()), limits=Limits(str_max_length=10))
    with pytest.raises(StorageDecodeError):
        decode_value(encode_value([1, 2, 3]), limits=Limits(collection_max_length=2))
    with pytest.raises(StorageDecodeError):
        decode_value(b"\x01\x21" + b"\x01" * 33)
