"""Restoring a backup into an empty MySQL database on the operator's machine and checking the result: a dump is fed to the mysql client, an export is loaded row by row into the schema revision it was taken at and exported again to compare every table checksum, and both are checked with the ledger invariant verifier."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import json
import logging
import os
import tarfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any, Final

from alembic.script import ScriptDirectory
from alembic.util.exc import CommandError
from sqlalchemy import URL, delete, insert, make_url, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.backup.client import MYSQL, ClientError, ConnectionOptions, client_process, collect_stderr, stderr_errors, validate_database_name
from rapidwire.backup.dump import COMPLETION_MARKER, ends_with_marker
from rapidwire.backup.encoding import TableSpec, decode_row, raw_table
from rapidwire.backup.export import export_tables
from rapidwire.backup.files import in_thread
from rapidwire.backup.manifest import Manifest
from rapidwire.backup.totals import totals_from_report
from rapidwire.backup.verify import FileKind, Unreadable, iter_export, kind_of, read_manifest
from rapidwire.core.db import consistent_snapshot
from rapidwire.core.invariants import InvariantReport, verify_invariants
from rapidwire.db.engine import create_engine
from rapidwire.db.migrate import make_config, upgrade_async

__all__ = ["RestoreError", "RestoreOutcome", "RestoreTarget", "parse_target_url", "restore_backup"]

log = logging.getLogger("rapidwire.backup")

DEFAULT_PORT: Final = 3306
PASSWORD_ENV: Final = "MYSQL_PWD"  # noqa: S105 - the name of the variable, not a password.
# Rows inserted per statement while loading an export.
INSERT_BATCH: Final = 500
_FEED_CHUNK: Final = 1024 * 1024
_TAIL_BYTES: Final = 4096
_TARGET_SCHEMES: Final = frozenset({"mysql", "mysql+asyncmy"})
# information_schema.COLUMNS.EXTRA of generated columns; DEFAULT_GENERATED marks a column with an expression default, which is stored data.
_GENERATED_EXTRA: Final = frozenset({"VIRTUAL GENERATED", "STORED GENERATED"})
_HEADER: Final = b"-- MySQL dump"


class RestoreError(Exception):
    """The backup cannot be restored into the target."""


@dataclass(frozen=True)
class RestoreTarget:
    """The database to restore into; password is never shown in its repr."""

    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str

    @property
    def url(self) -> URL:
        return URL.create("mysql+asyncmy", username=self.user, password=self.password, host=self.host, port=self.port, database=self.database, query={"charset": "utf8mb4"})

    @property
    def options(self) -> ConnectionOptions:
        return ConnectionOptions(host=self.host, port=self.port, user=self.user, password=self.password, database=self.database)

    def describe(self) -> str:
        return f"{self.user}@{self.host}:{self.port}/{self.database}"


@dataclass
class RestoreOutcome:
    """The invariant report of the restored database and every mismatch found after the restore."""

    kind: FileKind
    report: InvariantReport
    problems: list[str] = field(default_factory=list)
    rows: int | None = None

    @property
    def ok(self) -> bool:
        return self.report.ok and not self.problems


def parse_target_url(raw: str, environ: dict[str, str] | None = None) -> RestoreTarget:
    """Parse mysql://user[:password]@host[:port]/database; without a password in the URL the MYSQL_PWD environment variable supplies it, and without either the password is empty."""
    try:
        url = make_url(raw)
    except Exception as exc:
        raise RestoreError("the target URL is not of the form mysql://user@host:port/database") from exc
    if url.drivername not in _TARGET_SCHEMES or not url.host or not url.database or not url.username:
        raise RestoreError("the target URL is not of the form mysql://user@host:port/database")
    env = os.environ if environ is None else environ
    password = url.password if url.password is not None else env.get(PASSWORD_ENV, "")
    try:
        validate_database_name(url.database)
    except ClientError as exc:
        raise RestoreError(str(exc)) from exc
    return RestoreTarget(host=url.host, port=url.port or DEFAULT_PORT, user=url.username, password=str(password), database=url.database)


async def _require_empty(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        count = (await conn.execute(text("SELECT COUNT(*) FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"))).scalar_one()
        routines = (await conn.execute(text("SELECT COUNT(*) FROM information_schema.ROUTINES WHERE ROUTINE_SCHEMA = DATABASE()"))).scalar_one()
    if count or routines:
        raise RestoreError("the target database is not empty; restore into a new, empty database")


def _read_chunk(stream: IO[bytes]) -> bytes:
    return stream.read(_FEED_CHUNK)


async def _restore_dump(path: Path, target: RestoreTarget) -> list[str]:
    """Feed the decompressed dump to the mysql client connected to the target database and return the problems of the dump itself; a file that does not start with mysqldump's header is refused before the client starts, and one without the completion marker is a problem."""
    with gzip.open(path, "rb") as stream:
        first = await in_thread(_read_chunk, stream)
        if not first.startswith(_HEADER):
            raise RestoreError("the file does not start with mysqldump's header, so it is not a dump")
        tail = first[-_TAIL_BYTES:]
        async with client_process(MYSQL, ["--default-character-set=utf8mb4", "--batch", target.database], target.options, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL) as process:
            if process.stdin is None or process.stderr is None:
                raise ClientError("the mysql client has no pipes")
            stderr_task = asyncio.create_task(collect_stderr(process.stderr))
            try:
                with contextlib.suppress(BrokenPipeError, ConnectionResetError):
                    chunk = first
                    while chunk:
                        process.stdin.write(chunk)
                        await process.stdin.drain()
                        chunk = await in_thread(_read_chunk, stream)
                        tail = (tail + chunk)[-_TAIL_BYTES:]
                    process.stdin.close()
                returncode = await process.wait()
                stderr = await stderr_task
            finally:
                if not stderr_task.done():
                    stderr_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await stderr_task
    errors = stderr_errors(stderr)
    if returncode != 0 or errors:
        raise ClientError(f"mysql exited with status {returncode}: {'; '.join(errors[:5]) or 'no error output'}")
    if not ends_with_marker(tail):
        return [f"the dump does not end with {COMPLETION_MARKER.decode()!r}, so it is incomplete and the restored database may be missing data"]
    return []


def _known_revision(revision: str) -> None:
    try:
        script = ScriptDirectory.from_config(make_config()).get_revision(revision)
    except CommandError as exc:
        raise RestoreError(f"this rapidwire version does not know schema revision {revision!r}") from exc
    if script is None:
        raise RestoreError(f"this rapidwire version does not know schema revision {revision!r}")


async def _check_schema(conn: AsyncConnection, manifest: Manifest) -> None:
    """Require the migrated target to have exactly the manifest's tables, each with exactly its stored columns."""
    rows = (
        await conn.execute(
            text("SELECT TABLE_NAME, COLUMN_NAME, EXTRA FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME <> 'alembic_version' ORDER BY TABLE_NAME, ORDINAL_POSITION")
        )
    ).all()
    stored: dict[str, list[str]] = {}
    for table_name, column_name, extra in rows:
        columns = stored.setdefault(str(table_name), [])
        if str(extra).upper() not in _GENERATED_EXTRA:
            columns.append(str(column_name))
    expected = {entry.spec.name: list(entry.spec.column_names) for entry in manifest.tables}
    if stored.keys() != expected.keys():
        raise RestoreError(f"schema revision {manifest.schema_revision} has tables {sorted(stored)}, but the export has {sorted(expected)}")
    for name, columns in expected.items():
        if sorted(stored[name]) != sorted(columns):
            raise RestoreError(f"table {name} has columns {sorted(stored[name])} at schema revision {manifest.schema_revision}, but the export has {sorted(columns)}")


def _read_lines(handle: IO[bytes], count: int) -> list[bytes]:
    lines: list[bytes] = []
    for line in handle:
        lines.append(line)
        if len(lines) >= count:
            break
    return lines


async def _load_table(conn: AsyncConnection, spec: TableSpec, handle: IO[bytes]) -> int:
    target = raw_table(spec)
    loaded = 0
    while lines := await in_thread(_read_lines, handle, INSERT_BATCH):
        rows: list[dict[str, Any]] = [decode_row(spec, json.loads(line)) for line in lines]
        await conn.execute(insert(target), rows)
        loaded += len(rows)
    return loaded


class _Archive:
    """An export archive opened for one streaming pass."""

    def __init__(self, path: Path) -> None:
        self.archive = tarfile.open(path, mode="r|gz")  # noqa: SIM115 - closed by close().
        self.manifest = read_manifest(self.archive)

    def members(self, problems: list[str]) -> Iterator[tuple[TableSpec, IO[bytes]]]:
        return iter_export(self.archive, self.manifest, problems)

    def close(self) -> None:
        self.archive.close()


def _next_member(members: Iterator[tuple[TableSpec, IO[bytes]]]) -> tuple[TableSpec, IO[bytes]] | None:
    return next(members, None)


async def _restore_export(path: Path, engine: AsyncEngine) -> tuple[Manifest, int, list[str]]:
    """Migrate the target to the export's schema revision, replace the rows the migrations seeded with the exported rows, and return the manifest, the loaded row count and the archive problems."""
    archive = await in_thread(_Archive, path)
    problems: list[str] = []
    try:
        manifest = archive.manifest
        _known_revision(manifest.schema_revision)
        await upgrade_async(engine, manifest.schema_revision)
        loaded = 0
        async with engine.connect() as conn:
            await _check_schema(conn, manifest)
            await conn.execute(text("SET SESSION FOREIGN_KEY_CHECKS = 0"))
            try:
                for entry in manifest.tables:
                    await conn.execute(delete(raw_table(entry.spec)))
                members = archive.members(problems)
                while (item := await in_thread(_next_member, members)) is not None:
                    spec, handle = item
                    loaded += await _load_table(conn, spec, handle)
                    await conn.commit()
                await conn.commit()
            finally:
                await conn.rollback()
                await conn.execute(text("SET SESSION FOREIGN_KEY_CHECKS = 1"))
                await conn.commit()
    finally:
        archive.close()
    return manifest, loaded, problems


async def _compare_export(engine: AsyncEngine, manifest: Manifest, report: InvariantReport) -> list[str]:
    """Export the restored tables again and compare each table's row count, size and SHA-256, and the invariant values, with the manifest."""
    problems: list[str] = []
    async with consistent_snapshot(engine) as conn:
        digests = await export_tables(conn, [entry.spec for entry in manifest.tables])
    for entry, digest in zip(manifest.tables, digests, strict=True):
        if (digest.rows, digest.size, digest.sha256) != (entry.rows, entry.size, entry.sha256):
            problems.append(
                f"restored table {entry.spec.name} exports as {digest.rows} rows, {digest.size} bytes, SHA-256 {digest.sha256}; the manifest says {entry.rows} rows, {entry.size} bytes, {entry.sha256}"
            )
    problems.extend(f"restored invariant values differ: {item}" for item in manifest.totals.differences(totals_from_report(report)))
    return problems


async def restore_backup(path: Path, target: RestoreTarget) -> RestoreOutcome:
    """Restore the backup file at path into the target database, which must exist and hold no tables, then verify the restored database; the kind of backup follows from the file name."""
    kind = kind_of(path)
    if kind is None:
        raise RestoreError("the file name ends neither in .sql.gz (a dump) nor in .tar.gz (an export)")
    if not await in_thread(path.is_file):
        raise RestoreError(f"{path} is not a file")
    engine = create_engine(target.url)
    try:
        await _require_empty(engine)
        log.info("restoring backup", extra={"file": path.name, "kind": kind.value, "target": target.describe()})
        if kind is FileKind.DUMP:
            dump_problems = await _restore_dump(path, target)
            report = await verify_invariants(engine)
            return RestoreOutcome(kind=kind, report=report, problems=dump_problems)
        try:
            manifest, loaded, problems = await _restore_export(path, engine)
        except Unreadable as exc:
            raise RestoreError(f"the export cannot be read: {exc}") from exc
        report = await verify_invariants(engine)
        problems.extend(await _compare_export(engine, manifest, report))
        return RestoreOutcome(kind=kind, report=report, problems=problems, rows=loaded)
    finally:
        await engine.dispose()
