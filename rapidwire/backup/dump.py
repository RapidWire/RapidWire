"""The compressed dump: mysqldump of the whole database in one consistent transaction, streamed through gzip into a partial file and published when mysqldump finished cleanly and wrote its completion marker."""

from __future__ import annotations

import asyncio
import contextlib
import gzip
import logging
import os
from pathlib import Path
from typing import BinaryIO, Final

from rapidwire.backup.client import MYSQLDUMP, ClientError, ConnectionOptions, client_process, collect_stderr, stderr_errors
from rapidwire.backup.files import create_partial, discard, in_thread, publish
from rapidwire.backup.naming import BackupKind, parse_stamp, partial_name

__all__ = ["COMPLETION_MARKER", "DUMP_ARGUMENTS", "ConnectionOptions", "dump_arguments", "write_dump"]

log = logging.getLogger("rapidwire.backup")

# One REPEATABLE READ snapshot without locking tables; binary columns as hex literals; no tablespace statements (they need the PROCESS privilege) and no GTID statements, so an unprivileged account can take the dump and restore it.
# Without --databases the dump has no CREATE DATABASE or USE statement and restores into whichever database the client names.
DUMP_ARGUMENTS: Final = (
    "--single-transaction",
    "--hex-blob",
    "--no-tablespaces",
    "--set-gtid-purged=OFF",
    "--default-character-set=utf8mb4",
    "--triggers",
    "--skip-routines",
    "--skip-events",
    "--quick",
)
# mysqldump writes this comment last, and only when the dump finished.
COMPLETION_MARKER: Final = b"-- Dump completed"
_WRITE_BATCH: Final = 1024 * 1024
_TAIL_BYTES: Final = 4096
_COMPRESS_LEVEL: Final = 6


def dump_arguments(database: str) -> list[str]:
    return [*DUMP_ARGUMENTS, database]


def ends_with_marker(tail: bytes) -> bool:
    """Whether the last non-empty line of a dump is mysqldump's completion comment."""
    lines = [line for line in tail.splitlines() if line.strip()]
    return bool(lines) and lines[-1].startswith(COMPLETION_MARKER)


def _finish(compressor: gzip.GzipFile, raw: BinaryIO) -> None:
    compressor.close()
    raw.flush()
    os.fsync(raw.fileno())


async def _stream(options: ConnectionOptions, raw: BinaryIO, mtime: float) -> None:
    compressor = gzip.GzipFile(filename="", mode="wb", fileobj=raw, compresslevel=_COMPRESS_LEVEL, mtime=mtime)
    async with client_process(MYSQLDUMP, dump_arguments(options.database), options, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE) as process:
        if process.stdout is None or process.stderr is None:
            raise ClientError("mysqldump has no output pipes")
        stderr_task = asyncio.create_task(collect_stderr(process.stderr))
        try:
            tail = b""
            pending: list[bytes] = []
            size = 0
            while chunk := await process.stdout.read(_WRITE_BATCH):
                tail = (tail + chunk)[-_TAIL_BYTES:]
                pending.append(chunk)
                size += len(chunk)
                if size >= _WRITE_BATCH:
                    await in_thread(compressor.write, b"".join(pending))
                    pending, size = [], 0
            if pending:
                await in_thread(compressor.write, b"".join(pending))
            returncode = await process.wait()
            stderr = await stderr_task
        finally:
            if not stderr_task.done():
                stderr_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stderr_task
    errors = stderr_errors(stderr)
    if returncode != 0 or errors:
        detail = "; ".join(errors[:5]) or "no error output"
        raise ClientError(f"mysqldump exited with status {returncode}: {detail}")
    if stderr.strip():
        log.warning("mysqldump reported warnings", extra={"stderr": stderr})
    if not ends_with_marker(tail):
        raise ClientError("mysqldump output does not end with its completion marker")
    await in_thread(_finish, compressor, raw)


async def write_dump(options: ConnectionOptions, directory: Path, stamp: str) -> Path:
    """Dump the database of options into the generation's dump file in directory and return its path; on failure or cancellation the partial file is removed and mysqldump is stopped."""
    final = directory / BackupKind.DUMP.file_name(stamp)
    partial = directory / partial_name(BackupKind.DUMP, stamp)
    fd = create_partial(partial)
    try:
        with os.fdopen(fd, "wb") as raw:
            await _stream(options, raw, parse_stamp(stamp).timestamp())
        path = publish(partial, final)
    except BaseException:
        discard(partial)
        raise
    log.info("backup dump written", extra={"file": final.name, "size": path.stat().st_size})
    return path
