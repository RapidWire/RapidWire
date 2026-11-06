from __future__ import annotations

from typing import Annotated, Any

import pytest
from pydantic import BaseModel, ValidationError

from rapidwire.api.formats import (
    BIGINT_MIN,
    MINIMAL_UNIT,
    Amount,
    IdStr,
    IntOut,
    IntString,
    NonNegativeAmount,
    NumberFormatError,
    PositiveAmount,
    PositiveIdStr,
    format_amount,
    parse_amount,
    parse_int,
)
from rapidwire.core.validation import BIGINT_MAX
from rapidwire.db.types import AMOUNT_MAX, AMOUNT_MIN


class AmountIn(BaseModel):
    amount: PositiveAmount


class MaybeZeroIn(BaseModel):
    amount: NonNegativeAmount


class IdsIn(BaseModel):
    account_id: IdStr
    currency_id: PositiveIdStr
    gas: Annotated[int, IntString(1, 500)]


class Out(BaseModel):
    amount: Amount
    account_id: IntOut
    maybe: Amount | None = None


@pytest.mark.parametrize(
    ("text", "units"),
    [("0.001", 1), ("1", 1000), ("1.5", 1500), ("1.50", 1500), ("1.500", 1500), ("12.345", 12345), ("0.1", 100), ("100000", 100_000_000)],
)
def test_amount_strings_convert_exactly(text: str, units: int) -> None:
    assert parse_amount(text, allow_zero=False) == units
    assert AmountIn.model_validate({"amount": text}).amount == units


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        (1, "amount_type"),
        (1.5, "amount_type"),
        (True, "amount_type"),
        (None, "amount_type"),
        ("1.2345", "amount_format"),
        ("-1", "amount_negative"),
        ("-0.001", "amount_negative"),
        ("+1", "amount_format"),
        ("1e3", "amount_format"),
        (" 1", "amount_format"),
        ("1 ", "amount_format"),
        ("01", "amount_format"),
        ("00.5", "amount_format"),
        (".5", "amount_format"),
        ("1.", "amount_format"),
        ("", "amount_format"),
        ("1,000", "amount_format"),
        (chr(0xFF11), "amount_format"),
        ("NaN", "amount_format"),
        ("0", "amount_zero"),
        ("0.000", "amount_zero"),
        ("9" * 60, "amount_range"),
    ],
)
def test_invalid_amounts_are_rejected(value: object, kind: str) -> None:
    with pytest.raises(NumberFormatError) as caught:
        parse_amount(value, allow_zero=False)
    assert caught.value.kind == kind
    with pytest.raises(ValidationError) as invalid:
        AmountIn.model_validate({"amount": value})
    assert invalid.value.errors()[0]["type"] == kind


def test_zero_is_allowed_only_for_non_negative_amounts() -> None:
    assert MaybeZeroIn.model_validate({"amount": "0"}).amount == 0
    assert MaybeZeroIn.model_validate({"amount": "0.000"}).amount == 0
    with pytest.raises(ValidationError):
        MaybeZeroIn.model_validate({"amount": "-1"})
    with pytest.raises(ValidationError):
        MaybeZeroIn.model_validate({"amount": 0})


def test_amount_range_ends_at_amount_max() -> None:
    whole, fraction = divmod(AMOUNT_MAX, 1000)
    largest = f"{whole}.{fraction:03d}"
    assert parse_amount(largest, allow_zero=False) == AMOUNT_MAX
    above = f"{whole}.{fraction + 1:03d}" if fraction < 999 else f"{whole + 1}"
    with pytest.raises(NumberFormatError) as caught:
        parse_amount(above, allow_zero=False)
    assert caught.value.kind == "amount_range"


def test_minimal_unit_is_one() -> None:
    assert parse_amount(MINIMAL_UNIT, allow_zero=False) == 1


@pytest.mark.parametrize(("units", "text"), [(0, "0.000"), (1, "0.001"), (12500, "12.500"), (-3000, "-3.000"), (-1, "-0.001"), (AMOUNT_MAX, "9" * 35 + ".999"), (AMOUNT_MIN, "-" + "9" * 35 + ".999")])
def test_canonical_amount_output(units: int, text: str) -> None:
    assert format_amount(units) == text
    assert Out(amount=units, account_id=1).model_dump(mode="json")["amount"] == text
    assert Out(amount=units, account_id=1).model_dump()["amount"] == text


def test_canonical_output_round_trips() -> None:
    for units in (1, 999, 1000, 1001, 123456789, AMOUNT_MAX):
        assert parse_amount(format_amount(units), allow_zero=False) == units


def test_output_types_take_ints_and_emit_strings() -> None:
    dumped = Out(amount=5, account_id=-7).model_dump(mode="json")
    assert dumped == {"amount": "0.005", "account_id": "-7", "maybe": None}
    with pytest.raises(ValidationError):
        Out(amount=AMOUNT_MAX + 1, account_id=1)
    with pytest.raises(ValidationError):
        Out.model_validate({"amount": True, "account_id": 1})
    with pytest.raises(ValidationError):
        Out.model_validate({"amount": 1, "account_id": "1"})


def test_id_strings_convert_within_range() -> None:
    parsed = IdsIn.model_validate({"account_id": "-1", "currency_id": str(BIGINT_MAX), "gas": "500"})
    assert (parsed.account_id, parsed.currency_id, parsed.gas) == (-1, BIGINT_MAX, 500)
    assert IdsIn.model_validate({"account_id": str(BIGINT_MIN), "currency_id": "1", "gas": "1"}).account_id == BIGINT_MIN
    assert parsed.model_dump(mode="json") == {"account_id": "-1", "currency_id": str(BIGINT_MAX), "gas": "500"}


@pytest.mark.parametrize(
    ("field", "value", "kind"),
    [
        ("account_id", 1, "int_string_type"),
        ("account_id", 1.0, "int_string_type"),
        ("account_id", True, "int_string_type"),
        ("account_id", "01", "int_string_format"),
        ("account_id", "-0", "int_string_format"),
        ("account_id", "+1", "int_string_format"),
        ("account_id", "1.0", "int_string_format"),
        ("account_id", " 1", "int_string_format"),
        ("account_id", "1e3", "int_string_format"),
        ("account_id", "", "int_string_format"),
        ("account_id", str(BIGINT_MAX + 1), "int_string_range"),
        ("account_id", str(BIGINT_MIN - 1), "int_string_range"),
        ("currency_id", "0", "int_string_range"),
        ("gas", "501", "int_string_range"),
    ],
)
def test_invalid_id_strings_are_rejected(field: str, value: object, kind: str) -> None:
    payload: dict[str, Any] = {"account_id": "1", "currency_id": "1", "gas": "1", field: value}
    with pytest.raises(ValidationError) as invalid:
        IdsIn.model_validate(payload)
    errors = invalid.value.errors(include_input=False)
    assert [(error["loc"], error["type"]) for error in errors] == [((field,), kind)]


def test_parse_int_messages_never_contain_the_input() -> None:
    for value in ("secret-123", "99999999999999999999999999"):
        with pytest.raises(NumberFormatError) as caught:
            parse_int(value, 1, 10)
        assert value not in caught.value.message


def test_json_schema_describes_strings() -> None:
    schema = IdsIn.model_json_schema()
    for name in ("account_id", "currency_id", "gas"):
        assert schema["properties"][name]["type"] == "string"
    assert AmountIn.model_json_schema()["properties"]["amount"]["type"] == "string"
    assert Out.model_json_schema(mode="serialization")["properties"]["amount"]["type"] == "string"
