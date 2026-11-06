from __future__ import annotations

from typing import Any

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncConnection

from rapidwire.db.engine import SESSION_SQL_MODE, create_engine

EXPECTED = ("READ-COMMITTED", "+00:00", frozenset(SESSION_SQL_MODE.split(",")), "utf8mb4_0900_bin", "utf8mb4")


async def _session_state(conn: AsyncConnection) -> tuple[int, tuple[Any, ...]]:
    row = (
        await conn.execute(text("SELECT CONNECTION_ID(), @@SESSION.transaction_isolation, @@SESSION.time_zone, @@SESSION.sql_mode, @@SESSION.collation_connection, @@SESSION.character_set_connection"))
    ).one()
    connection_id, isolation, time_zone, sql_mode, collation, charset = row
    return int(connection_id), (isolation, time_zone, frozenset(sql_mode.split(",")), collation, charset)


async def test_session_settings_survive_reconnects(migrated_url: URL) -> None:
    engine = create_engine(migrated_url, pool_size=1, max_overflow=0)
    killer = create_engine(migrated_url)
    try:
        async with engine.connect() as conn:
            first_id, state = await _session_state(conn)
        assert state == EXPECTED

        async with killer.connect() as admin:
            await admin.execute(text(f"KILL {first_id}"))

        async with engine.connect() as conn:
            second_id, state = await _session_state(conn)
        assert second_id != first_id
        assert state == EXPECTED

        async with engine.connect() as conn:
            await conn.invalidate()

        async with engine.connect() as conn:
            third_id, state = await _session_state(conn)
        assert third_id not in (first_id, second_id)
        assert state == EXPECTED
    finally:
        await engine.dispose()
        await killer.dispose()
