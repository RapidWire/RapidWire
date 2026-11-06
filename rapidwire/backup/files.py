"""File handling shared by the backup writers: partial files created inside the backup directory, publication by rename, and blocking work run in a thread that is always awaited, so cancellation never leaves a thread writing behind the caller's cleanup."""

from __future__ import annotations

import asyncio
import contextlib
import functools
import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Final

from rapidwire.backup.retention import fsync_directory

__all__ = ["FILE_MODE", "create_partial", "discard", "in_thread", "publish"]

FILE_MODE: Final = 0o640
_CREATE_FLAGS: Final = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC


def create_partial(path: Path) -> int:
    """Create path as a new file and return a descriptor open for writing; an existing entry of that name raises FileExistsError."""
    return os.open(path, _CREATE_FLAGS, FILE_MODE)


def publish(partial: Path, final: Path) -> Path:
    """Rename the synced partial file to its final name in the same directory and sync the directory; an existing final name raises FileExistsError instead of being replaced."""
    if os.path.lexists(final):
        raise FileExistsError(f"{final.name} already exists")
    os.rename(partial, final)
    fsync_directory(final.parent)
    return final


def discard(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()


async def in_thread[T](function: Callable[..., T], *args: object, stop: threading.Event | None = None) -> T:
    """Run function(*args) in the default executor; when the caller is cancelled it sets stop, which a long call checks to end early, and waits for the call to finish before the cancellation propagates."""
    future = asyncio.get_running_loop().run_in_executor(None, functools.partial(function, *args))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        if stop is not None:
            stop.set()
        with contextlib.suppress(BaseException):
            await future
        raise
