"""API tokens: one active bearer token per account grants every write the account can make; only its SHA-256 is stored, and the plaintext is returned once in IssuedToken and never written to the database, execution JSON or logs."""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import Select, insert, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.core.clock import resolve_now
from rapidwire.core.db import Db
from rapidwire.core.errors import NotFoundError
from rapidwire.core.ledger import LedgerTx, LedgerUsageError, OperationResult, is_retryable, run_operation
from rapidwire.core.paging import TOKENS_PER_PAGE_DEFAULT, TOKENS_PER_PAGE_MAX, Page, read_page
from rapidwire.core.types import ExecutionType
from rapidwire.core.validation import validate_positive_id, validate_user_account_id
from rapidwire.db.tables import api_token

__all__ = [
    "LAST_USED_RESOLUTION",
    "TOKEN_PREFIX",
    "IssuedToken",
    "SecretToken",
    "TokenInfo",
    "TokenIssue",
    "TokenRevocation",
    "authenticate_token",
    "generate_token",
    "hash_token",
    "is_well_formed_token",
    "issue_token",
    "issue_token_in_tx",
    "list_tokens",
    "revoke_tokens",
    "revoke_tokens_in_tx",
    "token_account_in_tx",
]

TOKEN_PREFIX = "rw_"  # noqa: S105
_TOKEN_BYTES = 32
_TOKEN_PATTERN = re.compile(re.escape(TOKEN_PREFIX) + r"[A-Za-z0-9_-]{43}")
LAST_USED_RESOLUTION = timedelta(minutes=1)

log = logging.getLogger("rapidwire.core.tokens")


def _ids(values: Iterable[Any]) -> tuple[int, ...]:
    return tuple(sorted(int(value) for value in values))


def _id_list(op: OperationResult, key: str) -> tuple[int, ...]:
    values = op.output.get(key)
    return _ids(values if isinstance(values, list) else [])


@dataclass(frozen=True)
class IssuedToken:
    """A newly issued token; token is the plaintext to hand to the user once and is excluded from repr, and an issue is never replayed."""

    token: str = field(repr=False)
    token_id: int
    execution_id: int
    revoked_token_ids: tuple[int, ...]


@dataclass(frozen=True)
class TokenIssue:
    """The stored side of an issue: the new token's ID and the earlier tokens it revoked; replayed takes no part in equality."""

    execution_id: int
    token_id: int
    revoked_token_ids: tuple[int, ...]
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"token_id": self.token_id, "revoked_token_ids": self.revoked_token_ids}

    @classmethod
    def from_operation(cls, op: OperationResult) -> TokenIssue:
        return cls(execution_id=op.execution_id, token_id=op.int_field("token_id"), revoked_token_ids=_id_list(op, "revoked_token_ids"), replayed=op.replayed)


@dataclass(frozen=True)
class TokenRevocation:
    """The tokens a revoke revoked (empty when none was active); replayed takes no part in equality."""

    execution_id: int
    revoked_token_ids: tuple[int, ...]
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"revoked_token_ids": self.revoked_token_ids}

    @classmethod
    def from_operation(cls, op: OperationResult) -> TokenRevocation:
        return cls(execution_id=op.execution_id, revoked_token_ids=_id_list(op, "revoked_token_ids"), replayed=op.replayed)


@dataclass(frozen=True)
class TokenInfo:
    """Metadata of a stored token; the hash is never exposed."""

    token_id: int
    account_id: int
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class SecretToken:
    """A plaintext token whose repr and str are redacted, so tracebacks that render locals do not show it; reveal() returns the text."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def __repr__(self) -> str:
        return "<redacted token>"

    __str__ = __repr__

    def reveal(self) -> str:
        return self._value


def generate_token() -> str:
    """A fresh token: the prefix followed by 32 random bytes in URL-safe base64 without padding."""
    return TOKEN_PREFIX + secrets.token_urlsafe(_TOKEN_BYTES)


def is_well_formed_token(token: object) -> bool:
    return isinstance(token, str) and _TOKEN_PATTERN.fullmatch(token) is not None


def hash_token(token: str) -> bytes:
    """The 32-byte SHA-256 digest stored in api_token.token_hash."""
    return hashlib.sha256(token.encode("ascii")).digest()


async def _lock_active(tx: LedgerTx, account_id: int, *, exclude: int | None = None) -> list[int]:
    # A locking read waits for rows that other transactions inserted but have not committed.
    stmt = select(api_token.c.api_token_id).where(api_token.c.account_id == account_id, api_token.c.revoked_at.is_(None))
    if exclude is not None:
        stmt = stmt.where(api_token.c.api_token_id != exclude)
    rows = await tx.conn.execute(stmt.order_by(api_token.c.api_token_id).with_for_update())
    return [int(row.api_token_id) for row in rows]


async def _revoke(tx: LedgerTx, token_ids: Iterable[int]) -> tuple[int, ...]:
    ids = _ids(set(token_ids))
    if ids:
        await tx.conn.execute(update(api_token).where(api_token.c.api_token_id.in_(ids)).values(revoked_at=tx.now))
    return ids


async def issue_token_in_tx(tx: LedgerTx, execution_id: int, *, account_id: int, token_hash: bytes) -> TokenIssue:
    """Store token_hash as the account's only active token, revoking the earlier ones; issuance is not a contract builtin, so the plaintext stays with issue_token().

    Earlier active rows, committed or not, are locked before the insert, so issuers of one account run one after another; two first-ever issuers that insert at the same time deadlock in the second locking read and the ledger retries the loser, which then revokes the winner's token.
    """
    validate_user_account_id(account_id)
    if len(token_hash) != hashlib.sha256().digest_size:
        raise LedgerUsageError("token_hash must be a SHA-256 digest")
    await tx.require_accounts([account_id])
    earlier = await _lock_active(tx, account_id)
    result = await tx.conn.execute(insert(api_token).values(account_id=account_id, token_hash=token_hash, created_at=tx.now))
    key = result.inserted_primary_key
    if key is None:
        raise LedgerUsageError("api_token insert returned no primary key")
    token_id = int(key[0])
    concurrent = await _lock_active(tx, account_id, exclude=token_id)
    return TokenIssue(execution_id=execution_id, token_id=token_id, revoked_token_ids=await _revoke(tx, [*earlier, *concurrent]))


async def issue_token(engine: AsyncEngine, *, account_id: int, now: datetime | None = None) -> IssuedToken:
    """Issue a new token for account_id as a token_issue execution, revoking the account's earlier tokens; the result holds the only copy of the plaintext, which until then is kept in a wrapper with a redacted repr."""
    secret = SecretToken(generate_token())
    token_hash = hash_token(secret.reveal())

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await issue_token_in_tx(tx, execution_id, account_id=account_id, token_hash=token_hash)).to_output()

    issued = TokenIssue.from_operation(await run_operation(engine, type=ExecutionType.TOKEN_ISSUE, initiator_id=account_id, input={}, body=body, now=now))
    return IssuedToken(token=secret.reveal(), token_id=issued.token_id, execution_id=issued.execution_id, revoked_token_ids=issued.revoked_token_ids)


async def revoke_tokens_in_tx(tx: LedgerTx, execution_id: int, *, account_id: int, token_id: int | None = None) -> TokenRevocation:
    """Revoke the account's active tokens, or only token_id when given; a token_id the account does not own raises NotFoundError, and an already revoked one revokes nothing."""
    validate_user_account_id(account_id)
    if token_id is None:
        return TokenRevocation(execution_id=execution_id, revoked_token_ids=await _revoke(tx, await _lock_active(tx, account_id)))
    validate_positive_id("token_id", token_id)
    stmt = select(api_token.c.account_id, api_token.c.revoked_at).where(api_token.c.api_token_id == token_id).with_for_update()
    row = (await tx.conn.execute(stmt)).one_or_none()
    if row is None or row.account_id != account_id:
        raise NotFoundError("The token was not found.")
    revoked = () if row.revoked_at is not None else await _revoke(tx, [token_id])
    return TokenRevocation(execution_id=execution_id, revoked_token_ids=revoked)


async def revoke_tokens(
    engine: AsyncEngine,
    *,
    account_id: int,
    token_id: int | None = None,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> TokenRevocation:
    """Revoke the account's active tokens (or one of them) as a token_revoke execution initiated by the owner."""

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await revoke_tokens_in_tx(tx, execution_id, account_id=account_id, token_id=token_id)).to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.TOKEN_REVOKE,
        initiator_id=account_id,
        input={"token_id": token_id},
        body=body,
        now=now,
        idempotency_key=idempotency_key,
    )
    return TokenRevocation.from_operation(op)


def _active_token_query(token: str) -> Select[Any]:
    return select(api_token.c.api_token_id, api_token.c.account_id, api_token.c.last_used_at).where(api_token.c.token_hash == hash_token(token), api_token.c.revoked_at.is_(None))


async def authenticate_token(engine: AsyncEngine, token: object, *, now: datetime | None = None) -> int | None:
    """The account ID of an active token, or None for an unknown, revoked or malformed one (a malformed token never reaches the database).

    The lookup is a plain read; when last_used_at is older than LAST_USED_RESOLUTION it is refreshed best-effort in a separate short transaction that touches only an unrevoked row.
    The refresh locks the row with SKIP LOCKED, so while a write holds the token row (its token check takes FOR SHARE) the refresh is skipped instead of waiting, and any database or connection error in it (a deadlock, a lock wait timeout, a dropped connection, a pool timeout) is logged and skipped; neither changes the answer.
    """
    if not isinstance(token, str) or not is_well_formed_token(token):
        return None
    at = resolve_now(now)
    async with engine.connect() as conn:
        row = (await conn.execute(_active_token_query(token))).one_or_none()
    if row is None:
        return None
    stale = at - LAST_USED_RESOLUTION
    if row.last_used_at is None or row.last_used_at <= stale:
        await _touch(engine, row.api_token_id, stale, at)
    return int(row.account_id)


async def _touch(engine: AsyncEngine, token_id: int, stale: datetime, at: datetime) -> None:
    """Set last_used_at to at when the unrevoked row is not locked by another transaction and its value is not newer than stale; any database or connection error is logged and leaves the value unchanged."""
    unchanged = (api_token.c.api_token_id == token_id, api_token.c.revoked_at.is_(None), api_token.c.last_used_at.is_(None) | (api_token.c.last_used_at <= stale))
    try:
        async with engine.begin() as conn:
            locked = (await conn.execute(select(api_token.c.api_token_id).where(*unchanged).with_for_update(skip_locked=True))).first()
            if locked is not None:
                await conn.execute(update(api_token).where(*unchanged).values(last_used_at=at))
    except (DBAPIError, PoolTimeoutError, OSError) as exc:
        if isinstance(exc, DBAPIError) and is_retryable(exc):
            log.warning("token last_used_at refresh skipped after a lock conflict", extra={"token_id": token_id})
        else:
            log.warning("token last_used_at refresh failed: %s", type(exc).__name__, extra={"token_id": token_id})


async def token_account_in_tx(conn: AsyncConnection, token: object) -> int | None:
    """The account ID of an active token read with FOR SHARE inside the caller's transaction, or None; a revoke committed before the read makes it None, and a revoke started after it waits for the caller's commit.

    It never writes; authenticate_token() is the call that refreshes last_used_at.
    """
    if not isinstance(token, str) or not is_well_formed_token(token):
        return None
    row = (await conn.execute(_active_token_query(token).with_for_update(read=True))).one_or_none()
    return None if row is None else int(row.account_id)


async def list_tokens(db: Db, account_id: int, *, include_revoked: bool = True, page: int = 1, per_page: int = TOKENS_PER_PAGE_DEFAULT) -> Page[TokenInfo]:
    """One page of the account's tokens in issue order, without hashes."""
    stmt = select(api_token.c.api_token_id, api_token.c.account_id, api_token.c.created_at, api_token.c.revoked_at, api_token.c.last_used_at).where(api_token.c.account_id == account_id)
    if not include_revoked:
        stmt = stmt.where(api_token.c.revoked_at.is_(None))
    return await read_page(
        db,
        stmt.order_by(api_token.c.api_token_id),
        key=api_token.c.api_token_id,
        page=page,
        per_page=per_page,
        per_page_max=TOKENS_PER_PAGE_MAX,
        convert=lambda row: TokenInfo(token_id=row.api_token_id, account_id=row.account_id, created_at=row.created_at, revoked_at=row.revoked_at, last_used_at=row.last_used_at),
    )
