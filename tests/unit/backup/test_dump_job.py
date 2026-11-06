"""The dump writer and the backup job against a fake mysqldump on PATH and an unreachable database, so no MySQL is needed."""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.backup.client import NO_LOGIN_FILE, OPTION_FILE_ARGUMENT, ClientError, ConnectionOptions, client_environment, option_value, stderr_errors, validate_database_name
from rapidwire.backup.dump import DUMP_ARGUMENTS, write_dump
from rapidwire.backup.jobs import run_backup
from rapidwire.backup.naming import BackupKind, partial_name, staging_name
from rapidwire.config import Settings
from rapidwire.db.engine import create_engine
from rapidwire.logging import JsonFormatter

PASSWORD = 'dump-pass"#\\ word'
STAMP = "20261031T030000Z"

FAKE_MYSQLDUMP = r"""#!/bin/sh
printf '%s\n' "$@" > "$FAKE_DIR/argv"
cat "${1#--defaults-file=}" > "$FAKE_DIR/options"
env > "$FAKE_DIR/env"
echo $$ > "$FAKE_DIR/pid"
case "$FAKE_MODE" in
  ok)
    printf -- '-- MySQL dump 10.13\nCREATE TABLE `t` (`x` int);\n'
    printf -- '-- Dump completed on 2026-10-31  3:00:00\n'
    echo "mysqldump: [Warning] a harmless warning" >&2
    ;;
  fail)
    echo "mysqldump: Got error: 1045: Access denied for user 'app' (using password: YES)" >&2
    exit 2
    ;;
  stderr)
    printf -- '-- MySQL dump\n-- Dump completed\n'
    echo "mysqldump: Couldn't execute 'SHOW CREATE TABLE': error" >&2
    ;;
  nomarker)
    printf -- '-- MySQL dump\nINSERT INTO `t` VALUES (1);\n'
    ;;
  hang)
    printf -- '-- MySQL dump\n'
    sleep 1000 &
    echo $! > "$FAKE_DIR/grandchild"
    exec sleep 1000
    ;;
esac
"""


@pytest.fixture
def fake_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "mysqldump"
    script.write_text(FAKE_MYSQLDUMP, encoding="utf-8")
    script.chmod(0o755)
    record = tmp_path / "record"
    record.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_DIR", str(record))
    monkeypatch.setenv("FAKE_MODE", "ok")
    return record


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, db_host="127.0.0.1", db_port=1, db_name="rapidwire", db_user="app", db_password=PASSWORD, backup_dir=tmp_path / "backups", backup_retention=2)


@pytest_asyncio.fixture
async def unreachable_engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(settings.database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


def _options(settings: Settings) -> ConnectionOptions:
    return ConnectionOptions(host=settings.db_host, port=settings.db_port, user=settings.db_user, password=PASSWORD, database=settings.db_name)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_option_values_are_quoted_and_escaped() -> None:
    assert option_value('a"b\\c#d e\n') == '"a\\"b\\\\c#d e\\n"'
    with pytest.raises(ClientError):
        option_value("a\0b")
    content = ConnectionOptions(host="db", port=3307, user="app", password="pw", database="rw").option_file().decode()
    assert content.splitlines() == ["[client]", 'host="db"', "port=3307", 'user="app"', 'password="pw"', "protocol=TCP"]
    assert "pw" not in repr(ConnectionOptions(host="db", port=3307, user="app", password="pw-secret", database="rw"))


def test_warnings_are_not_errors() -> None:
    text = "mysqldump: [Warning] something\nWarning: other\n\nmysqldump: Got error: 2003\n"
    assert stderr_errors(text) == ["mysqldump: Got error: 2003"]


def test_database_names_are_plain_identifiers() -> None:
    for name in ("rapidwire", "rw_test_1", "A" * 64):
        assert validate_database_name(name) == name
    for name in ("", "-rw", "--all-databases", "rw db", "rw;drop", "a" * 65, "rw`x", "rw$1", "123"):
        with pytest.raises(ClientError, match="database name"):
            ConnectionOptions(host="db", port=3306, user="app", password="pw", database=name)


def test_the_client_environment_has_no_mysql_variables() -> None:
    environ = {"PATH": "/bin", "HOME": "/home/op", "MYSQL_PWD": "x", "MYSQL_HOST": "evil", "MYSQL_TCP_PORT": "1", "MYSQL_UNIX_PORT": "/s", "mysql_home": "/m", "LIBMYSQL_ENABLE_CLEARTEXT_PLUGIN": "1"}
    assert client_environment(environ) == {"PATH": "/bin", "HOME": "/home/op", "MYSQL_TEST_LOGIN_FILE": NO_LOGIN_FILE}
    assert not Path(NO_LOGIN_FILE).exists()


async def test_dump_streams_through_gzip_and_passes_the_password_only_in_the_option_pipe(
    fake_dump: Path, settings: Settings, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, value in (("MYSQL_PWD", "env-pw"), ("MYSQL_HOST", "evil"), ("MYSQL_TCP_PORT", "1"), ("MYSQL_TEST_LOGIN_FILE", "/root/.mylogin.cnf")):
        monkeypatch.setenv(name, value)
    directory = settings.backup_dir
    directory.mkdir()
    with caplog.at_level(logging.INFO):
        path = await write_dump(_options(settings), directory, STAMP)
    assert path == directory / BackupKind.DUMP.file_name(STAMP)
    assert sorted(entry.name for entry in directory.iterdir()) == [path.name]
    assert gzip.decompress(path.read_bytes()).decode().endswith("-- Dump completed on 2026-10-31  3:00:00\n")
    argv = (fake_dump / "argv").read_text().splitlines()
    assert argv[0].startswith(f"{OPTION_FILE_ARGUMENT}=/dev/fd/")
    assert argv[1:] == [*DUMP_ARGUMENTS, "rapidwire"]
    assert all(PASSWORD not in argument for argument in argv)
    assert f"password={option_value(PASSWORD)}" in (fake_dump / "options").read_text()
    child_env = dict(line.split("=", 1) for line in (fake_dump / "env").read_text().splitlines() if "=" in line)
    assert {name for name in child_env if name.startswith("MYSQL")} == {"MYSQL_TEST_LOGIN_FILE"}
    assert child_env["MYSQL_TEST_LOGIN_FILE"] == NO_LOGIN_FILE
    assert (path.stat().st_mode & 0o777) == 0o640
    rendered = "\n".join(JsonFormatter().format(record) for record in caplog.records)
    assert "harmless warning" in rendered
    assert PASSWORD not in rendered


@pytest.mark.parametrize(("mode", "message"), [("fail", "status 2: mysqldump: Got error: 1045"), ("stderr", "Couldn't execute"), ("nomarker", "completion marker")])
async def test_failed_dumps_leave_no_file(fake_dump: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch, mode: str, message: str) -> None:
    monkeypatch.setenv("FAKE_MODE", mode)
    directory = settings.backup_dir
    directory.mkdir()
    with pytest.raises(ClientError, match=message):
        await write_dump(_options(settings), directory, STAMP)
    assert list(directory.iterdir()) == []


async def test_missing_mysqldump_is_a_client_error(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "nothing"))
    settings.backup_dir.mkdir()
    with pytest.raises(ClientError, match="not found on PATH"):
        await write_dump(_options(settings), settings.backup_dir, STAMP)
    assert list(settings.backup_dir.iterdir()) == []


def _has_content(path: Path) -> bool:
    return path.exists() and bool(path.read_text().strip())


async def _wait_for(path: Path) -> None:
    deadline = time.monotonic() + 10
    while not _has_content(path):
        assert time.monotonic() < deadline, f"{path} did not appear"
        await asyncio.sleep(0.02)


async def test_cancelling_a_run_removes_partial_files_and_stops_the_child(fake_dump: Path, settings: Settings, unreachable_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "hang")
    task = asyncio.create_task(run_backup(unreachable_engine, settings, now=datetime(2026, 10, 31, 3, tzinfo=UTC)))
    await _wait_for(fake_dump / "grandchild")
    await _wait_for(fake_dump / "pid")
    assert (settings.backup_dir / partial_name(BackupKind.DUMP, STAMP)).exists()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(settings.backup_dir.iterdir()) == []
    for name in ("pid", "grandchild"):
        pid = int((fake_dump / name).read_text())
        deadline = time.monotonic() + 5
        while _alive(pid):
            assert time.monotonic() < deadline, f"process {pid} survived the cancellation"
            await asyncio.sleep(0.02)


async def test_a_failed_run_keeps_older_generations_and_removes_leftovers(fake_dump: Path, settings: Settings, unreachable_engine: AsyncEngine) -> None:
    directory = settings.backup_dir
    directory.mkdir()
    old = ["20261027T030000Z", "20261028T030000Z", "20261029T030000Z"]
    for stamp in old:
        for kind in BackupKind:
            (directory / kind.file_name(stamp)).write_bytes(b"old")
    (directory / partial_name(BackupKind.EXPORT, "20261030T030000Z")).write_bytes(b"crashed")
    (directory / staging_name("20261030T030000Z")).mkdir()
    (directory / BackupKind.DUMP.file_name("20261030T120000Z")).write_bytes(b"failed earlier")

    result = await run_backup(unreachable_engine, settings, now=datetime(2026, 10, 31, 3, tzinfo=UTC))

    assert not result.ok
    assert result.dump_path == directory / BackupKind.DUMP.file_name(STAMP)
    assert result.export_path is None
    assert result.deleted == (BackupKind.DUMP.file_name("20261030T120000Z"),)
    assert len(result.errors) == 1
    assert result.errors[0].startswith("export failed:")
    assert PASSWORD not in json.dumps(result.errors)
    expected = {kind.file_name(stamp) for stamp in old for kind in BackupKind} | {BackupKind.DUMP.file_name(STAMP)}
    assert {entry.name for entry in directory.iterdir()} == expected

    again = await run_backup(unreachable_engine, settings, now=datetime(2026, 10, 31, 4, tzinfo=UTC))
    assert again.deleted == (BackupKind.DUMP.file_name(STAMP),)
    assert {entry.name for entry in directory.iterdir()} == (expected - {BackupKind.DUMP.file_name(STAMP)}) | {BackupKind.DUMP.file_name("20261031T040000Z")}


async def test_an_invalid_database_name_fails_the_dump_without_starting_mysqldump(fake_dump: Path, settings: Settings, unreachable_engine: AsyncEngine) -> None:
    bad = settings.model_copy(update={"db_name": "--all-databases"})
    result = await run_backup(unreachable_engine, bad, now=datetime(2026, 10, 31, 3, tzinfo=UTC))
    assert result.errors[0].startswith("dump failed: database name")
    assert not (fake_dump / "argv").exists()


async def test_a_run_with_both_parts_failing_reports_both(fake_dump: Path, settings: Settings, unreachable_engine: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE_MODE", "fail")
    result = await run_backup(unreachable_engine, settings, now=datetime(2026, 10, 31, 3, tzinfo=UTC))
    assert [error.split(":")[0] for error in result.errors] == ["dump failed", "export failed"]
    assert (result.dump_path, result.export_path) == (None, None)
    assert list(settings.backup_dir.iterdir()) == []


async def test_an_unwritable_backup_directory_fails_the_run(settings: Settings, unreachable_engine: AsyncEngine) -> None:
    settings.backup_dir.write_bytes(b"not a directory")
    result = await run_backup(unreachable_engine, settings)
    assert not result.ok
    assert result.errors[0].startswith("preparing the backup directory failed")
