"""Outbox: external side effects (Discord messages and roles) are written as pending rows in the caller's transaction, so a rolled-back transaction sends nothing, and the worker's dispatcher sends them after commit."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import insert
from sqlalchemy.ext.asyncio import AsyncConnection

from rapidwire.core.clock import ensure_utc, resolve_now
from rapidwire.core.ledger import LedgerTx
from rapidwire.core.types import OutboxKind, OutboxStatus, to_json_object
from rapidwire.db.tables import outbox

__all__ = ["CLAIM_CREATED", "CONTRACT_DISCORD_KINDS", "OutboxKind", "enqueue"]

CLAIM_CREATED = OutboxKind.CLAIM_CREATED
# The kinds contracts queue through their Discord builtins; their outcomes are public with the execution, unlike claim_created.
CONTRACT_DISCORD_KINDS = frozenset({OutboxKind.DISCORD_SEND, OutboxKind.DISCORD_ADD_ROLE, OutboxKind.DISCORD_REMOVE_ROLE})


async def enqueue(
    target: LedgerTx | AsyncConnection,
    *,
    kind: str,
    payload: Mapping[str, Any],
    execution_id: int | None = None,
    available_at: datetime | None = None,
    now: datetime | None = None,
) -> int:
    """Insert a pending outbox row in the caller's transaction and return its ID; the time defaults to the LedgerTx's now (or the clock), and available_at defaults to that time."""
    if isinstance(target, LedgerTx):
        conn, at = target.conn, ensure_utc(now) if now is not None else target.now
    else:
        conn, at = target, resolve_now(now)
    values = {
        "execution_id": execution_id,
        "kind": str(kind),
        "payload": to_json_object(payload),
        "status": OutboxStatus.PENDING.value,
        "attempts": 0,
        "available_at": ensure_utc(available_at) if available_at is not None else at,
        "created_at": at,
    }
    result = await conn.execute(insert(outbox).values(**values))
    key = result.inserted_primary_key
    if key is None:
        raise RuntimeError("outbox insert returned no primary key")
    return int(key[0])
