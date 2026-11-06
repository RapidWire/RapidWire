"""Offline verification of downloaded backup files: an export archive is checked member by member against its manifest, including the invariant values recomputed from its rows, and a dump is checked to decompress completely and end with mysqldump's completion marker."""

from __future__ import annotations

import gzip
import hashlib
import json
import posixpath
import tarfile
import zlib
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import IO, Any, Final

from rapidwire.backup.dump import COMPLETION_MARKER, ends_with_marker
from rapidwire.backup.encoding import EncodingError, TableSpec, canonical_line, decode_row
from rapidwire.backup.manifest import MANIFEST_NAME, Manifest, ManifestError
from rapidwire.backup.totals import TOTALS_TABLES, TotalsBuilder, totals_column_problems

__all__ = ["FileKind", "FileReport", "Unreadable", "iter_export", "kind_of", "read_manifest", "verify_dump", "verify_export", "verify_file"]

_READ_CHUNK: Final = 1024 * 1024
_TAIL_BYTES: Final = 4096
# Problems reported per table before the rest are only counted.
MAX_PROBLEMS_PER_TABLE: Final = 10
# Largest manifest read into memory.
MANIFEST_MAX_BYTES: Final = 64 * 1024 * 1024
_READ_ERRORS: Final = (OSError, EOFError, zlib.error, tarfile.TarError, gzip.BadGzipFile)


class FileKind(StrEnum):
    DUMP = "dump"
    EXPORT = "export"


class Unreadable(Exception):
    """The file cannot be read as a backup of its kind."""


@dataclass
class FileReport:
    """The outcome for one file: problems are mismatches found in readable content, unreadable says why the file could not be read at all."""

    path: Path
    kind: FileKind | None
    problems: list[str] = field(default_factory=list)
    unreadable: str | None = None
    summary: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems and self.unreadable is None


def kind_of(path: Path) -> FileKind | None:
    """The kind of backup a file name says it is: a .sql.gz file is a dump and a .tar.gz file an export."""
    name = path.name
    if name.endswith(".sql.gz"):
        return FileKind.DUMP
    if name.endswith(".tar.gz"):
        return FileKind.EXPORT
    return None


def verify_file(path: Path) -> FileReport:
    kind = kind_of(path)
    if kind is None:
        return FileReport(path=path, kind=None, unreadable="the name ends neither in .sql.gz (a dump) nor in .tar.gz (an export)")
    try:
        return verify_dump(path) if kind is FileKind.DUMP else verify_export(path)
    except Unreadable as exc:
        return FileReport(path=path, kind=kind, unreadable=str(exc))
    except _READ_ERRORS as exc:
        return FileReport(path=path, kind=kind, unreadable=f"{type(exc).__name__}: {exc}")


def verify_dump(path: Path) -> FileReport:
    """Decompress the whole dump, which checks gzip's CRC and length, and require its first line to be mysqldump's header and its last non-empty line the completion marker."""
    report = FileReport(path=path, kind=FileKind.DUMP)
    size = 0
    head = b""
    tail = b""
    with gzip.open(path, "rb") as stream:
        while chunk := stream.read(_READ_CHUNK):
            if len(head) < _TAIL_BYTES:
                head += chunk[: _TAIL_BYTES - len(head)]
            tail = (tail + chunk)[-_TAIL_BYTES:]
            size += len(chunk)
    if not head.startswith(b"-- MySQL dump"):
        report.problems.append("the dump does not start with mysqldump's header")
    if not ends_with_marker(tail):
        report.problems.append(f"the dump does not end with {COMPLETION_MARKER.decode()!r}, so it is incomplete")
    report.summary = f"{size} bytes of SQL"
    return report


def _safe_member_name(name: str) -> bool:
    return bool(name) and not name.startswith("/") and "\\" not in name and posixpath.normpath(name) == name and ".." not in name.split("/")


def read_manifest(archive: tarfile.TarFile) -> Manifest:
    """Read the first member of an archive opened for streaming, which must be the manifest."""
    member = archive.next()
    if member is None or member.name != MANIFEST_NAME or not member.isfile():
        raise Unreadable(f"the first member of the archive is not {MANIFEST_NAME}")
    if member.size > MANIFEST_MAX_BYTES:
        raise Unreadable("the manifest is too large")
    handle = archive.extractfile(member)
    if handle is None:
        raise Unreadable("the manifest cannot be read")
    try:
        manifest = Manifest.from_bytes(handle.read())
    except ManifestError as exc:
        raise Unreadable(str(exc)) from exc
    listed = {entry.spec.name for entry in manifest.tables}
    problems = [f"table {name} is missing" for name in sorted(TOTALS_TABLES - listed)]
    problems += [problem for entry in manifest.tables for problem in totals_column_problems(entry.spec)]
    if problems:
        raise Unreadable("the manifest's tables do not have the columns the invariant values are computed from: " + "; ".join(problems))
    return manifest


def iter_export(archive: tarfile.TarFile, manifest: Manifest, problems: list[str]) -> Iterator[tuple[TableSpec, IO[bytes]]]:
    """Yield each table member after the manifest with its spec, adding a problem for every unsafe, unexpected, duplicate or missing member."""
    expected = {entry.spec.file: entry.spec for entry in manifest.tables}
    seen: set[str] = set()
    while (member := archive.next()) is not None:
        name = member.name
        if not _safe_member_name(name):
            problems.append(f"the archive member {name!r} has an unsafe path")
            continue
        if name not in expected:
            problems.append(f"the archive has an unexpected member {name!r}")
            continue
        if name in seen:
            problems.append(f"the archive has {name!r} twice")
            continue
        seen.add(name)
        if not member.isfile():
            problems.append(f"the archive member {name!r} is not a regular file")
            continue
        handle = archive.extractfile(member)
        if handle is None:
            problems.append(f"the archive member {name!r} cannot be read")
            continue
        yield expected[name], handle
    for name in sorted(expected.keys() - seen):
        problems.append(f"the archive is missing {name!r}")


def _check_table(spec: TableSpec, handle: IO[bytes], totals: TotalsBuilder) -> tuple[int, int, str, list[str]]:
    """Read one table member, returning its line count, byte size, SHA-256 and the problems of its lines."""
    hasher = hashlib.sha256()
    rows = 0
    size = 0
    problems: list[str] = []
    bad = 0
    previous: tuple[Any, ...] | None = None
    for line in handle:
        rows += 1
        size += len(line)
        hasher.update(line)
        try:
            if not line.endswith(b"\n"):
                raise EncodingError("the line has no terminating newline")
            encoded = json.loads(line)
            if not isinstance(encoded, dict):
                raise EncodingError("the line is not a JSON object")
            if canonical_line(encoded) != line:
                raise EncodingError("the line is not in canonical form")
            decoded = decode_row(spec, encoded)
            key = tuple(decoded[name] for name in spec.primary_key)
            if previous is not None and not previous < key:
                raise EncodingError("the row is not after the previous one in primary key order")
            previous = key
            if spec.name in TOTALS_TABLES:
                totals.add(spec.name, decoded)
        except (EncodingError, ValueError, TypeError) as exc:
            bad += 1
            if bad <= MAX_PROBLEMS_PER_TABLE:
                problems.append(f"{spec.file} line {rows}: {exc}")
            continue
    if bad > MAX_PROBLEMS_PER_TABLE:
        problems.append(f"{spec.file}: {bad - MAX_PROBLEMS_PER_TABLE} more invalid lines")
    return rows, size, hasher.hexdigest(), problems


def verify_export(path: Path) -> FileReport:
    """Check every table file's size, row count and SHA-256 against the manifest, the encoding of every line and that rows are in strictly ascending primary key order, the archive's member list, and the invariant values recomputed from the rows (including that every currency's balances sum to zero) against the manifest's."""
    report = FileReport(path=path, kind=FileKind.EXPORT)
    totals = TotalsBuilder()
    with tarfile.open(path, mode="r|gz") as archive:
        manifest = read_manifest(archive)
        entries = {entry.spec.name: entry for entry in manifest.tables}
        for spec, handle in iter_export(archive, manifest, report.problems):
            entry = entries[spec.name]
            rows, size, digest, problems = _check_table(spec, handle, totals)
            report.problems.extend(problems)
            if (rows, size, digest) != (entry.rows, entry.size, entry.sha256):
                report.problems.append(f"{spec.file} has {rows} rows, {size} bytes and SHA-256 {digest}; the manifest says {entry.rows} rows, {entry.size} bytes and {entry.sha256}")
    computed = totals.build()
    report.problems.extend(f"invariant values differ: {item}" for item in manifest.totals.differences(computed))
    for currency_id, total in totals.unbalanced().items():
        report.problems.append(f"the balances of currency {currency_id} sum to {total}, not 0")
    if not manifest.invariants_ok or manifest.invariant_violations:
        report.problems.append(f"the database had {manifest.invariant_violations} invariant violations when this export was taken")
    report.summary = (
        f"schema {manifest.schema_revision}, taken {manifest.created_at.isoformat()} by rapidwire {manifest.app_version},"
        f" {len(manifest.tables)} tables, {sum(entry.rows for entry in manifest.tables)} rows, {len(computed.currencies)} currencies, {len(computed.pools)} pools"
    )
    return report
