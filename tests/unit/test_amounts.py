from __future__ import annotations

import pytest

from rapidwire.api import formats
from rapidwire.core import amounts
from rapidwire.core.amounts import NumberFormatError, format_amount, format_amount_display, parse_amount, parse_int
from rapidwire.db.types import AMOUNT_MAX


def test_api_formats_reexports_the_shared_helpers() -> None:
    for name in ("parse_amount", "format_amount", "parse_int", "NumberFormatError", "DECIMALS", "MINIMAL_UNIT", "UNITS_PER_CURRENCY_UNIT", "AMOUNT_PATTERN", "AMOUNT_OUT_PATTERN", "INT_PATTERN"):
        assert getattr(formats, name) is getattr(amounts, name)


@pytest.mark.parametrize(
    ("text", "units"),
    [("1", 1000), ("12.5", 12500), ("0.001", 1), ("0.1", 100), ("1234567.891", 1234567891), ("0", 0)],
)
def test_parse_amount_is_exact(text: str, units: int) -> None:
    assert parse_amount(text, allow_zero=True) == units


@pytest.mark.parametrize(
    ("value", "kind"),
    [
        ("1e3", "amount_format"),
        ("1.2345", "amount_format"),
        ("-1", "amount_negative"),
        ("", "amount_format"),
        (" 1", "amount_format"),
        ("1 ", "amount_format"),
        ("+1", "amount_format"),
        ("01", "amount_format"),
        ("1.", "amount_format"),
        (".5", "amount_format"),
        ("1,000", "amount_format"),
        ("nan", "amount_format"),
        ("inf", "amount_format"),
        (chr(0xFF11), "amount_format"),
        (chr(0x0661), "amount_format"),
        ("0", "amount_zero"),
        ("0.000", "amount_zero"),
        (1.5, "amount_type"),
        (1, "amount_type"),
        ("9" * 49, "amount_range"),
    ],
)
def test_parse_amount_rejects(value: object, kind: str) -> None:
    with pytest.raises(NumberFormatError) as caught:
        parse_amount(value, allow_zero=False)
    assert caught.value.kind == kind


def test_parse_amount_range_limit() -> None:
    whole, fraction = divmod(AMOUNT_MAX, 1000)
    assert parse_amount(f"{whole}.{fraction:03d}", allow_zero=False) == AMOUNT_MAX
    for text in (str(whole + 1), "9" * 48):
        with pytest.raises(NumberFormatError) as caught:
            parse_amount(text, allow_zero=False)
        assert caught.value.kind == "amount_range"


def test_parse_amount_large_values_are_exact() -> None:
    assert parse_amount("123456789012345678901234567890.123", allow_zero=False) == 123456789012345678901234567890123
    assert parse_amount("99999999999999999999999999999999999.999", allow_zero=False) == 10**38 - 1


@pytest.mark.parametrize(
    ("units", "text"),
    [
        (0, "0.000"),
        (1, "0.001"),
        (999, "0.999"),
        (1000, "1.000"),
        (1234567, "1,234.567"),
        (1_000_000_000, "1,000,000.000"),
        (-1_234_500, "-1,234.500"),
        (AMOUNT_MAX, f"{AMOUNT_MAX // 1000:,}.999"),
    ],
)
def test_format_amount_display(units: int, text: str) -> None:
    assert format_amount_display(units) == text


def test_display_matches_the_canonical_form_without_separators() -> None:
    for units in (0, 5, 12_500, 10**20 + 7, AMOUNT_MAX, -AMOUNT_MAX):
        assert format_amount_display(units).replace(",", "") == format_amount(units)


def test_parse_int() -> None:
    assert parse_int("123", 1, 200) == 123
    for value in ("0123", "+1", "1.0", "", "abc"):
        with pytest.raises(NumberFormatError):
            parse_int(value, 0, 10**6)
    with pytest.raises(NumberFormatError) as caught:
        parse_int("300", 1, 200)
    assert caught.value.kind == "int_string_range"
