"""The RWC1 binary format: header, constant pool, function table and code section, with an exact encoder and a decoder that raises only FormatError.

Layout (all fixed-width integers little-endian):

- Header, 20 bytes: magic b"RWC1", u16 format version, u16 flags (must be 0), u32 constant pool length, u32 function table length, u32 code section length; the file is exactly the header plus the three sections in that order.
- Constant pool: entries until the section ends, each a tag byte, a uvarint body length and the body. Tag 1 is a str (strict UTF-8), tag 2 an int (two's complement little-endian in the fewest bytes that hold it, zero being the empty body), tag 3 a type (one type tree filling the body).
- Function table: entries until the section ends, each: uvarint name constant index, flags byte (bit 0 = public, other bits 0), uvarint parameter count, that many type trees, the return type tree, uvarint local count, uvarint code offset and uvarint code length (offset relative to the code section).
- Type tree: the Kind tag byte, followed by the element type for list and the key and value types for dict.
- Code section: instructions (see opcodes); the decoder keeps it as bytes and the verifier decodes it.

Varints are unsigned LEB128 of at most 64 bits in their shortest form, so every accepted binary re-encodes to the same bytes; signed operands use zigzag.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import IntEnum

from rapidwire.contract.errors import FormatError
from rapidwire.contract.types import ARITY, ENCODABLE_KINDS, MAX_TYPE_DEPTH, Kind, Type

MAGIC = b"RWC1"
HEADER = struct.Struct("<4sHHIII")
HEADER_SIZE = HEADER.size
FORMAT_VERSION = 1
SUPPORTED_VERSIONS = frozenset({FORMAT_VERSION})

UVARINT_MAX = 2**64 - 1
UVARINT_MAX_BYTES = 10
SECTION_MAX = 2**32 - 1

FUNCTION_PUBLIC = 0x01


class ConstTag(IntEnum):
    STR = 1
    INT = 2
    TYPE = 3


Constant = str | int | Type


@dataclass(frozen=True, slots=True)
class FunctionEntry:
    """One function table entry; params occupy locals 0 .. len(params) - 1."""

    name_index: int
    public: bool
    params: tuple[Type, ...]
    returns: Type
    local_count: int
    code_offset: int
    code_length: int


@dataclass(frozen=True, slots=True)
class Program:
    """A decoded binary; encode(decode(b)) == b and decode(encode(p)) == p."""

    version: int
    constants: tuple[Constant, ...]
    functions: tuple[FunctionEntry, ...]
    code: bytes
    flags: int = 0


def encode_uvarint(value: int) -> bytes:
    """Unsigned LEB128 of value; raises ValueError outside 0 .. 2**64 - 1."""
    if type(value) is not int or not 0 <= value <= UVARINT_MAX:
        raise ValueError(f"uvarint out of range: {value!r}")
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def read_uvarint(data: bytes, pos: int, end: int) -> tuple[int, int]:
    """Decode a shortest-form uvarint at data[pos:end]; return (value, next position) or raise FormatError."""
    result = 0
    shift = 0
    start = pos
    while True:
        if pos >= end:
            raise FormatError(f"truncated varint at byte {start}")
        if pos - start >= UVARINT_MAX_BYTES:
            raise FormatError(f"varint longer than {UVARINT_MAX_BYTES} bytes at byte {start}")
        byte = data[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        shift += 7
        if not byte & 0x80:
            if byte == 0 and pos - start > 1:
                raise FormatError(f"varint not in shortest form at byte {start}")
            if result > UVARINT_MAX:
                raise FormatError(f"varint exceeds 64 bits at byte {start}")
            return result, pos


def zigzag_encode(value: int) -> int:
    """Map a signed value in -2**63 .. 2**63 - 1 to an unsigned one; raises ValueError outside that range."""
    if type(value) is not int or not -(2**63) <= value < 2**63:
        raise ValueError(f"signed operand out of range: {value!r}")
    return value * 2 if value >= 0 else -value * 2 - 1


def zigzag_decode(value: int) -> int:
    return value >> 1 if not value & 1 else -((value + 1) >> 1)


def encode_signed_int(value: int) -> bytes:
    """Two's complement little-endian in the fewest bytes that hold value; 0 is empty."""
    if value == 0:
        return b""
    length = (value.bit_length() + 8) // 8
    return value.to_bytes(length, "little", signed=True)


def decode_signed_int(body: bytes) -> int | None:
    """Inverse of encode_signed_int, or None when body is not in that shortest form."""
    if not body:
        return 0
    value = int.from_bytes(body, "little", signed=True)
    return value if encode_signed_int(value) == body else None


def encode_type(t: Type) -> bytes:
    """Encode a type tree; raises ValueError for a type variable."""
    if t.kind not in ENCODABLE_KINDS:
        raise ValueError(f"type {t} cannot be encoded")
    return bytes([t.kind]) + b"".join(encode_type(arg) for arg in t.args)


def read_type(data: bytes, pos: int, end: int, depth: int = 1) -> tuple[Type, int]:
    """Decode one type tree at data[pos:end]; raises FormatError for an unknown tag, truncation or nesting deeper than MAX_TYPE_DEPTH."""
    if depth > MAX_TYPE_DEPTH:
        raise FormatError(f"type nesting deeper than {MAX_TYPE_DEPTH} at byte {pos}")
    if pos >= end:
        raise FormatError(f"truncated type at byte {pos}")
    tag = data[pos]
    try:
        kind = Kind(tag)
    except ValueError:
        raise FormatError(f"unknown type tag {tag} at byte {pos}") from None
    if kind not in ENCODABLE_KINDS:
        raise FormatError(f"unknown type tag {tag} at byte {pos}")
    pos += 1
    args: list[Type] = []
    for _ in range(ARITY.get(kind, 0)):
        arg, pos = read_type(data, pos, end, depth + 1)
        args.append(arg)
    return Type(kind, tuple(args)), pos


def encode_constant(constant: Constant) -> bytes:
    if type(constant) is str:
        tag, body = ConstTag.STR, constant.encode("utf-8")
    elif type(constant) is int:
        tag, body = ConstTag.INT, encode_signed_int(constant)
    elif type(constant) is Type:
        tag, body = ConstTag.TYPE, encode_type(constant)
    else:
        raise ValueError(f"not a constant: {constant!r}")
    return bytes([tag]) + encode_uvarint(len(body)) + body


def _encode_function(entry: FunctionEntry) -> bytes:
    parts = [
        encode_uvarint(entry.name_index),
        bytes([FUNCTION_PUBLIC if entry.public else 0]),
        encode_uvarint(len(entry.params)),
        *(encode_type(param) for param in entry.params),
        encode_type(entry.returns),
        encode_uvarint(entry.local_count),
        encode_uvarint(entry.code_offset),
        encode_uvarint(entry.code_length),
    ]
    return b"".join(parts)


def encode(program: Program) -> bytes:
    """Serialize program; raises ValueError when a field cannot be represented."""
    if not 0 <= program.version <= 0xFFFF or not 0 <= program.flags <= 0xFFFF:
        raise ValueError("version and flags must fit in u16")
    pool = b"".join(encode_constant(constant) for constant in program.constants)
    table = b"".join(_encode_function(entry) for entry in program.functions)
    code = bytes(program.code)
    if max(len(pool), len(table), len(code)) > SECTION_MAX:
        raise ValueError("section longer than 2**32 - 1 bytes")
    return HEADER.pack(MAGIC, program.version, program.flags, len(pool), len(table), len(code)) + pool + table + code


def decode(raw: bytes | bytearray | memoryview) -> Program:
    """Parse a binary; raises FormatError (and nothing else) for any malformed input."""
    if not isinstance(raw, bytes | bytearray | memoryview):
        raise FormatError("binary must be bytes")
    data = bytes(raw)
    if len(data) < HEADER_SIZE:
        raise FormatError(f"binary shorter than the {HEADER_SIZE}-byte header")
    magic, version, flags, pool_length, table_length, code_length = HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise FormatError("bad magic, not an RWC1 binary")
    if version not in SUPPORTED_VERSIONS:
        raise FormatError(f"unsupported format version {version}")
    if flags != 0:
        raise FormatError(f"reserved header flags must be 0, got {flags}")
    total = HEADER_SIZE + pool_length + table_length + code_length
    if total != len(data):
        raise FormatError(f"section lengths add up to {total} bytes but the binary has {len(data)}")
    pool_end = HEADER_SIZE + pool_length
    table_end = pool_end + table_length
    constants = _decode_pool(data, HEADER_SIZE, pool_end)
    functions = _decode_table(data, pool_end, table_end)
    return Program(version=version, constants=constants, functions=functions, code=data[table_end:], flags=flags)


def _decode_pool(data: bytes, pos: int, end: int) -> tuple[Constant, ...]:
    constants: list[Constant] = []
    while pos < end:
        start = pos
        tag = data[pos]
        length, pos = read_uvarint(data, pos + 1, end)
        if length > end - pos:
            raise FormatError(f"constant {len(constants)} at byte {start} runs past the constant pool")
        body = data[pos : pos + length]
        pos += length
        if tag == ConstTag.STR:
            try:
                constants.append(body.decode("utf-8", errors="strict"))
            except UnicodeDecodeError:
                raise FormatError(f"constant {len(constants)} is not valid UTF-8") from None
        elif tag == ConstTag.INT:
            value = decode_signed_int(body)
            if value is None:
                raise FormatError(f"constant {len(constants)} is an int not in shortest form")
            constants.append(value)
        elif tag == ConstTag.TYPE:
            t, type_end = read_type(body, 0, len(body))
            if type_end != len(body):
                raise FormatError(f"constant {len(constants)} has bytes after its type")
            constants.append(t)
        else:
            raise FormatError(f"constant {len(constants)} has unknown tag {tag}")
    return tuple(constants)


def _decode_table(data: bytes, pos: int, end: int) -> tuple[FunctionEntry, ...]:
    functions: list[FunctionEntry] = []
    while pos < end:
        name_index, pos = read_uvarint(data, pos, end)
        if pos >= end:
            raise FormatError(f"function {len(functions)} is truncated")
        flags = data[pos]
        pos += 1
        if flags & ~FUNCTION_PUBLIC:
            raise FormatError(f"function {len(functions)} has undefined flag bits {flags:#04x}")
        param_count, pos = read_uvarint(data, pos, end)
        if param_count > end - pos:
            raise FormatError(f"function {len(functions)} is truncated")
        params: list[Type] = []
        for _ in range(param_count):
            param, pos = read_type(data, pos, end)
            params.append(param)
        returns, pos = read_type(data, pos, end)
        local_count, pos = read_uvarint(data, pos, end)
        code_offset, pos = read_uvarint(data, pos, end)
        code_length, pos = read_uvarint(data, pos, end)
        functions.append(FunctionEntry(name_index, bool(flags & FUNCTION_PUBLIC), tuple(params), returns, local_count, code_offset, code_length))
    return tuple(functions)
