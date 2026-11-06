"""The backup job: one run writes a dump and an export of one generation into the backup directory and then applies retention; the caller holds the job lease named BACKUP_JOB_NAME."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.backup.dump import ConnectionOptions, write_dump
from rapidwire.backup.export import write_export
from rapidwire.backup.naming import format_stamp
from rapidwire.backup.retention import apply_retention, newest_complete_mtime, remove_leftovers
from rapidwire.config import Settings
from rapidwire.core.clock import ensure_utc
from rapidwire.logging import redact, register_secret

__all__ = ["BACKUP_JOB_NAME", "BACKUP_LEASE_TTL", "BackupRunResult", "next_backup_due", "run_backup"]

BACKUP_JOB_NAME: Final = "backup"
# Lease length for a backup run; the holder renews it while the run lasts.
BACKUP_LEASE_TTL: Final = timedelta(minutes=5)
_DIRECTORY_MODE: Final = 0o750

log = logging.getLogger("rapidwire.backup")


@dataclass(frozen=True)
class BackupRunResult:
    """What one run did: the published files (None when that part failed), the names retention deleted, the errors (empty on success), and the invariant violations the export's snapshot had (None when the export failed)."""

    started_at: datetime
    dump_path: Path | None
    export_path: Path | None
    deleted: tuple[str, ...]
    errors: tuple[str, ...]
    invariant_violations: int | None = None

    @property
    def ok(self) -> bool:
        return not self.errors and self.dump_path is not None and self.export_path is not None


def next_backup_due(backup_dir: Path, interval: timedelta, now: datetime) -> datetime:
    """When the next run is due: interval after the newest complete generation's files were written, or now when there is none; a failed run does not move it, so the caller applies its own retry delay."""
    newest = newest_complete_mtime(backup_dir)
    current = ensure_utc(now)
    if newest is None:
        return current
    return newest + interval


async def run_backup(engine: AsyncEngine, settings: Settings, *, now: datetime | None = None) -> BackupRunResult:
    """Remove leftovers of an interrupted run, write the dump and the export of one generation, then apply retention, which after a failure prunes only incomplete generations and always keeps this run's files, so the directory holds at most backup_retention complete generations and one incomplete one.

    Failures are logged and returned in errors; cancellation removes the partial files, stops the child processes and propagates.
    """
    started = ensure_utc(now) if now is not None else datetime.now(UTC)
    stamp = format_stamp(started)
    directory = settings.backup_dir
    password = settings.db_password.get_secret_value()
    register_secret(password)
    errors: list[str] = []
    dump_path: Path | None = None
    export_path: Path | None = None
    violations: int | None = None
    log.info("backup started", extra={"generation": stamp, "backup_dir": str(directory)})

    try:
        directory.mkdir(mode=_DIRECTORY_MODE, parents=True, exist_ok=True)
        remove_leftovers(directory)
    except OSError as exc:
        errors.append(redact(f"preparing the backup directory failed: {exc}"))
        log.exception("preparing the backup directory failed", extra={"backup_dir": str(directory)})
        return BackupRunResult(started_at=started, dump_path=None, export_path=None, deleted=(), errors=tuple(errors))

    try:
        options = ConnectionOptions(host=settings.db_host, port=settings.db_port, user=settings.db_user, password=password, database=settings.db_name)
        dump_path = await write_dump(options, directory, stamp)
    except Exception as exc:
        errors.append(redact(f"dump failed: {exc}"))
        log.exception("backup dump failed", extra={"generation": stamp})

    try:
        export = await write_export(engine, directory, stamp, created_at=started)
        export_path = export.path
        violations = export.invariant_violations
    except Exception as exc:
        errors.append(redact(f"export failed: {exc}"))
        log.exception("backup export failed", extra={"generation": stamp})

    deleted: list[str] = []
    succeeded = not errors
    try:
        deleted = apply_retention(directory, settings.backup_retention, current=stamp, prune_complete=succeeded)
    except OSError as exc:
        errors.append(redact(f"retention failed: {exc}"))
        log.exception("backup retention failed", extra={"generation": stamp})

    result = BackupRunResult(
        started_at=started,
        dump_path=dump_path,
        export_path=export_path,
        deleted=tuple(deleted),
        errors=tuple(errors),
        invariant_violations=violations,
    )
    files = [path.name for path in (dump_path, export_path) if path is not None]
    if result.ok:
        log.info("backup finished", extra={"generation": stamp, "files": files, "deleted": list(deleted), "invariant_violations": violations})
    else:
        log.error("backup failed", extra={"generation": stamp, "files": files, "errors": list(errors)})
    if violations:
        log.error("the exported snapshot violates the ledger invariants", extra={"generation": stamp, "violations": violations})
    return result
