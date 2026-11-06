"""Fixtures for backup tests: the MySQL client binaries run from the mysql:8.4 image through mysql_client_wrapper.py, an unprivileged application account with an awkward password, and fresh empty databases to restore into."""

from __future__ import annotations

import os
import secrets
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.backup.restore import RestoreTarget
from rapidwire.config import Settings
from rapidwire.db.engine import create_engine
from tests.integration.backup import mysql_client_wrapper as wrapper

WRAPPER = Path(wrapper.__file__).resolve()
CLIENT_TOOLS = ("mysqldump", "mysql")
# Characters that need quoting or escaping in an option file, a URL or SQL.
APP_PASSWORD_CHARS = "p#a\"s s\\w'o$rd;&=@/%"

type DatabaseFactory = Callable[[], Awaitable[RestoreTarget]]


def _docker(*args: str, timeout: float = 1800) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout, check=False)


@pytest.fixture(scope="session")
def mysql_clients(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """A directory put first on PATH holding mysqldump and mysql shims that run the MySQL 8.4 clients in containers; the containers left at the end of the session are removed."""
    if _docker("image", "inspect", wrapper.IMAGE).returncode != 0:
        pulled = _docker("pull", wrapper.IMAGE)
        if pulled.returncode != 0:
            pytest.fail(f"docker pull {wrapper.IMAGE} failed: {pulled.stderr.strip()}")
    directory = tmp_path_factory.mktemp("mysql-clients")
    for tool in CLIENT_TOOLS:
        shim = directory / tool
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{WRAPPER}" {tool} "$@"\n', encoding="utf-8")
        shim.chmod(0o755)
    session = uuid.uuid4().hex
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("PATH", f"{directory}{os.pathsep}{os.environ.get('PATH', '')}")
        patch.setenv(wrapper.OWNER_PID_ENV, str(os.getpid()))
        patch.setenv(wrapper.SESSION_ENV, session)
        try:
            yield directory
        finally:
            listing = _docker("ps", "-aq", "--filter", f"label={wrapper.SESSION_LABEL}={session}", timeout=120)
            for container in listing.stdout.split():
                _docker("rm", "--force", container, timeout=120)


async def _execute(url: URL, *statements: tuple[str, dict[str, str]]) -> None:
    engine = create_engine(url)
    try:
        async with engine.begin() as conn:
            for statement, params in statements:
                await conn.execute(text(statement), params)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def app_account(migrated_url: URL) -> AsyncIterator[tuple[str, str]]:
    """A MySQL account with all privileges on the test database only, like the compose application user, and a password full of special characters."""
    user = f"rw_app_{secrets.token_hex(4)}"
    password = APP_PASSWORD_CHARS + secrets.token_hex(4)
    await _execute(
        migrated_url,
        ("CREATE USER :user@'%' IDENTIFIED BY :password", {"user": user, "password": password}),
        (f"GRANT ALL PRIVILEGES ON `{migrated_url.database}`.* TO :user@'%'", {"user": user}),
    )
    try:
        yield user, password
    finally:
        await _execute(migrated_url, ("DROP USER IF EXISTS :user@'%'", {"user": user}))


@pytest.fixture
def backup_settings(migrated_url: URL, app_account: tuple[str, str], tmp_path: Path) -> Settings:
    """Settings that connect as the application account and write backups under tmp_path."""
    user, password = app_account
    return Settings(
        _env_file=None,
        db_host=migrated_url.host,
        db_port=migrated_url.port,
        db_name=migrated_url.database,
        db_user=user,
        db_password=password,
        backup_dir=tmp_path / "backups",
        backup_retention=3,
    )


@pytest_asyncio.fixture(loop_scope="session")
async def app_engine(backup_settings: Settings) -> AsyncIterator[AsyncEngine]:
    engine = create_engine(backup_settings.database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest_asyncio.fixture(loop_scope="session")
async def fresh_database(migrated_url: URL) -> AsyncIterator[DatabaseFactory]:
    """Create empty databases on the test server on demand, as restore targets reached with the test server's own account, and drop them afterwards."""
    created: list[str] = []

    async def create() -> RestoreTarget:
        name = f"rw_restore_{secrets.token_hex(4)}_test"
        await _execute(migrated_url, (f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin", {}))
        created.append(name)
        return RestoreTarget(host=str(migrated_url.host), port=int(migrated_url.port or 3306), user=str(migrated_url.username), password=str(migrated_url.password or ""), database=name)

    try:
        yield create
    finally:
        for name in created:
            await _execute(migrated_url, (f"DROP DATABASE IF EXISTS `{name}`", {}))
