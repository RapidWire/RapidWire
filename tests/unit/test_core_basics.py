from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import mysql

from rapidwire.core import errors
from rapidwire.core.clock import FixedClock, SystemClock, ensure_utc, get_clock, resolve_now, set_clock
from rapidwire.core.errors import DomainError, InsufficientBalance, RevertError, error_class, error_from_code
from rapidwire.core.ledger import OperationResult, is_retryable, mysql_error_code
from rapidwire.core.types import ExecutionStatus, ExecutionType, to_json, to_json_object
from rapidwire.db.types import UtcDateTime

DIALECT = mysql.dialect()
REQUIRED_CODES = {
    "not_found",
    "currency_not_found",
    "claim_not_found",
    "account_not_found",
    "insufficient_balance",
    "insufficient_allowance",
    "insufficient_stake",
    "invalid_amount",
    "invalid_argument",
    "permission_denied",
    "guild_unavailable",
    "currency_renounced",
    "timelock_active",
    "request_expired",
    "no_pending_request",
    "already_exists",
    "invalid_state",
    "amount_overflow",
    "idempotency_conflict",
    "reverted",
    "pool_not_found",
    "pool_already_exists",
    "slippage_exceeded",
    "insufficient_liquidity",
    "insufficient_shares",
    "no_route",
}


def test_error_codes_are_registered() -> None:
    for code in REQUIRED_CODES:
        cls = error_class(code)
        assert cls is not None, code
        assert cls.code == code
        assert issubclass(cls, DomainError)


def test_error_from_code_round_trip() -> None:
    error = error_from_code("insufficient_balance", "custom", execution_id=9)
    assert isinstance(error, InsufficientBalance)
    assert (error.message, error.execution_id, str(error)) == ("custom", 9, "custom")
    unknown = error_from_code("brand_new_code", "later milestone")
    assert type(unknown) is DomainError
    assert unknown.code == "brand_new_code"


def test_duplicate_code_is_rejected() -> None:
    with pytest.raises(TypeError, match="already used"):
        type("Clash", (DomainError,), {"code": "insufficient_balance"})


def test_messages_are_ascii_english() -> None:
    for name in dir(errors):
        cls = getattr(errors, name)
        if isinstance(cls, type) and issubclass(cls, DomainError):
            assert cls.default_message.isascii()
            assert cls().message == cls.default_message


def test_revert_maps_to_reverted_status() -> None:
    assert RevertError.status is ExecutionStatus.REVERTED
    assert InsufficientBalance.status is ExecutionStatus.FAILED


def test_execution_types_fit_the_column() -> None:
    assert all(len(member.value) <= 32 for member in ExecutionType)
    assert ExecutionType.CURRENCY_DELETE_FINALIZE.value == "currency_delete_finalize"


def test_to_json() -> None:
    moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone(timedelta(hours=9)))
    assert to_json({"a": 10**30, "b": [1, (2, None)], "c": True, "d": "x", "e": moment, "f": ExecutionType.MINT}) == {
        "a": "1" + "0" * 30,
        "b": ["1", ["2", None]],
        "c": True,
        "d": "x",
        "e": "2026-01-01T18:04:05+00:00",
        "f": "mint",
    }
    for bad in (1.5, b"x", {1: "a"}, datetime(2026, 1, 1)):
        with pytest.raises(TypeError):
            to_json(bad)
    with pytest.raises(TypeError):
        to_json_object([1])  # type: ignore[arg-type]


def test_operation_result_int_field() -> None:
    result = OperationResult(execution_id=1, status=ExecutionStatus.SUCCESS, output={"amount": "12", "name": None})
    assert result.int_field("amount") == 12
    with pytest.raises(KeyError):
        result.int_field("name")


def test_retryable_detection() -> None:
    class Orig(Exception):
        pass

    from sqlalchemy.exc import OperationalError

    deadlock = OperationalError("stmt", {}, Orig(1213, "Deadlock found"))
    timeout = OperationalError("stmt", {}, Orig(1205, "Lock wait timeout"))
    other = OperationalError("stmt", {}, Orig(1062, "Duplicate"))
    assert mysql_error_code(deadlock) == 1213
    assert is_retryable(deadlock)
    assert is_retryable(timeout)
    assert not is_retryable(other)
    assert not is_retryable(ValueError("x"))


def test_clock() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    fixed = FixedClock(start)
    previous = set_clock(fixed)
    try:
        assert get_clock() is fixed
        assert resolve_now() == start
        fixed.advance(timedelta(days=7))
        assert resolve_now() == start + timedelta(days=7)
        explicit = datetime(2026, 1, 1, 9, tzinfo=timezone(timedelta(hours=9)))
        assert resolve_now(explicit) == start
        assert resolve_now(explicit).tzinfo is UTC
    finally:
        set_clock(previous)
    assert isinstance(get_clock(), SystemClock)
    with pytest.raises(ValueError, match="naive"):
        ensure_utc(datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="naive"):
        FixedClock(datetime(2026, 1, 1))


def test_utc_datetime_binding() -> None:
    column = UtcDateTime()
    local = datetime(2026, 1, 1, 9, 0, 0, 1, tzinfo=timezone(timedelta(hours=9)))
    assert column.process_bind_param(local, DIALECT) == datetime(2026, 1, 1, 0, 0, 0, 1)
    assert column.process_bind_param(None, DIALECT) is None
    with pytest.raises(ValueError, match="naive"):
        column.process_bind_param(datetime(2026, 1, 1), DIALECT)
    with pytest.raises(TypeError):
        column.process_bind_param("2026-01-01", DIALECT)
    assert column.process_result_value(datetime(2026, 1, 1), DIALECT) == datetime(2026, 1, 1, tzinfo=UTC)
    assert column.process_result_value(None, DIALECT) is None
