from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from rapidwire_client import Account, ContractValue, Currency, amount_string, decode_value, encode_args, encode_value, from_units, parse_amount, to_units
from rapidwire_client.amounts import AMOUNT_MAX_UNITS


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (Decimal("12.5"), "12.500"),
        (Decimal("12.500000"), "12.500"),
        (Decimal("0"), "0.000"),
        (Decimal("1E+2"), "100.000"),
        ("0.5", "0.500"),
        ("7", "7.000"),
        ("0.001", "0.001"),
        (from_units(1), "0.001"),
        (from_units(AMOUNT_MAX_UNITS), "99999999999999999999999999999999999.999"),
    ],
)
def test_amount_string_is_canonical(value: Decimal | str, text: str) -> None:
    assert amount_string(value) == text


@pytest.mark.parametrize("value", [1.5, 0.0, 12, True])
def test_floats_and_ints_are_type_errors(value: Any) -> None:
    with pytest.raises(TypeError):
        amount_string(value)


@pytest.mark.parametrize("value", ["00.5", "01", ".5", "1.", "1e3", "-1", " 1", "1.2345", "", Decimal("1.2345"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity"), from_units(AMOUNT_MAX_UNITS + 1)])
def test_invalid_amounts_are_value_errors(value: Decimal | str) -> None:
    with pytest.raises(ValueError):
        amount_string(value)


def test_units_round_trip_exactly_at_every_size() -> None:
    for units in (0, 1, 999, 1000, 12_500, -3000, AMOUNT_MAX_UNITS, -AMOUNT_MAX_UNITS):
        assert to_units(from_units(units)) == units
    assert from_units(12_500) == Decimal("12.5")
    assert str(from_units(AMOUNT_MAX_UNITS)) == "99999999999999999999999999999999999.999"
    with pytest.raises(TypeError):
        from_units(True)


def test_parse_amount() -> None:
    assert parse_amount("-3.000") == Decimal(-3)
    assert str(parse_amount("12.500")) == "12.500"
    for text in ("12.5", "1e3", "12.5000"):
        with pytest.raises(ValueError):
            parse_amount(text)


def test_typed_values_round_trip() -> None:
    value: ContractValue = {"a": Account(5), 7: [1, "x", True, Currency(-2)], Account(1): {"nested": False}}
    encoded = encode_value(value)
    assert encoded["type"] == "dict"
    assert encoded["value"][0] == {"key": {"type": "str", "value": "a"}, "value": {"type": "Account", "value": "5"}}
    assert decode_value(encoded) == value
    assert encode_args([5, "hi", False, Account(3), Currency(4)]) == [
        {"type": "int", "value": "5"},
        {"type": "str", "value": "hi"},
        {"type": "bool", "value": False},
        {"type": "Account", "value": "3"},
        {"type": "Currency", "value": "4"},
    ]


@pytest.mark.parametrize("value", [1.5, None, b"x", (1, 2), {True: 1}, {1.5: 1}])
def test_unencodable_values_are_type_errors(value: Any) -> None:
    with pytest.raises(TypeError):
        encode_value(value)


@pytest.mark.parametrize(
    "obj",
    [
        {"type": "int", "value": 5},
        {"type": "int", "value": "05"},
        {"type": "bool", "value": "true"},
        {"type": "float", "value": "1"},
        {"type": "int", "value": "1", "extra": 1},
        {"type": "dict", "value": [{"key": {"type": "bool", "value": True}, "value": {"type": "int", "value": "1"}}]},
        {"type": "dict", "value": [{"key": {"type": "int", "value": "1"}, "value": {"type": "int", "value": "1"}}] * 2},
    ],
)
def test_malformed_typed_values_are_value_errors(obj: object) -> None:
    with pytest.raises(ValueError):
        decode_value(obj)


def test_ids_must_be_ints() -> None:
    with pytest.raises(TypeError):
        Account(True)
    with pytest.raises(TypeError):
        Currency("1")  # type: ignore[arg-type]
