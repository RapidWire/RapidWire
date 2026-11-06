"""Generations of published backups in the backup directory: listing them, deleting all but the newest few, and removing what an interrupted run left behind; names that do not follow the backup naming are never touched."""

from __future__ import annotations

import logging
import os
import shutil
import stat
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rapidwire.backup.naming import BackupKind, BackupName, is_leftover_name, parse_backup_name

__all__ = ["Generation", "apply_retention", "fsync_directory", "list_generations", "newest_complete_mtime", "remove_leftovers", "retained_stamps"]

log = logging.getLogger("rapidwire.backup")


@dataclass(frozen=True)
class Generation:
    """The published files of one run, keyed by kind, with their modification times in nanoseconds."""

    stamp: str
    files: dict[BackupKind, int]

    @property
    def complete(self) -> bool:
        return set(self.files) == set(BackupKind)

    def names(self) -> list[str]:
        return sorted(kind.file_name(self.stamp) for kind in self.files)


def _regular_backups(directory: Path) -> list[tuple[BackupName, int]]:
    found: list[tuple[BackupName, int]] = []
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return found
    for entry in entries:
        parsed = parse_backup_name(entry.name)
        if parsed is None or not entry.is_file(follow_symlinks=False):
            continue
        try:
            info = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        found.append((parsed, info.st_mtime_ns))
    return found


def list_generations(directory: Path) -> list[Generation]:
    """The generations in directory, oldest first by timestamp; a missing directory has none."""
    files: dict[str, dict[BackupKind, int]] = defaultdict(dict)
    for parsed, mtime_ns in _regular_backups(directory):
        files[parsed.stamp][parsed.kind] = mtime_ns
    return [Generation(stamp=stamp, files=files[stamp]) for stamp in sorted(files)]


def newest_complete_mtime(directory: Path) -> datetime | None:
    """The latest modification time among the files of the newest generation that has both files, None when there is none."""
    complete = [generation for generation in list_generations(directory) if generation.complete]
    if not complete:
        return None
    seconds, nanos = divmod(max(complete[-1].files.values()), 1_000_000_000)
    return datetime.fromtimestamp(seconds, UTC) + timedelta(microseconds=nanos // 1000)


def fsync_directory(directory: Path) -> None:
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def retained_stamps(generations: list[Generation], keep: int, *, current: str | None, prune_complete: bool) -> set[str]:
    """The generations retention keeps: the current run's generation, the newest keep complete generations (all of them when prune_complete is False, the current one counting as one of them), and at most one incomplete generation, which is the current one when it is incomplete and otherwise the newest incomplete one if it is newer than every complete generation."""
    if keep < 1:
        raise ValueError("backup retention must keep at least one generation")
    complete = [generation.stamp for generation in generations if generation.complete]
    incomplete = [generation.stamp for generation in generations if not generation.complete]
    kept: set[str] = set()
    slots = keep
    if current is not None and current in complete:
        kept.add(current)
        slots -= 1
    others = [stamp for stamp in complete if stamp != current]
    if not prune_complete:
        kept.update(others)
    elif slots > 0:
        kept.update(others[-slots:])
    if current is not None and current in incomplete:
        kept.add(current)
        return kept
    newest_complete = max(complete, default=None)
    newer = [stamp for stamp in incomplete if newest_complete is None or stamp > newest_complete]
    if newer:
        kept.add(newer[-1])
    return kept


def apply_retention(directory: Path, keep: int, *, current: str | None = None, prune_complete: bool = True) -> list[str]:
    """Delete every file of the generations retained_stamps() does not keep and return the deleted names in ascending order; afterwards the directory holds at most one incomplete generation, and after a failed run (prune_complete False) no complete generation was deleted."""
    generations = list_generations(directory)
    kept = retained_stamps(generations, keep, current=current, prune_complete=prune_complete)
    deleted: list[str] = []
    for generation in generations:
        if generation.stamp in kept:
            continue
        for name in generation.names():
            try:
                (directory / name).unlink()
            except FileNotFoundError:
                continue
            deleted.append(name)
    if deleted:
        fsync_directory(directory)
        log.info("old backups deleted", extra={"deleted": deleted, "kept_generations": sorted(kept)})
    return sorted(deleted)


def remove_leftovers(directory: Path) -> list[str]:
    """Remove the partial files and staging directories an interrupted run left in directory and return their names; only names of the backup's own partial and staging pattern are touched, and a symbolic link is removed without following it."""
    removed: list[str] = []
    try:
        entries = list(os.scandir(directory))
    except FileNotFoundError:
        return removed
    for entry in entries:
        if not is_leftover_name(entry.name):
            continue
        path = directory / entry.name
        try:
            mode = os.lstat(path).st_mode
            if stat.S_ISDIR(mode):
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            continue
        removed.append(entry.name)
    if removed:
        fsync_directory(directory)
        log.warning("removed files of an interrupted backup run", extra={"removed": sorted(removed)})
    return sorted(removed)
