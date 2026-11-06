"""Canonical storage encoding of VM values: every storable value has exactly one encoding, so equal values always produce equal bytes and decode(encode(v)) == v.

The tag layout is the one values.encode_value uses: 1 int (uvarint length + shortest two's complement little-endian), 2 str (uvarint length + UTF-8), 3 True, 4 False, 5 Account and 6 Currency (int payload), 7 list (uvarint count + items), 8 dict (uvarint count + key, value pairs).
Dict entries are ordered by the bytes of their encoded keys, whatever the insertion order, and decode() requires that order, so every accepted byte string re-encodes to itself; canonicalize() turns the insertion-ordered bytes of values.encode_value into this form without changing their length.
Dict keys are str, Account or Currency values or ints from -2**63 to 2**63 - 1 (values.is_key); a bool key or a larger int key is rejected by both encode() and decode().
A stored dict therefore reads back in canonical key order, not in the order its entries were inserted: the order of the encoded key bytes, which for str keys shorter than 128 UTF-8 bytes means shorter keys first and equal lengths in byte order, and for int, Account and Currency keys follows the little-endian encoding rather than numeric order.
Storage keys are separate from values: a key is a str of 1 to limits.storage_key_max_length characters, and an entry's accounted size is the UTF-8 length of its key plus the length of its encoded value.
"""

from __future__ import annotations

from rapidwire.contract.errors import ContractRuntimeError, FormatError
from rapidwire.contract.format import decode_signed_int, encode_signed_int, encode_uvarint, read_uvarint
from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.contract.values import INT_KEY_MAX, INT_KEY_MIN, KEY_TYPES, Account, Currency, Key, StorageDecodeError, Value, is_key

__all__ = [
    "StorageDecodeError",
    "StorageEncodeError",
    "StorageKeyError",
    "canonicalize",
    "decode",
    "encode",
    "encoded_size",
    "entry_size",
    "validate_key",
]

TAG_INT = 0x01
TAG_STR = 0x02
TAG_TRUE = 0x03
TAG_FALSE = 0x04
TAG_ACCOUNT = 0x05
TAG_CURRENCY = 0x06
TAG_LIST = 0x07
TAG_DICT = 0x08

_STORABLE_TYPES: frozenset[type] = frozenset({int, str, bool, Account, Currency, list, dict})


class StorageEncodeError(ContractRuntimeError):
    """The value cannot be stored: it contains a record or another non-storable object, breaks a value limit, nests too deeply, or encodes to more than limits.storage_value_max_bytes."""


class StorageKeyError(ContractRuntimeError):
    """The storage key is not a str of 1 to limits.storage_key_max_length characters of valid Unicode."""


def validate_key(key: object, *, limits: Limits = DEFAULT_LIMITS) -> str:
    """Return key when it can name a storage entry, else raise StorageKeyError."""
    if type(key) is not str or not isinstance(key, str):
        raise StorageKeyError("storage keys must be str")
    if not 1 <= len(key) <= limits.storage_key_max_length:
        raise StorageKeyError(f"storage keys must be 1 to {limits.storage_key_max_length} characters")
    try:
        key.encode("utf-8")
    except UnicodeEncodeError:
        raise StorageKeyError("storage key is not valid Unicode") from None
    return key


def entry_size(key: str, value_size: int) -> int:
    """The accounted size of a storage entry: UTF-8 bytes of key plus value_size, the length of its encoded value."""
    return len(key.encode("utf-8")) + value_size


def encode(value: Value, *, limits: Limits = DEFAULT_LIMITS) -> bytes:
    """The canonical bytes of value; raises StorageEncodeError when value is not storable within limits, stopping soon after the size limit is passed."""
    data = _encode(value, 1, limits)
    if len(data) > limits.storage_value_max_bytes:
        raise StorageEncodeError(f"storage value larger than {limits.storage_value_max_bytes} bytes")
    return data


def encoded_size(value: Value, *, limits: Limits = DEFAULT_LIMITS) -> int:
    """len(encode(value)) computed without building the encoding and without applying the size limit; raises StorageEncodeError for every other reason encode() would."""
    return _size(value, 1, limits)


def decode(data: bytes, *, limits: Limits = DEFAULT_LIMITS, canonical: bool = True) -> Value:
    """The value whose canonical encoding is data; raises StorageDecodeError for anything encode() cannot produce within limits, including non-canonical forms.

    With canonical False, dict entries may come in any order (as values.encode_value writes them, in insertion order) and keep it; everything else is checked the same way.
    """
    if not isinstance(data, bytes | bytearray | memoryview):
        raise StorageDecodeError("storage value must be bytes")
    raw = bytes(data)
    if not raw:
        raise StorageDecodeError("empty storage value")
    if len(raw) > limits.storage_value_max_bytes:
        raise StorageDecodeError(f"storage value larger than {limits.storage_value_max_bytes} bytes")
    try:
        value, pos = _decode(raw, 0, 1, limits, canonical)
    except StorageDecodeError:
        raise
    except (IndexError, ValueError, FormatError) as exc:
        raise StorageDecodeError(f"corrupt storage value: {exc}") from None
    if pos != len(raw):
        raise StorageDecodeError("trailing bytes after storage value")
    return value


def canonicalize(data: bytes, *, limits: Limits = DEFAULT_LIMITS) -> bytes:
    """The canonical encoding of the value data encodes in any dict order, of the same length as data; raises StorageDecodeError like decode(data, canonical=False)."""
    return encode(decode(data, limits=limits, canonical=False), limits=limits)


def _int_payload(value: int) -> bytes:
    body = encode_signed_int(value)
    return encode_uvarint(len(body)) + body


def _int_payload_size(value: int) -> int:
    length = len(encode_signed_int(value))
    return len(encode_uvarint(length)) + length


def _check_int(value: int, limits: Limits) -> None:
    if not limits.int_ok(value):
        raise StorageEncodeError("int out of range")


def _str_body(value: str, limits: Limits) -> bytes:
    if len(value) > limits.str_max_length:
        raise StorageEncodeError("str too long")
    try:
        return value.encode("utf-8")
    except UnicodeEncodeError:
        raise StorageEncodeError("str is not valid Unicode") from None


def _check_collection(value: list[Value] | dict[Key, Value], depth: int, limits: Limits) -> None:
    if depth > limits.storage_value_max_depth:
        raise StorageEncodeError(f"storage value nested deeper than {limits.storage_value_max_depth}")
    if len(value) > limits.collection_max_length:
        raise StorageEncodeError(f"collection longer than {limits.collection_max_length}")


def _type_name(value: object) -> str:
    kind = getattr(value, "kind", None)
    name = getattr(kind, "name", None)
    return name if isinstance(name, str) else type(value).__name__


def _key_problem(key: object) -> str | None:
    """Why key cannot be a dict key (the rule of values.is_key), or None."""
    if is_key(key):
        return None
    if type(key) is int:
        return f"int dict keys must be from {INT_KEY_MIN} to {INT_KEY_MAX}"
    return f"a {_type_name(key)} cannot be a dict key"


def _require_key(key: object) -> None:
    problem = _key_problem(key)
    if problem is not None:
        raise StorageEncodeError(problem)


def _require_storable(value: object) -> None:
    # Exact types only: subclasses of the Python types are never VM values.
    if type(value) not in _STORABLE_TYPES:
        raise StorageEncodeError(f"a {_type_name(value)} cannot be stored")


def _encode(value: object, depth: int, limits: Limits) -> bytes:
    _require_storable(value)
    if isinstance(value, bool):
        return bytes([TAG_TRUE if value else TAG_FALSE])
    if isinstance(value, int):
        _check_int(value, limits)
        return bytes([TAG_INT]) + _int_payload(value)
    if isinstance(value, str):
        body = _str_body(value, limits)
        return bytes([TAG_STR]) + encode_uvarint(len(body)) + body
    if isinstance(value, Account | Currency):
        return bytes([TAG_ACCOUNT if isinstance(value, Account) else TAG_CURRENCY]) + _int_payload(value.id)
    if isinstance(value, list):
        _check_collection(value, depth, limits)
        out = bytearray([TAG_LIST])
        out += encode_uvarint(len(value))
        for item in list(value):
            out += _encode(item, depth + 1, limits)
            _check_budget(len(out), limits)
        return bytes(out)
    if isinstance(value, dict):
        _check_collection(value, depth, limits)
        entries: list[tuple[bytes, bytes]] = []
        total = 1 + len(encode_uvarint(len(value)))
        for key, item in list(value.items()):
            _require_key(key)
            encoded_key = _encode(key, depth + 1, limits)
            encoded_item = _encode(item, depth + 1, limits)
            total += len(encoded_key) + len(encoded_item)
            _check_budget(total, limits)
            entries.append((encoded_key, encoded_item))
        entries.sort(key=lambda entry: entry[0])
        out = bytearray([TAG_DICT])
        out += encode_uvarint(len(value))
        for encoded_key, encoded_item in entries:
            out += encoded_key
            out += encoded_item
        return bytes(out)
    raise StorageEncodeError(f"a {_type_name(value)} cannot be stored")


def _check_budget(size: int, limits: Limits) -> None:
    if size > limits.storage_value_max_bytes:
        raise StorageEncodeError(f"storage value larger than {limits.storage_value_max_bytes} bytes")


def _size(value: object, depth: int, limits: Limits) -> int:
    _require_storable(value)
    if isinstance(value, bool):
        return 1
    if isinstance(value, int):
        _check_int(value, limits)
        return 1 + _int_payload_size(value)
    if isinstance(value, str):
        length = len(_str_body(value, limits))
        return 1 + len(encode_uvarint(length)) + length
    if isinstance(value, Account | Currency):
        return 1 + _int_payload_size(value.id)
    if isinstance(value, list):
        _check_collection(value, depth, limits)
        return 1 + len(encode_uvarint(len(value))) + sum(_size(item, depth + 1, limits) for item in list(value))
    if isinstance(value, dict):
        _check_collection(value, depth, limits)
        total = 1 + len(encode_uvarint(len(value)))
        for key, item in list(value.items()):
            _require_key(key)
            total += _size(key, depth + 1, limits) + _size(item, depth + 1, limits)
        return total
    raise StorageEncodeError(f"a {_type_name(value)} cannot be stored")


def _read_uvarint(data: bytes, pos: int) -> tuple[int, int]:
    return read_uvarint(data, pos, len(data))


def _read_int_payload(data: bytes, pos: int) -> tuple[int, int]:
    length, pos = _read_uvarint(data, pos)
    if length > len(data) - pos:
        raise StorageDecodeError("truncated int")
    value = decode_signed_int(data[pos : pos + length])
    if value is None:
        raise StorageDecodeError("int not in shortest form")
    return value, pos + length


def _decode(data: bytes, pos: int, depth: int, limits: Limits, canonical: bool) -> tuple[Value, int]:
    if pos >= len(data):
        raise StorageDecodeError("truncated storage value")
    tag = data[pos]
    pos += 1
    if tag == TAG_INT:
        number, pos = _read_int_payload(data, pos)
        if not limits.int_ok(number):
            raise StorageDecodeError("int out of range")
        return number, pos
    if tag == TAG_STR:
        length, pos = _read_uvarint(data, pos)
        if length > len(data) - pos:
            raise StorageDecodeError("truncated str")
        text = data[pos : pos + length].decode("utf-8", errors="strict")
        if len(text) > limits.str_max_length:
            raise StorageDecodeError("str too long")
        return text, pos + length
    if tag == TAG_TRUE:
        return True, pos
    if tag == TAG_FALSE:
        return False, pos
    if tag in (TAG_ACCOUNT, TAG_CURRENCY):
        ident, pos = _read_int_payload(data, pos)
        return (Account(ident) if tag == TAG_ACCOUNT else Currency(ident)), pos
    if tag in (TAG_LIST, TAG_DICT):
        if depth > limits.storage_value_max_depth:
            raise StorageDecodeError("storage value nested too deeply")
        count, pos = _read_uvarint(data, pos)
        if count > limits.collection_max_length or count > len(data) - pos:
            raise StorageDecodeError("collection too large")
        if tag == TAG_LIST:
            items: list[Value] = []
            for _ in range(count):
                item, pos = _decode(data, pos, depth + 1, limits, canonical)
                items.append(item)
            return items, pos
        entries: dict[Key, Value] = {}
        previous: bytes | None = None
        for _ in range(count):
            start = pos
            key, pos = _decode(data, pos, depth + 1, limits, canonical)
            if type(key) not in KEY_TYPES or not isinstance(key, int | str | Account | Currency):
                raise StorageDecodeError("dict key is not an int, str, Account or Currency")
            if not is_key(key):
                raise StorageDecodeError(f"int dict key out of range {INT_KEY_MIN} to {INT_KEY_MAX}")
            raw_key = data[start:pos]
            if canonical and previous is not None and raw_key <= previous:
                raise StorageDecodeError("dict keys not in canonical order")
            if key in entries:
                raise StorageDecodeError("duplicate dict key")
            previous = raw_key
            item, pos = _decode(data, pos, depth + 1, limits, canonical)
            entries[key] = item
        return entries, pos
    raise StorageDecodeError(f"unknown storage tag {tag}")
