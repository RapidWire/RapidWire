from __future__ import annotations

import hashlib
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from rapidwire.api.backups import BackupChanged, ChecksumCache, FileIdentity, is_backup_name, list_backups, media_type_for, open_backup, sha256_of_fd, start_checksum_warmup
from rapidwire.core.queries import PRIVATE_EXECUTION_TYPES
from rapidwire.core.types import ExecutionType


@pytest.mark.parametrize("name", ["a", "A0", "rapidwire-dump-20261031T000000Z.sql.gz", "export_1.tar.gz", "x" * 201, "tmp", "a.tmpx", "partial.gz"])
def test_backup_names(name: str) -> None:
    assert is_backup_name(name)


@pytest.mark.parametrize(
    "name",
    ["", ".", "..", ".hidden", "-a", "_a", "x" * 202, "a/b", "../a", "/etc/passwd", "a\\b", "a\x00", "a b", "caf\xe9", "a.tmp", "a.gz.partial", "a\n", "a:b"],
)
def test_rejected_names(name: str) -> None:
    assert not is_backup_name(name)


def test_media_types() -> None:
    assert media_type_for("a.sql.gz") == "application/gzip"
    assert media_type_for("a.tgz") == "application/gzip"
    assert media_type_for("a.jsonl") == "application/octet-stream"


def test_open_backup_checks_the_name_before_the_filesystem(tmp_path: Path) -> None:
    directory = tmp_path / "backups"
    directory.mkdir()
    (tmp_path / "secret").write_bytes(b"secret")
    cache = ChecksumCache()
    for name in ("../secret", "..", str(tmp_path / "secret"), "a\\secret"):
        assert open_backup(directory, name, cache) is None
    assert open_backup(tmp_path / "absent", "a.gz", cache) is None
    assert list_backups(tmp_path / "absent", cache) is None
    assert list_backups(tmp_path / "secret", cache) is None


def test_open_backup_hashes_the_opened_file(tmp_path: Path) -> None:
    (tmp_path / "a.gz").write_bytes(b"content")
    cache = ChecksumCache()
    backup = open_backup(tmp_path, "a.gz", cache)
    assert backup is not None
    try:
        assert (backup.size, backup.sha256) == (7, hashlib.sha256(b"content").hexdigest())
    finally:
        backup.close()
        backup.close()


def test_short_file_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "a"
    path.write_bytes(b"abc")
    fd = os.open(path, os.O_RDONLY)
    try:
        assert sha256_of_fd(fd, 3) == hashlib.sha256(b"abc").hexdigest()
        with pytest.raises(BackupChanged):
            sha256_of_fd(fd, 4)
    finally:
        os.close(fd)


def test_cache_matches_identity_and_retains_listed_names() -> None:
    cache = ChecksumCache()
    identity = FileIdentity(name="a", device=1, inode=2, size=3, mtime_ns=4)
    cache.store(identity, "digest")
    assert cache.lookup(identity) == "digest"
    for changed in (FileIdentity("a", 1, 9, 3, 4), FileIdentity("a", 1, 2, 9, 4), FileIdentity("a", 1, 2, 3, 9), FileIdentity("a", 9, 2, 3, 4)):
        assert cache.lookup(changed) is None
    cache.retain(["b"])
    assert cache.lookup(identity) is None


def test_concurrent_digests_of_one_identity_compute_once() -> None:
    cache = ChecksumCache()
    identity = FileIdentity(name="a", device=1, inode=2, size=3, mtime_ns=4)
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    def compute() -> str:
        calls.append("a")
        started.set()
        assert release.wait(5)
        return "digest"

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(cache.digest, identity, compute)
        assert started.wait(5)
        waiters = [pool.submit(cache.digest, identity, compute) for _ in range(3)]
        release.set()
        assert [future.result(5) for future in (first, *waiters)] == ["digest"] * 4
    assert calls == ["a"]
    other = FileIdentity(name="a", device=1, inode=2, size=3, mtime_ns=5)
    assert cache.digest(other, lambda: "changed") == "changed"


def test_a_failed_digest_is_computed_again_by_a_waiter() -> None:
    cache = ChecksumCache()
    identity = FileIdentity(name="a", device=1, inode=2, size=3, mtime_ns=4)
    started = threading.Event()
    release = threading.Event()

    def failing() -> str:
        started.set()
        assert release.wait(5)
        raise OSError("read error")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache.digest, identity, failing)
        assert started.wait(5)
        waiter = pool.submit(cache.digest, identity, lambda: "digest")
        release.set()
        with pytest.raises(OSError):
            first.result(5)
        assert waiter.result(5) == "digest"
    assert cache.lookup(identity) == "digest"


def test_warmup_hashes_without_dropping_entries_and_ignores_a_missing_directory(tmp_path: Path) -> None:
    (tmp_path / "a.gz").write_bytes(b"content")
    cache = ChecksumCache()
    kept = FileIdentity(name="elsewhere", device=1, inode=2, size=3, mtime_ns=4)
    cache.store(kept, "digest")
    warmup = start_checksum_warmup(tmp_path, cache)
    assert cache.warmup is warmup
    assert warmup.join(5)
    assert cache.lookup(kept) == "digest"
    listed = list_backups(tmp_path, cache)
    assert listed is not None and [file.sha256 for file in listed] == [hashlib.sha256(b"content").hexdigest()]
    assert start_checksum_warmup(tmp_path / "absent", ChecksumCache()).join(5)


def test_backup_changed_is_not_an_os_error() -> None:
    # Starlette turns an OSError raised while streaming into a client disconnect.
    assert not issubclass(BackupChanged, OSError)


def test_backup_executions_are_public() -> None:
    assert ExecutionType.BACKUP_LIST not in PRIVATE_EXECUTION_TYPES
    assert ExecutionType.BACKUP_DOWNLOAD not in PRIVATE_EXECUTION_TYPES
