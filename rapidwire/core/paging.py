"""Paged reads: the Page result, the page sizes of every paged endpoint, and the argument check and bounded count they share."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Final

from sqlalchemy import Row, Select, func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from rapidwire.core.db import Db, consistent_snapshot
from rapidwire.core.errors import InvalidArgument
from rapidwire.core.validation import BIGINT_MAX, require_int_in_range

__all__ = [
    "ALLOWANCE_HISTORY_PER_PAGE_DEFAULT",
    "ALLOWANCE_HISTORY_PER_PAGE_MAX",
    "BACKUPS_PER_PAGE_DEFAULT",
    "BACKUPS_PER_PAGE_MAX",
    "BALANCES_PER_PAGE_DEFAULT",
    "BALANCES_PER_PAGE_MAX",
    "CLAIMS_PER_PAGE_DEFAULT",
    "CLAIMS_PER_PAGE_MAX",
    "CONTRACT_STORAGE_PER_PAGE_DEFAULT",
    "CONTRACT_STORAGE_PER_PAGE_MAX",
    "CONTRACT_VERSIONS_PER_PAGE_DEFAULT",
    "CONTRACT_VERSIONS_PER_PAGE_MAX",
    "DISCORD_PERMISSIONS_PER_PAGE_DEFAULT",
    "DISCORD_PERMISSIONS_PER_PAGE_MAX",
    "EXECUTIONS_PER_PAGE_DEFAULT",
    "EXECUTIONS_PER_PAGE_MAX",
    "HISTORY_PER_PAGE_DEFAULT",
    "HISTORY_PER_PAGE_MAX",
    "MAX_WINDOW",
    "NOTIFICATIONS_PER_PAGE_DEFAULT",
    "NOTIFICATIONS_PER_PAGE_MAX",
    "POOLS_PER_PAGE_DEFAULT",
    "POOLS_PER_PAGE_MAX",
    "POSITIONS_PER_PAGE_DEFAULT",
    "POSITIONS_PER_PAGE_MAX",
    "STAKES_PER_PAGE_DEFAULT",
    "STAKES_PER_PAGE_MAX",
    "TOKENS_PER_PAGE_DEFAULT",
    "TOKENS_PER_PAGE_MAX",
    "Page",
    "bounded_count",
    "count_bounded",
    "read_page",
    "validate_paging",
]

# Lists of a user's or guild's own rows (balances, claims, stakes, allowance changes, tokens, notification and Discord permissions, pool positions), the pool list, a contract's versions and storage and the operators' backup list default to 15 per page up to 100; transfer history and executions default to 10 up to 20.
POOLS_PER_PAGE_DEFAULT: Final = 15
POOLS_PER_PAGE_MAX: Final = 100
POSITIONS_PER_PAGE_DEFAULT: Final = 15
POSITIONS_PER_PAGE_MAX: Final = 100
TOKENS_PER_PAGE_DEFAULT: Final = 15
TOKENS_PER_PAGE_MAX: Final = 100
NOTIFICATIONS_PER_PAGE_DEFAULT: Final = 15
NOTIFICATIONS_PER_PAGE_MAX: Final = 100
DISCORD_PERMISSIONS_PER_PAGE_DEFAULT: Final = 15
DISCORD_PERMISSIONS_PER_PAGE_MAX: Final = 100
BALANCES_PER_PAGE_DEFAULT: Final = 15
BALANCES_PER_PAGE_MAX: Final = 100
CLAIMS_PER_PAGE_DEFAULT: Final = 15
CLAIMS_PER_PAGE_MAX: Final = 100
STAKES_PER_PAGE_DEFAULT: Final = 15
STAKES_PER_PAGE_MAX: Final = 100
ALLOWANCE_HISTORY_PER_PAGE_DEFAULT: Final = 15
ALLOWANCE_HISTORY_PER_PAGE_MAX: Final = 100
HISTORY_PER_PAGE_DEFAULT: Final = 10
HISTORY_PER_PAGE_MAX: Final = 20
EXECUTIONS_PER_PAGE_DEFAULT: Final = 10
EXECUTIONS_PER_PAGE_MAX: Final = 20
CONTRACT_VERSIONS_PER_PAGE_DEFAULT: Final = 15
CONTRACT_VERSIONS_PER_PAGE_MAX: Final = 100
CONTRACT_STORAGE_PER_PAGE_DEFAULT: Final = 15
CONTRACT_STORAGE_PER_PAGE_MAX: Final = 100
BACKUPS_PER_PAGE_DEFAULT: Final = 15
BACKUPS_PER_PAGE_MAX: Final = 100

MAX_WINDOW: Final = 10_000


@dataclass(frozen=True)
class Page[T]:
    """One page of results; total is exact unless total_is_lower_bound, in which case at least total rows match and only the first MAX_WINDOW are reachable."""

    items: tuple[T, ...]
    total: int
    page: int
    per_page: int
    total_is_lower_bound: bool = False

    @property
    def pages(self) -> int:
        """The number of reachable pages, 0 when nothing matches."""
        return math.ceil(self.total / self.per_page)


def validate_paging(page: object, per_page: object, per_page_max: int) -> int:
    """Return the row offset of a 1-based page; raises InvalidArgument for a page below 1, a per_page outside 1..per_page_max, or a page reaching past the first MAX_WINDOW rows."""
    page_number = require_int_in_range("page", page, 1, BIGINT_MAX)
    size = require_int_in_range("per_page", per_page, 1, per_page_max)
    offset = (page_number - 1) * size
    if offset + size > MAX_WINDOW:
        raise InvalidArgument(f"Only the first {MAX_WINDOW} results can be paged through; narrow the search.")
    return offset


def bounded_count(stmt: Select[Any]) -> Select[Any]:
    """Count the rows of stmt (selecting any single column), stopping after MAX_WINDOW + 1."""
    return select(func.count()).select_from(stmt.limit(MAX_WINDOW + 1).subquery())


async def count_bounded(conn: AsyncConnection, statements: Sequence[Select[Any]]) -> tuple[int, bool]:
    """Sum the bounded counts and return (total capped at MAX_WINDOW, whether the cap was exceeded)."""
    total = 0
    for stmt in statements:
        total += int((await conn.execute(stmt)).scalar_one())
    return min(total, MAX_WINDOW), total > MAX_WINDOW


async def read_page[T](db: Db, stmt: Select[Any], *, key: Any, page: int, per_page: int, per_page_max: int, convert: Callable[[Row[Any]], T]) -> Page[T]:
    """Read one page of stmt, ordered by stmt's own ORDER BY, with a bounded count of its key column, all from one consistent_snapshot(); paging is checked by validate_paging()."""
    offset = validate_paging(page, per_page, per_page_max)
    async with consistent_snapshot(db) as conn:
        total, bounded = await count_bounded(conn, [bounded_count(stmt.with_only_columns(key).order_by(None))])
        rows = (await conn.execute(stmt.limit(per_page).offset(offset))).all()
    return Page(items=tuple(convert(row) for row in rows), total=total, page=page, per_page=per_page, total_is_lower_bound=bounded)
