"""Async engine and session factories for MySQL through asyncmy, with session settings that do not depend on the server configuration."""

from __future__ import annotations

from typing import Any

from sqlalchemy import URL, event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from sqlalchemy.ext.asyncio import create_async_engine as _create_async_engine
from sqlalchemy.pool import ConnectionPoolEntry

POOL_RECYCLE_SECONDS = 3600
ISOLATION_LEVEL = "READ COMMITTED"
SESSION_TIME_ZONE = "+00:00"
SESSION_SQL_MODE = "STRICT_ALL_TABLES,NO_ZERO_DATE,NO_ZERO_IN_DATE,ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION,ONLY_FULL_GROUP_BY"
SESSION_CHARSET = "utf8mb4"
SESSION_COLLATION = "utf8mb4_0900_bin"

LOCK_WAIT_TIMEOUT_SECONDS = 5


def init_command(lock_wait_timeout: int = LOCK_WAIT_TIMEOUT_SECONDS) -> str:
    """The session setup statement; a lock wait longer than lock_wait_timeout seconds fails with MySQL error 1205, which the ledger retries."""
    if isinstance(lock_wait_timeout, bool) or not isinstance(lock_wait_timeout, int) or lock_wait_timeout < 1:
        raise ValueError("lock_wait_timeout must be a positive integer number of seconds")
    return f"SET SESSION time_zone = '{SESSION_TIME_ZONE}', SESSION sql_mode = '{SESSION_SQL_MODE}', SESSION innodb_lock_wait_timeout = {lock_wait_timeout}"


# asyncmy runs this during the handshake, before SQLAlchemy's first-connect probe reads sql_mode to decide identifier quoting.
INIT_COMMAND = init_command()


def _set_collation(dbapi_connection: Any, _record: ConnectionPoolEntry) -> None:
    # The dialect's own connect hook issues a bare SET NAMES, which resets collation_connection, so the collation is applied after it.
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"SET NAMES {SESSION_CHARSET} COLLATE {SESSION_COLLATION}")
    finally:
        cursor.close()


def create_engine(url: URL | str, *, lock_wait_timeout: int = LOCK_WAIT_TIMEOUT_SECONDS, **kw: Any) -> AsyncEngine:
    """Create an AsyncEngine whose connections use READ COMMITTED, UTC, a strict sql_mode, a binary collation and a short lock wait timeout, with liveness checks and recycling; keyword arguments override the defaults."""
    connect_args = {"init_command": init_command(lock_wait_timeout), **kw.pop("connect_args", {})}
    options: dict[str, Any] = {"pool_pre_ping": True, "pool_recycle": POOL_RECYCLE_SECONDS, "isolation_level": ISOLATION_LEVEL, "connect_args": connect_args}
    options.update(kw)
    engine = _create_async_engine(url, **options)
    event.listen(engine.sync_engine, "connect", _set_collation)
    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Session factory whose objects stay readable after commit."""
    return async_sessionmaker(engine, expire_on_commit=False)
