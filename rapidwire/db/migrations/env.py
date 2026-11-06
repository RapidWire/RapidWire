"""Alembic environment that runs migrations on a caller-supplied connection or through the async engine."""

from __future__ import annotations

import asyncio

from alembic import context
from sqlalchemy import URL, Connection, pool

import rapidwire.db.tables
from rapidwire.db.engine import create_engine

config = context.config
target_metadata = rapidwire.db.tables.metadata


def _database_url() -> URL | str:
    url: URL | str | None = config.attributes.get("url") or config.get_main_option("sqlalchemy.url")
    if url:
        return url
    from rapidwire.config import get_settings

    return get_settings().database_url


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
            await connection.commit()
    finally:
        await engine.dispose()


def _run_offline() -> None:
    url = _database_url()
    rendered = url.render_as_string(hide_password=False) if isinstance(url, URL) else url
    context.configure(url=rendered, target_metadata=target_metadata, literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    _run_offline()
else:
    supplied: Connection | None = config.attributes.get("connection")
    if supplied is not None:
        _run(supplied)
    else:
        asyncio.run(_run_async())
