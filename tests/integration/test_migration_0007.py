"""Migration 0007 on existing executions: contract management rows get their initiator, contract calls their input.contract_id when it is a valid ID, every other row NULL, and a downgrade drops the column."""

from __future__ import annotations

import json

from alembic import command
from sqlalchemy import Connection, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.ledger import execution_contract_id
from rapidwire.db.migrate import head_revision, make_config

ALICE = 1_000_001
CONTRACT = 1_000_002

# (execution_id, type, input JSON, expected contract_id)
ROWS = (
    (1, "contract_call", '{"contract_id": "1000002", "function": "f"}', CONTRACT),
    (2, "contract_call", '{"contract_id": "9223372036854775807"}', 9223372036854775807),
    (3, "contract_call", '{"contract_id": "9223372036854775808"}', None),
    (4, "contract_call", '{"contract_id": "01"}', None),
    (5, "contract_call", '{"contract_id": "abc"}', None),
    (6, "contract_call", '{"contract_id": null}', None),
    (7, "contract_call", "{}", None),
    (8, "contract_call", '{"contract_id": "-5"}', None),
    (9, "contract_update", "{}", ALICE),
    (10, "contract_lock", "{}", ALICE),
    (11, "contract_pause", "{}", ALICE),
    (12, "contract_resume", "{}", ALICE),
    (13, "contract_delete", "{}", ALICE),
    (14, "transfer", '{"contract_id": "1000002"}', None),
    (15, "contract_call", '{"contract_id": 1000002}', CONTRACT),
    (16, "contract_call", '{"contract_id": 1.5}', None),
    (17, "contract_call", '{"contract_id": true}', None),
    (18, "contract_call", '{"contract_id": 0}', None),
    (19, "contract_call", '{"contract_id": 100000000000000000000}', None),
)


def _downgrade_on(connection: Connection, revision: str) -> None:
    command.downgrade(make_config(connection=connection), revision)


def _upgrade_on(connection: Connection, revision: str) -> None:
    command.upgrade(make_config(connection=connection), revision)


async def test_migration_0007_backfills_and_drops_contract_id(db: AsyncEngine) -> None:
    assert head_revision() == "0007"
    async with db.begin() as conn:
        await conn.run_sync(_downgrade_on, "0006")
    try:
        async with db.begin() as conn:
            await conn.execute(text("INSERT INTO account (account_id, kind, created_at) VALUES (:id, 'user', '2026-01-01 00:00:00')"), {"id": ALICE})
            for execution_id, type_, payload, _ in ROWS:
                await conn.execute(
                    text("INSERT INTO execution (execution_id, type, initiator_id, status, input, created_at) VALUES (:id, :type, :initiator, 'success', :input, '2026-01-01 00:00:00')"),
                    {"id": execution_id, "type": type_, "initiator": ALICE, "input": payload},
                )
        async with db.begin() as conn:
            await conn.run_sync(_upgrade_on, "0007")
        async with db.connect() as conn:
            rows = (await conn.execute(text("SELECT execution_id, contract_id FROM execution ORDER BY execution_id"))).all()
        found = {row.execution_id: row.contract_id for row in rows}
        assert found == {execution_id: expected for execution_id, _, _, expected in ROWS}
        async with db.begin() as conn:
            await conn.run_sync(_downgrade_on, "0006")
        async with db.connect() as conn:
            columns = (await conn.execute(text("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'execution'"))).scalars().all()
        assert "contract_id" not in columns
    finally:
        async with db.begin() as conn:
            await conn.run_sync(_upgrade_on, "head")


def test_the_ledger_rule_matches_the_migration() -> None:
    for _, type_, payload, expected in ROWS:
        assert execution_contract_id(type_, ALICE, json.loads(payload)) == expected, (type_, payload)
