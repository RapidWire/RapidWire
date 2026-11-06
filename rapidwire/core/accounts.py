"""Ledger accounts, which are never updated or deleted: Discord users, inserted lazily under their user ID, and system accounts with negative IDs (stake custody at -1, and one issuance account per currency and one custody account per DEX pool from an allocator)."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection

from rapidwire.core.clock import resolve_now
from rapidwire.core.errors import AccountNotFound, InvalidArgument
from rapidwire.core.types import AccountKind
from rapidwire.core.validation import validate_user_account_id
from rapidwire.db.tables import STAKE_CUSTODY_ACCOUNT_ID, account, system_account_allocation

__all__ = [
    "STAKE_CUSTODY_ACCOUNT_ID",
    "AccountInfo",
    "account_infos",
    "account_kind",
    "account_kinds",
    "create_pool_custody_account",
    "ensure_stake_custody_account",
    "ensure_user_account",
    "ensure_user_accounts",
    "get_or_create_issuance_account",
    "issuance_account_id",
    "pool_custody_account_id",
    "require_account_infos",
    "require_accounts",
]

_ALLOCATION_ATTEMPTS = 16


@dataclass(frozen=True)
class AccountInfo:
    """An account's kind and, for system accounts, the currency (issuance) or pool (pool custody) it belongs to."""

    account_id: int
    kind: AccountKind
    currency_id: int | None
    pool_id: int | None


async def account_infos(conn: AsyncConnection, account_ids: Iterable[int]) -> dict[int, AccountInfo]:
    """The existing accounts among account_ids; missing accounts are absent from the result."""
    ids = sorted(set(account_ids))
    if not ids:
        return {}
    rows = await conn.execute(select(account.c.account_id, account.c.kind, account.c.currency_id, account.c.pool_id).where(account.c.account_id.in_(ids)))
    return {row.account_id: AccountInfo(row.account_id, AccountKind(row.kind), row.currency_id, row.pool_id) for row in rows}


async def account_kinds(conn: AsyncConnection, account_ids: Iterable[int]) -> dict[int, AccountKind]:
    """The kind of each existing account among account_ids; missing accounts are absent from the result."""
    return {account_id: info.kind for account_id, info in (await account_infos(conn, account_ids)).items()}


async def account_kind(conn: AsyncConnection, account_id: int) -> AccountKind | None:
    """The kind of the account, or None when it does not exist."""
    return (await account_kinds(conn, [account_id])).get(account_id)


async def ensure_user_accounts(conn: AsyncConnection, account_ids: Iterable[int], *, now: datetime | None = None) -> None:
    """Insert the missing user accounts in ascending ID order with INSERT IGNORE, so a racing creator waits for the first insert to commit and then ignores the duplicate; every ID must be a valid Discord user ID (InvalidArgument otherwise)."""
    ids = sorted({validate_user_account_id(account_id) for account_id in account_ids})
    if not ids:
        return
    existing = await account_kinds(conn, ids)
    missing = [account_id for account_id in ids if account_id not in existing]
    if not missing:
        return
    created_at = resolve_now(now)
    for account_id in missing:
        await conn.execute(insert(account).prefix_with("IGNORE").values(account_id=account_id, kind=AccountKind.USER, currency_id=None, pool_id=None, created_at=created_at))
    await _confirm_inserted(conn, missing, AccountKind.USER)


async def _confirm_inserted(conn: AsyncConnection, account_ids: list[int], kind: AccountKind) -> None:
    """Re-read rows written with INSERT IGNORE, which turns constraint errors into warnings, and raise RuntimeError unless each exists with the expected kind."""
    found = await account_kinds(conn, account_ids)
    for account_id in account_ids:
        if found.get(account_id) is not kind:
            raise RuntimeError(f"account {account_id} could not be created as {kind} (found {found.get(account_id)})")


async def ensure_user_account(conn: AsyncConnection, account_id: int, *, now: datetime | None = None) -> None:
    """ensure_user_accounts for one ID."""
    await ensure_user_accounts(conn, [account_id], now=now)


async def ensure_stake_custody_account(conn: AsyncConnection, *, now: datetime | None = None) -> int:
    """Return STAKE_CUSTODY_ACCOUNT_ID, inserting its row when it is missing (the migration seeds it)."""
    if await account_kind(conn, STAKE_CUSTODY_ACCOUNT_ID) is None:
        values = {"account_id": STAKE_CUSTODY_ACCOUNT_ID, "kind": AccountKind.STAKE_CUSTODY, "currency_id": None, "pool_id": None, "created_at": resolve_now(now)}
        await conn.execute(insert(account).prefix_with("IGNORE").values(**values))
        await _confirm_inserted(conn, [STAKE_CUSTODY_ACCOUNT_ID], AccountKind.STAKE_CUSTODY)
    return STAKE_CUSTODY_ACCOUNT_ID


async def issuance_account_id(conn: AsyncConnection, currency_id: int) -> int | None:
    """The issuance account of the currency, or None when none was created."""
    row = await conn.execute(select(account.c.account_id).where(account.c.kind == AccountKind.ISSUANCE, account.c.currency_id == currency_id))
    return row.scalar_one_or_none()


async def pool_custody_account_id(conn: AsyncConnection, pool_id: int) -> int | None:
    """The custody account of the pool, or None when none was created."""
    row = await conn.execute(select(account.c.account_id).where(account.c.kind == AccountKind.POOL_CUSTODY, account.c.pool_id == pool_id))
    return row.scalar_one_or_none()


async def _allocate_system_account(conn: AsyncConnection, kind: AccountKind, *, currency_id: int | None, pool_id: int | None, now: datetime) -> int:
    for _ in range(_ALLOCATION_ATTEMPTS):
        result = await conn.execute(insert(system_account_allocation).values())
        allocation_id = result.inserted_primary_key
        if allocation_id is None:
            raise RuntimeError("system account allocator returned no id")
        account_id = -(int(allocation_id[0]) + 1)
        if await account_kind(conn, account_id) is not None:
            continue
        await conn.execute(insert(account).values(account_id=account_id, kind=kind, currency_id=currency_id, pool_id=pool_id, created_at=now))
        return account_id
    raise RuntimeError("could not allocate a free system account id")


async def _lookup_owned(conn: AsyncConnection, kind: AccountKind, owner_id: int) -> int | None:
    if kind is AccountKind.ISSUANCE:
        return await issuance_account_id(conn, owner_id)
    return await pool_custody_account_id(conn, owner_id)


async def _get_or_create(conn: AsyncConnection, kind: AccountKind, owner_id: int, *, now: datetime | None) -> int:
    if isinstance(owner_id, bool) or not isinstance(owner_id, int) or owner_id <= 0:
        raise InvalidArgument("Currency and pool IDs must be positive integers.")
    existing = await _lookup_owned(conn, kind, owner_id)
    if existing is not None:
        return existing
    currency_id = owner_id if kind is AccountKind.ISSUANCE else None
    pool_id = owner_id if kind is AccountKind.POOL_CUSTODY else None
    # A concurrent creator makes the insert fail on the unique currency_id or pool_id; the savepoint keeps the caller's transaction usable so the winner's row can be read.
    savepoint = await conn.begin_nested()
    try:
        account_id = await _allocate_system_account(conn, kind, currency_id=currency_id, pool_id=pool_id, now=resolve_now(now))
    except IntegrityError:
        await savepoint.rollback()
        found = await _lookup_owned(conn, kind, owner_id)
        if found is None:
            raise
        return found
    await savepoint.commit()
    return account_id


async def get_or_create_issuance_account(conn: AsyncConnection, currency_id: int, *, now: datetime | None = None) -> int:
    """The issuance account of the currency, created when missing; the account may exist before the currency row that references it."""
    return await _get_or_create(conn, AccountKind.ISSUANCE, currency_id, now=now)


async def create_pool_custody_account(conn: AsyncConnection, pool_id: int, *, now: datetime | None = None) -> int:
    """The custody account of the pool, created when missing (returns the existing one otherwise)."""
    return await _get_or_create(conn, AccountKind.POOL_CUSTODY, pool_id, now=now)


async def require_account_infos(conn: AsyncConnection, account_ids: Iterable[int], *, now: datetime | None = None) -> dict[int, AccountInfo]:
    """Make sure every account exists and return it: user IDs are created lazily, stake custody is created when missing, other system accounts must exist (AccountNotFound otherwise)."""
    ids = sorted(set(account_ids))
    users = [account_id for account_id in ids if account_id > 0]
    if users:
        await ensure_user_accounts(conn, users, now=now)
    if STAKE_CUSTODY_ACCOUNT_ID in ids:
        await ensure_stake_custody_account(conn, now=now)
    infos = await account_infos(conn, ids)
    for account_id in ids:
        if account_id not in infos:
            raise AccountNotFound(f"Account {account_id} was not found.")
    return infos


async def require_accounts(conn: AsyncConnection, account_ids: Iterable[int], *, now: datetime | None = None) -> dict[int, AccountKind]:
    """require_account_infos returning only the kinds."""
    return {account_id: info.kind for account_id, info in (await require_account_infos(conn, account_ids, now=now)).items()}
