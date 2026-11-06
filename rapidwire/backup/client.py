"""Child processes of the MySQL command line clients, which read their connection settings, password included, from an inherited pipe given as their only option file (--defaults-file), so the password is never in an argument list, an environment or a file on disk.

Their environment has no MySQL variables except MYSQL_TEST_LOGIN_FILE naming a missing file, so no other option file, login path file or variable such as MYSQL_HOST can redirect them.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import signal
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import IO, Any, Final

from rapidwire.config import DB_NAME_PATTERN
from rapidwire.logging import redact

__all__ = [
    "MYSQL",
    "MYSQLDUMP",
    "NO_LOGIN_FILE",
    "OPTION_FILE_ARGUMENT",
    "ClientError",
    "ConnectionOptions",
    "client_environment",
    "client_process",
    "collect_stderr",
    "option_value",
    "stderr_errors",
    "terminate",
    "validate_database_name",
]

MYSQLDUMP: Final = "mysqldump"
MYSQL: Final = "mysql"
OPTION_FILE_ARGUMENT: Final = "--defaults-file"
# The clients read ~/.mylogin.cnf even with --defaults-file unless this variable names another file; this path does not exist.
NO_LOGIN_FILE: Final = "/nonexistent/rapidwire-no-mysql-login-file"
# Seconds a child gets to exit after SIGTERM before it is killed.
TERMINATE_GRACE_SECONDS: Final = 5.0
# Bytes of a child's standard error that are kept for the error message and the log.
STDERR_LIMIT: Final = 64 * 1024
# Largest option file written into a pipe before the child starts; a pipe buffers at least this much, so the write never blocks.
OPTION_FILE_MAX: Final = 4096
_DATABASE_NAME_RE: Final = re.compile(DB_NAME_PATTERN)
_CLIENT_ENV_PREFIXES: Final = ("MYSQL", "LIBMYSQL")
_WARNING_LINE: Final = re.compile(r"\[Warning\]|^Warning:|^\s*$")
_OPTION_ESCAPES: Final = {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r", "\t": "\\t", "\b": "\\b"}


class ClientError(Exception):
    """A MySQL client could not be started, failed, or reported an error."""


def validate_database_name(name: str) -> str:
    """name when it matches DB_NAME_PATTERN (unquoted identifier characters only, so the clients never read it as an option), else ClientError."""
    if _DATABASE_NAME_RE.fullmatch(name) is None:
        raise ClientError(f"database name {name!r} must match {DB_NAME_PATTERN}")
    return name


@dataclass(frozen=True)
class ConnectionOptions:
    host: str
    port: int
    user: str
    password: str = field(repr=False)
    database: str

    def __post_init__(self) -> None:
        validate_database_name(self.database)

    def option_file(self) -> bytes:
        """The [client] option group with host, port, user and password, connecting over TCP."""
        lines = [
            "[client]",
            f"host={option_value(self.host)}",
            f"port={self.port}",
            f"user={option_value(self.user)}",
            f"password={option_value(self.password)}",
            "protocol=TCP",
            "",
        ]
        return "\n".join(lines).encode("utf-8")


def option_value(value: str) -> str:
    """value as a double-quoted MySQL option file value, with the escapes the option file parser understands."""
    if "\0" in value:
        raise ClientError("connection settings must not contain NUL characters")
    return '"' + "".join(_OPTION_ESCAPES.get(char, char) for char in value) + '"'


def client_environment(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a client child: the given one (default the process's) without any MYSQL* or LIBMYSQL* variable, plus MYSQL_TEST_LOGIN_FILE set to NO_LOGIN_FILE."""
    source = os.environ if environ is None else environ
    clean = {name: value for name, value in source.items() if not name.upper().startswith(_CLIENT_ENV_PREFIXES)}
    clean["MYSQL_TEST_LOGIN_FILE"] = NO_LOGIN_FILE
    return clean


def stderr_errors(text: str) -> list[str]:
    """The lines of a client's standard error that are not warnings."""
    return [line for line in text.splitlines() if not _WARNING_LINE.search(line)]


async def collect_stderr(stream: asyncio.StreamReader) -> str:
    """Read the stream to its end, keeping its first STDERR_LIMIT bytes, decoded and redacted."""
    kept = bytearray()
    while chunk := await stream.read(65536):
        if len(kept) < STDERR_LIMIT:
            kept.extend(chunk[: STDERR_LIMIT - len(kept)])
    return redact(kept.decode("utf-8", errors="replace"))


async def terminate(process: asyncio.subprocess.Process) -> None:
    """Stop the process group of a child that is still running, SIGTERM first and SIGKILL after TERMINATE_GRACE_SECONDS, and reap it."""
    if process.returncode is not None:
        return
    with contextlib.suppress(ProcessLookupError):
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), TERMINATE_GRACE_SECONDS)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await process.wait()


def _option_pipe(content: bytes) -> int:
    """A pipe whose read end holds content followed by end of file; returns the read end."""
    read_fd, write_fd = os.pipe()
    try:
        if len(content) > OPTION_FILE_MAX:
            raise ClientError("connection settings are too long for an option pipe")
        view = memoryview(content)
        while view:
            view = view[os.write(write_fd, view) :]
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)
    return read_fd


@asynccontextmanager
async def client_process(
    program: str,
    arguments: Sequence[str],
    options: ConnectionOptions,
    *,
    stdin: int | IO[Any] | None,
    stdout: int | IO[Any] | None,
) -> AsyncIterator[asyncio.subprocess.Process]:
    """Start program in its own process group and client_environment() with OPTION_FILE_ARGUMENT naming an inherited pipe that holds options.option_file(), followed by arguments; standard error is a pipe, and on exit a child that is still running is stopped."""
    executable = shutil.which(program)
    if executable is None:
        raise ClientError(f"{program} was not found on PATH")
    read_fd = _option_pipe(options.option_file())
    try:
        process = await asyncio.create_subprocess_exec(
            executable,
            f"{OPTION_FILE_ARGUMENT}=/dev/fd/{read_fd}",
            *arguments,
            stdin=stdin,
            stdout=stdout,
            stderr=asyncio.subprocess.PIPE,
            pass_fds=(read_fd,),
            start_new_session=True,
            env=client_environment(),
        )
    finally:
        os.close(read_fd)
    try:
        yield process
    finally:
        await terminate(process)
