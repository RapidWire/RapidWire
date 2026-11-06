"""Discord operation permissions for contracts: a row (guild_id, account_id) lets the contracts owned by account_id send messages and change roles in guild_id, and only the guild owner or an Administrator may allow, deny or list them."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.db import Db, reader
from rapidwire.core.ledger import DUPLICATE_KEY_ERROR, LedgerTx, OperationResult, mysql_error_code, run_operation
from rapidwire.core.paging import DISCORD_PERMISSIONS_PER_PAGE_DEFAULT, DISCORD_PERMISSIONS_PER_PAGE_MAX, Page, read_page
from rapidwire.core.permissions import GuildInfo, GuildResolver, GuildSnapshot, require_guild_administrator, snapshot_guild
from rapidwire.core.types import ExecutionType
from rapidwire.core.validation import validate_guild_id, validate_user_account_id
from rapidwire.db.tables import discord_permission

__all__ = [
    "DiscordPermission",
    "DiscordPermissionChange",
    "allow_discord",
    "allow_discord_in_tx",
    "deny_discord",
    "deny_discord_in_tx",
    "is_discord_allowed",
    "list_discord_permissions",
    "list_discord_permissions_as",
]

_DENIED = "Only the server owner or an administrator can manage Discord permissions for contracts."


@dataclass(frozen=True)
class DiscordPermissionChange:
    """The result of an allow or deny; changed is False when the permission already had the requested state (an existing permission keeps its granted_by), and replayed takes no part in equality."""

    execution_id: int
    guild_id: int
    account_id: int
    changed: bool
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"guild_id": self.guild_id, "account_id": self.account_id, "changed": self.changed}

    @classmethod
    def from_operation(cls, op: OperationResult) -> DiscordPermissionChange:
        return cls(
            execution_id=op.execution_id,
            guild_id=op.int_field("guild_id"),
            account_id=op.int_field("account_id"),
            changed=op.output.get("changed") is True,
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class DiscordPermission:
    guild_id: int
    account_id: int
    granted_by: int
    created_at: datetime


def _validate(guild: GuildSnapshot, guild_id: int, account_id: int, acting_user_id: int) -> None:
    validate_guild_id(guild_id)
    validate_user_account_id(account_id)
    validate_user_account_id(acting_user_id)
    require_guild_administrator(guild, guild_id, acting_user_id, message=_DENIED)


async def allow_discord_in_tx(tx: LedgerTx, execution_id: int, guild: GuildSnapshot, *, guild_id: int, account_id: int, acting_user_id: int) -> DiscordPermissionChange:
    """Let the contracts of account_id perform Discord operations in guild_id, recording acting_user_id as granted_by; raises GuildUnavailable or PermissionDenied from the snapshot."""
    _validate(guild, guild_id, account_id, acting_user_id)
    await tx.require_accounts([account_id, acting_user_id])
    key = (discord_permission.c.guild_id == guild_id, discord_permission.c.account_id == account_id)
    unchanged = DiscordPermissionChange(execution_id=execution_id, guild_id=guild_id, account_id=account_id, changed=False)
    if (await tx.conn.execute(select(discord_permission.c.account_id).where(*key).with_for_update())).first() is not None:
        return unchanged
    # A concurrent allow of the same pair makes this insert fail on the primary key after the other transaction commits.
    savepoint = await tx.conn.begin_nested()
    try:
        await tx.conn.execute(insert(discord_permission).values(guild_id=guild_id, account_id=account_id, granted_by=acting_user_id, created_at=tx.now))
    except IntegrityError as exc:
        await savepoint.rollback()
        if mysql_error_code(exc) == DUPLICATE_KEY_ERROR:
            return unchanged
        raise
    await savepoint.commit()
    return DiscordPermissionChange(execution_id=execution_id, guild_id=guild_id, account_id=account_id, changed=True)


async def deny_discord_in_tx(tx: LedgerTx, execution_id: int, guild: GuildSnapshot, *, guild_id: int, account_id: int, acting_user_id: int) -> DiscordPermissionChange:
    """Withdraw the Discord permission of account_id in guild_id; changed is False when none existed."""
    _validate(guild, guild_id, account_id, acting_user_id)
    result = await tx.conn.execute(delete(discord_permission).where(discord_permission.c.guild_id == guild_id, discord_permission.c.account_id == account_id))
    return DiscordPermissionChange(execution_id=execution_id, guild_id=guild_id, account_id=account_id, changed=result.rowcount > 0)


async def _change(
    engine: AsyncEngine,
    guild_info: GuildInfo,
    *,
    allow: bool,
    guild_id: int,
    account_id: int,
    acting_user_id: int,
    now: datetime | None,
    idempotency_key: str | None,
) -> DiscordPermissionChange:
    guild = GuildResolver(guild_info, guild_id, acting_user_id)
    change_in_tx = allow_discord_in_tx if allow else deny_discord_in_tx

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await change_in_tx(tx, execution_id, guild.snapshot, guild_id=guild_id, account_id=account_id, acting_user_id=acting_user_id)).to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.DISCORD_PERMISSION_ALLOW if allow else ExecutionType.DISCORD_PERMISSION_DENY,
        initiator_id=acting_user_id,
        input={"guild_id": guild_id, "account_id": account_id},
        body=body,
        now=now,
        idempotency_key=idempotency_key,
        user_accounts=[account_id] if allow else [],
        before=guild.resolve,
    )
    return DiscordPermissionChange.from_operation(op)


async def allow_discord(
    engine: AsyncEngine,
    guild_info: GuildInfo,
    *,
    guild_id: int,
    account_id: int,
    acting_user_id: int,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> DiscordPermissionChange:
    """Run allow_discord_in_tx as a discord_permission_allow execution initiated by acting_user_id; a failed administrator check is recorded as a failed execution."""
    return await _change(engine, guild_info, allow=True, guild_id=guild_id, account_id=account_id, acting_user_id=acting_user_id, now=now, idempotency_key=idempotency_key)


async def deny_discord(
    engine: AsyncEngine,
    guild_info: GuildInfo,
    *,
    guild_id: int,
    account_id: int,
    acting_user_id: int,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> DiscordPermissionChange:
    """Run deny_discord_in_tx as a discord_permission_deny execution initiated by acting_user_id; a failed administrator check is recorded as a failed execution."""
    return await _change(engine, guild_info, allow=False, guild_id=guild_id, account_id=account_id, acting_user_id=acting_user_id, now=now, idempotency_key=idempotency_key)


async def list_discord_permissions(db: Db, guild_id: int, *, page: int = 1, per_page: int = DISCORD_PERMISSIONS_PER_PAGE_DEFAULT) -> Page[DiscordPermission]:
    """One page of the accounts whose contracts may perform Discord operations in guild_id, oldest permission first, without any caller check."""
    stmt = (
        select(discord_permission.c.guild_id, discord_permission.c.account_id, discord_permission.c.granted_by, discord_permission.c.created_at)
        .where(discord_permission.c.guild_id == guild_id)
        .order_by(discord_permission.c.created_at, discord_permission.c.account_id)
    )
    return await read_page(
        db,
        stmt,
        key=discord_permission.c.account_id,
        page=page,
        per_page=per_page,
        per_page_max=DISCORD_PERMISSIONS_PER_PAGE_MAX,
        convert=lambda row: DiscordPermission(guild_id=row.guild_id, account_id=row.account_id, granted_by=row.granted_by, created_at=row.created_at),
    )


async def list_discord_permissions_as(db: Db, guild_info: GuildInfo, *, guild_id: int, user_id: int, page: int = 1, per_page: int = DISCORD_PERMISSIONS_PER_PAGE_DEFAULT) -> Page[DiscordPermission]:
    """list_discord_permissions for a caller who must own guild_id or have Administrator there (GuildUnavailable or PermissionDenied otherwise); it writes no execution."""
    validate_guild_id(guild_id)
    validate_user_account_id(user_id)
    require_guild_administrator(await snapshot_guild(guild_info, guild_id, user_id), guild_id, user_id, message=_DENIED)
    return await list_discord_permissions(db, guild_id, page=page, per_page=per_page)


async def is_discord_allowed(db: Db, guild_id: int, account_id: int) -> bool:
    """Whether the contracts of account_id may perform Discord operations in guild_id."""
    stmt = select(discord_permission.c.account_id).where(discord_permission.c.guild_id == guild_id, discord_permission.c.account_id == account_id)
    async with reader(db) as conn:
        return (await conn.execute(stmt)).first() is not None
