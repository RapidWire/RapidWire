"""Backups under hostile conditions: a client configuration on the machine that points elsewhere, cancellation in the middle of an export, and a ledger that violates its invariants and holds both SQL NULL and JSON null documents."""

from __future__ import annotations

import asyncio
import os
import subprocess
import threading
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.backup import export as export_module
from rapidwire.backup.client import ConnectionOptions
from rapidwire.backup.dump import write_dump
from rapidwire.backup.encoding import TableSpec
from rapidwire.backup.export import TableDigest, write_export
from rapidwire.backup.jobs import run_backup
from rapidwire.backup.manifest import MANIFEST_NAME, Manifest
from rapidwire.backup.naming import BackupKind
from rapidwire.backup.restore import restore_backup
from rapidwire.backup.totals import TotalsBuilder
from rapidwire.backup.verify import verify_file
from rapidwire.config import Settings
from rapidwire.core.db import consistent_snapshot
from rapidwire.db.engine import create_engine
from rapidwire.db.tables import currency, execution
from tests.integration.backup import mysql_client_wrapper as wrapper
from tests.integration.backup.conftest import DatabaseFactory
from tests.integration.backup.seed import BOB, RAPID, seed_ledger

STAMP = "20261031T030000Z"
HOSTILE_MY_CNF = """[client]
host=127.0.0.1
port=1
user=nobody
password=wrong-password
[mysqldump]
databases
[mysql]
database=mysql
"""


def _manifest(path: Path) -> Manifest:
    import tarfile

    with tarfile.open(path, "r:gz") as archive:
        member = archive.extractfile(MANIFEST_NAME)
        assert member is not None
        return Manifest.from_bytes(member.read())


async def test_a_conflicting_client_configuration_is_ignored(
    db: AsyncEngine, backup_settings: Settings, mysql_clients: Path, fresh_database: DatabaseFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await seed_ledger(db)
    my_cnf = tmp_path / "my.cnf"
    my_cnf.write_text(HOSTILE_MY_CNF, encoding="utf-8")
    my_cnf.chmod(0o644)
    # The planted file is what the client reads by default, as a client without --defaults-file shows.
    shown = await asyncio.to_thread(
        subprocess.run,
        ["docker", "run", "--rm", "--label", "rapidwire-test=1", "--volume", f"{my_cnf}:/root/.my.cnf:ro", "--entrypoint", "mysqldump", wrapper.IMAGE, "--print-defaults"],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert "--port=1" in shown.stdout, shown.stdout + shown.stderr
    monkeypatch.setenv(wrapper.MY_CNF_ENV, str(my_cnf))
    for name, value in (("MYSQL_HOST", "nonexistent.invalid"), ("MYSQL_TCP_PORT", "1"), ("MYSQL_PWD", "wrong-password"), ("MYSQL_UNIX_PORT", "/nonexistent.sock")):
        monkeypatch.setenv(name, value)

    settings = backup_settings
    settings.backup_dir.mkdir()
    options = ConnectionOptions(host=settings.db_host, port=settings.db_port, user=settings.db_user, password=settings.db_password.get_secret_value(), database=settings.db_name)
    dump = await write_dump(options, settings.backup_dir, STAMP)
    assert verify_file(dump).ok

    target = await fresh_database()
    outcome = await restore_backup(dump, target)
    assert outcome.ok, (outcome.report.violations, outcome.problems)
    engine = create_engine(target.url)
    try:
        async with engine.connect() as conn:
            assert (await conn.execute(select(currency.c.currency_id).order_by(currency.c.currency_id))).scalars().all() == [111, 222, 333]
    finally:
        await engine.dispose()
    assert os.environ["MYSQL_HOST"] == "nonexistent.invalid"


async def test_cancelling_the_export_while_tables_are_read_removes_its_staging_directory(db: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = asyncio.Event()

    async def stuck(conn: AsyncConnection, specs: list[TableSpec], *, directory: Path | None = None, totals: TotalsBuilder | None = None) -> list[TableDigest]:
        assert directory is not None
        (directory / f"{specs[0].name}.jsonl").write_bytes(b"{}\n")
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    monkeypatch.setattr(export_module, "export_tables", stuck)
    directory = tmp_path / "backups"
    directory.mkdir()
    task = asyncio.create_task(write_export(db, directory, STAMP, created_at=_created()))
    await asyncio.wait_for(started.wait(), 30)
    assert [entry.name for entry in directory.iterdir()] == [f".rapidwire-export-{STAMP}.staging"]
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(directory.iterdir()) == []


async def test_cancelling_the_export_while_packing_removes_the_partial_archive(db: AsyncEngine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    await seed_ledger(db)
    packing = threading.Event()
    stopped: list[bool] = []

    def slow_pack(fd: int, manifest: bytes, tables: Path, entries: Any, mtime: int, stop: threading.Event) -> None:
        try:
            os.write(fd, b"partial bytes")
            packing.set()
            stopped.append(stop.wait(30))
            raise RuntimeError("packing stopped")
        finally:
            os.close(fd)

    monkeypatch.setattr(export_module, "_pack", slow_pack)
    directory = tmp_path / "backups"
    directory.mkdir()
    task = asyncio.create_task(write_export(db, directory, STAMP, created_at=_created()))
    while not packing.is_set():
        assert not task.done(), task
        await asyncio.sleep(0.01)
    assert sorted(entry.name for entry in directory.iterdir()) == sorted([f".rapidwire-export-{STAMP}.staging", f"{BackupKind.EXPORT.file_name(STAMP)}.partial"])
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert stopped == [True]
    assert list(directory.iterdir()) == []


def _created() -> Any:
    from datetime import UTC, datetime

    return datetime(2026, 10, 31, 3, tzinfo=UTC)


async def _corrupt(engine: AsyncEngine) -> tuple[int, int]:
    """Point RAPID's issuance at Bob's user account and give one execution a SQL NULL output and another a JSON null document; returns those two execution ids."""
    async with engine.begin() as conn:
        await conn.execute(text("SET SESSION FOREIGN_KEY_CHECKS = 0"))
        try:
            await conn.execute(currency.update().where(currency.c.currency_id == RAPID).values(issuance_account_id=BOB))
            ids = (await conn.execute(select(execution.c.execution_id).order_by(execution.c.execution_id).limit(2))).scalars().all()
            await conn.execute(text("UPDATE execution SET output = NULL WHERE execution_id = :id"), {"id": ids[0]})
            await conn.execute(text("UPDATE execution SET output = CAST('null' AS JSON) WHERE execution_id = :id"), {"id": ids[1]})
        finally:
            await conn.execute(text("SET SESSION FOREIGN_KEY_CHECKS = 1"))
    return ids[0], ids[1]


async def _outputs(engine: AsyncEngine, ids: tuple[int, int]) -> list[tuple[int, Any]]:
    async with engine.connect() as conn:
        rows = await conn.execute(text("SELECT execution_id, output IS NULL, JSON_TYPE(output) FROM execution WHERE execution_id IN (:a, :b) ORDER BY execution_id"), {"a": ids[0], "b": ids[1]})
        return [(int(row[0]), (bool(row[1]), row[2])) for row in rows]


async def test_a_ledger_that_violates_its_invariants_is_backed_up_and_restored_exactly(
    db: AsyncEngine, app_engine: AsyncEngine, backup_settings: Settings, mysql_clients: Path, fresh_database: DatabaseFactory
) -> None:
    await seed_ledger(db)
    ids = await _corrupt(db)
    expected_outputs = await _outputs(db, ids)
    assert [value for _, value in expected_outputs] == [(True, None), (False, "NULL")]

    result = await run_backup(app_engine, backup_settings)
    assert result.ok, result.errors
    assert result.invariant_violations
    assert result.export_path is not None and result.dump_path is not None
    manifest = _manifest(result.export_path)
    assert not manifest.invariants_ok
    assert manifest.invariant_violations == result.invariant_violations
    assert manifest.totals.currencies[RAPID].supply < 0

    report = verify_file(result.export_path)
    assert report.unreadable is None
    assert report.problems == [f"the database had {manifest.invariant_violations} invariant violations when this export was taken"]
    assert verify_file(result.dump_path).ok

    for path in (result.export_path, result.dump_path):
        target = await fresh_database()
        outcome = await restore_backup(path, target)
        assert outcome.problems == []
        assert len(outcome.report.violations) == manifest.invariant_violations
        engine = create_engine(target.url)
        try:
            assert await _outputs(engine, ids) == expected_outputs
            async with consistent_snapshot(engine) as conn:
                digests = await export_module.export_tables(conn, [entry.spec for entry in manifest.tables])
        finally:
            await engine.dispose()
        assert {digest.spec.name: digest.sha256 for digest in digests} == {entry.spec.name: entry.sha256 for entry in manifest.tables}
