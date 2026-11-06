from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.errors import InvalidArgument
from rapidwire.core.notifications import allow_notifications, deny_notifications, is_notification_allowed, list_allowed
from rapidwire.core.types import ExecutionStatus, ExecutionType
from rapidwire.db.tables import execution, notification_permission

NOW = datetime(2026, 5, 2, tzinfo=UTC)
PAYER = 111
CLAIMANT = 222
OTHER = 333


async def _records(engine: AsyncEngine) -> list[tuple[str, str, object]]:
    async with engine.connect() as conn:
        rows = await conn.execute(select(execution.c.type, execution.c.status, execution.c.output).order_by(execution.c.execution_id))
        return [(row.type, row.status, row.output) for row in rows]


async def test_allow_list_and_deny(db: AsyncEngine) -> None:
    assert not await is_notification_allowed(db, PAYER, CLAIMANT)
    first = await allow_notifications(db, user_id=PAYER, allowed_user_id=CLAIMANT, now=NOW)
    assert first.changed
    await allow_notifications(db, user_id=PAYER, allowed_user_id=OTHER, now=NOW + timedelta(seconds=1))
    assert await is_notification_allowed(db, PAYER, CLAIMANT)
    assert not await is_notification_allowed(db, CLAIMANT, PAYER)
    allowed = await list_allowed(db, PAYER)
    assert [(item.allowed_user_id, item.created_at) for item in allowed.items] == [(CLAIMANT, NOW), (OTHER, NOW + timedelta(seconds=1))]
    assert allowed.total == 2
    assert [item.allowed_user_id for item in (await list_allowed(db, PAYER, page=2, per_page=1)).items] == [OTHER]
    assert (await list_allowed(db, CLAIMANT)).items == ()
    with pytest.raises(InvalidArgument):
        await list_allowed(db, PAYER, per_page=101)
    denied = await deny_notifications(db, user_id=PAYER, allowed_user_id=CLAIMANT, now=NOW)
    assert denied.changed
    async with db.connect() as conn:
        assert not await is_notification_allowed(conn, PAYER, CLAIMANT)
    assert [item.allowed_user_id for item in (await list_allowed(db, PAYER)).items] == [OTHER]
    records = await _records(db)
    assert [(type, status) for type, status, _ in records] == [
        (ExecutionType.NOTIFICATION_ALLOW, ExecutionStatus.SUCCESS),
        (ExecutionType.NOTIFICATION_ALLOW, ExecutionStatus.SUCCESS),
        (ExecutionType.NOTIFICATION_DENY, ExecutionStatus.SUCCESS),
    ]


async def test_repeated_allow_and_missing_deny_are_unchanged_successes(db: AsyncEngine) -> None:
    await allow_notifications(db, user_id=PAYER, allowed_user_id=CLAIMANT, now=NOW)
    again = await allow_notifications(db, user_id=PAYER, allowed_user_id=CLAIMANT, now=NOW + timedelta(days=1))
    assert not again.changed
    assert [item.created_at for item in (await list_allowed(db, PAYER)).items] == [NOW]
    missing = await deny_notifications(db, user_id=PAYER, allowed_user_id=OTHER, now=NOW)
    assert not missing.changed
    pair, other = {"user_id": str(PAYER), "allowed_user_id": str(CLAIMANT)}, {"user_id": str(PAYER), "allowed_user_id": str(OTHER)}
    assert [output for _, _, output in await _records(db)] == [pair | {"changed": True}, pair | {"changed": False}, other | {"changed": False}]


async def test_self_is_rejected_and_recorded(db: AsyncEngine) -> None:
    with pytest.raises(InvalidArgument) as allow_error:
        await allow_notifications(db, user_id=PAYER, allowed_user_id=PAYER, now=NOW)
    with pytest.raises(InvalidArgument):
        await deny_notifications(db, user_id=PAYER, allowed_user_id=PAYER, now=NOW)
    assert allow_error.value.execution_id is not None
    assert [status for _, status, _ in await _records(db)] == [ExecutionStatus.FAILED, ExecutionStatus.FAILED]
    async with db.connect() as conn:
        assert (await conn.execute(select(notification_permission))).first() is None


async def test_invalid_allowed_user_is_rejected(db: AsyncEngine) -> None:
    with pytest.raises(InvalidArgument):
        await allow_notifications(db, user_id=PAYER, allowed_user_id=0, now=NOW)
    with pytest.raises(InvalidArgument):
        await allow_notifications(db, user_id=PAYER, allowed_user_id=-1, now=NOW)


async def test_concurrent_allows_insert_one_row(db: AsyncEngine) -> None:
    results = await asyncio.gather(*(allow_notifications(db, user_id=PAYER, allowed_user_id=CLAIMANT, now=NOW) for _ in range(4)))
    assert sum(result.changed for result in results) == 1
    assert (await list_allowed(db, PAYER)).total == 1


async def test_idempotency_key_replays(db: AsyncEngine) -> None:
    first = await allow_notifications(db, user_id=PAYER, allowed_user_id=CLAIMANT, now=NOW, idempotency_key="n-1")
    again = await allow_notifications(db, user_id=PAYER, allowed_user_id=CLAIMANT, now=NOW, idempotency_key="n-1")
    assert again.replayed
    assert again.changed
    assert again.execution_id == first.execution_id
