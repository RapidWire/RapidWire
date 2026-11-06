from __future__ import annotations

import uuid

import pytest
from sqlalchemy import URL, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.db.engine import create_engine
from rapidwire.db.migrate import head_revision


async def test_server_is_mysql_8_4(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        version = (await conn.execute(text("SELECT VERSION()"))).scalar_one()
    assert str(version).startswith("8.4")


async def test_alembic_version_is_head(engine: AsyncEngine) -> None:
    head = head_revision()
    assert head == "0007"
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    assert rows == [head]


async def test_database_default_collation_is_binary(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        charset, collation = (await conn.execute(text("SELECT DEFAULT_CHARACTER_SET_NAME, DEFAULT_COLLATION_NAME FROM information_schema.SCHEMATA WHERE SCHEMA_NAME = DATABASE()"))).one()
    assert (charset, collation) == ("utf8mb4", "utf8mb4_0900_bin")


async def test_session_variables_do_not_depend_on_server(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        isolation, time_zone, sql_mode, charset, collation = (
            await conn.execute(text("SELECT @@SESSION.transaction_isolation, @@SESSION.time_zone, @@SESSION.sql_mode, @@SESSION.character_set_connection, @@SESSION.collation_connection"))
        ).one()
    assert isolation == "READ-COMMITTED"
    assert time_zone == "+00:00"
    assert set(sql_mode.split(",")) == {"STRICT_ALL_TABLES", "NO_ZERO_DATE", "NO_ZERO_IN_DATE", "ERROR_FOR_DIVISION_BY_ZERO", "NO_ENGINE_SUBSTITUTION", "ONLY_FULL_GROUP_BY"}
    assert charset == "utf8mb4"
    assert collation == "utf8mb4_0900_bin"


async def test_dialect_detects_session_quoting(engine: AsyncEngine) -> None:
    # The docker-managed server defaults to ANSI_QUOTES, so this proves the session sql_mode is in place before SQLAlchemy inspects it.
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT 1"))).scalar_one() == 1
    assert getattr(engine.dialect, "_server_ansiquotes", None) is False
    assert engine.dialect.identifier_preparer.quote("alembic_version") == "alembic_version"
    assert engine.dialect.identifier_preparer.quote_identifier("x") == "`x`"


async def test_string_comparison_is_case_sensitive(engine: AsyncEngine) -> None:
    table = f"collation_probe_{uuid.uuid4().hex[:8]}"
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT 'a' = 'A'"))).scalar_one() == 0
        await conn.execute(text(f"CREATE TABLE `{table}` (name VARCHAR(16) NOT NULL)"))
        try:
            await conn.execute(text(f"INSERT INTO `{table}` (name) VALUES ('abc')"))
            assert (await conn.execute(text(f"SELECT COUNT(*) FROM `{table}` WHERE name = 'ABC'"))).scalar_one() == 0
            assert (await conn.execute(text(f"SELECT COUNT(*) FROM `{table}` WHERE name = 'abc'"))).scalar_one() == 1
            column_collation = (await conn.execute(text("SELECT COLLATION_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = :t"), {"t": table})).scalar_one()
            assert column_collation == "utf8mb4_0900_bin"
        finally:
            await conn.execute(text(f"DROP TABLE `{table}`"))


async def test_now_is_utc(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        drift = (await conn.execute(text("SELECT ABS(TIMESTAMPDIFF(SECOND, UTC_TIMESTAMP(), NOW()))"))).scalar_one()
    assert drift <= 1


async def test_decimal_overflow_is_an_error(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        await conn.execute(text("CREATE TEMPORARY TABLE overflow_probe (v DECIMAL(38,0) NOT NULL)"))
        try:
            await conn.execute(text("INSERT INTO overflow_probe (v) VALUES (99999999999999999999999999999999999999)"))
            with pytest.raises(DBAPIError, match="Out of range"):
                await conn.execute(text("INSERT INTO overflow_probe (v) VALUES (100000000000000000000000000000000000000)"))
            await conn.rollback()
        finally:
            await conn.execute(text("DROP TEMPORARY TABLE IF EXISTS overflow_probe"))


async def test_isolation_level_override(migrated_url: URL) -> None:
    override = create_engine(migrated_url, isolation_level="SERIALIZABLE")
    try:
        async with override.connect() as conn:
            assert (await conn.execute(text("SELECT @@SESSION.transaction_isolation"))).scalar_one() == "SERIALIZABLE"
            assert (await conn.execute(text("SELECT @@SESSION.time_zone"))).scalar_one() == "+00:00"
    finally:
        await override.dispose()


TRUNCATION_PROBE_ACCOUNT = 424242


@pytest.mark.parametrize("run", ["first", "second"])
async def test_each_test_starts_with_empty_tables(db: AsyncEngine, run: str) -> None:
    # Both runs start by asserting emptiness and then leave a row behind, so whichever runs second fails unless the fixture truncated in between.
    async with db.connect() as conn:
        assert (await conn.execute(text("SELECT COUNT(*) FROM account"))).scalar_one() == 0
    async with db.begin() as conn:
        await conn.execute(text("INSERT INTO account (account_id, kind, created_at) VALUES (:id, 'user', UTC_TIMESTAMP(6))"), {"id": TRUNCATION_PROBE_ACCOUNT})


async def test_begin_commits(db: AsyncEngine) -> None:
    table = f"scaffold_probe_{uuid.uuid4().hex[:8]}"
    async with db.begin() as conn:
        await conn.execute(text(f"CREATE TABLE `{table}` (id INT PRIMARY KEY)"))
    try:
        async with db.begin() as conn:
            await conn.execute(text(f"INSERT INTO `{table}` (id) VALUES (1)"))
        async with db.connect() as other:
            count = (await other.execute(text(f"SELECT COUNT(*) FROM `{table}`"))).scalar_one()
    finally:
        async with db.begin() as conn:
            await conn.execute(text(f"DROP TABLE `{table}`"))
    assert count == 1
