"""Test stand-in for mysqldump and mysql (`python mysql_client_wrapper.py TOOL ARGS...`): runs TOOL from the mysql:8.4 image in a labelled throwaway container on the host network, forwarding the option pipe given as --defaults-file=/dev/fd/N through standard input to /dev/fd/3 ahead of the caller's own input.

MYSQL* variables reach the container unchanged, RAPIDWIRE_TEST_CLIENT_MY_CNF names a file mounted as the client's ~/.my.cnf, and SIGTERM or SIGINT kills the container.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import uuid
from types import FrameType

IMAGE = "mysql:8.4"
OPTION_PREFIX = "--defaults-file=/dev/fd/"
MY_CNF_ENV = "RAPIDWIRE_TEST_CLIENT_MY_CNF"
OWNER_PID_ENV = "RAPIDWIRE_TEST_CLIENT_OWNER_PID"
SESSION_ENV = "RAPIDWIRE_TEST_CLIENT_SESSION"
ARGV_LOG_ENV = "RAPIDWIRE_TEST_CLIENT_ARGV_LOG"
SESSION_LABEL = "rapidwire-test.client-session"

# Reads the option file length and bytes from standard input one byte at a time (bash's read does not buffer on a pipe), so the rest of standard input reaches the client untouched.
SCRIPT = r"""
set -eu
IFS= read -r length
options=""
if [ "$length" -gt 0 ]; then IFS= read -r -d "" -N "$length" options || true; fi
exec 3< <(printf "%s" "$options")
tool="$1"
shift
exec "$tool" --defaults-file=/dev/fd/3 "$@"
"""


def _read_fd(fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(fd, 65536):
        chunks.append(chunk)
    os.close(fd)
    return b"".join(chunks)


def main(argv: list[str]) -> int:
    tool, arguments = argv[0], argv[1:]
    log_path = os.environ.get(ARGV_LOG_ENV)
    if log_path:
        with open(log_path, "a", encoding="utf-8") as log:
            log.write("\0".join([tool, *arguments]) + "\n")
    if not arguments or not arguments[0].startswith(OPTION_PREFIX):
        print(f"wrapper: {tool} must be called with {OPTION_PREFIX}N first", file=sys.stderr)
        return 2
    options = _read_fd(int(arguments[0].removeprefix(OPTION_PREFIX)))
    name = f"rapidwire-test-client-{uuid.uuid4().hex[:12]}"
    command = [
        "docker",
        "run",
        "--rm",
        "-i",
        "--name",
        name,
        "--network",
        "host",
        "--label",
        "rapidwire-test=1",
        "--label",
        f"rapidwire-test.pid={os.environ.get(OWNER_PID_ENV, os.getppid())}",
        "--label",
        f"rapidwire-test.host={socket.gethostname()}",
        "--label",
        f"{SESSION_LABEL}={os.environ.get(SESSION_ENV, 'none')}",
        "--env",
        "LC_ALL=C",
        *(item for name in sorted(os.environ) if name.upper().startswith("MYSQL") for item in ("--env", name)),
        *(("--volume", f"{os.environ[MY_CNF_ENV]}:/root/.my.cnf:ro") if os.environ.get(MY_CNF_ENV) else ()),
        "--entrypoint",
        "bash",
        IMAGE,
        "-c",
        SCRIPT,
        "bash",
        tool,
        *arguments[1:],
    ]
    process = subprocess.Popen(command, stdin=subprocess.PIPE)

    def stop(signum: int, frame: FrameType | None) -> None:
        subprocess.run(["docker", "kill", name], capture_output=True, check=False)
        process.kill()
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    if process.stdin is None:
        return 2
    try:
        process.stdin.write(f"{len(options)}\n".encode("ascii") + options)
        process.stdin.flush()
        while chunk := os.read(0, 1024 * 1024):
            process.stdin.write(chunk)
        process.stdin.close()
    except BrokenPipeError:
        pass
    return process.wait()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
