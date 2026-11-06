from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.cli import main
from rapidwire.config import reset_settings_cache
from rapidwire.db.migrate import head_revision, upgrade_async
from rapidwire.db.tables import metadata
from rapidwire.logging import clear_secrets


@pytest.fixture
def cli_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, migrated_url: URL) -> Iterator[None]:
    """Point RAPIDWIRE_DB_* at the test database, run from an empty directory and restore logging afterwards."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RAPIDWIRE_DB_HOST", str(migrated_url.host))
    monkeypatch.setenv("RAPIDWIRE_DB_PORT", str(migrated_url.port))
    monkeypatch.setenv("RAPIDWIRE_DB_NAME", str(migrated_url.database))
    monkeypatch.setenv("RAPIDWIRE_DB_USER", str(migrated_url.username))
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", str(migrated_url.password))
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    reset_settings_cache()
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.captureWarnings(False)
    clear_secrets()
    reset_settings_cache()


async def _versions(engine: AsyncEngine) -> list[str]:
    async with engine.connect() as conn:
        return list((await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all())


def test_cli_migrate_is_idempotent(cli_env: None, migrated_url: URL, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate"]) == 0
    assert main(["migrate"]) == 0
    out = capsys.readouterr().out
    assert out.count('"msg": "migrations applied"') == 2
    assert str(migrated_url.password) not in out


async def test_upgrade_async_reapplies_from_base(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            for table in metadata.sorted_tables:
                await conn.execute(text(f"DROP TABLE `{table.name}`"))
        finally:
            await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))
        await conn.execute(text("DROP TABLE alembic_version"))
    await upgrade_async(engine, "head")
    assert await _versions(engine) == [head_revision()]
    async with engine.connect() as conn:
        kind = (await conn.execute(text("SELECT kind FROM account WHERE account_id = -1"))).scalar_one()
    assert kind == "stake_custody"


async def test_upgrade_async_on_connection_at_head_is_noop(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await upgrade_async(conn, "head")
    assert await _versions(engine) == [head_revision()]
