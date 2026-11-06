"""Runtime values of the contract VM and the storage encoding of values.

int, str and bool are the plain Python types (bool is never accepted where int is expected); Account and Currency are distinct wrappers, so Account(5) never equals 5 or Currency(5).
list and dict are VM-owned Python containers whose elements are values and whose keys are scalars; lists and dicts have reference semantics as in Python.
Records are flat read-only tuples of scalar fields; the value field of a failed CallResult holds None, which the VM reports as a runtime error when read.
Nothing else is ever a value.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from rapidwire.contract.errors import ContractRuntimeError, ErrorKind, FormatError
from rapidwire.contract.format import decode_signed_int, encode_signed_int, encode_uvarint, read_uvarint
from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.contract.types import KIND_NAMES, RECORD_FIELDS, RECORD_KINDS, Kind, Type

ID_MIN = -(2**63)
ID_MAX = 2**63 - 1


_ACCOUNT_TAG = 1
_CURRENCY_TAG = 2


def _checked_id(kind: str, ident: object) -> int:
    if type(ident) is not int or not ID_MIN <= ident <= ID_MAX:
        raise ValueError(f"{kind} id must be a signed 64-bit int, got {ident!r}")
    return ident


class Account(tuple[int, int]):
    """An account id; equal only to an Account with the same id.

    It is the tuple (1, id), so equality and hashing run in C while an Account never equals an int, a Currency (2, id) or a plain value of another type.
    """

    __slots__ = ()

    def __new__(cls, id: int) -> Account:
        return tuple.__new__(cls, (_ACCOUNT_TAG, _checked_id("account", id)))

    @property
    def id(self) -> int:
        return self[1]

    def __repr__(self) -> str:
        return f"Account({self[1]})"


class Currency(tuple[int, int]):
    """A currency id; equal only to a Currency with the same id; it is the tuple (2, id). Currency(0) is the currency of an empty Paid and names no real currency."""

    __slots__ = ()

    def __new__(cls, id: int) -> Currency:
        return tuple.__new__(cls, (_CURRENCY_TAG, _checked_id("currency", id)))

    @property
    def id(self) -> int:
        return self[1]

    def __repr__(self) -> str:
        return f"Currency({self[1]})"


class Record:
    """A read-only record of a fixed kind; values follow types.RECORD_FIELDS[kind].

    Records compare by identity (the VM never compares them); fields_equal() compares contents.
    """

    __slots__ = ("kind", "values")

    kind: Kind
    values: tuple[Scalar | None, ...]

    def __init__(self, kind: Kind, values: tuple[Scalar | None, ...]) -> None:
        object.__setattr__(self, "kind", kind)
        object.__setattr__(self, "values", values)

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("records are read-only")

    __hash__ = None  # type: ignore[assignment]

    def fields_equal(self, other: object) -> bool:
        """Whether other is a record of the same kind with fields of the same types and values."""
        if not isinstance(other, Record):
            return False
        return self.kind is other.kind and len(self.values) == len(other.values) and all(type(a) is type(b) and a == b for a, b in zip(self.values, other.values, strict=True))

    def field(self, name: str) -> Scalar | None:
        for record_field, value in zip(RECORD_FIELDS[self.kind], self.values, strict=True):
            if record_field.name == name:
                return value
        raise KeyError(name)

    def __repr__(self) -> str:
        fields = ", ".join(f"{f.name}={format_value(v)}" for f, v in zip(RECORD_FIELDS[self.kind], self.values, strict=True))
        return f"{KIND_NAMES[self.kind]}({fields})"


Scalar = int | str | bool | Account | Currency
Key = int | str | Account | Currency
type Value = int | str | bool | Account | Currency | list[Value] | dict[Key, Value] | Record
"""A VM value; containers are typed loosely because the VM checks element types where it uses them."""

SCALAR_TYPES: frozenset[type] = frozenset({int, str, bool, Account, Currency})
# bool is excluded because Python would treat True and 1 as the same dict key.
KEY_TYPES: frozenset[type] = frozenset({int, str, Account, Currency})
PUBLIC_RESULT_TYPES: frozenset[type] = frozenset({int, str, bool})
# Int dict keys are signed 64-bit, so few of them can share a hash and every dict operation stays constant time.
INT_KEY_MIN = -(2**63)
INT_KEY_MAX = 2**63 - 1


def is_key(value: object) -> bool:
    """Whether value can be a dict key: a str, Account, Currency, or an int within INT_KEY_MIN .. INT_KEY_MAX."""
    t = type(value)
    if t is int:
        return INT_KEY_MIN <= value <= INT_KEY_MAX  # type: ignore[operator]
    return t is str or t is Account or t is Currency


def keys_are_valid(keys: list[Any]) -> bool:
    """Whether every element of keys is a valid dict key, using only C-level scans."""
    if not KEY_TYPES.issuperset(map(type, keys)):
        return False
    ints = list(filter(int.__instancecheck__, keys))
    return not ints or (min(ints) >= INT_KEY_MIN and max(ints) <= INT_KEY_MAX)


def contains_is_ambiguous(items: list[Any], needle: Scalar) -> bool:
    """Whether Python's == could confuse needle with an element of another type (a bool needle among ints, or an int 0 or 1 among bools)."""
    tn = type(needle)
    if tn is bool:
        return int in set(map(type, items))
    if tn is int and needle in (0, 1):
        return bool in set(map(type, items))
    return False


def list_contains(items: list[Any], needle: Scalar) -> bool:
    """needle in items with type-strict equality: True never matches 1 and False never matches 0."""
    if contains_is_ambiguous(items, needle):
        tn = type(needle)
        return any(type(item) is tn and item == needle for item in items)
    return needle in items


PYTHON_TYPES: dict[Kind, type] = {
    Kind.INT: int,
    Kind.STR: str,
    Kind.BOOL: bool,
    Kind.ACCOUNT: Account,
    Kind.CURRENCY: Currency,
    Kind.LIST: list,
    Kind.DICT: dict,
}


@dataclass(frozen=True, slots=True)
class Attachment:
    """Currency attached to a call; amount is positive."""

    currency: Currency
    amount: int


def matches(value: object, t: Type) -> bool:
    """Shallow check that value has the outer kind of t: element types are not inspected; Value and type variables accept any value."""
    kind = t.kind
    pytype = PYTHON_TYPES.get(kind)
    if pytype is not None:
        return type(value) is pytype
    if kind in RECORD_KINDS:
        return type(value) is Record and value.kind is kind
    if kind is Kind.VALUE or kind is Kind.VAR:
        return value is not None
    return False


def make_record(kind: Kind, **fields: Scalar | None) -> Record:
    """Build a record of kind from keyword fields; raises ValueError when a field is missing, unknown or of the wrong scalar type."""
    if kind not in RECORD_KINDS:
        raise ValueError(f"{kind.name} is not a record kind")
    layout = RECORD_FIELDS[kind]
    unknown = set(fields) - {f.name for f in layout}
    if unknown:
        raise ValueError(f"unknown fields for {kind.name}: {sorted(unknown)}")
    values: list[Scalar | None] = []
    for record_field in layout:
        if record_field.name not in fields:
            raise ValueError(f"missing field {kind.name}.{record_field.name}")
        value = fields[record_field.name]
        if record_field.type.kind is Kind.VALUE:
            if value is not None and type(value) not in PUBLIC_RESULT_TYPES:
                raise ValueError(f"{kind.name}.{record_field.name} must be int, str, bool or None")
        elif not matches(value, record_field.type):
            raise ValueError(f"{kind.name}.{record_field.name} must be {record_field.type}, got {value!r}")
        values.append(value)
    return Record(kind, tuple(values))


def paid_record(attachment: Attachment | None) -> Record:
    """The Paid record of an attachment; an empty one has Currency(0) and amount 0."""
    if attachment is None:
        return Record(Kind.PAID, (Currency(0), 0))
    return Record(Kind.PAID, (attachment.currency, attachment.amount))


def call_result_ok(value: int | str | bool) -> Record:
    return Record(Kind.CALL_RESULT, (True, value, "", ""))


def call_result_failure(error: ErrorKind, message: str) -> Record:
    return Record(Kind.CALL_RESULT, (False, None, error.value, message))


def record_problem(value: object, kind: Kind, limits: Limits = DEFAULT_LIMITS) -> str | None:
    """Why value is not a well-formed record of kind within limits (as a host must return it), or None."""
    if type(value) is not Record or value.kind is not kind:
        return f"expected a {kind.name} record"
    layout = RECORD_FIELDS[kind]
    if len(value.values) != len(layout):
        return f"{kind.name} record has {len(value.values)} fields"
    for record_field, field_value in zip(layout, value.values, strict=True):
        if record_field.type.kind is Kind.VALUE:
            if field_value is None:
                continue
            problem = scalar_problem(field_value, limits)
        elif not matches(field_value, record_field.type):
            problem = f"field {record_field.name} must be {record_field.type}"
        else:
            problem = scalar_problem(field_value, limits)
        if problem is not None:
            return problem
    return None


def scalar_problem(value: object, limits: Limits = DEFAULT_LIMITS) -> str | None:
    """Why value is not a scalar within limits (an int in range, a str of valid Unicode within the length limit, a bool, Account or Currency), or None."""
    t = type(value)
    if type(value) is int:
        return None if limits.int_ok(value) else "int out of range"
    if type(value) is str:
        if len(value) > limits.str_max_length:
            return "str too long"
        if not value.isascii():
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                return "str is not valid Unicode"
        return None
    if t is bool or t is Account or t is Currency:
        return None
    return f"{t.__name__} is not a scalar value"


def format_value(value: object, depth: int = 0) -> str:
    """A short ASCII rendering of a value for listings and messages."""
    if depth > 4:
        return "..."
    t = type(value)
    if t is str:
        return json.dumps(value, ensure_ascii=True)
    if t is bool or t is int or t is Account or t is Currency or value is None:
        return repr(value)
    if type(value) is list:
        items = [format_value(item, depth + 1) for item in value[:8]]
        if len(value) > 8:
            items.append("...")
        return "[" + ", ".join(items) + "]"
    if type(value) is dict:
        entries = [f"{format_value(k, depth + 1)}: {format_value(v, depth + 1)}" for k, v in list(value.items())[:8]]
        if len(value) > 8:
            entries.append("...")
        return "{" + ", ".join(entries) + "}"
    if t is Record:
        return repr(value)
    return f"<{t.__name__}>"


class StorageDecodeError(ValueError):
    """Stored bytes are not a valid encoded value; the stored data is corrupt, which is a server-side fault."""


_S_INT = 0x01
_S_STR = 0x02
_S_TRUE = 0x03
_S_FALSE = 0x04
_S_ACCOUNT = 0x05
_S_CURRENCY = 0x06
_S_LIST = 0x07
_S_DICT = 0x08


def encode_value(value: Value, *, limits: Limits = DEFAULT_LIMITS) -> bytes:
    """Encode a storable value (scalars, lists and dicts of storable values) for storage.

    Tags: 1 int (uvarint length + two's complement little-endian), 2 str (uvarint length + UTF-8), 3 True, 4 False, 5 Account and 6 Currency (int payload), 7 list (uvarint count + items), 8 dict (uvarint count + key, value pairs in insertion order).
    Raises StorageValueError (a ContractRuntimeError) for a record, nesting deeper than limits.storage_value_max_depth, or an encoding longer than limits.storage_value_max_bytes; encoding stops as soon as the size limit is passed.
    """
    return encode_value_counted(value, limits=limits)[0]


class StorageValueError(ContractRuntimeError):
    """A value cannot be stored; encoded_bytes and values tell how much was encoded before the failure, for charging that work."""

    def __init__(self, message: str, *, encoded_bytes: int, values: int) -> None:
        super().__init__(message)
        self.encoded_bytes = encoded_bytes
        self.values = values


def encode_value_counted(value: Value, *, limits: Limits = DEFAULT_LIMITS) -> tuple[bytes, int]:
    """encode_value() plus the number of values encoded (every scalar, list and dict counts one)."""
    out = bytearray()
    count = [0]
    try:
        _encode_into(out, value, 1, limits, count)
    except ContractRuntimeError as exc:
        raise StorageValueError(exc.message, encoded_bytes=len(out), values=count[0]) from None
    return bytes(out), count[0]


def _encode_int_payload(out: bytearray, value: int) -> None:
    body = encode_signed_int(value)
    out += encode_uvarint(len(body))
    out += body


def _encode_into(out: bytearray, value: Any, depth: int, limits: Limits, count: list[int]) -> None:
    count[0] += 1
    t = type(value)
    if t is int:
        out.append(_S_INT)
        _encode_int_payload(out, value)
    elif t is str:
        body = value.encode("utf-8")
        out.append(_S_STR)
        out += encode_uvarint(len(body))
        out += body
    elif t is bool:
        out.append(_S_TRUE if value else _S_FALSE)
    elif t is Account:
        out.append(_S_ACCOUNT)
        _encode_int_payload(out, value.id)
    elif t is Currency:
        out.append(_S_CURRENCY)
        _encode_int_payload(out, value.id)
    elif t is list or t is dict:
        if depth > limits.storage_value_max_depth:
            raise ContractRuntimeError(f"storage value nested deeper than {limits.storage_value_max_depth}")
        out.append(_S_LIST if t is list else _S_DICT)
        out += encode_uvarint(len(value))
        if t is list:
            for item in value:
                if len(out) > limits.storage_value_max_bytes:
                    break
                _encode_into(out, item, depth + 1, limits, count)
        else:
            for key, item in value.items():
                if len(out) > limits.storage_value_max_bytes:
                    break
                _encode_into(out, key, depth + 1, limits, count)
                _encode_into(out, item, depth + 1, limits, count)
    else:
        raise ContractRuntimeError(f"a {_type_name(value)} cannot be stored")
    if len(out) > limits.storage_value_max_bytes:
        raise ContractRuntimeError(f"storage value larger than {limits.storage_value_max_bytes} bytes")


def _type_name(value: object) -> str:
    if isinstance(value, Record):
        return KIND_NAMES[value.kind]
    return type(value).__name__


def decode_value(data: bytes, *, limits: Limits = DEFAULT_LIMITS) -> Value:
    """Inverse of encode_value; raises StorageDecodeError for bytes encode_value cannot produce within limits."""
    return decode_value_counted(data, limits=limits)[0]


def decode_value_counted(data: bytes, *, limits: Limits = DEFAULT_LIMITS) -> tuple[Value, int]:
    """decode_value() plus the number of values decoded; bytes longer than limits.storage_value_max_bytes are rejected before any decoding."""
    if len(data) > limits.storage_value_max_bytes:
        raise StorageDecodeError("storage value larger than the limit")
    count = [0]
    try:
        value, pos = _decode_from(data, 0, 1, limits, count)
    except (IndexError, ValueError) as exc:
        if isinstance(exc, StorageDecodeError):
            raise
        raise StorageDecodeError(f"corrupt storage value: {exc}") from None
    if pos != len(data):
        raise StorageDecodeError("trailing bytes after storage value")
    return value, count[0]


def _read_uvarint(data: bytes, pos: int) -> tuple[int, int]:
    try:
        return read_uvarint(data, pos, len(data))
    except FormatError as exc:
        raise StorageDecodeError(str(exc)) from None


def _decode_int_payload(data: bytes, pos: int) -> tuple[int, int]:
    length, pos = _read_uvarint(data, pos)
    if length > len(data) - pos:
        raise StorageDecodeError("truncated int")
    value = decode_signed_int(data[pos : pos + length])
    if value is None:
        raise StorageDecodeError("int not in shortest form")
    return value, pos + length


def _decode_from(data: bytes, pos: int, depth: int, limits: Limits, counter: list[int]) -> tuple[Value, int]:
    counter[0] += 1
    tag = data[pos]
    pos += 1
    if tag == _S_INT:
        value, pos = _decode_int_payload(data, pos)
        if not limits.int_ok(value):
            raise StorageDecodeError("int out of range")
        return value, pos
    if tag == _S_STR:
        length, pos = _read_uvarint(data, pos)
        if length > len(data) - pos:
            raise StorageDecodeError("truncated str")
        text = data[pos : pos + length].decode("utf-8", errors="strict")
        if len(text) > limits.str_max_length:
            raise StorageDecodeError("str too long")
        return text, pos + length
    if tag == _S_TRUE:
        return True, pos
    if tag == _S_FALSE:
        return False, pos
    if tag in (_S_ACCOUNT, _S_CURRENCY):
        ident, pos = _decode_int_payload(data, pos)
        return (Account(ident) if tag == _S_ACCOUNT else Currency(ident)), pos
    if tag in (_S_LIST, _S_DICT):
        if depth > limits.storage_value_max_depth:
            raise StorageDecodeError("storage value nested too deeply")
        count, pos = _read_uvarint(data, pos)
        if count > limits.collection_max_length or count > len(data) - pos:
            raise StorageDecodeError("collection too large")
        if tag == _S_LIST:
            items: list[Value] = []
            for _ in range(count):
                item, pos = _decode_from(data, pos, depth + 1, limits, counter)
                items.append(item)
            return items, pos
        entries: dict[Key, Value] = {}
        for _ in range(count):
            key, pos = _decode_from(data, pos, depth + 1, limits, counter)
            if not is_key(key):
                raise StorageDecodeError("dict key is not a 64-bit int, str, Account or Currency")
            item, pos = _decode_from(data, pos, depth + 1, limits, counter)
            if key in entries:
                raise StorageDecodeError("duplicate dict key")
            entries[key] = item  # type: ignore[index]
        return entries, pos
    raise StorageDecodeError(f"unknown storage tag {tag}")
