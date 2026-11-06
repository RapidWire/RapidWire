"""Connections for read-only functions that accept either an AsyncEngine or an open AsyncConnection."""

from __future__ import annotations

import weakref
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

__all__ = ["Db", "consistent_snapshot", "in_consistent_snapshot", "reader"]

type Db = AsyncEngine | AsyncConnection

_SNAPSHOTS: weakref.WeakSet[AsyncConnection] = weakref.WeakSet()
_START_SNAPSHOT = "START TRANSACTION WITH CONSISTENT SNAPSHOT, READ ONLY"


@asynccontextmanager
async def reader(db: Db) -> AsyncIterator[AsyncConnection]:
    """Yield the caller's connection, or a new connection of the engine that is closed afterwards; for single statements that need no snapshot.

    A caller's connection that was idle is returned idle, the transaction its reads began being rolled back; one in a transaction is read within it.
    """
    if isinstance(db, AsyncConnection):
        was_idle = not db.in_transaction()
        try:
            yield db
        finally:
            if was_idle and db.in_transaction():
                await db.rollback()
        return
    async with db.connect() as conn:
        yield conn


def in_consistent_snapshot(conn: AsyncConnection) -> bool:
    """Whether conn is inside a block of consistent_snapshot()."""
    return conn in _SNAPSHOTS


@asynccontextmanager
async def _start_snapshot(conn: AsyncConnection) -> AsyncIterator[AsyncConnection]:
    """Run the block in a snapshot transaction on an idle conn; when starting it fails, the half-open transaction is rolled back first so the isolation level can be restored and the original error propagates."""
    previous = await conn.get_isolation_level()
    await conn.execution_options(isolation_level="REPEATABLE READ")
    try:
        try:
            await conn.execute(text(_START_SNAPSHOT))
        except BaseException:
            if conn.in_transaction():
                await conn.rollback()
            raise
        _SNAPSHOTS.add(conn)
        try:
            yield conn
        finally:
            _SNAPSHOTS.discard(conn)
            await conn.rollback()
    finally:
        await conn.execution_options(isolation_level=previous)


@asynccontextmanager
async def consistent_snapshot(db: Db, *, allow_open_transaction: bool = True) -> AsyncIterator[AsyncConnection]:
    """Yield a connection whose reads all see one consistent snapshot, in a read-only REPEATABLE READ transaction started WITH CONSISTENT SNAPSHOT.

    An engine gets a new connection and an idle connection is used for the block and returned idle with its isolation level restored; a connection already inside a snapshot is yielded as is.
    A connection in any other transaction is yielded as is, so the reads join the caller's transaction under its isolation level, unless allow_open_transaction is False, which raises ValueError instead.
    """
    if isinstance(db, AsyncEngine):
        async with db.connect() as conn, _start_snapshot(conn):
            yield conn
        return
    if in_consistent_snapshot(db):
        yield db
        return
    if db.in_transaction():
        if not allow_open_transaction:
            raise ValueError("this read needs an idle connection or one inside consistent_snapshot()")
        yield db
        return
    async with _start_snapshot(db):
        yield db
