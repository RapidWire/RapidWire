from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from rapidwire.core.currency import delete_request_active, validate_currency_name, validate_currency_symbol, validate_hourly_rate
from rapidwire.core.errors import InvalidArgument

NAME_SHAPE = "Names must start with a letter, end with an alphanumeric character, and contain only alphanumeric characters and underscores."
SYMBOL_SHAPE = "Symbols must contain only letters (A-Z) and hyphens."


@pytest.mark.parametrize("name", ["a", "Z", "ab", "Coin", "a1", "My_Coin_2", "a_b_c_d_e_f", "A" * 24, "x9"])
def test_valid_names(name: str) -> None:
    assert validate_currency_name(name) == name


@pytest.mark.parametrize(
    ("name", "message"),
    [
        ("", NAME_SHAPE),
        ("1coin", NAME_SHAPE),
        ("_coin", NAME_SHAPE),
        ("coin_", NAME_SHAPE),
        ("co-in", NAME_SHAPE),
        ("co in", NAME_SHAPE),
        ("coin!", NAME_SHAPE),
        ("1", NAME_SHAPE),
        ("_", NAME_SHAPE),
        ("coin\n", NAME_SHAPE),
        ("caf" + chr(0xE9), NAME_SHAPE),
        ("a_b_c_d_e_f_g", "Names can contain at most 5 underscores."),
        ("a__b", "Names cannot contain consecutive underscores."),
        ("A" * 25, "Names can be at most 24 characters long."),
    ],
)
def test_invalid_names(name: str, message: str) -> None:
    with pytest.raises(InvalidArgument) as info:
        validate_currency_name(name)
    assert info.value.message == message


@pytest.mark.parametrize("symbol", ["A", "ABC", "A-B", "A-B-C", "ABCDEFGH", "AB-CD-EF"])
def test_valid_symbols(symbol: str) -> None:
    assert validate_currency_symbol(symbol) == symbol


@pytest.mark.parametrize(
    ("symbol", "message"),
    [
        ("", SYMBOL_SHAPE),
        ("abc", SYMBOL_SHAPE),
        ("AB1", SYMBOL_SHAPE),
        ("A_B", SYMBOL_SHAPE),
        ("A B", SYMBOL_SHAPE),
        ("A\n", SYMBOL_SHAPE),
        ("A-B-C-D", "Symbols can contain at most 2 hyphens."),
        ("A--B", "Symbols cannot contain consecutive hyphens."),
        ("-AB", "Symbols cannot start or end with a hyphen."),
        ("AB-", "Symbols cannot start or end with a hyphen."),
        ("-", "Symbols cannot start or end with a hyphen."),
        ("ABCDEFGHI", "Symbols can be at most 8 characters long."),
    ],
)
def test_invalid_symbols(symbol: str, message: str) -> None:
    with pytest.raises(InvalidArgument) as info:
        validate_currency_symbol(symbol)
    assert info.value.message == message


def test_symbol_is_not_uppercased() -> None:
    with pytest.raises(InvalidArgument):
        validate_currency_symbol("Abc")


@pytest.mark.parametrize("rate", [0, 1, 1_000_000])
def test_valid_rates(rate: int) -> None:
    assert validate_hourly_rate(rate) == rate


@pytest.mark.parametrize("rate", [-1, 1_000_001, True])
def test_invalid_rates(rate: int) -> None:
    with pytest.raises(InvalidArgument):
        validate_hourly_rate(rate)


def test_delete_request_lapses_after_ten_days() -> None:
    at = datetime(2026, 1, 1, tzinfo=UTC)
    assert not delete_request_active(None, at)
    assert delete_request_active(at, at + timedelta(days=10))
    assert not delete_request_active(at, at + timedelta(days=10, microseconds=1))
