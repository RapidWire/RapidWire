from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, insert, select, text, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.core import tokens
from rapidwire.core.accounts import ensure_user_account
from rapidwire.core.errors import InvalidArgument, NotFoundError
from rapidwire.core.ledger import LedgerTx, run_operation
from rapidwire.core.tokens import (
    LAST_USED_RESOLUTION,
    TOKEN_PREFIX,
    authenticate_token,
    generate_token,
    hash_token,
    issue_token,
    issue_token_in_tx,
    list_tokens,
    revoke_tokens,
    token_account_in_tx,
)
from rapidwire.core.types import ExecutionStatus, ExecutionType
from rapidwire.db.tables import api_token, execution

NOW = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
ALICE = 111
BOB = 222


async def _executions(engine: AsyncEngine, type: str) -> list[dict[str, object]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(execution).where(execution.c.type == type).order_by(execution.c.execution_id))).mappings().all()
    return [dict(row) for row in rows]


async def _active_ids(engine: AsyncEngine, account_id: int) -> list[int]:
    return [info.token_id for info in (await list_tokens(engine, account_id, include_revoked=False)).items]


async def test_issue_stores_only_the_hash(db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    assert issued.token.startswith(TOKEN_PREFIX)
    assert issued.token not in repr(issued)
    async with db.connect() as conn:
        rows = (await conn.execute(select(api_token))).mappings().all()
        executions = (await conn.execute(select(execution))).mappings().all()
    assert len(rows) == 1
    assert rows[0]["token_hash"] == hashlib.sha256(issued.token.encode()).digest()
    assert rows[0]["api_token_id"] == issued.token_id
    assert rows[0]["account_id"] == ALICE
    secret_part = issued.token.removeprefix(TOKEN_PREFIX)
    for row in rows:
        assert all(secret_part not in str(value) for value in row.values())
    assert len(executions) == 1
    record = executions[0]
    assert record["execution_id"] == issued.execution_id
    assert record["type"] == ExecutionType.TOKEN_ISSUE
    assert record["status"] == ExecutionStatus.SUCCESS
    assert record["initiator_id"] == ALICE
    stored = json.dumps([record["input"], record["output"]])
    assert secret_part not in stored
    assert hash_token(issued.token).hex() not in stored
    assert record["output"] == {"token_id": str(issued.token_id), "revoked_token_ids": []}


async def test_authenticate_returns_the_account(db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    assert await authenticate_token(db, issued.token, now=NOW) == ALICE
    assert await authenticate_token(db, generate_token(), now=NOW) is None


async def test_reissue_invalidates_the_previous_token(db: AsyncEngine) -> None:
    first = await issue_token(db, account_id=ALICE, now=NOW)
    other = await issue_token(db, account_id=BOB, now=NOW)
    second = await issue_token(db, account_id=ALICE, now=NOW + timedelta(seconds=5))
    assert first.token != second.token
    assert second.revoked_token_ids == (first.token_id,)
    assert await authenticate_token(db, first.token, now=NOW) is None
    assert await authenticate_token(db, second.token, now=NOW) == ALICE
    assert await authenticate_token(db, other.token, now=NOW) == BOB
    listed = await list_tokens(db, ALICE)
    assert (listed.total, listed.page, listed.per_page) == (2, 1, 15)
    infos = listed.items
    assert [(info.token_id, info.active) for info in infos] == [(first.token_id, False), (second.token_id, True)]
    assert infos[0].revoked_at == NOW + timedelta(seconds=5)


async def test_concurrent_issues_leave_one_active_token(db: AsyncEngine) -> None:
    issued = await asyncio.gather(*(issue_token(db, account_id=ALICE, now=NOW) for _ in range(4)))
    active = await _active_ids(db, ALICE)
    assert len(active) == 1
    winners = [token for token in issued if await authenticate_token(db, token.token, now=NOW) == ALICE]
    assert [token.token_id for token in winners] == active
    assert len(await _executions(db, ExecutionType.TOKEN_ISSUE)) == 4


async def test_overlapping_first_issues_leave_one_active_token(db: AsyncEngine) -> None:
    async def issue(index: int) -> int:
        async def body(tx: LedgerTx, execution_id: int) -> dict[str, object]:
            issued = await issue_token_in_tx(tx, execution_id, account_id=BOB, token_hash=hash_token(f"{TOKEN_PREFIX}{index:043d}"))
            await asyncio.sleep(0.05)
            return issued.to_output()

        result = await run_operation(db, type=ExecutionType.TOKEN_ISSUE, initiator_id=BOB, input={}, body=body, now=NOW)
        return result.int_field("token_id")

    await asyncio.gather(issue(1), issue(2))
    assert len(await _active_ids(db, BOB)) == 1


async def test_revoke_all(db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    revocation = await revoke_tokens(db, account_id=ALICE, now=NOW + timedelta(minutes=1))
    assert revocation.revoked_token_ids == (issued.token_id,)
    assert await authenticate_token(db, issued.token, now=NOW) is None
    again = await revoke_tokens(db, account_id=ALICE, now=NOW + timedelta(minutes=2))
    assert again.revoked_token_ids == ()
    records = await _executions(db, ExecutionType.TOKEN_REVOKE)
    assert [record["status"] for record in records] == [ExecutionStatus.SUCCESS, ExecutionStatus.SUCCESS]
    assert records[0]["output"] == {"revoked_token_ids": [str(issued.token_id)]}


async def test_revoke_one_token_and_ownership(db: AsyncEngine) -> None:
    alice = await issue_token(db, account_id=ALICE, now=NOW)
    bob = await issue_token(db, account_id=BOB, now=NOW)
    with pytest.raises(NotFoundError) as denied:
        await revoke_tokens(db, account_id=ALICE, token_id=bob.token_id, now=NOW)
    assert denied.value.execution_id is not None
    assert await authenticate_token(db, bob.token, now=NOW) == BOB
    with pytest.raises(NotFoundError):
        await revoke_tokens(db, account_id=ALICE, token_id=999_999, now=NOW)
    revocation = await revoke_tokens(db, account_id=ALICE, token_id=alice.token_id, now=NOW)
    assert revocation.revoked_token_ids == (alice.token_id,)
    assert (await revoke_tokens(db, account_id=ALICE, token_id=alice.token_id, now=NOW)).revoked_token_ids == ()
    statuses = [record["status"] for record in await _executions(db, ExecutionType.TOKEN_REVOKE)]
    assert statuses == [ExecutionStatus.FAILED, ExecutionStatus.FAILED, ExecutionStatus.SUCCESS, ExecutionStatus.SUCCESS]


async def test_revoke_is_idempotent_with_a_key(db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    first = await revoke_tokens(db, account_id=ALICE, now=NOW, idempotency_key="revoke-1")
    again = await revoke_tokens(db, account_id=ALICE, now=NOW, idempotency_key="revoke-1")
    assert again.replayed
    assert again.execution_id == first.execution_id
    assert again.revoked_token_ids == (issued.token_id,)


@pytest.mark.parametrize("token", ["", "rw_short", "rw_" + "a" * 42 + "'", "rw_" + "a" * 42 + chr(0xE9), None])
async def test_malformed_tokens_are_rejected_on_a_live_database(db: AsyncEngine, token: object) -> None:
    await issue_token(db, account_id=ALICE, now=NOW)
    assert await authenticate_token(db, token, now=NOW) is None


async def test_last_used_is_updated_coarsely(db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    assert (await list_tokens(db, ALICE)).items[0].last_used_at is None
    assert await authenticate_token(db, issued.token, now=NOW) == ALICE
    assert (await list_tokens(db, ALICE)).items[0].last_used_at == NOW
    assert await authenticate_token(db, issued.token, now=NOW + LAST_USED_RESOLUTION - timedelta(seconds=1)) == ALICE
    assert (await list_tokens(db, ALICE)).items[0].last_used_at == NOW
    later = NOW + LAST_USED_RESOLUTION
    assert await authenticate_token(db, issued.token, now=later) == ALICE
    assert (await list_tokens(db, ALICE)).items[0].last_used_at == later


async def test_last_used_refresh_skips_a_token_row_held_by_a_write(db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    async with db.connect() as writer:
        await writer.begin()
        assert await token_account_in_tx(writer, issued.token) == ALICE
        # The write's FOR SHARE lock would make a waiting refresh fail with a lock wait timeout; the refresh skips the row at once instead.
        assert await asyncio.wait_for(authenticate_token(db, issued.token, now=NOW), timeout=2) == ALICE
        await writer.commit()
    assert (await list_tokens(db, ALICE)).items[0].last_used_at is None
    assert await authenticate_token(db, issued.token, now=NOW) == ALICE
    assert (await list_tokens(db, ALICE)).items[0].last_used_at == NOW


def _failing_execute(code: int) -> Callable[..., Awaitable[object]]:
    async def execute(self: AsyncConnection, *args: object, **kwargs: object) -> object:
        raise OperationalError("SELECT api_token", {}, Exception(code, "injected"))

    return execute


async def test_last_used_refresh_lock_conflicts_are_logged_and_skipped(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    async with db.connect() as conn:
        token_id = (await conn.execute(select(api_token.c.api_token_id))).scalar_one()
    for code in (1205, 1213):
        with caplog.at_level(logging.WARNING, logger="rapidwire.core.tokens"), monkeypatch.context() as patch:
            patch.setattr(AsyncConnection, "execute", _failing_execute(code))
            await tokens._touch(db, token_id, NOW, NOW)
    assert [record.getMessage() for record in caplog.records] == ["token last_used_at refresh skipped after a lock conflict"] * 2
    assert (await list_tokens(db, ALICE)).items[0].last_used_at is None
    assert await authenticate_token(db, issued.token, now=NOW) == ALICE


@pytest.mark.parametrize("error", [OperationalError("SELECT api_token", {}, Exception(2013, "Lost connection")), PoolTimeoutError("QueuePool limit reached"), ConnectionResetError("reset")])
async def test_last_used_refresh_failures_never_fail_authentication(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, error: Exception) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)

    def failing_begin(self: AsyncEngine) -> object:
        raise error

    with caplog.at_level(logging.WARNING, logger="rapidwire.core.tokens"), monkeypatch.context() as patch:
        # The lookup reads through connect(); only the refresh transaction fails.
        patch.setattr(AsyncEngine, "begin", failing_begin)
        assert await authenticate_token(db, issued.token, now=NOW) == ALICE
    assert [record.getMessage() for record in caplog.records] == [f"token last_used_at refresh failed: {type(error).__name__}"]
    [info] = (await list_tokens(db, ALICE)).items
    assert info.last_used_at is None
    with monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, "execute", _failing_execute(2013))
        await tokens._touch(db, info.token_id, NOW, NOW)
    assert (await list_tokens(db, ALICE)).items[0].last_used_at is None


async def test_token_revoked_in_the_database_is_rejected(db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    async with db.begin() as conn:
        await conn.execute(update(api_token).values(revoked_at=NOW))
    assert await authenticate_token(db, issued.token, now=NOW) is None
    async with db.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(api_token))).scalar_one() == 1


async def test_authenticating_a_revoked_token_leaves_last_used_unchanged(db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    assert await authenticate_token(db, issued.token, now=NOW) == ALICE
    await revoke_tokens(db, account_id=ALICE, now=NOW)
    assert await authenticate_token(db, issued.token, now=NOW + timedelta(hours=1)) is None
    assert (await list_tokens(db, ALICE)).items[0].last_used_at == NOW


@pytest.mark.parametrize("token_id", [True, False, 0, -3])
async def test_revoke_with_invalid_token_id_fails_and_is_recorded(db: AsyncEngine, token_id: int) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    with pytest.raises(InvalidArgument) as error:
        await revoke_tokens(db, account_id=ALICE, token_id=token_id, now=NOW)
    assert error.value.execution_id is not None
    records = await _executions(db, ExecutionType.TOKEN_REVOKE)
    assert [(record["execution_id"], record["status"]) for record in records] == [(error.value.execution_id, ExecutionStatus.FAILED)]
    assert await authenticate_token(db, issued.token, now=NOW) == ALICE


async def _wait_for_lock_wait(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for _ in range(200):
            if (await conn.execute(text("SELECT COUNT(*) FROM performance_schema.data_lock_waits"))).scalar_one():
                return
            await conn.rollback()
            await asyncio.sleep(0.05)
        states = (await conn.execute(text("SELECT id, command, state, info FROM information_schema.processlist WHERE command <> 'Sleep'"))).all()
    raise AssertionError(f"no transaction started waiting for a lock: {states}")


async def test_issue_waits_for_an_uncommitted_token_and_revokes_it(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await ensure_user_account(conn, BOB, now=NOW)
    async with db.connect() as other:
        await other.begin()
        result = await other.execute(insert(api_token).values(account_id=BOB, token_hash=hash_token(generate_token()), created_at=NOW))
        assert result.inserted_primary_key is not None
        pending_id = int(result.inserted_primary_key[0])
        task = asyncio.create_task(issue_token(db, account_id=BOB, now=NOW + timedelta(seconds=1)))
        await _wait_for_lock_wait(db)
        assert not task.done()
        await other.commit()
    issued = await task
    assert issued.revoked_token_ids == (pending_id,)
    infos = (await list_tokens(db, BOB)).items
    assert [(info.token_id, info.revoked_at) for info in infos] == [(pending_id, NOW + timedelta(seconds=1)), (issued.token_id, None)]


async def test_token_account_in_tx_reads_without_writing_and_blocks_revoke(db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE, now=NOW)
    async with db.connect() as conn:
        await conn.begin()
        assert await token_account_in_tx(conn, issued.token) == ALICE
        assert await token_account_in_tx(conn, "not-a-token") is None
        revoke = asyncio.create_task(revoke_tokens(db, account_id=ALICE, now=NOW))
        await _wait_for_lock_wait(db)
        assert not revoke.done()
        await conn.commit()
    await revoke
    assert (await list_tokens(db, ALICE)).items[0].last_used_at is None
    async with db.begin() as conn:
        assert await token_account_in_tx(conn, issued.token) is None
