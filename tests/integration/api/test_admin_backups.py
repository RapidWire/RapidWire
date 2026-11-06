from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import anyio.from_thread
import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.requests import ClientDisconnect
from starlette.types import Message

from rapidwire.api import backups
from rapidwire.api.app import create_app
from rapidwire.api.routers import admin
from rapidwire.config import Settings
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.tokens import revoke_tokens
from rapidwire.core.types import ExecutionStatus, ExecutionType
from rapidwire.db.tables import execution
from tests.integration.api.conftest import API_TEST_PASSWORD, OPERATOR_ID, AuthHeaders, bearer

OTHER = 900_002
BASE_NS = 1_780_000_000_123_456_789
SECRET = b"outside-the-backup-dir-27182818"
LIST = "/v1/admin/backups"
BACKUP_TYPES = (ExecutionType.BACKUP_LIST, ExecutionType.BACKUP_DOWNLOAD)


def _write(directory: Path, name: str, data: bytes, mtime_ns: int) -> Path:
    path = directory / name
    path.write_bytes(data)
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _created_at(mtime_ns: int) -> datetime:
    seconds, nanos = divmod(mtime_ns, 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC).replace(microsecond=nanos // 1000)


async def _backup_executions(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(execution).where(execution.c.type.in_(BACKUP_TYPES)).order_by(execution.c.execution_id))).mappings().all()
    return [dict(row) for row in rows]


def _assert_error(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code


@pytest.fixture
def count_hashes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Count the sizes passed to every SHA-256 computation."""
    calls: list[int] = []
    real = backups.sha256_of_fd

    def counting(fd: int, size: int) -> str:
        calls.append(size)
        return real(fd, size)

    monkeypatch.setattr(backups, "sha256_of_fd", counting)
    return calls


@pytest.fixture
def closes(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the name of every OpenedBackup whose descriptor is closed."""
    closed: list[str] = []
    real = backups.OpenedBackup.close

    def recording(self: backups.OpenedBackup) -> None:
        if not self._closed:
            closed.append(self.name)
        real(self)

    monkeypatch.setattr(backups.OpenedBackup, "close", recording)
    return closed


async def test_operator_lists_backups_newest_first(client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    data = {"old.sql.gz": b"old dump", "new.tar.gz": b"newest export" * 100, "mid-backup_1.jsonl": b""}
    times = {"old.sql.gz": BASE_NS, "new.tar.gz": BASE_NS + 2_000_000_000, "mid-backup_1.jsonl": BASE_NS + 1_000_000_000}
    for name, content in data.items():
        _write(backup_dir, name, content, times[name])
    response = await client.get(LIST, headers=await auth_headers(OPERATOR_ID))
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert set(body) == {"execution_id", "items", "page", "per_page", "total", "total_is_lower_bound"}
    assert (body["page"], body["per_page"], body["total"], body["total_is_lower_bound"]) == (1, 15, 3, False)
    assert [item["name"] for item in body["items"]] == ["new.tar.gz", "mid-backup_1.jsonl", "old.sql.gz"]
    for item in body["items"]:
        name = item["name"]
        assert set(item) == {"name", "created_at", "size", "sha256"}
        assert item["size"] == len(data[name])
        assert item["sha256"] == hashlib.sha256(data[name]).hexdigest()
        assert item["created_at"].endswith("Z")
        assert datetime.fromisoformat(item["created_at"]) == _created_at(times[name])
    [record] = await _backup_executions(db)
    assert str(record["execution_id"]) == body["execution_id"]
    assert (record["type"], record["initiator_id"], record["status"]) == (ExecutionType.BACKUP_LIST, OPERATOR_ID, ExecutionStatus.SUCCESS)
    assert (record["input"], record["output"], record["idempotency_key"]) == ({"page": "1", "per_page": "15"}, {"count": "3", "total": "3"}, None)


async def test_listing_is_paged_and_hashes_only_the_page(client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders, db: AsyncEngine, count_hashes: list[int]) -> None:
    names = [f"b{index}.gz" for index in range(5)]
    for index, name in enumerate(names):
        _write(backup_dir, name, name.encode() * (index + 1), BASE_NS + index * 1_000_000_000)
    headers = await auth_headers(OPERATOR_ID)
    second = (await client.get(LIST, params={"page": 2, "per_page": 2}, headers=headers)).json()
    assert [item["name"] for item in second["items"]] == ["b2.gz", "b1.gz"]
    assert (second["page"], second["per_page"], second["total"]) == (2, 2, 5)
    assert sorted(count_hashes) == [len(b"b1.gz") * 2, len(b"b2.gz") * 3]
    last = (await client.get(LIST, params={"page": 3, "per_page": 2}, headers=headers)).json()
    assert [item["name"] for item in last["items"]] == ["b0.gz"]
    beyond = (await client.get(LIST, params={"page": 4, "per_page": 2}, headers=headers)).json()
    assert (beyond["items"], beyond["total"]) == ([], 5)
    records = [record for record in await _backup_executions(db) if record["type"] == ExecutionType.BACKUP_LIST]
    assert [(record["input"], record["output"]) for record in records] == [
        ({"page": "2", "per_page": "2"}, {"count": "2", "total": "5"}),
        ({"page": "3", "per_page": "2"}, {"count": "1", "total": "5"}),
        ({"page": "4", "per_page": "2"}, {"count": "0", "total": "5"}),
    ]
    for params in ({"per_page": 101}, {"page": 0}):
        assert (await client.get(LIST, params=params, headers=headers)).status_code == 422


async def test_equal_times_are_ordered_by_name(client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders) -> None:
    for name in ("b.gz", "a.gz", "c.gz"):
        _write(backup_dir, name, name.encode(), BASE_NS)
    body = (await client.get(LIST, headers=await auth_headers(OPERATOR_ID))).json()
    assert [item["name"] for item in body["items"]] == ["a.gz", "b.gz", "c.gz"]


async def test_listing_skips_entries_that_are_not_backups(client: httpx.AsyncClient, backup_dir: Path, tmp_path: Path, auth_headers: AuthHeaders) -> None:
    _write(backup_dir, "good.sql.gz", b"good", BASE_NS)
    for name in (".hidden.gz", "dump.sql.gz.tmp", "export.tar.gz.partial", "bad name.gz", "-dash.gz", "caf\xe9.gz", "x" * 202):
        _write(backup_dir, name, b"skip", BASE_NS)
    (backup_dir / "subdir.gz").mkdir()
    _write(backup_dir / "subdir.gz", "inner.gz", b"inner", BASE_NS)
    outside = _write(tmp_path, "secret.gz", SECRET, BASE_NS)
    (backup_dir / "escape.gz").symlink_to(outside)
    (backup_dir / "alias.gz").symlink_to(backup_dir / "good.sql.gz")
    (backup_dir / "dangling.gz").symlink_to(tmp_path / "missing")
    os.mkfifo(backup_dir / "pipe.gz")
    response = await client.get(LIST, headers=await auth_headers(OPERATOR_ID))
    assert response.status_code == 200, response.text
    assert [item["name"] for item in response.json()["items"]] == ["good.sql.gz"]
    assert hashlib.sha256(SECRET).hexdigest() not in response.text


async def test_download_is_byte_exact_with_headers(client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders, db: AsyncEngine, closes: list[str]) -> None:
    content = os.urandom(70_000)
    _write(backup_dir, "rapidwire-dump-20261031T000000Z.sql.gz", content, BASE_NS)
    _write(backup_dir, "rapidwire-export.jsonl", b'{"table": "x"}\n', BASE_NS)
    headers = await auth_headers(OPERATOR_ID)
    response = await client.get(f"{LIST}/rapidwire-dump-20261031T000000Z.sql.gz", headers=headers)
    assert response.status_code == 200, response.text
    assert response.content == content
    digest = hashlib.sha256(content)
    assert response.headers["content-type"] == "application/gzip"
    assert response.headers["content-length"] == str(len(content))
    assert response.headers["content-disposition"] == 'attachment; filename="rapidwire-dump-20261031T000000Z.sql.gz"'
    assert response.headers["x-checksum-sha256"] == digest.hexdigest()
    assert response.headers["repr-digest"] == f"sha-256=:{base64.b64encode(digest.digest()).decode()}:"
    assert response.headers["cache-control"] == "no-store"
    assert "content-encoding" not in response.headers
    plain = await client.get(f"{LIST}/rapidwire-export.jsonl", headers=headers)
    assert plain.headers["content-type"] == "application/octet-stream"
    assert plain.content == b'{"table": "x"}\n'
    records = await _backup_executions(db)
    assert [(record["type"], record["initiator_id"], record["status"]) for record in records] == [(ExecutionType.BACKUP_DOWNLOAD, OPERATOR_ID, ExecutionStatus.SUCCESS)] * 2
    assert records[0]["input"] == {"name": "rapidwire-dump-20261031T000000Z.sql.gz"}
    assert records[0]["output"] == {"name": "rapidwire-dump-20261031T000000Z.sql.gz", "size": str(len(content)), "sha256": digest.hexdigest()}
    assert response.headers["x-execution-id"] == str(records[0]["execution_id"])
    assert closes == ["rapidwire-dump-20261031T000000Z.sql.gz", "rapidwire-export.jsonl"]


async def test_empty_file_downloads(client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders) -> None:
    _write(backup_dir, "empty.bin", b"", BASE_NS)
    response = await client.get(f"{LIST}/empty.bin", headers=await auth_headers(OPERATOR_ID))
    assert response.status_code == 200
    assert response.content == b""
    assert response.headers["content-length"] == "0"
    assert response.headers["x-checksum-sha256"] == hashlib.sha256(b"").hexdigest()


async def test_non_operator_is_refused_without_a_record(client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    _write(backup_dir, "dump.sql.gz", b"dump", BASE_NS)
    headers = await auth_headers(OTHER)
    for path in (LIST, f"{LIST}/dump.sql.gz", f"{LIST}/missing.gz", f"{LIST}/%2E%2E", f"{LIST}/a%2Fb", f"{LIST}/a/b", f"{LIST}/", f"{LIST}/..%2F..%2Fetc%2Fpasswd"):
        response = await client.get(path, headers=headers)
        _assert_error(response, 403, "permission_denied")
        assert "dump.sql.gz" not in response.text
    assert await _backup_executions(db) == []


async def test_missing_or_invalid_token_is_401(client: httpx.AsyncClient, backup_dir: Path, db: AsyncEngine) -> None:
    _write(backup_dir, "dump.sql.gz", b"dump", BASE_NS)
    for headers in ({}, bearer("not-a-token"), {"Authorization": "Basic abc"}):
        for path in (LIST, f"{LIST}/dump.sql.gz"):
            response = await client.get(path, headers=headers)
            _assert_error(response, 401, "unauthorized")
            assert response.headers["www-authenticate"] == "Bearer"
    assert await _backup_executions(db) == []


async def test_revoked_token_is_401(client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    _write(backup_dir, "dump.sql.gz", b"dump", BASE_NS)
    headers = await auth_headers(OPERATOR_ID)
    await revoke_tokens(db, account_id=OPERATOR_ID)
    for path in (LIST, f"{LIST}/dump.sql.gz"):
        _assert_error(await client.get(path, headers=headers), 401, "unauthorized")
    assert await _backup_executions(db) == []


async def test_revoke_during_the_request_records_nothing(
    client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders, db: AsyncEngine, monkeypatch: pytest.MonkeyPatch, closes: list[str]
) -> None:
    _write(backup_dir, "dump.sql.gz", b"dump", BASE_NS)

    def revoking[**P, T](function: Callable[P, T]) -> Callable[P, T]:
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> T:
            # Runs in the worker thread after authentication; the revoke commits before the execution is recorded.
            anyio.from_thread.run(lambda: revoke_tokens(db, account_id=OPERATOR_ID))
            return function(*args, **kwargs)

        return wrapper

    monkeypatch.setattr(admin, "list_backup_page", revoking(backups.list_backup_page))
    monkeypatch.setattr(admin, "open_backup", revoking(backups.open_backup))
    _assert_error(await client.get(LIST, headers=await auth_headers(OPERATOR_ID)), 401, "unauthorized")
    _assert_error(await client.get(f"{LIST}/dump.sql.gz", headers=await auth_headers(OPERATOR_ID)), 401, "unauthorized")
    assert await _backup_executions(db) == []
    assert closes == ["dump.sql.gz"]


@pytest.mark.parametrize(
    "path",
    [
        "..%2Fsecret.gz",
        "..%2F..%2Fetc%2Fpasswd",
        "..%5Csecret.gz",
        "%2E%2E%5Csecret.gz",
        "%00",
        "dump.sql.gz%00",
        "dump.sql.gz%00.txt",
        "escape.gz",
        "inner-escape.gz",
        "%2Fetc%2Fpasswd",
        "%2E%2E",
        "%2E",
        "subdir.gz",
        "subdir.gz%2Finner.gz",
        ".hidden.gz",
        "dump.sql.gz.tmp",
        "pipe.gz",
        "missing.gz",
        "DUMP.SQL.GZ",
        "dump.sql.gz%20",
        "a%2Fb",
        "a/b",
        "subdir.gz/inner.gz",
        "",
    ],
)
async def test_names_outside_the_allowlist_never_serve_data(client: httpx.AsyncClient, backup_dir: Path, tmp_path: Path, auth_headers: AuthHeaders, db: AsyncEngine, path: str) -> None:
    _write(backup_dir, "dump.sql.gz", b"dump", BASE_NS)
    _write(backup_dir, ".hidden.gz", SECRET, BASE_NS)
    _write(backup_dir, "dump.sql.gz.tmp", SECRET, BASE_NS)
    outside = _write(tmp_path, "secret.gz", SECRET, BASE_NS)
    (backup_dir / "escape.gz").symlink_to(outside)
    (backup_dir / "subdir.gz").mkdir()
    _write(backup_dir / "subdir.gz", "inner.gz", SECRET, BASE_NS)
    (backup_dir / "inner-escape.gz").symlink_to("subdir.gz/inner.gz")
    os.mkfifo(backup_dir / "pipe.gz")
    response = await client.get(f"{LIST}/{path}", headers=await auth_headers(OPERATOR_ID))
    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"
    assert SECRET not in response.content
    assert await _backup_executions(db) == []


async def test_absolute_path_is_not_served(client: httpx.AsyncClient, tmp_path: Path, auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    outside = _write(tmp_path, "secret.gz", SECRET, BASE_NS)
    for path in (f"{LIST}/{quote(str(outside), safe='')}", f"{LIST}/{quote(str(outside), safe='/')}", f"{LIST}{outside}"):
        response = await client.get(path, headers=await auth_headers(OPERATOR_ID))
        assert response.status_code == 404, path
        assert SECRET not in response.content
    assert await _backup_executions(db) == []


async def test_missing_file_is_404_without_a_record(client: httpx.AsyncClient, auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    response = await client.get(f"{LIST}/gone.sql.gz", headers=await auth_headers(OPERATOR_ID))
    _assert_error(response, 404, "not_found")
    assert response.json()["error"]["message"] == "The backup was not found."
    assert await _backup_executions(db) == []


async def test_every_list_and_download_is_one_execution(client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders, db: AsyncEngine) -> None:
    _write(backup_dir, "a.gz", b"a", BASE_NS)
    headers = await auth_headers(OPERATOR_ID)
    ids = []
    for _ in range(2):
        ids.append((await client.get(LIST, headers=headers)).json()["execution_id"])
        ids.append((await client.get(f"{LIST}/a.gz", headers=headers)).headers["x-execution-id"])
    (backup_dir / "a.gz").unlink()
    assert (await client.get(LIST, headers={**headers, "Idempotency-Key": "ignored"})).json()["items"] == []
    records = await _backup_executions(db)
    assert [record["type"] for record in records] == [ExecutionType.BACKUP_LIST, ExecutionType.BACKUP_DOWNLOAD] * 2 + [ExecutionType.BACKUP_LIST]
    assert [str(record["execution_id"]) for record in records[:4]] == ids
    assert {record["initiator_id"] for record in records} == {OPERATOR_ID}
    assert [record["output"] for record in records if record["type"] == ExecutionType.BACKUP_LIST] == [{"count": "1", "total": "1"}, {"count": "1", "total": "1"}, {"count": "0", "total": "0"}]
    assert {record["idempotency_key"] for record in records} == {None}


async def test_checksums_are_cached_until_the_file_changes(client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders, count_hashes: list[int]) -> None:
    path = _write(backup_dir, "a.gz", b"first content", BASE_NS)
    _write(backup_dir, "b.gz", b"other", BASE_NS)
    headers = await auth_headers(OPERATOR_ID)

    async def sums() -> dict[str, str]:
        return {item["name"]: item["sha256"] for item in (await client.get(LIST, headers=headers)).json()["items"]}

    assert await sums() == {"a.gz": hashlib.sha256(b"first content").hexdigest(), "b.gz": hashlib.sha256(b"other").hexdigest()}
    assert len(count_hashes) == 2
    assert len(await sums()) == 2
    assert (await client.get(f"{LIST}/a.gz", headers=headers)).headers["x-checksum-sha256"] == hashlib.sha256(b"first content").hexdigest()
    assert len(count_hashes) == 2
    path.write_bytes(b"other content")
    os.utime(path, ns=(BASE_NS + 1, BASE_NS + 1))
    assert (await sums())["a.gz"] == hashlib.sha256(b"other content").hexdigest()
    assert len(count_hashes) == 3
    # A file renamed into place gets a new inode even with the same size and time.
    replacement = _write(backup_dir, "a.gz.tmp", b"third content", BASE_NS + 1)
    replacement.replace(path)
    response = await client.get(f"{LIST}/a.gz", headers=headers)
    assert response.headers["x-checksum-sha256"] == hashlib.sha256(b"third content").hexdigest()
    assert response.content == b"third content"
    assert len(count_hashes) == 4
    assert (await sums())["a.gz"] == hashlib.sha256(b"third content").hexdigest()
    assert len(count_hashes) == 4


async def test_cache_is_per_application(db: AsyncEngine, api_settings: Settings, backup_dir: Path, auth_headers: AuthHeaders, count_hashes: list[int]) -> None:
    _write(backup_dir, "a.gz", b"a", BASE_NS)
    headers = await auth_headers(OPERATOR_ID)
    for _ in range(2):
        app = create_app(api_settings, engine=db, guild_info=InMemoryGuildInfo())
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            assert (await http.get(LIST, headers=headers)).status_code == 200
            assert (await http.get(LIST, headers=headers)).status_code == 200
    assert len(count_hashes) == 2


async def test_missing_directory_lists_nothing_and_warns_once(db: AsyncEngine, tmp_path: Path, auth_headers: AuthHeaders, caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(_env_file=None, db_password=API_TEST_PASSWORD, operator_account_ids=[OPERATOR_ID], backup_dir=tmp_path / "absent")
    app = create_app(settings, engine=db, guild_info=InMemoryGuildInfo())
    headers = await auth_headers(OPERATOR_ID)
    with caplog.at_level(logging.WARNING, logger="rapidwire.api.backups"):
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            for _ in range(3):
                response = await http.get(LIST, headers=headers)
                assert response.status_code == 200
                assert response.json()["items"] == []
            _assert_error(await http.get(f"{LIST}/a.gz", headers=headers), 404, "not_found")
    assert len([record for record in caplog.records if record.name == "rapidwire.api.backups"]) == 1
    records = await _backup_executions(db)
    assert [record["type"] for record in records] == [ExecutionType.BACKUP_LIST] * 3


async def _call_asgi(app: FastAPI, path: str, headers: dict[str, str], send: Callable[[Message], Any]) -> None:
    request_sent = False
    never = asyncio.Event()

    async def receive() -> Message:
        nonlocal request_sent
        if not request_sent:
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await never.wait()
        return {"type": "http.disconnect"}

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"test"), *((name.lower().encode(), value.encode()) for name, value in headers.items())],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
        "state": {},
    }
    await app(scope, receive, send)


async def test_large_file_is_streamed_in_chunks(api_app: FastAPI, backup_dir: Path, auth_headers: AuthHeaders, closes: list[str]) -> None:
    content = os.urandom(5 * backups.CHUNK_SIZE + 12_345)
    _write(backup_dir, "large.sql.gz", content, BASE_NS)
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    await _call_asgi(api_app, f"{LIST}/large.sql.gz", await auth_headers(OPERATOR_ID), send)
    start, *bodies = messages
    assert start["status"] == 200
    assert (b"content-length", str(len(content)).encode()) in start["headers"]
    chunks = [message["body"] for message in bodies if message["body"]]
    assert len(chunks) == 6
    assert max(len(chunk) for chunk in chunks) == backups.CHUNK_SIZE
    assert b"".join(chunks) == content
    assert bodies[-1]["more_body"] is False
    assert closes == ["large.sql.gz"]


async def test_client_abort_still_counts_and_closes_the_file(api_app: FastAPI, backup_dir: Path, auth_headers: AuthHeaders, db: AsyncEngine, closes: list[str]) -> None:
    _write(backup_dir, "large.sql.gz", os.urandom(3 * backups.CHUNK_SIZE), BASE_NS)
    bodies = 0

    async def send(message: Message) -> None:
        nonlocal bodies
        if message["type"] == "http.response.body":
            bodies += 1
            if bodies == 2:
                raise OSError("connection reset by peer")

    with pytest.raises(ClientDisconnect):
        await _call_asgi(api_app, f"{LIST}/large.sql.gz", await auth_headers(OPERATOR_ID), send)
    assert closes == ["large.sql.gz"]
    [record] = await _backup_executions(db)
    assert (record["type"], record["status"]) == (ExecutionType.BACKUP_DOWNLOAD, ExecutionStatus.SUCCESS)


async def test_truncated_backup_fails_the_stream_and_closes_the_file(api_app: FastAPI, backup_dir: Path, auth_headers: AuthHeaders, db: AsyncEngine, closes: list[str]) -> None:
    path = _write(backup_dir, "large.sql.gz", os.urandom(3 * backups.CHUNK_SIZE), BASE_NS)
    bodies: list[bytes] = []

    async def send(message: Message) -> None:
        if message["type"] == "http.response.body":
            bodies.append(message["body"])
            if len(bodies) == 1:
                # The file shrinks in place after the first chunk went out.
                os.truncate(path, backups.CHUNK_SIZE + 10)

    with pytest.raises(backups.BackupChanged):
        await _call_asgi(api_app, f"{LIST}/large.sql.gz", await auth_headers(OPERATOR_ID), send)
    assert closes == ["large.sql.gz"]
    assert sum(len(body) for body in bodies) < 3 * backups.CHUNK_SIZE
    [record] = await _backup_executions(db)
    assert record["type"] == ExecutionType.BACKUP_DOWNLOAD


async def test_startup_hashes_existing_backups_in_the_background(db: AsyncEngine, api_settings: Settings, backup_dir: Path, auth_headers: AuthHeaders, count_hashes: list[int]) -> None:
    for name in ("a.gz", "b.gz", "c.gz"):
        _write(backup_dir, name, name.encode() * 1000, BASE_NS)
    _write(backup_dir, "skip.gz.tmp", b"partial", BASE_NS)
    app = create_app(api_settings, engine=db, guild_info=InMemoryGuildInfo())
    headers = await auth_headers(OPERATOR_ID)
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        warmup = backups.checksum_cache(app.state).warmup
        assert warmup is not None
        assert await asyncio.to_thread(warmup.join, 10.0)
        assert sorted(count_hashes) == [4000, 4000, 4000]
        listed = (await http.get(LIST, headers=headers)).json()
        assert {item["name"]: item["sha256"] for item in listed["items"]} == {name: hashlib.sha256(name.encode() * 1000).hexdigest() for name in ("a.gz", "b.gz", "c.gz")}
        assert (await http.get(f"{LIST}/b.gz", headers=headers)).status_code == 200
    assert len(count_hashes) == 3


async def test_startup_warmup_failure_is_logged_and_listing_still_works(
    db: AsyncEngine, api_settings: Settings, backup_dir: Path, auth_headers: AuthHeaders, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _write(backup_dir, "a.gz", b"a", BASE_NS)
    real = backups.sha256_of_fd
    calls: list[int] = []

    def failing_first(fd: int, size: int) -> str:
        calls.append(size)
        if len(calls) == 1:
            raise OSError("read error")
        return real(fd, size)

    monkeypatch.setattr(backups, "sha256_of_fd", failing_first)
    app = create_app(api_settings, engine=db, guild_info=InMemoryGuildInfo())
    with caplog.at_level(logging.ERROR, logger="rapidwire.api.backups"):
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
            warmup = backups.checksum_cache(app.state).warmup
            assert warmup is not None
            assert await asyncio.to_thread(warmup.join, 10.0)
            response = await http.get(LIST, headers=await auth_headers(OPERATOR_ID))
    assert response.status_code == 200
    assert [item["sha256"] for item in response.json()["items"]] == [hashlib.sha256(b"a").hexdigest()]
    assert [record.message for record in caplog.records if record.levelno == logging.ERROR] == ["hashing the backups in the background failed; listings hash them on demand"]


async def test_concurrent_requests_hash_a_file_once(client: httpx.AsyncClient, backup_dir: Path, auth_headers: AuthHeaders, count_hashes: list[int]) -> None:
    _write(backup_dir, "big.gz", os.urandom(4 * backups.CHUNK_SIZE), BASE_NS)
    headers = await auth_headers(OPERATOR_ID)
    responses = await asyncio.gather(*(client.get(LIST, headers=headers) for _ in range(3)), *(client.get(f"{LIST}/big.gz", headers=headers) for _ in range(3)))
    assert {response.status_code for response in responses} == {200}
    digests = {item["sha256"] for response in responses[:3] for item in response.json()["items"]} | {response.headers["x-checksum-sha256"] for response in responses[3:]}
    assert len(digests) == 1
    assert count_hashes == [4 * backups.CHUNK_SIZE]


async def test_openapi_documents_the_admin_routes(client: httpx.AsyncClient) -> None:
    document = (await client.get("/openapi.json")).json()
    assert {"/v1/admin/backups", "/v1/admin/backups/{name}"} <= set(document["paths"])
    assert document["paths"]["/v1/admin/backups"]["get"]["security"] == [{"BearerToken": []}]
