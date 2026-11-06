from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rapidwire.backup.restore import RestoreError, parse_target_url
from rapidwire.cli import COMMANDS, EXIT_CONFIG, EXIT_FAILURE, EXIT_INVARIANT_VIOLATION, EXIT_OK, EXIT_USAGE, main

GOOD_DUMP = b"-- MySQL dump 10.13  Distrib 8.4.13\n\nCREATE TABLE `t` (`x` int);\n\n-- Dump completed on 2026-10-31  3:00:00\n"


def _write(path: Path, data: bytes) -> Path:
    path.write_bytes(data)
    return path


def test_backup_is_implemented() -> None:
    assert "backup" in COMMANDS


def test_verify_needs_no_settings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    good = _write(tmp_path / "rapidwire-dump-20261031T030000Z.sql.gz", gzip.compress(GOOD_DUMP))
    assert main(["backup", "verify", str(good)]) == EXIT_OK
    captured = capsys.readouterr()
    assert captured.out.splitlines()[0].startswith(f"OK {good} (dump)")
    assert captured.out.splitlines()[-1] == "1 files checked, 1 passed, 0 mismatched, 0 unreadable"
    assert json.loads(captured.err.splitlines()[-1])["msg"] == "backup files verified"


def test_verify_reports_an_incomplete_dump_as_a_mismatch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    cut = _write(tmp_path / "cut.sql.gz", gzip.compress(GOOD_DUMP.rsplit(b"-- Dump", 1)[0]))
    assert main(["backup", "verify", str(cut)]) == EXIT_INVARIANT_VIOLATION
    assert "is incomplete" in capsys.readouterr().out


def test_verify_reports_unreadable_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    truncated = _write(tmp_path / "truncated.sql.gz", gzip.compress(GOOD_DUMP)[:-12])
    plain = _write(tmp_path / "plain.tar.gz", b"not gzip")
    unknown = _write(tmp_path / "backup.zip", b"")
    missing = tmp_path / "missing.sql.gz"
    assert main(["backup", "verify", str(truncated), str(plain), str(unknown), str(missing)]) == EXIT_FAILURE
    lines = capsys.readouterr().out.splitlines()
    assert [line.split(" ")[0] for line in lines[:-1]] == ["UNREADABLE"] * 4
    assert lines[-1] == "4 files checked, 0 passed, 0 mismatched, 4 unreadable"


def test_a_mismatch_outranks_an_unreadable_file(tmp_path: Path) -> None:
    cut = _write(tmp_path / "cut.sql.gz", gzip.compress(b"-- MySQL dump\n"))
    assert main(["backup", "verify", str(cut), str(tmp_path / "missing.tar.gz")]) == EXIT_INVARIANT_VIOLATION


def test_an_export_whose_manifest_lists_no_tables_is_unreadable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import io
    import tarfile

    from rapidwire.backup.manifest import MANIFEST_NAME, Manifest
    from rapidwire.backup.totals import InvariantTotals

    manifest = Manifest(
        app_version="2.0.0",
        schema_revision="0005",
        created_at=datetime(2026, 10, 31, 3, tzinfo=UTC),
        tables=(),
        totals=InvariantTotals(currencies={}, pools={}),
        invariants_ok=True,
        invariant_violations=0,
    ).to_bytes()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        info = tarfile.TarInfo(MANIFEST_NAME)
        info.size = len(manifest)
        archive.addfile(info, io.BytesIO(manifest))
    empty = _write(tmp_path / "rapidwire-export-20261031T030000Z.tar.gz", buffer.getvalue())
    assert main(["backup", "verify", str(empty)]) == EXIT_FAILURE
    out = capsys.readouterr().out
    assert out.startswith(f"UNREADABLE {empty}")
    for table in ("balance", "currency", "pool", "pool_share", "stake"):
        assert f"table {table} is missing" in out


def test_verify_needs_a_file(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["backup", "verify"]) == EXIT_USAGE


def test_restore_validates_the_target_url_before_connecting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dump = _write(tmp_path / "rapidwire-dump-20261031T030000Z.sql.gz", gzip.compress(GOOD_DUMP))
    assert main(["backup", "restore", "--target-url", "postgresql://u@h/db", str(dump)]) == EXIT_FAILURE
    assert "mysql://user@host:port/database" in capsys.readouterr().err
    assert main(["backup", "restore", str(dump)]) == EXIT_USAGE


def test_restore_failure_does_not_print_the_password(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("MYSQL_PWD", "restore-secret-pw")
    dump = _write(tmp_path / "rapidwire-dump-20261031T030000Z.sql.gz", gzip.compress(GOOD_DUMP))
    assert main(["backup", "restore", "--target-url", "mysql://root@127.0.0.1:1/rw_restore", str(dump)]) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert "restore failed" in captured.err
    assert "restore-secret-pw" not in captured.out + captured.err
    assert main(["backup", "restore", "--target-url", "mysql://root:url-secret-pw@127.0.0.1:1/rw_restore", str(dump)]) == EXIT_FAILURE
    captured = capsys.readouterr()
    assert "url-secret-pw" not in captured.out + captured.err


def test_restore_refuses_a_file_of_unknown_kind(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    other = _write(tmp_path / "backup.zip", b"")
    assert main(["backup", "restore", "--target-url", "mysql://root@127.0.0.1:1/rw_restore", str(other)]) == EXIT_FAILURE
    assert ".sql.gz" in capsys.readouterr().err


def _backup_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "backup-secret-pw")
    monkeypatch.setenv("RAPIDWIRE_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("RAPIDWIRE_DB_PORT", "1")
    monkeypatch.setenv("RAPIDWIRE_BACKUP_DIR", str(tmp_path / "backups"))


def test_a_lost_lease_stops_the_backup_and_exits_1(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from rapidwire.backup import jobs
    from rapidwire.core import joblock

    events: list[str] = []

    @asynccontextmanager
    async def losing_lease(engine: object, name: str, *, holder: str, ttl: timedelta, renew_interval: timedelta | None = None) -> AsyncIterator[None]:
        task = asyncio.current_task()
        assert task is not None
        asyncio.get_running_loop().call_later(0.05, task.cancel)
        try:
            yield
        except asyncio.CancelledError:
            raise joblock.LeaseLost(name) from None

    async def slow_backup(engine: object, settings: object, *, now: datetime | None = None) -> jobs.BackupRunResult:
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            events.append("cancelled")
            raise
        raise AssertionError("the run was not stopped")

    monkeypatch.setattr(joblock, "hold_lease", losing_lease)
    monkeypatch.setattr(jobs, "run_backup", slow_backup)
    _backup_env(monkeypatch, tmp_path)
    assert main(["backup"]) == EXIT_FAILURE
    assert events == ["cancelled"]
    messages = [json.loads(line)["msg"] for line in capsys.readouterr().out.splitlines()]
    assert messages[-1] == "the backup job lease was lost, so the backup was stopped"


def test_a_backup_of_an_inconsistent_ledger_exits_4(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from rapidwire.backup import jobs
    from rapidwire.core import joblock

    @asynccontextmanager
    async def free_lease(engine: object, name: str, *, holder: str, ttl: timedelta, renew_interval: timedelta | None = None) -> AsyncIterator[None]:
        yield

    def result(violations: int, errors: tuple[str, ...] = ()) -> jobs.BackupRunResult:
        return jobs.BackupRunResult(started_at=datetime(2026, 10, 31, tzinfo=UTC), dump_path=tmp_path, export_path=tmp_path, deleted=(), errors=errors, invariant_violations=violations)

    outcomes = iter([result(2), result(0), result(2, ("dump failed: x",))])

    async def fake_backup(engine: object, settings: object, *, now: datetime | None = None) -> jobs.BackupRunResult:
        return next(outcomes)

    monkeypatch.setattr(joblock, "hold_lease", free_lease)
    monkeypatch.setattr(jobs, "run_backup", fake_backup)
    _backup_env(monkeypatch, tmp_path)
    assert main(["backup"]) == EXIT_INVARIANT_VIOLATION
    assert main(["backup"]) == EXIT_OK
    assert main(["backup"]) == EXIT_FAILURE


def test_restore_refuses_a_database_name_that_is_not_an_identifier(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    dump = _write(tmp_path / "rapidwire-dump-20261031T030000Z.sql.gz", gzip.compress(GOOD_DUMP))
    assert main(["backup", "restore", "--target-url", "mysql://root@127.0.0.1:1/rw%20x", str(dump)]) == EXIT_FAILURE
    assert "database name" in capsys.readouterr().err


@pytest.mark.parametrize("name", ["123", "a" * 65, "a$b"])
def test_parse_target_url_refuses_names_outside_the_database_name_rule(name: str) -> None:
    with pytest.raises(RestoreError, match="database name"):
        parse_target_url(f"mysql://root@127.0.0.1:3307/{name}", environ={})


@pytest.mark.parametrize("name", ["2026_backup", "rw_dump", "a" * 64])
def test_parse_target_url_accepts_names_inside_the_database_name_rule(name: str) -> None:
    target = parse_target_url(f"mysql://root@127.0.0.1:3307/{name}", environ={"MYSQL_PWD": "local-secret"})
    assert (target.database, target.host, target.port, target.user, target.password) == (name, "127.0.0.1", 3307, "root", "local-secret")


def test_taking_a_backup_needs_settings(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["backup"]) == EXIT_CONFIG
    assert "RAPIDWIRE_DB_PASSWORD" in capsys.readouterr().err


def test_taking_a_backup_without_a_database_exits_1_without_the_password(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "backup-secret-pw")
    monkeypatch.setenv("RAPIDWIRE_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("RAPIDWIRE_DB_PORT", "1")
    monkeypatch.setenv("RAPIDWIRE_BACKUP_DIR", str(tmp_path / "backups"))
    assert main(["backup"]) == EXIT_FAILURE
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines()]
    assert records[-1]["msg"] == "command failed"
    assert records[-1]["command"] == "backup"
    assert "backup-secret-pw" not in captured.out + captured.err
    assert not (tmp_path / "backups").exists()
