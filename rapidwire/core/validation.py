"""Argument checks shared by the services: storable integers, amounts, user account IDs, guild IDs and other BIGINT identifiers."""

from __future__ import annotations

from typing import Final

from rapidwire.core.errors import AmountOverflow, InvalidAmount, InvalidArgument
from rapidwire.db.types import AMOUNT_MAX

__all__ = [
    "BIGINT_MAX",
    "BIGINT_MIN",
    "USER_ID_MAX",
    "is_guild_id",
    "is_user_account_id",
    "require_int_in_range",
    "require_storable_int",
    "require_user_account",
    "validate_bigint_id",
    "validate_guild_id",
    "validate_non_negative_amount",
    "validate_positive_amount",
    "validate_positive_id",
    "validate_user_account_id",
]

BIGINT_MIN: Final = -(2**63)
BIGINT_MAX: Final = 2**63 - 1
USER_ID_MAX: Final = BIGINT_MAX


def require_int_in_range(name: str, value: object, low: int, high: int) -> int:
    """Return value when it is an int (not a bool) from low to high, else raise InvalidArgument naming it."""
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise InvalidArgument(f"{name} must be an integer from {low} to {high}.")
    return value


def validate_bigint_id(name: str, value: object) -> int:
    """Return value when it fits a signed BIGINT column, else raise InvalidArgument."""
    return require_int_in_range(name, value, BIGINT_MIN, BIGINT_MAX)


def validate_positive_id(name: str, value: object) -> int:
    """Return value when it is a positive BIGINT, the range of every AUTO_INCREMENT ID, else raise InvalidArgument."""
    return require_int_in_range(name, value, 1, BIGINT_MAX)


def require_storable_int(amount: object) -> int:
    """Return amount when it is an int (not a bool), else raise InvalidAmount; services call it before run_operation because other types cannot be stored as execution input."""
    if isinstance(amount, bool) or not isinstance(amount, int):
        raise InvalidAmount()
    return amount


def validate_positive_amount(amount: object) -> int:
    """Return amount when it is a positive int within the Amount range; raises InvalidAmount or AmountOverflow otherwise."""
    value = require_storable_int(amount)
    if value <= 0:
        raise InvalidAmount()
    if value > AMOUNT_MAX:
        raise AmountOverflow()
    return value


def validate_non_negative_amount(amount: object, message: str) -> int:
    """Return amount when it is a non-negative int within the Amount range; raises InvalidAmount with message or AmountOverflow otherwise."""
    value = require_storable_int(amount)
    if value < 0:
        raise InvalidAmount(message)
    if value > AMOUNT_MAX:
        raise AmountOverflow()
    return value


def is_user_account_id(account_id: object) -> bool:
    """Whether account_id is a Discord user ID, the range of user accounts."""
    return not isinstance(account_id, bool) and isinstance(account_id, int) and 1 <= account_id <= USER_ID_MAX


def validate_user_account_id(account_id: int) -> int:
    """Return account_id when it is a valid Discord user ID, else raise InvalidArgument."""
    if not is_user_account_id(account_id):
        raise InvalidArgument(f"Account ID must be an integer between 1 and {USER_ID_MAX}.")
    return account_id


def require_user_account(account_id: int, role: str) -> int:
    """Return account_id when it is a user account ID, else raise InvalidArgument naming the role (for example "recipient")."""
    if not is_user_account_id(account_id):
        raise InvalidArgument(f"The {role} must be a user account.")
    return account_id


def is_guild_id(guild_id: object) -> bool:
    """Whether guild_id is a Discord guild ID (a positive BIGINT)."""
    return not isinstance(guild_id, bool) and isinstance(guild_id, int) and 1 <= guild_id <= BIGINT_MAX


def validate_guild_id(guild_id: int) -> int:
    """Return guild_id when it is a Discord guild ID, else raise InvalidArgument."""
    if not is_guild_id(guild_id):
        raise InvalidArgument(f"The server ID must be an integer between 1 and {BIGINT_MAX}.")
    return guild_id
