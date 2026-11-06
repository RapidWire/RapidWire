from __future__ import annotations

import random

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from rapidwire.contract import storage_codec
from rapidwire.contract.errors import ContractRuntimeError
from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.contract.storage_codec import StorageDecodeError, StorageEncodeError, StorageKeyError, canonicalize, decode, encode, encoded_size, entry_size, validate_key
from rapidwire.contract.types import Kind
from rapidwire.contract.values import INT_KEY_MAX, INT_KEY_MIN, Account, Currency, Key, Value, decode_value, encode_value, make_record

INT_BOUND = DEFAULT_LIMITS.int_bound
# HIRAGANA_A takes 3 bytes in UTF-8 and EMOJI 4.
HIRAGANA_A = chr(0x3042)
EMOJI = chr(0x1F600)
ID = st.integers(min_value=-(2**63), max_value=2**63 - 1)
INTS = st.one_of(st.integers(min_value=-1000, max_value=1000), st.integers(min_value=-INT_BOUND + 1, max_value=INT_BOUND - 1))
SCALARS: st.SearchStrategy[Key] = st.one_of(INTS, st.text(max_size=20), st.booleans(), ID.map(Account), ID.map(Currency))
# Keys of one dict share a type, as the static types of the language guarantee.
KEY_STRATEGIES: list[st.SearchStrategy[Key]] = [st.one_of(st.integers(min_value=-1000, max_value=1000), ID), st.text(max_size=8), ID.map(Account), ID.map(Currency)]


# Property tests use a large byte limit so generated values never hit it; the default limit has its own tests.
LARGE = Limits(storage_value_max_bytes=1 << 20)


def _dicts(children: st.SearchStrategy[Value]) -> st.SearchStrategy[Value]:
    return st.sampled_from(KEY_STRATEGIES).flatmap(lambda keys: st.dictionaries(keys, children, max_size=4))


def _values(depth: int) -> st.SearchStrategy[Value]:
    if depth == 0:
        return SCALARS
    children = _values(depth - 1)
    return st.one_of(SCALARS, st.lists(children, max_size=4), _dicts(children))


VALUES = _values(4)


def _same(a: object, b: object) -> bool:
    """Equality that also requires equal types throughout, so True never matches 1 and dict order is compared."""
    if type(a) is not type(b):
        return False
    if isinstance(a, list) and isinstance(b, list):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, dict) and isinstance(b, dict):
        return len(a) == len(b) and all(_same(ka, kb) and _same(va, vb) for (ka, va), (kb, vb) in zip(a.items(), b.items(), strict=True))
    return a == b


def _canonical(value: Value) -> Value:
    """value with every dict reordered by encoded key, the order decode() returns."""
    if isinstance(value, list):
        return [_canonical(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical(item) for key, item in sorted(value.items(), key=lambda entry: encode(entry[0]))}
    return value


@settings(max_examples=300)
@given(VALUES)
def test_round_trip_preserves_types_and_canonical_order(value: Value) -> None:
    data = encode(value, limits=LARGE)
    decoded = decode(data, limits=LARGE)
    assert _same(decoded, _canonical(value))
    assert encode(decoded, limits=LARGE) == data


@settings(max_examples=300)
@given(VALUES)
def test_encoded_size_equals_encoding_length(value: Value) -> None:
    assert encoded_size(value) == len(encode(value, limits=LARGE))


@settings(max_examples=200)
@given(VALUES)
def test_canonical_bytes_are_readable_by_the_vm_decoder(value: Value) -> None:
    assert _same(decode_value(encode(value, limits=LARGE), limits=LARGE), _canonical(value))


@settings(max_examples=200)
@given(VALUES)
def test_canonicalize_reorders_vm_encodings_without_changing_length(value: Value) -> None:
    vm_bytes = encode_value(value, limits=LARGE)
    canonical = canonicalize(vm_bytes, limits=LARGE)
    assert canonical == encode(value, limits=LARGE)
    assert len(canonical) == len(vm_bytes)
    assert _same(decode(vm_bytes, limits=LARGE, canonical=False), value)


def test_bool_dict_keys_are_rejected() -> None:
    for value in ({True: 1}, {False: 1}, [{True: "x"}]):
        with pytest.raises(StorageEncodeError, match="bool cannot be a dict key"):
            encode(value)  # type: ignore[arg-type]
        with pytest.raises(StorageEncodeError, match="bool cannot be a dict key"):
            encoded_size(value)  # type: ignore[arg-type]
    for data in (b"\x08\x01\x03\x01\x00", b"\x08\x01\x04\x01\x00"):
        for canonical in (True, False):
            with pytest.raises(StorageDecodeError, match="dict key is not an int, str, Account or Currency"):
                decode(data, canonical=canonical)
    # A bool is still a storable value.
    assert decode(encode({"flag": True})) == {"flag": True}


def test_int_dict_keys_are_signed_64_bit() -> None:
    edges: dict[Key, Value] = {INT_KEY_MIN: 1, INT_KEY_MAX: 2}
    assert decode(encode(edges)) == edges
    assert encoded_size(edges) == len(encode(edges))
    for key in (INT_KEY_MAX + 1, INT_KEY_MIN - 1, 2**200):
        with pytest.raises(StorageEncodeError, match="int dict keys must be from"):
            encode({key: 1})
        with pytest.raises(StorageEncodeError, match="int dict keys must be from"):
            encoded_size([{key: 1}])
        data = b"\x08\x01" + encode(key) + b"\x03"
        for canonical in (True, False):
            with pytest.raises(StorageDecodeError, match="int dict key out of range"):
                decode(data, canonical=canonical)
        with pytest.raises(StorageDecodeError, match="int dict key out of range"):
            canonicalize(data)
        # Out-of-range ints remain storable as values.
        assert decode(encode([key])) == [key]


def test_canonicalize_rejects_what_lenient_decode_rejects() -> None:
    with pytest.raises(StorageDecodeError, match="duplicate dict key"):
        canonicalize(b"\x08\x02\x02\x01a\x03\x02\x01a\x03")
    assert canonicalize(b"\x08\x02\x02\x01b\x03\x02\x01a\x03") == b"\x08\x02\x02\x01a\x03\x02\x01b\x03"


@settings(max_examples=200)
@given(st.dictionaries(st.text(max_size=6), VALUES, min_size=2, max_size=6), st.randoms(use_true_random=False))
def test_dict_encoding_ignores_insertion_order(value: dict[Key, Value], rng: random.Random) -> None:
    items = list(value.items())
    rng.shuffle(items)
    shuffled: dict[Key, Value] = dict(items)
    assert encode(shuffled, limits=LARGE) == encode(value, limits=LARGE)
    nested_a: Value = [{"x": value, "y": 1}]
    nested_b: Value = [{"y": 1, "x": shuffled}]
    assert encode(nested_a, limits=LARGE) == encode(nested_b, limits=LARGE)


def test_dict_encoding_ignores_insertion_order_of_reversed_keys() -> None:
    forward: dict[Key, Value] = {Account(1): 1, Account(2): 2, Account(300): 3}
    backward: dict[Key, Value] = {Account(300): 3, Account(2): 2, Account(1): 1}
    assert list(forward) != list(backward)
    assert encode(forward) == encode(backward)
    decoded = decode(encode(backward))
    assert isinstance(decoded, dict)
    assert list(decoded) == [Account(1), Account(2), Account(300)]


def test_known_encodings() -> None:
    assert encode(0) == b"\x01\x00"
    assert encode(1) == b"\x01\x01\x01"
    assert encode(-1) == b"\x01\x01\xff"
    assert encode(128) == b"\x01\x02\x80\x00"
    assert encode("") == b"\x02\x00"
    assert encode("a") == b"\x02\x01a"
    assert encode(True) == b"\x03"
    assert encode(False) == b"\x04"
    assert encode(Account(5)) == b"\x05\x01\x05"
    assert encode(Currency(5)) == b"\x06\x01\x05"
    assert encode([]) == b"\x07\x00"
    assert encode({"b": 1, "a": True}) == b"\x08\x02" + b"\x02\x01a\x03" + b"\x02\x01b\x01\x01\x01"


def test_dicts_read_back_in_canonical_key_order() -> None:
    by_str = decode(encode({"aa": 1, "b": 2, "ab": 3, HIRAGANA_A: 4}))
    assert isinstance(by_str, dict)
    assert list(by_str) == ["b", "aa", "ab", HIRAGANA_A]
    by_int = decode(encode({256: 0, -1: 0, 1: 0, 0: 0}))
    assert isinstance(by_int, dict)
    assert list(by_int) == [0, 1, -1, 256]


def test_types_stay_distinct() -> None:
    for value in (1, True, Account(1), Currency(1), "1"):
        decoded = decode(encode(value))
        assert type(decoded) is type(value)
        assert decoded == value
    assert encode(1) != encode(True) != encode(Account(1)) != encode(Currency(1))


@pytest.mark.parametrize(
    "value",
    [
        None,
        1.5,
        b"bytes",
        (1, 2),
        {1, 2},
        object(),
        [None],
        {"a": 1.0},
        {(1, 2): 1},
        {None: 1},
        make_record(Kind.PAID, currency=Currency(1), amount=5),
        [make_record(Kind.PAID, currency=Currency(1), amount=5)],
    ],
)
def test_unstorable_values_rejected(value: object) -> None:
    with pytest.raises(StorageEncodeError):
        encode(value)  # type: ignore[arg-type]
    with pytest.raises(StorageEncodeError):
        encoded_size(value)  # type: ignore[arg-type]


def test_encode_errors_are_contract_runtime_errors() -> None:
    assert issubclass(StorageEncodeError, ContractRuntimeError)
    assert issubclass(StorageKeyError, ContractRuntimeError)


def test_limits_on_encode() -> None:
    small = Limits(storage_value_max_bytes=16, storage_value_max_depth=2, collection_max_length=3, str_max_length=5, int_bits=64)
    assert encode("x" * 5, limits=small) == b"\x02\x05xxxxx"
    with pytest.raises(StorageEncodeError, match="str too long"):
        encode("x" * 6, limits=small)
    with pytest.raises(StorageEncodeError, match="int out of range"):
        encode(2**64, limits=small)
    assert decode(encode(2**64 - 1, limits=small), limits=small) == 2**64 - 1
    with pytest.raises(StorageEncodeError, match="collection longer"):
        encode([1, 2, 3, 4], limits=small)
    assert encode([[1]], limits=small) == b"\x07\x01\x07\x01\x01\x01\x01"
    with pytest.raises(StorageEncodeError, match="nested deeper"):
        encode([[[1]]], limits=small)
    with pytest.raises(StorageEncodeError, match="larger than 16 bytes"):
        encode(["xxxxx", "xxxxx", "xxxxx"], limits=small)
    assert encoded_size(["xxxxx", "xxxxx", "xxxxx"], limits=small) == 23


def test_default_value_limit_is_exact() -> None:
    # A list costs 2 header bytes here and each str of 128 to 16383 ASCII characters 3 header bytes plus its characters.
    limit = DEFAULT_LIMITS.storage_value_max_bytes
    fits: Value = ["x" * 2000, "y" * (limit - 2000 - 2 - 3 - 3)]
    assert len(encode(fits)) == limit
    with pytest.raises(StorageEncodeError):
        encode(["x" * 2000, "y" * (limit - 2000 - 2 - 3 - 2)])


def test_string_encoding_counts_utf8_bytes() -> None:
    assert encoded_size(HIRAGANA_A) == 1 + 1 + 3
    assert decode(encode(HIRAGANA_A + EMOJI)) == HIRAGANA_A + EMOJI
    with pytest.raises(StorageEncodeError, match="not valid Unicode"):
        encode("\ud800")


@pytest.mark.parametrize(
    ("data", "match"),
    [
        (b"", "empty"),
        (b"\x00", "unknown storage tag"),
        (b"\x09", "unknown storage tag"),
        (b"\x01", "truncated|corrupt"),
        (b"\x01\x02\x01", "truncated int"),
        (b"\x01\x01\x00", "shortest form"),
        (b"\x01\x02\x01\x00", "shortest form"),
        (b"\x01\x80\x00", "corrupt"),
        (b"\x02\x02a", "truncated str"),
        (b"\x02\x01\xff", "corrupt"),
        (b"\x03\x03", "trailing bytes"),
        (b"\x07\x02\x03", "collection too large|truncated"),
        (b"\x07\x01", "collection too large|truncated"),
        (b"\x08\x01\x07\x00\x03", "not an int, str, Account or Currency"),
        (b"\x08\x01\x03\x03", "not an int, str, Account or Currency"),
        (b"\x08\x02\x02\x01b\x03\x02\x01a\x03", "canonical order"),
        (b"\x08\x02\x02\x01a\x03\x02\x01a\x03", "canonical order"),
        (b"\x08\x02\x01\x01\x01\x03\x03\x03", "not an int, str, Account or Currency"),
        (b"\x05\x09" + b"\xff" * 8 + b"\x7f", "corrupt"),
    ],
)
def test_malformed_bytes_rejected(data: bytes, match: str) -> None:
    with pytest.raises(StorageDecodeError, match=match):
        decode(data)


def test_decode_applies_limits() -> None:
    small = Limits(storage_value_max_bytes=16, storage_value_max_depth=2, collection_max_length=3, str_max_length=5)
    with pytest.raises(StorageDecodeError, match="larger than"):
        decode(encode("x" * 20), limits=small)
    with pytest.raises(StorageDecodeError, match="str too long"):
        decode(encode("x" * 6), limits=small)
    with pytest.raises(StorageDecodeError, match="collection too large"):
        decode(encode([1, 2, 3, 4]), limits=small)
    with pytest.raises(StorageDecodeError, match="nested too deeply"):
        decode(encode([[[1]]]), limits=small)
    with pytest.raises(StorageDecodeError, match="int out of range"):
        decode(encode(2**80), limits=Limits(int_bits=64))
    with pytest.raises(StorageDecodeError, match="must be bytes"):
        decode("text")  # type: ignore[arg-type]


@settings(max_examples=300)
@given(st.binary(max_size=64))
def test_arbitrary_bytes_decode_canonically_or_fail_cleanly(data: bytes) -> None:
    try:
        value = decode(data)
    except StorageDecodeError:
        return
    assert encode(value) == data


def test_storage_keys() -> None:
    assert validate_key("a") == "a"
    assert validate_key("k" * DEFAULT_LIMITS.storage_key_max_length) == "k" * DEFAULT_LIMITS.storage_key_max_length
    for bad in ("", "k" * (DEFAULT_LIMITS.storage_key_max_length + 1), 1, None, b"a", "\udfff"):
        with pytest.raises(StorageKeyError):
            validate_key(bad)
    assert entry_size("ab", 3) == 5
    assert entry_size(HIRAGANA_A, 3) == 6


def test_module_exports() -> None:
    assert set(storage_codec.__all__) == {"StorageDecodeError", "StorageEncodeError", "StorageKeyError", "canonicalize", "decode", "encode", "encoded_size", "entry_size", "validate_key"}
