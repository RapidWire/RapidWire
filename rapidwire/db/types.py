"""Column types shared by the schema."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Dialect, Numeric
from sqlalchemy.dialects.mysql import DATETIME
from sqlalchemy.types import TypeDecorator

DATETIME_FSP = 6

AMOUNT_DIGITS = 38
AMOUNT_MAX = 10**AMOUNT_DIGITS - 1
AMOUNT_MIN = -AMOUNT_MAX


class AmountRangeError(ValueError):
    """An amount does not fit DECIMAL(38,0)."""


def check_amount(value: int) -> int:
    """Return value when it is an int (not bool) within AMOUNT_MIN..AMOUNT_MAX, else raise TypeError or AmountRangeError."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"amount must be int, got {type(value).__name__}")
    if not AMOUNT_MIN <= value <= AMOUNT_MAX:
        raise AmountRangeError(f"amount {value} is outside the DECIMAL({AMOUNT_DIGITS},0) range")
    return value


class Amount(TypeDecorator[int]):
    """DECIMAL(38,0) column mapped exactly to Python int in both directions."""

    impl = Numeric(AMOUNT_DIGITS, 0, asdecimal=True)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        return Decimal(check_amount(value))

    def process_result_value(self, value: Any, dialect: Dialect) -> int | None:
        if value is None:
            return None
        decimal = value if isinstance(value, Decimal) else Decimal(str(value))
        if decimal != decimal.to_integral_value():
            raise AmountRangeError(f"amount {decimal} read from the database is not integral")
        return check_amount(int(decimal))


class UtcDateTime(TypeDecorator[datetime]):
    """DATETIME(6) column holding naive UTC, mapped to timezone-aware UTC datetimes; binding a naive datetime raises ValueError."""

    impl = DATETIME(fsp=DATETIME_FSP)
    cache_ok = True

    def process_bind_param(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime, got {type(value).__name__}")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime cannot be stored; pass a timezone-aware value")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(f"expected datetime from the database, got {type(value).__name__}")
        return value.replace(tzinfo=UTC)
