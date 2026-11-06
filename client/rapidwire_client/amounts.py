"""Currency amounts: requests take Decimal or str in currency units and are sent as canonical strings, minimal units go through from_units() and to_units(), and responses are parsed into Decimal with exactly DECIMALS places; floats are always refused."""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Final

__all__ = ["AMOUNT_MAX_UNITS", "DECIMALS", "AmountInput", "amount_string", "from_units", "parse_amount", "to_units"]

DECIMALS: Final = 3
AMOUNT_MAX_UNITS: Final = 10**38 - 1
_UNITS_PER_CURRENCY_UNIT: Final = 10**DECIMALS

_REQUEST_RE: Final = re.compile(rf"(0|[1-9][0-9]*)(\.[0-9]{{1,{DECIMALS}}})?")
_RESPONSE_RE: Final = re.compile(rf"-?(0|[1-9][0-9]*)\.[0-9]{{{DECIMALS}}}")

type AmountInput = Decimal | str


def _format_units(units: int) -> str:
    whole, fraction = divmod(abs(units), _UNITS_PER_CURRENCY_UNIT)
    sign = "-" if units < 0 else ""
    return f"{sign}{whole}.{fraction:0{DECIMALS}d}"


def from_units(units: int) -> Decimal:
    """The amount of a number of minimal units, such as Decimal("12.500") for 12500; the conversion is exact for every size."""
    if isinstance(units, bool) or not isinstance(units, int):
        raise TypeError("minimal units must be an int")
    return Decimal(_format_units(units))


def to_units(amount: Decimal) -> int:
    """The minimal units of an amount, such as 12500 for Decimal("12.5"); raises ValueError for a value that is not finite or has more than DECIMALS decimal places, since amounts are never rounded."""
    if not isinstance(amount, Decimal):
        raise TypeError("the amount must be a Decimal")
    if not amount.is_finite():
        raise ValueError("the amount must be finite")
    sign, digits, exponent = amount.as_tuple()
    number = int("".join(map(str, digits)) or "0")
    # A finite Decimal always has an int exponent.
    shift = int(exponent) + DECIMALS
    if shift >= 0:
        units = number * int(10**shift)
    else:
        units, rest = divmod(number, int(10**-shift))
        if rest:
            raise ValueError(f"amounts have at most {DECIMALS} decimal places")
    return -units if sign else units


def amount_string(value: AmountInput) -> str:
    """The canonical request string of an amount given as Decimal or str in currency units, such as "12.500"; raises TypeError for float, int and other types and ValueError for a negative, malformed or out-of-range value."""
    if isinstance(value, float):
        raise TypeError("floats are not accepted as amounts; pass a Decimal or a str such as '12.5'")
    if isinstance(value, bool | int):
        raise TypeError("ints are not accepted as amounts; pass a Decimal or a str in currency units, or from_units() for minimal units")
    if isinstance(value, str):
        if _REQUEST_RE.fullmatch(value) is None:
            raise ValueError(f"amount strings are decimal numbers with at most {DECIMALS} decimal places, without sign, exponent, spaces or leading zeros")
        units = to_units(Decimal(value))
    elif isinstance(value, Decimal):
        units = to_units(value)
    else:
        raise TypeError(f"a {type(value).__name__} is not an amount; pass a Decimal or a str")
    if units < 0:
        raise ValueError("request amounts must not be negative")
    if units > AMOUNT_MAX_UNITS:
        raise ValueError("the amount is above the largest supported amount")
    return _format_units(units)


def parse_amount(text: str) -> Decimal:
    """The Decimal of a response amount string, which always has exactly DECIMALS places; raises ValueError for any other text."""
    if not isinstance(text, str) or _RESPONSE_RE.fullmatch(text) is None:
        raise ValueError("not an amount string")
    return Decimal(text)
