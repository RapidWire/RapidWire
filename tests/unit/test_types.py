from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.dialects import mysql

from rapidwire.db.types import AMOUNT_MAX, AMOUNT_MIN, Amount, AmountRangeError

DIALECT = mysql.dialect()


def test_bounds() -> None:
    assert AMOUNT_MAX == 10**38 - 1
    assert AMOUNT_MIN == -AMOUNT_MAX
    assert str(Amount().compile(dialect=DIALECT)) == "NUMERIC(38, 0)"


@pytest.mark.parametrize("value", [0, 1, -1, AMOUNT_MAX, AMOUNT_MIN])
def test_bind_accepts_int(value: int) -> None:
    assert Amount().process_bind_param(value, DIALECT) == Decimal(value)


@pytest.mark.parametrize("value", [True, False, 1.0, Decimal(1), "1"])
def test_bind_rejects_non_int(value: Any) -> None:
    with pytest.raises(TypeError, match="amount must be int"):
        Amount().process_bind_param(value, DIALECT)


@pytest.mark.parametrize("value", [AMOUNT_MAX + 1, AMOUNT_MIN - 1])
def test_bind_rejects_out_of_range(value: int) -> None:
    with pytest.raises(AmountRangeError, match="outside the DECIMAL"):
        Amount().process_bind_param(value, DIALECT)


def test_result_conversion() -> None:
    amount = Amount()
    assert amount.process_result_value(Decimal(AMOUNT_MAX), DIALECT) == AMOUNT_MAX
    assert type(amount.process_result_value(Decimal("5"), DIALECT)) is int
    assert amount.process_result_value(None, DIALECT) is None
    assert amount.process_bind_param(None, DIALECT) is None
    with pytest.raises(AmountRangeError, match="not integral"):
        amount.process_result_value(Decimal("1.5"), DIALECT)
