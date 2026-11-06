"""Claim notification permissions, off by default: a row (user_id, allowed_user_id) lets the worker send user_id a direct message about each claim allowed_user_id makes against them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.db import Db, reader
from rapidwire.core.errors import InvalidArgument
from rapidwire.core.ledger import DUPLICATE_KEY_ERROR, LedgerTx, OperationResult, mysql_error_code, run_operation
from rapidwire.core.paging import NOTIFICATIONS_PER_PAGE_DEFAULT, NOTIFICATIONS_PER_PAGE_MAX, Page, read_page
from rapidwire.core.types import ExecutionType
from rapidwire.core.validation import validate_user_account_id
from rapidwire.db.tables import notification_permission

__all__ = [
    "NotificationChange",
    "NotificationPermission",
    "allow_notifications",
    "allow_notifications_in_tx",
    "deny_notifications",
    "deny_notifications_in_tx",
    "is_notification_allowed",
    "list_allowed",
]


@dataclass(frozen=True)
class NotificationChange:
    """The result of an allow or deny; changed is False when the permission already had the requested state, and replayed takes no part in equality."""

    execution_id: int
    user_id: int
    allowed_user_id: int
    changed: bool
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"user_id": self.user_id, "allowed_user_id": self.allowed_user_id, "changed": self.changed}

    @classmethod
    def from_operation(cls, op: OperationResult) -> NotificationChange:
        return cls(
            execution_id=op.execution_id,
            user_id=op.int_field("user_id"),
            allowed_user_id=op.int_field("allowed_user_id"),
            changed=op.output.get("changed") is True,
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class NotificationPermission:
    allowed_user_id: int
    created_at: datetime


def _validate_pair(user_id: int, allowed_user_id: int) -> None:
    validate_user_account_id(user_id)
    validate_user_account_id(allowed_user_id)
    if user_id == allowed_user_id:
        raise InvalidArgument("You cannot change claim notifications for yourself.")


async def allow_notifications_in_tx(tx: LedgerTx, execution_id: int, *, user_id: int, allowed_user_id: int) -> NotificationChange:
    """Let claims from allowed_user_id to user_id be sent to user_id; changed is False when the permission already existed."""
    _validate_pair(user_id, allowed_user_id)
    await tx.require_accounts([user_id, allowed_user_id])
    key = (notification_permission.c.user_id == user_id, notification_permission.c.allowed_user_id == allowed_user_id)
    unchanged = NotificationChange(execution_id=execution_id, user_id=user_id, allowed_user_id=allowed_user_id, changed=False)
    if (await tx.conn.execute(select(notification_permission.c.user_id).where(*key).with_for_update())).first() is not None:
        return unchanged
    # A concurrent allow of the same pair makes this insert fail on the primary key after the other transaction commits.
    savepoint = await tx.conn.begin_nested()
    try:
        await tx.conn.execute(insert(notification_permission).values(user_id=user_id, allowed_user_id=allowed_user_id, created_at=tx.now))
    except IntegrityError as exc:
        await savepoint.rollback()
        if mysql_error_code(exc) == DUPLICATE_KEY_ERROR:
            return unchanged
        raise
    await savepoint.commit()
    return NotificationChange(execution_id=execution_id, user_id=user_id, allowed_user_id=allowed_user_id, changed=True)


async def deny_notifications_in_tx(tx: LedgerTx, execution_id: int, *, user_id: int, allowed_user_id: int) -> NotificationChange:
    """Stop sending user_id claims from allowed_user_id; changed is False when no permission existed."""
    _validate_pair(user_id, allowed_user_id)
    result = await tx.conn.execute(delete(notification_permission).where(notification_permission.c.user_id == user_id, notification_permission.c.allowed_user_id == allowed_user_id))
    return NotificationChange(execution_id=execution_id, user_id=user_id, allowed_user_id=allowed_user_id, changed=result.rowcount > 0)


async def _change(engine: AsyncEngine, *, allow: bool, user_id: int, allowed_user_id: int, now: datetime | None, idempotency_key: str | None) -> NotificationChange:
    change_in_tx = allow_notifications_in_tx if allow else deny_notifications_in_tx

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await change_in_tx(tx, execution_id, user_id=user_id, allowed_user_id=allowed_user_id)).to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.NOTIFICATION_ALLOW if allow else ExecutionType.NOTIFICATION_DENY,
        initiator_id=user_id,
        input={"allowed_user_id": allowed_user_id},
        body=body,
        now=now,
        idempotency_key=idempotency_key,
        user_accounts=[allowed_user_id] if allow else [],
    )
    return NotificationChange.from_operation(op)


async def allow_notifications(engine: AsyncEngine, *, user_id: int, allowed_user_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> NotificationChange:
    """Run allow_notifications_in_tx as a notification_allow execution initiated by user_id; allowing oneself fails with InvalidArgument."""
    return await _change(engine, allow=True, user_id=user_id, allowed_user_id=allowed_user_id, now=now, idempotency_key=idempotency_key)


async def deny_notifications(engine: AsyncEngine, *, user_id: int, allowed_user_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> NotificationChange:
    """Run deny_notifications_in_tx as a notification_deny execution initiated by user_id; denying oneself fails with InvalidArgument."""
    return await _change(engine, allow=False, user_id=user_id, allowed_user_id=allowed_user_id, now=now, idempotency_key=idempotency_key)


async def list_allowed(db: Db, user_id: int, *, page: int = 1, per_page: int = NOTIFICATIONS_PER_PAGE_DEFAULT) -> Page[NotificationPermission]:
    """One page of the users whose claims user_id receives as direct messages, oldest permission first."""
    stmt = (
        select(notification_permission.c.allowed_user_id, notification_permission.c.created_at)
        .where(notification_permission.c.user_id == user_id)
        .order_by(notification_permission.c.created_at, notification_permission.c.allowed_user_id)
    )
    return await read_page(
        db,
        stmt,
        key=notification_permission.c.allowed_user_id,
        page=page,
        per_page=per_page,
        per_page_max=NOTIFICATIONS_PER_PAGE_MAX,
        convert=lambda row: NotificationPermission(allowed_user_id=row.allowed_user_id, created_at=row.created_at),
    )


async def is_notification_allowed(db: Db, payer_id: int, claimant_id: int) -> bool:
    """Whether payer_id receives direct messages about claims from claimant_id."""
    stmt = select(notification_permission.c.user_id).where(notification_permission.c.user_id == payer_id, notification_permission.c.allowed_user_id == claimant_id)
    async with reader(db) as conn:
        return (await conn.execute(stmt)).first() is not None
