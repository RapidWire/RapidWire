"""Programmatic Alembic entry points that work from an installed wheel without a repo-root alembic.ini."""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import URL, Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def make_config(url: URL | str | None = None, connection: Connection | None = None) -> Config:
    """Alembic Config pointing at the packaged migrations; env.py uses attributes["connection"] when set, else attributes["url"]."""
    config = Config()
    config.set_main_option("script_location", str(MIGRATIONS_DIR))
    if url is not None:
        config.attributes["url"] = url
    if connection is not None:
        config.attributes["connection"] = connection
    return config


def upgrade(url: URL | str, revision: str = "head") -> None:
    """Apply migrations up to revision against the database at url; must not be called while an event loop is running in this thread."""
    command.upgrade(make_config(url), revision)


def _upgrade_on(connection: Connection, revision: str) -> None:
    command.upgrade(make_config(connection=connection), revision)


async def upgrade_async(target: AsyncEngine | AsyncConnection, revision: str = "head") -> None:
    """Apply migrations from inside a running event loop; an engine gets its own committed transaction, a connection is left for the caller to commit."""
    if isinstance(target, AsyncEngine):
        async with target.begin() as conn:
            await conn.run_sync(_upgrade_on, revision)
    else:
        await target.run_sync(_upgrade_on, revision)


def head_revision() -> str | None:
    """The newest revision in the packaged migration scripts."""
    return ScriptDirectory.from_config(make_config()).get_current_head()
