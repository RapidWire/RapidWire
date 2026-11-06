from __future__ import annotations

import struct

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rapidwire.contract.errors import FormatError
from rapidwire.contract.format import (
    HEADER,
    HEADER_SIZE,
    MAGIC,
    Constant,
    FunctionEntry,
    Program,
    decode,
    decode_signed_int,
    encode,
    encode_signed_int,
    encode_type,
    encode_uvarint,
    read_uvarint,
    zigzag_decode,
    zigzag_encode,
)
from rapidwire.contract.types import ENCODABLE_KINDS, INT, MAX_TYPE_DEPTH, STR, Kind, Type, dict_of, list_of, parse_type, type_var

leaf_types = st.sampled_from([Type(kind) for kind in ENCODABLE_KINDS if kind not in (Kind.LIST, Kind.DICT)])
types = st.recursive(leaf_types, lambda inner: st.one_of(inner.map(list_of), st.tuples(inner, inner).map(lambda kv: dict_of(*kv))), max_leaves=6).filter(lambda t: t.depth() <= MAX_TYPE_DEPTH)
constants: st.SearchStrategy[Constant] = st.one_of(st.text(max_size=40), st.integers(min_value=-(2**300), max_value=2**300), types)
uvarints = st.integers(min_value=0, max_value=2**64 - 1)
entries = st.builds(
    FunctionEntry,
    name_index=uvarints,
    public=st.booleans(),
    params=st.lists(types, max_size=5).map(tuple),
    returns=types,
    local_count=uvarints,
    code_offset=uvarints,
    code_length=uvarints,
)
programs = st.builds(
    Program,
    version=st.just(1),
    constants=st.lists(constants, max_size=8).map(tuple),
    functions=st.lists(entries, max_size=5).map(tuple),
    code=st.binary(max_size=64),
)


@settings(max_examples=300)
@given(programs)
def test_round_trip(program: Program) -> None:
    data = encode(program)
    assert decode(data) == program
    assert encode(decode(data)) == data


@given(uvarints)
def test_uvarint_round_trip(value: int) -> None:
    data = encode_uvarint(value)
    assert read_uvarint(data, 0, len(data)) == (value, len(data))


@given(st.integers(min_value=-(2**63), max_value=2**63 - 1))
def test_zigzag_round_trip(value: int) -> None:
    assert zigzag_decode(zigzag_encode(value)) == value


@given(st.integers(min_value=-(2**600), max_value=2**600))
def test_signed_int_round_trip(value: int) -> None:
    assert decode_signed_int(encode_signed_int(value)) == value


def test_header_layout() -> None:
    program = Program(version=1, constants=("main",), functions=(FunctionEntry(0, True, (INT,), STR, 1, 0, 1),), code=b"\x26")
    data = encode(program)
    magic, version, flags, pool, table, code = struct.unpack_from("<4sHHIII", data, 0)
    assert (magic, version, flags) == (b"RWC1", 1, 0)
    assert HEADER_SIZE == 20
    assert pool == 6 and code == 1
    assert len(data) == HEADER_SIZE + pool + table + code
    assert data[HEADER_SIZE:].startswith(b"\x01\x04main")
    assert data[HEADER_SIZE + pool :].startswith(b"\x00\x01\x01\x01\x02\x01\x00\x01")


def test_encoder_rejects_unrepresentable_programs() -> None:
    with pytest.raises(ValueError):
        encode(Program(version=1, constants=(type_var("T"),), functions=(), code=b""))
    with pytest.raises(ValueError):
        encode(Program(version=1, constants=(1.5,), functions=(), code=b""))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        encode(Program(version=70000, constants=(), functions=(), code=b""))
    with pytest.raises(ValueError):
        encode_uvarint(-1)
    with pytest.raises(ValueError):
        zigzag_encode(2**63)


def header(pool: bytes = b"", table: bytes = b"", code: bytes = b"", *, magic: bytes = MAGIC, version: int = 1, flags: int = 0) -> bytes:
    return HEADER.pack(magic, version, flags, len(pool), len(table), len(code)) + pool + table + code


def fn_entry(params: bytes = b"\x00", returns: bytes = b"\x01", rest: bytes = b"\x00\x00\x01") -> bytes:
    return b"\x00\x01" + params + returns + rest


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"RWC", "shorter than"),
        (header(magic=b"RWC2"), "bad magic"),
        (header(version=2), "unsupported format version 2"),
        (header(flags=1), "reserved header flags"),
        (header() + b"\x00", "add up to"),
        (header(b"\x01\x00")[:-1], "add up to"),
        (header(b"\x01"), "truncated varint"),
        (header(b"\x01\x80\x00"), "shortest form"),
        (header(b"\x01" + b"\xff" * 9 + b"\x02"), "exceeds 64 bits"),
        (header(b"\x01" + b"\x80" * 10 + b"\x01"), "longer than 10 bytes"),
        (header(b"\x01\x05ab"), "runs past"),
        (header(b"\x01\x01\xff"), "not valid UTF-8"),
        (header(b"\x01\x03\xed\xa0\x80"), "not valid UTF-8"),
        (header(b"\x02\x02\x01\x00"), "not in shortest form"),
        (header(b"\x02\x01\x00"), "not in shortest form"),
        (header(b"\x03\x02\x01\x01"), "bytes after its type"),
        (header(b"\x03\x01\x63"), "unknown type tag 99"),
        (header(b"\x03\x01\xff"), "unknown type tag 255"),
        (header(b"\x03\x01\x06"), "truncated type"),
        (header(b"\x03\x09" + b"\x06" * 8 + b"\x01"), "nesting deeper"),
        (header(b"\x09\x00"), "unknown tag 9"),
        (header(table=b"\x00"), "truncated"),
        (header(table=b"\x00\x02"), "undefined flag bits"),
        (header(table=b"\x00\x01\x05\x01"), "truncated"),
        (header(table=fn_entry(params=b"\x01\x0e")), "unknown type tag 14"),
        (header(table=fn_entry(rest=b"\x00\x00")), "truncated varint"),
        (header(table=fn_entry(returns=b"")), "truncated"),
    ],
)
def test_format_errors(data: bytes, message: str) -> None:
    with pytest.raises(FormatError, match=message):
        decode(data)


def test_every_supported_version_has_an_instruction_set() -> None:
    from rapidwire.contract.format import SUPPORTED_VERSIONS
    from rapidwire.contract.opcodes import INSTRUCTION_SETS

    assert frozenset(INSTRUCTION_SETS) == SUPPORTED_VERSIONS
    assert all(iset.version == version for version, iset in INSTRUCTION_SETS.items())


def test_decode_rejects_non_bytes() -> None:
    with pytest.raises(FormatError):
        decode("RWC1")  # type: ignore[arg-type]


def test_decode_accepts_bytearray_and_memoryview() -> None:
    data = encode(Program(version=1, constants=("x",), functions=(), code=b""))
    assert decode(bytearray(data)) == decode(memoryview(data)) == decode(data)


def test_type_text_round_trip() -> None:
    for text in ["int", "list[dict[str,Account]]", "dict[Currency,list[Value]]", "CallResult", "None"]:
        assert str(parse_type(text)) == text
        assert encode_type(parse_type(text))
    assert parse_type(" dict[ str , int ] ") == dict_of(STR, INT)
    assert parse_type("dict[K,V]") == dict_of(type_var("K"), type_var("V"))
    for bad in ["list", "list[int", "dict[int]", "Foo", "int]", ""]:
        with pytest.raises(ValueError):
            parse_type(bad)
