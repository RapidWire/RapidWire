"""Shared fixtures; the MySQL fixtures start a throwaway docker container only when an integration test requests them."""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import signal
import socket
import subprocess
import time
import uuid
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType
from typing import Any

import asyncmy
import pytest
import pytest_asyncio
from sqlalchemy import URL, make_url, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.db.engine import create_engine
from rapidwire.db.migrate import upgrade
from rapidwire.db.tables import metadata

INTEGRATION_DIR = Path(__file__).parent / "integration"
DB_FIXTURES = frozenset({"mysql_url", "migrated_url", "engine", "db"})
MYSQL_IMAGE = "mysql:8.4"
MYSQL_DATABASE = "rapidwire_test"
CONTAINER_LABEL = "rapidwire-test=1"
PID_LABEL = "rapidwire-test.pid"
HOST_LABEL = "rapidwire-test.host"
MYSQL_START_TIMEOUT_SECONDS = 180.0
DOCKER_TIMEOUT_SECONDS = 120.0
DOCKER_PULL_TIMEOUT_SECONDS = float(os.environ.get("RAPIDWIRE_TEST_DOCKER_PULL_TIMEOUT", "1800"))

# Server defaults that differ from what the application needs, so every local integration run proves the engine sets its own session state.
HOSTILE_SERVER_ARGS = (
    "--sql-mode=ANSI_QUOTES,PIPES_AS_CONCAT",
    "--default-time-zone=+09:00",
    "--transaction-isolation=REPEATABLE-READ",
    "--character-set-server=utf8mb4",
    "--collation-server=utf8mb4_0900_ai_ci",
)

_started_containers: set[str] = set()
_previous_sigterm: Any = None


def _sigterm_to_keyboard_interrupt(signum: int, frame: FrameType | None) -> None:
    raise KeyboardInterrupt(f"received signal {signum}")


def pytest_configure(config: pytest.Config) -> None:
    # SIGTERM becomes KeyboardInterrupt so pytest unwinds and fixture finalizers remove the test container.
    global _previous_sigterm
    external = os.environ.get("RAPIDWIRE_TEST_DB_URL")
    if external:
        external_test_url(external)
    _previous_sigterm = signal.signal(signal.SIGTERM, _sigterm_to_keyboard_interrupt)


def pytest_unconfigure(config: pytest.Config) -> None:
    for name in list(_started_containers):
        _remove_container(name)
    if _previous_sigterm is not None:
        signal.signal(signal.SIGTERM, _previous_sigterm)


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    for item in items:
        if INTEGRATION_DIR in item.path.parents:
            item.add_marker(pytest.mark.integration)
            continue
        used = DB_FIXTURES.intersection(getattr(item, "fixturenames", ()))
        if used:
            raise pytest.UsageError(f"{item.nodeid} uses database fixtures {sorted(used)} but is not under tests/integration")


def _docker(*args: str, check: bool = True, timeout: float = DOCKER_TIMEOUT_SECONDS) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(["docker", *args], check=False, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        if check:
            pytest.fail(f"docker {args[0]} did not finish within {timeout:.0f}s")
        return subprocess.CompletedProcess(["docker", *args], returncode=-1, stdout="", stderr="timeout")
    if check and result.returncode != 0:
        pytest.fail(f"docker {args[0]} failed with exit code {result.returncode}: {result.stderr.strip()}")
    return result


def _remove_container(name: str) -> None:
    _docker("rm", "--force", "--volumes", name, check=False)
    _started_containers.discard(name)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def sweep_orphan_containers() -> list[str]:
    """Remove labelled containers whose owning pytest process on this host has exited, leaving other hosts' and live sessions' containers alone; returns the removed names."""
    listing = _docker("ps", "-a", "--filter", f"label={CONTAINER_LABEL}", "--format", f'{{{{.Names}}}}\t{{{{.Label "{PID_LABEL}"}}}}\t{{{{.Label "{HOST_LABEL}"}}}}', check=False)
    removed: list[str] = []
    for line in listing.stdout.splitlines():
        name, _, rest = line.partition("\t")
        pid_text, _, host = rest.partition("\t")
        if host != socket.gethostname() or not pid_text.isdigit():
            continue
        if not _pid_alive(int(pid_text)):
            _remove_container(name)
            removed.append(name)
    return removed


def _docker_logs_tail(name: str, lines: int = 40) -> str:
    result = _docker("logs", "--tail", str(lines), name, check=False)
    return (result.stdout + result.stderr).strip()


async def _connect(url: URL) -> Any:
    return await asyncmy.connect(host=url.host, port=url.port, user=url.username, password=url.password, db=url.database, connect_timeout=5)


async def _try_select_one(url: URL) -> None:
    conn = await _connect(url)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT 1")
            await cursor.fetchone()
    finally:
        conn.close()


async def _drop_all_tables(url: URL) -> None:
    conn = await _connect(url)
    try:
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT TABLE_NAME, TABLE_TYPE FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()")
            rows = await cursor.fetchall()
            await cursor.execute("SET FOREIGN_KEY_CHECKS = 0")
            for name, kind in rows:
                await cursor.execute(f"DROP {'VIEW' if kind == 'VIEW' else 'TABLE'} IF EXISTS `{name}`")
            await cursor.execute("SET FOREIGN_KEY_CHECKS = 1")
        await conn.commit()
    finally:
        conn.close()


def _wait_for_mysql(name: str, url: URL) -> None:
    # The image's entrypoint runs a temporary server without networking during init and then restarts, so only a real query proves readiness.
    deadline = time.monotonic() + MYSQL_START_TIMEOUT_SECONDS
    last_error: BaseException | None = None
    while time.monotonic() < deadline:
        state = _docker("inspect", "-f", "{{.State.Running}}", name, check=False).stdout.strip()
        if state != "true":
            pytest.fail(f"MySQL test container {name} stopped during startup.\n--- docker logs (tail) ---\n{_docker_logs_tail(name)}")
        try:
            asyncio.run(_try_select_one(url))
            return
        except Exception as exc:
            last_error = exc
            time.sleep(1.0)
    pytest.fail(f"MySQL test container {name} was not ready within {MYSQL_START_TIMEOUT_SECONDS:.0f}s (last error: {last_error!r}).\n--- docker logs (tail) ---\n{_docker_logs_tail(name)}")


def external_test_url(raw: str) -> URL:
    """Parse RAPIDWIRE_TEST_DB_URL, refusing databases whose name does not end in "_test" because the fixture drops every table in it."""
    url = make_url(raw)
    if not (url.database or "").endswith("_test"):
        raise pytest.UsageError(f"RAPIDWIRE_TEST_DB_URL must name a disposable database ending in '_test', got {url.database!r}")
    return url


@contextmanager
def mysql_container() -> Iterator[URL]:
    """Run a throwaway mysql:8.4 container with deliberately unusual server defaults and remove it on exit, including when startup fails."""
    sweep_orphan_containers()
    if _docker("image", "inspect", MYSQL_IMAGE, check=False).returncode != 0:
        _docker("pull", MYSQL_IMAGE, timeout=DOCKER_PULL_TIMEOUT_SECONDS)

    name = f"rapidwire-test-mysql-{uuid.uuid4().hex[:12]}"
    root_password = secrets.token_hex(16)
    _started_containers.add(name)
    try:
        _docker(
            "run",
            "--detach",
            "--name",
            name,
            "--label",
            CONTAINER_LABEL,
            "--label",
            f"{PID_LABEL}={os.getpid()}",
            "--label",
            f"{HOST_LABEL}={socket.gethostname()}",
            "--publish",
            "127.0.0.1::3306",
            "--tmpfs",
            "/var/lib/mysql:rw",
            "--env",
            f"MYSQL_ROOT_PASSWORD={root_password}",
            "--env",
            f"MYSQL_DATABASE={MYSQL_DATABASE}",
            MYSQL_IMAGE,
            *HOSTILE_SERVER_ARGS,
        )
        mapping = _docker("port", name, "3306/tcp").stdout.splitlines()[0].strip()
        port = int(mapping.rsplit(":", 1)[1])
        url = URL.create("mysql+asyncmy", username="root", password=root_password, host="127.0.0.1", port=port, database=MYSQL_DATABASE, query={"charset": "utf8mb4"})
        _wait_for_mysql(name, url)
        yield url
    finally:
        _remove_container(name)


@pytest.fixture(scope="session")
def mysql_url() -> Iterator[URL]:
    """URL of a disposable MySQL database with no tables: RAPIDWIRE_TEST_DB_URL (name must end in "_test") with every table dropped first, or a throwaway container removed at session end."""
    external = os.environ.get("RAPIDWIRE_TEST_DB_URL")
    if external:
        url = external_test_url(external)
        asyncio.run(_drop_all_tables(url))
        yield url
        return

    if shutil.which("docker") is None:
        pytest.fail("Integration tests need either RAPIDWIRE_TEST_DB_URL or a docker CLI to start MySQL.")
    with mysql_container() as url:
        yield url


@pytest.fixture(scope="session")
def migrated_url(mysql_url: URL) -> URL:
    """mysql_url after applying every migration up to head."""
    upgrade(mysql_url, "head")
    return mysql_url


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def engine(migrated_url: URL) -> AsyncIterator[AsyncEngine]:
    """AsyncEngine connected to the migrated test database, disposed at session end."""
    eng = create_engine(migrated_url)
    try:
        yield eng
    finally:
        await eng.dispose()


async def truncate_all(engine: AsyncEngine) -> None:
    """Empty every table registered on the schema metadata."""
    async with engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            for table in reversed(metadata.sorted_tables):
                await conn.execute(text(f"TRUNCATE TABLE `{table.name}`"))
        finally:
            await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


@pytest_asyncio.fixture(loop_scope="session")
async def db(engine: AsyncEngine) -> AsyncEngine:
    """The session engine after emptying every table registered on the schema metadata, for tests that need a clean ledger."""
    await truncate_all(engine)
    return engine
