"""Backup files served to operators: which entries of the backup directory count as backups, their SHA-256 (computed here once per file identity, cached per application and warmed in the background at startup), and opening one relative to the directory without following symbolic links."""

from __future__ import annotations

import errno
import hashlib
import logging
import os
import re
import stat
import threading
from collections.abc import AsyncIterator, Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import anyio
from starlette.datastructures import State
from starlette.responses import StreamingResponse
from starlette.types import Receive, Scope, Send

__all__ = [
    "CHUNK_SIZE",
    "IN_PROGRESS_SUFFIXES",
    "NAME_PATTERN",
    "BackupChanged",
    "BackupFile",
    "BackupPage",
    "BackupStream",
    "ChecksumCache",
    "ChecksumWarmup",
    "FileIdentity",
    "OpenedBackup",
    "checksum_cache",
    "is_backup_name",
    "list_backup_page",
    "list_backups",
    "media_type_for",
    "open_backup",
    "sha256_of_fd",
    "start_checksum_warmup",
]

log = logging.getLogger("rapidwire.api.backups")

NAME_PATTERN: Final = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$"
IN_PROGRESS_SUFFIXES: Final = (".tmp", ".partial")
CHUNK_SIZE: Final = 1024 * 1024
_NAME_RE: Final = re.compile(NAME_PATTERN)
_GZIP_SUFFIXES: Final = (".gz", ".tgz")
_CACHE_ATTRIBUTE: Final = "rapidwire_backup_checksums"
_DIR_FLAGS: Final = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC
# O_NONBLOCK keeps a FIFO swapped in after the listing from blocking the open; fstat then rejects it.
_FILE_FLAGS: Final = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
# Errors meaning the name is not a servable regular file: gone, a symbolic link (ELOOP under O_NOFOLLOW), or a socket or device.
_UNAVAILABLE_ERRNOS: Final = frozenset({errno.ENOENT, errno.ELOOP, errno.ENOTDIR, errno.ENXIO, errno.ENODEV})


class BackupChanged(Exception):
    """The opened backup file holds fewer bytes than its size said, so it was truncated in place while being read; it is not an OSError, so a stream that fails with it is reported as a server error rather than a client disconnect."""


@dataclass(frozen=True)
class FileIdentity:
    """What the cached checksum of a backup is keyed by; any change of the file's inode, size or modification time makes the cache miss."""

    name: str
    device: int
    inode: int
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, name: str, info: os.stat_result) -> FileIdentity:
        return cls(name=name, device=info.st_dev, inode=info.st_ino, size=info.st_size, mtime_ns=info.st_mtime_ns)

    @property
    def created_at(self) -> datetime:
        """The modification time in UTC with microsecond precision, which is when the backup was completed."""
        seconds, nanos = divmod(self.mtime_ns, 1_000_000_000)
        return datetime.fromtimestamp(seconds, UTC) + timedelta(microseconds=nanos // 1000)


@dataclass(frozen=True)
class BackupFile:
    name: str
    created_at: datetime
    size: int
    sha256: str
    mtime_ns: int


class ChecksumCache:
    """SHA-256 digests of backup files keyed by FileIdentity, one entry per name, shared by the threads that list and open backups; warmup is the background hashing started with the application, if any."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, tuple[FileIdentity, str]] = {}
        self._pending: dict[FileIdentity, threading.Event] = {}
        self.directory_missing_logged = False
        self.warmup: ChecksumWarmup | None = None

    def lookup(self, identity: FileIdentity) -> str | None:
        with self._lock:
            return self._cached(identity)

    def _cached(self, identity: FileIdentity) -> str | None:
        entry = self._entries.get(identity.name)
        if entry is None or entry[0] != identity:
            return None
        return entry[1]

    def store(self, identity: FileIdentity, digest: str) -> None:
        with self._lock:
            self._entries[identity.name] = (identity, digest)

    def digest(self, identity: FileIdentity, compute: Callable[[], str]) -> str:
        """The cached digest of identity, else compute() run by exactly one of the threads asking for the same identity at once while the others wait for its result; when it fails, a waiting thread computes it in turn."""
        while True:
            with self._lock:
                cached = self._cached(identity)
                if cached is not None:
                    return cached
                pending = self._pending.get(identity)
                owner = pending is None
                if pending is None:
                    pending = self._pending[identity] = threading.Event()
            if not owner:
                pending.wait()
                continue
            try:
                digest = compute()
                self.store(identity, digest)
                return digest
            finally:
                with self._lock:
                    del self._pending[identity]
                pending.set()

    def retain(self, names: Iterable[str]) -> None:
        """Drop the entries of names that are no longer in the directory."""
        keep = set(names)
        with self._lock:
            for name in [name for name in self._entries if name not in keep]:
                del self._entries[name]


def checksum_cache(state: State) -> ChecksumCache:
    """The ChecksumCache of the application whose state this is, created on first use."""
    cache = getattr(state, _CACHE_ATTRIBUTE, None)
    if not isinstance(cache, ChecksumCache):
        cache = ChecksumCache()
        setattr(state, _CACHE_ATTRIBUTE, cache)
    return cache


def is_backup_name(name: str) -> bool:
    """Whether name may be listed or downloaded: it matches NAME_PATTERN (so it has no path separator, no leading dot and only ASCII letters, digits, ".", "_" and "-") and does not end in an IN_PROGRESS_SUFFIXES suffix."""
    return _NAME_RE.fullmatch(name) is not None and not name.endswith(IN_PROGRESS_SUFFIXES)


def media_type_for(name: str) -> str:
    return "application/gzip" if name.endswith(_GZIP_SUFFIXES) else "application/octet-stream"


def sha256_of_fd(fd: int, size: int) -> str:
    """The hex SHA-256 of the first size bytes of the open file fd, read with pread so the file offset is not used; BackupChanged when the file is shorter."""
    digest = hashlib.sha256()
    offset = 0
    while offset < size:
        chunk = os.pread(fd, min(CHUNK_SIZE, size - offset), offset)
        if not chunk:
            raise BackupChanged(f"backup file shrank while hashing at offset {offset}")
        digest.update(chunk)
        offset += len(chunk)
    return digest.hexdigest()


def _checksum(cache: ChecksumCache, fd: int, identity: FileIdentity) -> str:
    return cache.digest(identity, lambda: sha256_of_fd(fd, identity.size))


def _open_directory(directory: Path) -> int | None:
    """A descriptor of the backup directory, None when it does not exist or is not a directory."""
    try:
        return os.open(directory, _DIR_FLAGS)
    except (FileNotFoundError, NotADirectoryError):
        return None


def _open_entry(dir_fd: int, name: str) -> tuple[int, os.stat_result] | None:
    """Open name inside the directory without following a symbolic link and return the descriptor with its fstat, or None when it is not a regular file."""
    try:
        fd = os.open(name, _FILE_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in _UNAVAILABLE_ERRNOS:
            return None
        raise
    try:
        info = os.fstat(fd)
    except BaseException:
        os.close(fd)
        raise
    if not stat.S_ISREG(info.st_mode):
        os.close(fd)
        return None
    return fd, info


@dataclass(frozen=True)
class BackupPage:
    """One page of the backup list: total is the number of backups in the directory and files the requested slice of them, newest first."""

    total: int
    files: list[BackupFile]


def list_backups(directory: Path, cache: ChecksumCache) -> list[BackupFile] | None:
    """Every backup directly in directory, newest first (ties by name), each hashed unless the cache holds its identity; None when the directory does not exist (see list_backup_page())."""
    page = list_backup_page(directory, cache)
    return None if page is None else page.files


def _identities(dir_fd: int) -> list[FileIdentity]:
    """The identities of the backups in the open directory, newest first (ties by name), from one fstat each."""
    with os.scandir(dir_fd) as entries:
        names = sorted(entry.name for entry in entries if is_backup_name(entry.name) and entry.is_file(follow_symlinks=False))
    identities: list[FileIdentity] = []
    for name in names:
        opened = _open_entry(dir_fd, name)
        if opened is None:
            continue
        fd, info = opened
        os.close(fd)
        identities.append(FileIdentity.of(name, info))
    identities.sort(key=lambda identity: (-identity.mtime_ns, identity.name))
    return identities


def list_backup_page(directory: Path, cache: ChecksumCache, *, offset: int = 0, limit: int | None = None) -> BackupPage | None:
    """The backups directly in directory, newest first (ties by name), skipping offset of them and returning at most limit (all when None); only the returned files are hashed, each unless the cache holds its identity, and None means the directory does not exist.

    Entries that are not backup names, symbolic links, directories and other non-regular files are left out silently; a file that cannot be read for another reason raises, and a file removed between the listing and its hashing is left out of the page.
    """
    if offset < 0 or (limit is not None and limit < 0):
        raise ValueError("offset and limit must not be negative")
    dir_fd = _open_directory(directory)
    if dir_fd is None:
        return None
    try:
        identities = _identities(dir_fd)
        cache.retain(identity.name for identity in identities)
        selected = identities[offset:] if limit is None else identities[offset : offset + limit]
        files: list[BackupFile] = []
        for listed in selected:
            opened = _open_entry(dir_fd, listed.name)
            if opened is None:
                continue
            fd, info = opened
            try:
                identity = FileIdentity.of(listed.name, info)
                files.append(BackupFile(name=identity.name, created_at=identity.created_at, size=identity.size, sha256=_checksum(cache, fd, identity), mtime_ns=identity.mtime_ns))
            finally:
                os.close(fd)
    finally:
        os.close(dir_fd)
    return BackupPage(total=len(identities), files=files)


def _scan(directory: Path, cache: ChecksumCache, stop: threading.Event | None) -> list[BackupFile] | None:
    """The backups of directory in name order with their digests, None when the directory does not exist; stops early, returning what it has, once stop is set."""
    dir_fd = _open_directory(directory)
    if dir_fd is None:
        return None
    try:
        with os.scandir(dir_fd) as entries:
            names = sorted(entry.name for entry in entries if is_backup_name(entry.name) and entry.is_file(follow_symlinks=False))
        files: list[BackupFile] = []
        for name in names:
            if stop is not None and stop.is_set():
                break
            opened = _open_entry(dir_fd, name)
            if opened is None:
                continue
            fd, info = opened
            try:
                identity = FileIdentity.of(name, info)
                files.append(BackupFile(name=name, created_at=identity.created_at, size=identity.size, sha256=_checksum(cache, fd, identity), mtime_ns=identity.mtime_ns))
            finally:
                os.close(fd)
    finally:
        os.close(dir_fd)
    return files


class ChecksumWarmup:
    """A daemon thread hashing every backup of a directory into a cache, so the first listing after startup does not hash them all; it never blocks the caller, logs its failures, and stop() makes it finish after the file it is hashing."""

    def __init__(self, directory: Path, cache: ChecksumCache) -> None:
        self.directory = directory
        self.cache = cache
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="rapidwire-backup-checksums", daemon=True)

    def start(self) -> None:
        try:
            self._thread.start()
        except RuntimeError:
            log.exception("could not start hashing the backups in the background")

    def stop(self) -> None:
        self._stop.set()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the thread up to timeout seconds and return whether it has finished."""
        if self._thread.ident is not None:
            self._thread.join(timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        try:
            _scan(self.directory, self.cache, self._stop)
        except Exception:
            log.exception("hashing the backups in the background failed; listings hash them on demand", extra={"backup_dir": str(self.directory)})


def start_checksum_warmup(directory: Path, cache: ChecksumCache) -> ChecksumWarmup:
    """Start hashing the backups of directory into cache in a background thread and record it as cache.warmup; a missing directory hashes nothing and logs nothing."""
    warmup = ChecksumWarmup(directory, cache)
    cache.warmup = warmup
    warmup.start()
    return warmup


class OpenedBackup:
    """An open backup file with its verified size and SHA-256; close() releases the descriptor and may be called more than once."""

    def __init__(self, fd: int, identity: FileIdentity, sha256: str) -> None:
        self.fd = fd
        self.name = identity.name
        self.size = identity.size
        self.created_at = identity.created_at
        self.sha256 = sha256
        self._closed = False

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            os.close(self.fd)


def open_backup(directory: Path, name: str, cache: ChecksumCache) -> OpenedBackup | None:
    """Open the backup called name for streaming, None when name is not a backup name or no regular file of that name is directly in directory; the name is checked before the filesystem is touched."""
    if not is_backup_name(name):
        return None
    dir_fd = _open_directory(directory)
    if dir_fd is None:
        return None
    try:
        opened = _open_entry(dir_fd, name)
    finally:
        os.close(dir_fd)
    if opened is None:
        return None
    fd, info = opened
    try:
        identity = FileIdentity.of(name, info)
        return OpenedBackup(fd, identity, _checksum(cache, fd, identity))
    except BaseException:
        os.close(fd)
        raise


class BackupStream(StreamingResponse):
    """A response streaming an OpenedBackup in CHUNK_SIZE pieces read in a worker thread; the descriptor is closed when the response ends, fails or is abandoned before it starts."""

    def __init__(self, backup: OpenedBackup, headers: dict[str, str]) -> None:
        self.backup = backup
        super().__init__(self._chunks(), media_type=media_type_for(backup.name), headers={**headers, "Content-Length": str(backup.size)})

    async def _chunks(self) -> AsyncIterator[bytes]:
        backup = self.backup
        offset = 0
        while offset < backup.size:
            chunk = await anyio.to_thread.run_sync(os.pread, backup.fd, min(CHUNK_SIZE, backup.size - offset), offset)
            if not chunk:
                raise BackupChanged(f"backup file shrank while streaming at offset {offset}")
            yield chunk
            offset += len(chunk)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.backup.close()
