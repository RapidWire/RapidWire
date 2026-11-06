from __future__ import annotations

from typing import Any

import pytest

from rapidwire.api.contract_values import TypedValueError, decode_typed, encode_typed
from rapidwire.contract import storage_codec
from rapidwire.contract.limits import DEFAULT_LIMITS
from rapidwire.contract.runtime import encode_args
from rapidwire.contract.values import Account, Currency, Value
from rapidwire.core.types import to_json

SCALARS: list[Any] = [0, -5, 2**255, "", "x\xe9\U0001f600", True, False, Account(7), Account(-1), Currency(2**63 - 1)]


def test_scalars_match_the_execution_record() -> None:
    assert [encode_typed(value) for value in SCALARS] == to_json(encode_args(SCALARS))


@pytest.mark.parametrize("value", [*SCALARS, [], [1, "a", [True, Account(2)]], {}, {"b": 1, 3: [Currency(4)], Account(9): {"k": False}}])
def test_round_trip(value: Value) -> None:
    decoded = decode_typed(encode_typed(value))
    assert type(decoded) is type(value)
    assert decoded == value
    assert storage_codec.encode(decoded) == storage_codec.encode(value)


def test_dict_order_is_kept() -> None:
    value: Value = {"bb": 1, "a": 2}
    encoded = encode_typed(value)
    assert [entry["key"]["value"] for entry in encoded["value"]] == ["bb", "a"]
    assert isinstance(decoded := decode_typed(encoded), dict)
    assert list(decoded) == ["bb", "a"]


def _int_key_dict(key: int) -> dict[str, Any]:
    return {"type": "dict", "value": [{"key": {"type": "int", "value": str(key)}, "value": {"type": "bool", "value": True}}]}


@pytest.mark.parametrize("key", [2**63 - 1, -(2**63), 0, -1])
def test_int_keys_span_the_signed_64_bit_range(key: int) -> None:
    decoded = decode_typed(_int_key_dict(key))
    assert decoded == {key: True}
    assert encode_typed(decoded) == _int_key_dict(key)
    assert storage_codec.decode(storage_codec.encode(decoded)) == decoded


@pytest.mark.parametrize("key", [2**63, -(2**63) - 1, 2**64])
def test_int_keys_outside_the_signed_64_bit_range_are_rejected(key: int) -> None:
    with pytest.raises(TypedValueError, match="key"):
        decode_typed(_int_key_dict(key))


def _nested(depth: int) -> dict[str, Any]:
    value: dict[str, Any] = {"type": "int", "value": "1"}
    for _ in range(depth):
        value = {"type": "list", "value": [value]}
    return value


@pytest.mark.parametrize(
    "obj",
    [
        5,
        {"type": "int"},
        {"type": "int", "value": "1", "extra": 1},
        {"type": "int", "value": 1},
        {"type": "int", "value": "01"},
        {"type": "int", "value": "-0"},
        {"type": "int", "value": "+1"},
        {"type": "int", "value": str(2**256)},
        {"type": "int", "value": "1" * 5000},
        {"type": "bool", "value": 1},
        {"type": "str", "value": 1},
        {"type": "str", "value": "x" * (DEFAULT_LIMITS.str_max_length + 1)},
        {"type": "str", "value": "\ud800"},
        {"type": "Account", "value": str(2**63)},
        {"type": "Currency", "value": 3},
        {"type": "None", "value": None},
        {"type": "list", "value": {}},
        {"type": "list", "value": [{"type": "int", "value": "1"}] * (DEFAULT_LIMITS.collection_max_length + 1)},
        {"type": "dict", "value": [{"key": {"type": "bool", "value": True}, "value": {"type": "int", "value": "1"}}]},
        {"type": "dict", "value": [{"key": {"type": "int", "value": str(2**63)}, "value": {"type": "int", "value": "1"}}]},
        {"type": "dict", "value": [{"key": {"type": "str", "value": "a"}, "value": {"type": "int", "value": "1"}}] * 2},
        {"type": "dict", "value": [{"key": {"type": "str", "value": "a"}}]},
        _nested(DEFAULT_LIMITS.storage_value_max_depth + 1),
    ],
)
def test_rejects_malformed_values(obj: object) -> None:
    with pytest.raises(TypedValueError):
        decode_typed(obj)


def test_deepest_nesting_is_accepted_and_storable() -> None:
    value = decode_typed(_nested(DEFAULT_LIMITS.storage_value_max_depth))
    assert storage_codec.decode(storage_codec.encode(value)) == value


def test_records_have_no_encoding() -> None:
    from rapidwire.contract.values import paid_record

    with pytest.raises(TypeError):
        encode_typed(paid_record(None))
