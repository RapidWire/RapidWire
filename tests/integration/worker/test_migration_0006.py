"""Migration 0006 on existing outbox rows: terminal rows get completed_at and a result, the new CHECKs hold, and a downgrade turns skipped rows into failed ones."""

from __future__ import annotations

import pytest
from alembic import command
from sqlalchemy import Connection, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.db.migrate import head_revision, make_config


def _downgrade_on(connection: Connection, revision: str) -> None:
    command.downgrade(make_config(connection=connection), revision)


def _upgrade_on(connection: Connection, revision: str) -> None:
    command.upgrade(make_config(connection=connection), revision)


async def _rows(engine: AsyncEngine) -> list[tuple[int, str, str | None, str | None]]:
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT outbox_id, status, CAST(completed_at AS CHAR), CAST(result AS CHAR) FROM outbox ORDER BY outbox_id"))
        return [(row[0], row[1], row[2], row[3]) for row in result]


async def test_migration_0006_converts_and_restores_outbox_rows(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await conn.run_sync(_downgrade_on, "0005")
    try:
        async with db.begin() as conn:
            await conn.execute(
                text(
                    "INSERT INTO outbox (outbox_id, kind, payload, status, attempts, available_at, created_at, sent_at, last_error) VALUES"
                    " (1, 'discord_send', '{}', 'sent', 1, '2026-01-01 00:00:00', '2026-01-01 00:00:00', '2026-01-01 00:00:05', NULL),"
                    " (2, 'discord_send', '{}', 'failed', 3, '2026-01-01 00:00:00', '2026-01-01 00:00:01', NULL, 'gone'),"
                    " (3, 'claim_created', '{}', 'pending', 0, '2026-01-01 00:00:00', '2026-01-01 00:00:02', NULL, NULL)"
                )
            )
        async with db.begin() as conn:
            await conn.run_sync(_upgrade_on, "0006")

        rows = await _rows(db)
        assert [(outbox_id, status, completed_at) for outbox_id, status, completed_at, _ in rows] == [
            (1, "sent", "2026-01-01 00:00:05.000000"),
            (2, "failed", "2026-01-01 00:00:01.000000"),
            (3, "pending", None),
        ]
        assert [result for _, _, _, result in rows] == ['{"status": "sent"}', '{"status": "failed"}', None]

        for statement in (
            "UPDATE outbox SET status = 'sent' WHERE outbox_id = 3",
            "UPDATE outbox SET result = NULL WHERE outbox_id = 1",
            "UPDATE outbox SET completed_at = NULL WHERE outbox_id = 2",
            "UPDATE outbox SET status = 'lost' WHERE outbox_id = 1",
        ):
            with pytest.raises(DBAPIError, match="3819"):
                async with db.begin() as conn:
                    await conn.execute(text(statement))

        async with db.begin() as conn:
            await conn.execute(
                text("UPDATE outbox SET status = 'skipped', completed_at = '2026-01-02 00:00:00', result = JSON_OBJECT('status', 'skipped', 'reason', 'claim_paid') WHERE outbox_id = 3")
            )
            await conn.run_sync(_downgrade_on, "0005")
        async with db.connect() as conn:
            downgraded = (await conn.execute(text("SELECT outbox_id, status, CAST(sent_at AS CHAR) FROM outbox ORDER BY outbox_id"))).all()
        assert [tuple(row) for row in downgraded] == [(1, "sent", "2026-01-01 00:00:05.000000"), (2, "failed", "2026-01-01 00:00:01.000000"), (3, "failed", "2026-01-02 00:00:00.000000")]
    finally:
        async with db.begin() as conn:
            await conn.run_sync(_upgrade_on, "head")
    async with db.connect() as conn:
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == head_revision()
