"""The backup command: without a subcommand it takes a backup now under the backup job lease; verify and restore run on the operator's machine against downloaded files and read no RapidWire settings."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from pathlib import Path
from typing import Final

from rapidwire.cli import EXIT_FAILURE, EXIT_INVARIANT_VIOLATION, EXIT_OK
from rapidwire.config import Settings
from rapidwire.logging import configure_logging, redact, register_secret

__all__ = ["OPERATOR_COMMANDS", "add_arguments", "is_operator_command", "run_backup_command", "run_operator_command"]

OPERATOR_COMMANDS: Final = frozenset({"verify", "restore"})
OPERATOR_LOG_LEVEL: Final = "INFO"

log = logging.getLogger("rapidwire.cli")

VERIFY_HELP: Final = (
    "Check downloaded backup files offline."
    " An export (.tar.gz) must match its manifest: every table file's row count, size and SHA-256, the canonical encoding of every row, rows in strictly ascending primary key order, exactly the listed archive members at safe paths, and the invariant values recomputed from the rows, with every currency's balances summing to zero."
    " An export whose manifest records invariant violations of its snapshot counts as a mismatch, and one whose manifest lacks the tables or columns the invariant values come from cannot be read."
    " A dump (.sql.gz) must decompress completely, start with mysqldump's header and end with its completion marker."
    f" Exits {EXIT_OK} when every file passes, {EXIT_INVARIANT_VIOLATION} when any file has a mismatch, and otherwise {EXIT_FAILURE} when a file cannot be read."
)
RESTORE_HELP: Final = (
    "Restore a backup file into an existing, empty MySQL database (for example a local MySQL 8.4 container) and verify it."
    " A dump is fed to the mysql client after its header is checked, and a missing completion marker is a mismatch."
    " An export is loaded into the schema revision it was taken at and exported again, and every table checksum must equal the manifest."
    " The ledger invariants are then checked on the restored database."
    f" Exits {EXIT_OK} when the restore verifies, {EXIT_INVARIANT_VIOLATION} on a mismatch or invariant violation, and {EXIT_FAILURE} when the restore fails."
)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    commands = parser.add_subparsers(dest="backup_command", metavar="subcommand")
    verify = commands.add_parser("verify", help="Check downloaded backup files offline.", description=VERIFY_HELP)
    verify.add_argument("files", nargs="+", type=Path, metavar="FILE", help="A rapidwire-dump-*.sql.gz or rapidwire-export-*.tar.gz file.")
    restore = commands.add_parser("restore", help="Restore a backup into an empty database and verify it.", description=RESTORE_HELP)
    restore.add_argument(
        "--target-url",
        required=True,
        metavar="URL",
        help="mysql://user@host:port/database of an existing, empty database; the password is read from MYSQL_PWD unless the URL has one.",
    )
    restore.add_argument("file", type=Path, metavar="FILE", help="A rapidwire-dump-*.sql.gz or rapidwire-export-*.tar.gz file.")


def is_operator_command(args: argparse.Namespace) -> bool:
    return args.command == "backup" and getattr(args, "backup_command", None) in OPERATOR_COMMANDS


def _configure_operator_logging(secrets: list[str]) -> None:
    configure_logging(OPERATOR_LOG_LEVEL, secrets)
    for handler in logging.getLogger().handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.setStream(sys.stderr)


def run_operator_command(args: argparse.Namespace) -> int:
    """Run backup verify or backup restore with default logging on standard error and the report on standard output."""
    try:
        if args.backup_command == "verify":
            _configure_operator_logging([])
            return _verify(args.files)
        return _restore(args.target_url, args.file)
    except Exception:
        log.exception("command failed", extra={"command": f"backup {args.backup_command}"})
        return EXIT_FAILURE


def _verify(files: list[Path]) -> int:
    from rapidwire.backup.verify import verify_file

    mismatched = unreadable = 0
    for path in files:
        report = verify_file(path)
        if report.unreadable is not None:
            unreadable += 1
            print(f"UNREADABLE {path}: {report.unreadable}")
        elif report.problems:
            mismatched += 1
            print(f"MISMATCH {path} ({report.kind}): {len(report.problems)} problems")
            for problem in report.problems:
                print(f"  {problem}")
        else:
            print(f"OK {path} ({report.kind}): {report.summary}")
    print(f"{len(files)} files checked, {len(files) - mismatched - unreadable} passed, {mismatched} mismatched, {unreadable} unreadable")
    log.info("backup files verified", extra={"files": len(files), "mismatched": mismatched, "unreadable": unreadable})
    if mismatched:
        return EXIT_INVARIANT_VIOLATION
    return EXIT_FAILURE if unreadable else EXIT_OK


def _restore(raw_url: str, path: Path) -> int:
    from rapidwire.backup.client import ClientError
    from rapidwire.backup.restore import RestoreError, parse_target_url, restore_backup

    try:
        target = parse_target_url(raw_url)
    except RestoreError as exc:
        _configure_operator_logging([])
        print(f"restore: {exc}", file=sys.stderr)
        return EXIT_FAILURE
    _configure_operator_logging([target.password] if target.password else [])
    try:
        outcome = asyncio.run(restore_backup(path, target))
    except (RestoreError, ClientError) as exc:
        print(f"restore failed: {redact(str(exc))}", file=sys.stderr)
        log.error("restore failed", extra={"file": str(path), "target": target.describe(), "error": str(exc)})
        return EXIT_FAILURE
    except Exception:
        log.exception("restore failed", extra={"file": str(path), "target": target.describe()})
        print("restore failed; see the log above", file=sys.stderr)
        return EXIT_FAILURE
    report = outcome.report
    rows = f", {outcome.rows} rows loaded" if outcome.rows is not None else ""
    print(f"restored {outcome.kind} {path} into {target.describe()}{rows}")
    print(f"invariants: {len(report.currencies)} currencies checked, {len(report.pools)} pools checked, {len(report.violations)} violations")
    for violation in report.violations:
        print(f"violation [{violation.check}]: {violation.message}")
    for problem in outcome.problems:
        print(f"mismatch: {problem}")
    print("restore verified" if outcome.ok else "restore did NOT verify")
    log.info("restore finished", extra={"file": str(path), "target": target.describe(), "ok": outcome.ok, "violations": len(report.violations), "problems": len(outcome.problems)})
    return EXIT_OK if outcome.ok else EXIT_INVARIANT_VIOLATION


def run_backup_command(settings: Settings, args: argparse.Namespace) -> int:
    """Take one backup now while holding the backup job lease; exits with failure when another holder has the lease, the lease is lost or the run fails, and with EXIT_INVARIANT_VIOLATION when the backup was written but its snapshot violates the ledger invariants."""
    from rapidwire.backup.jobs import BACKUP_JOB_NAME, BACKUP_LEASE_TTL, BackupRunResult, run_backup
    from rapidwire.core.joblock import LeaseLost, LeaseUnavailable, hold_lease, make_holder
    from rapidwire.db.engine import create_engine

    register_secret(settings.db_password.get_secret_value())

    async def run() -> BackupRunResult:
        engine = create_engine(settings.database_url)
        try:
            async with hold_lease(engine, BACKUP_JOB_NAME, holder=make_holder(), ttl=BACKUP_LEASE_TTL):
                return await run_backup(engine, settings)
        finally:
            await engine.dispose()

    try:
        result = asyncio.run(run())
    except LeaseUnavailable:
        log.error("another backup is running: the backup job lease is held by another process", extra={"job": BACKUP_JOB_NAME})
        print("backup: another backup is running (the backup job lease is held); try again later", file=sys.stderr)
        return EXIT_FAILURE
    except LeaseLost:
        log.error("the backup job lease was lost, so the backup was stopped", extra={"job": BACKUP_JOB_NAME})
        return EXIT_FAILURE
    if not result.ok:
        return EXIT_FAILURE
    return EXIT_INVARIANT_VIOLATION if result.invariant_violations else EXIT_OK
