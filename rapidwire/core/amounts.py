"""Exact text forms of currency amounts and integers shared by the API and the Discord bot: amounts are decimal strings in currency units with up to DECIMALS fractional digits, parsed to minimal units without floats."""

from __future__ import annotations

import re
from typing import Final

from rapidwire.db.types import AMOUNT_MAX

__all__ = [
    "AMOUNT_OUT_PATTERN",
    "AMOUNT_PATTERN",
    "DECIMALS",
    "INT_PATTERN",
    "MINIMAL_UNIT",
    "UNITS_PER_CURRENCY_UNIT",
    "NumberFormatError",
    "format_amount",
    "format_amount_display",
    "parse_amount",
    "parse_int",
]

DECIMALS: Final = 3
UNITS_PER_CURRENCY_UNIT: Final = 10**DECIMALS
MINIMAL_UNIT: Final = "0." + "0" * (DECIMALS - 1) + "1"

# No sign, exponent, whitespace or leading zero (other than a lone 0 before the point); the fraction has 1 to DECIMALS digits.
AMOUNT_PATTERN: Final = rf"^(0|[1-9][0-9]*)(\.[0-9]{{1,{DECIMALS}}})?$"
AMOUNT_OUT_PATTERN: Final = rf"^-?(0|[1-9][0-9]*)\.[0-9]{{{DECIMALS}}}$"
# An optional minus followed by digits without leading zeros; "-0" is not accepted.
INT_PATTERN: Final = r"^(0|-?[1-9][0-9]*)$"

_AMOUNT_RE = re.compile(AMOUNT_PATTERN)
_INT_RE = re.compile(INT_PATTERN)
# Longer strings cannot be in range, so they are rejected before any conversion.
_MAX_TEXT_LENGTH: Final = 48


class NumberFormatError(ValueError):
    """A number string that does not follow the format; kind is a short snake_case error type (amount_type, amount_negative, amount_range, amount_format, amount_zero, int_string_type, int_string_format, int_string_range) and the English message never contains the input."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message


def parse_amount(value: object, *, allow_zero: bool) -> int:
    """Convert an amount string such as "12.5" to minimal units (12500); raise NumberFormatError for a non-string, a malformed or negative string, zero when allow_zero is False, or a value above AMOUNT_MAX."""
    if not isinstance(value, str):
        raise NumberFormatError("amount_type", f'Amounts must be JSON strings such as "12.5", with at most {DECIMALS} decimal places.')
    if value.startswith("-"):
        raise NumberFormatError("amount_negative", "The amount must not be negative.")
    if len(value) > _MAX_TEXT_LENGTH:
        raise NumberFormatError("amount_range", "The amount is outside the supported range.")
    if _AMOUNT_RE.fullmatch(value) is None:
        raise NumberFormatError("amount_format", f"Amounts are decimal numbers with at most {DECIMALS} decimal places, without sign, exponent, spaces or leading zeros.")
    whole, _, fraction = value.partition(".")
    units = int(whole) * UNITS_PER_CURRENCY_UNIT + int(fraction.ljust(DECIMALS, "0"))
    if units > AMOUNT_MAX:
        raise NumberFormatError("amount_range", "The amount is outside the supported range.")
    if units == 0 and not allow_zero:
        raise NumberFormatError("amount_zero", f"The amount must be at least {MINIMAL_UNIT}.")
    return units


def format_amount(units: int) -> str:
    """The canonical string of an amount in minimal units, with exactly DECIMALS fractional digits ("12.500", "-3.000")."""
    whole, fraction = divmod(abs(units), UNITS_PER_CURRENCY_UNIT)
    sign = "-" if units < 0 else ""
    return f"{sign}{whole}.{fraction:0{DECIMALS}d}"


def format_amount_display(units: int) -> str:
    """An amount in minimal units for people to read, with thousands separators and exactly DECIMALS fractional digits ("1,234,567.890")."""
    whole, fraction = divmod(abs(units), UNITS_PER_CURRENCY_UNIT)
    sign = "-" if units < 0 else ""
    return f"{sign}{whole:,}.{fraction:0{DECIMALS}d}"


def parse_int(value: object, minimum: int, maximum: int) -> int:
    """Convert a decimal integer string to an int within minimum..maximum; raise NumberFormatError for a non-string, a malformed string or an out-of-range value."""
    if not isinstance(value, str):
        raise NumberFormatError("int_string_type", 'IDs and large integers must be JSON strings such as "123".')
    if len(value) > _MAX_TEXT_LENGTH or _INT_RE.fullmatch(value) is None:
        raise NumberFormatError("int_string_format", "The value must be a decimal integer string, without plus sign, spaces or leading zeros.")
    number = int(value)
    if not minimum <= number <= maximum:
        raise NumberFormatError("int_string_range", f"The value must be between {minimum} and {maximum}.")
    return number
