"""The application-format export: every table of the schema read from one consistent snapshot as canonical JSON Lines, with a manifest of counts, checksums and the invariant values of the same snapshot, packed into one gzip-compressed tar archive."""

from __future__ import annotations

import gzip
import hashlib
import io
import logging
import os
import shutil
import tarfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire import __version__
from rapidwire.backup.encoding import TABLES_DIRECTORY, TableSpec, canonical_line, decode_row, encode_row, row_select, table_specs
from rapidwire.backup.files import create_partial, discard, in_thread, publish
from rapidwire.backup.manifest import MANIFEST_NAME, Manifest, TableEntry
from rapidwire.backup.naming import BackupKind, partial_name, staging_name
from rapidwire.backup.totals import TOTALS_TABLES, TotalsBuilder, totals_from_report
from rapidwire.core.db import consistent_snapshot
from rapidwire.core.invariants import verify_invariants
from rapidwire.db.tables import metadata

__all__ = ["ExportError", "ExportResult", "TableDigest", "export_tables", "schema_revision", "write_export"]

log = logging.getLogger("rapidwire.backup")

_WRITE_BATCH: Final = 1024 * 1024
_COPY_CHUNK: Final = 1024 * 1024
_FILE_MODE: Final = 0o644
_DIRECTORY_MODE: Final = 0o700
_COMPRESS_LEVEL: Final = 6


class ExportError(Exception):
    """The export could not be produced consistently."""


class _Stopped(Exception):
    """Packing was asked to stop because the run was cancelled."""


@dataclass
class TableDigest:
    spec: TableSpec
    rows: int = 0
    size: int = 0
    hasher: hashlib._Hash = field(default_factory=hashlib.sha256)

    @property
    def sha256(self) -> str:
        return self.hasher.hexdigest()

    def entry(self) -> TableEntry:
        return TableEntry(spec=self.spec, rows=self.rows, size=self.size, sha256=self.sha256)


@dataclass(frozen=True)
class ExportResult:
    path: Path
    manifest: Manifest

    @property
    def invariant_violations(self) -> int:
        return self.manifest.invariant_violations


async def schema_revision(conn: AsyncConnection) -> str:
    """The Alembic revision the database is at."""
    revisions = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    if len(revisions) != 1:
        raise ExportError(f"alembic_version holds {len(revisions)} revisions instead of one")
    return str(revisions[0])


def _write_all(handle: IO[bytes], data: bytes) -> None:
    handle.write(data)


async def export_tables(conn: AsyncConnection, specs: list[TableSpec], *, directory: Path | None = None, totals: TotalsBuilder | None = None) -> list[TableDigest]:
    """Read every table of specs in primary key order on conn, hash its canonical lines, write them to directory/<table>.jsonl when a directory is given, and feed the TOTALS_TABLES rows to totals."""
    digests: list[TableDigest] = []
    for spec in specs:
        digest = TableDigest(spec=spec)
        handle = open(directory / f"{spec.name}.jsonl", "xb") if directory is not None else None  # noqa: SIM115, ASYNC230 - the handle is closed below; creating an empty file does not block.
        try:
            pending: list[bytes] = []
            pending_size = 0
            result = await conn.stream(row_select(spec))
            async for row in result:
                encoded = encode_row(spec, row)
                line = canonical_line(encoded)
                digest.hasher.update(line)
                digest.rows += 1
                digest.size += len(line)
                if totals is not None and spec.name in TOTALS_TABLES:
                    totals.add(spec.name, decode_row(spec, encoded))
                if handle is not None:
                    pending.append(line)
                    pending_size += len(line)
                    if pending_size >= _WRITE_BATCH:
                        await in_thread(_write_all, handle, b"".join(pending))
                        pending, pending_size = [], 0
            if handle is not None and pending:
                await in_thread(_write_all, handle, b"".join(pending))
        finally:
            if handle is not None:
                handle.close()
        digests.append(digest)
    return digests


class _StoppableReader:
    def __init__(self, handle: IO[bytes], stop: threading.Event) -> None:
        self._handle = handle
        self._stop = stop

    def read(self, size: int = -1) -> bytes:
        if self._stop.is_set():
            raise _Stopped
        return self._handle.read(size if size > 0 else _COPY_CHUNK)


def _member(name: str, size: int, mtime: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mtime = mtime
    info.mode = _FILE_MODE
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _pack(fd: int, manifest: bytes, tables: Path, entries: tuple[TableEntry, ...], mtime: int, stop: threading.Event) -> None:
    """Write the archive (manifest first, then the table files in manifest order) into fd, flush and sync it, and close fd."""
    with open(fd, "wb") as raw:
        with (
            gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=_COMPRESS_LEVEL, mtime=mtime) as compressed,
            tarfile.open(fileobj=compressed, mode="w", format=tarfile.USTAR_FORMAT) as archive,
        ):
            archive.addfile(_member(MANIFEST_NAME, len(manifest), mtime), _StoppableReader(io.BytesIO(manifest), stop))
            for entry in entries:
                with open(tables / f"{entry.spec.name}.jsonl", "rb") as handle:
                    archive.addfile(_member(entry.spec.file, entry.size, mtime), _StoppableReader(handle, stop))
        raw.flush()
        os.fsync(raw.fileno())


async def write_export(engine: AsyncEngine, directory: Path, stamp: str, *, created_at: datetime) -> ExportResult:
    """Export every table on the schema metadata from one snapshot into the generation's export archive in directory and return it; on failure or cancellation the partial archive and the staging directory are removed."""
    final = directory / BackupKind.EXPORT.file_name(stamp)
    partial = directory / partial_name(BackupKind.EXPORT, stamp)
    staging = directory / staging_name(stamp)
    tables = staging / TABLES_DIRECTORY
    staging.mkdir(mode=_DIRECTORY_MODE)
    try:
        tables.mkdir(mode=_DIRECTORY_MODE)
        specs = table_specs(metadata)
        builder = TotalsBuilder()
        async with consistent_snapshot(engine) as conn:
            revision = await schema_revision(conn)
            digests = await export_tables(conn, specs, directory=tables, totals=builder)
            report = await verify_invariants(conn)
        expected = totals_from_report(report)
        mismatches = expected.differences(builder.build())
        if mismatches:
            raise ExportError("the exported rows disagree with the invariant values of their snapshot: " + "; ".join(mismatches[:5]))
        manifest = Manifest(
            app_version=__version__,
            schema_revision=revision,
            created_at=created_at,
            tables=tuple(digest.entry() for digest in digests),
            totals=expected,
            invariants_ok=report.ok,
            invariant_violations=len(report.violations),
        )
        fd = create_partial(partial)
        stop = threading.Event()
        try:
            await in_thread(_pack, fd, manifest.to_bytes(), tables, manifest.tables, int(created_at.timestamp()), stop, stop=stop)
            path = publish(partial, final)
        except BaseException:
            discard(partial)
            raise
    finally:
        await in_thread(shutil.rmtree, staging, True)
    log.info("backup export written", extra={"file": final.name, "size": path.stat().st_size, "tables": len(digests), "schema_revision": revision})
    return ExportResult(path=path, manifest=manifest)
