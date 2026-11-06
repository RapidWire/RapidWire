from __future__ import annotations

import hashlib
import warnings
from datetime import UTC, datetime
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.limits import STORAGE_KEY_MAX_LENGTH, STORAGE_VALUE_MAX_BYTES
from rapidwire.core.accounts import ensure_user_accounts
from rapidwire.db.migrate import head_revision, make_config
from rapidwire.db.tables import (
    CONTRACT_MAX_SIZE,
    CONTRACT_STORAGE_KEY_MAX,
    CONTRACT_STORAGE_MAX_BYTES,
    CONTRACT_STORAGE_MAX_KEYS,
    CONTRACT_STORAGE_VALUE_MAX,
    ContractStatus,
    contract,
    contract_storage,
    contract_version,
    execution,
    metadata,
)
from tests.integration.factories import create_currency

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ALICE = 111
CODE = b"RWC1 test bytes"
HIRAGANA_A = chr(0x3042)


async def _seed(engine: AsyncEngine) -> int:
    async with engine.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=NOW)
        result = await conn.execute(insert(execution).values(type="contract_update", origin="native", initiator_id=ALICE, status="success", input={}, created_at=NOW))
        assert result.inserted_primary_key is not None
        return int(result.inserted_primary_key[0])


def _version_row(execution_id: int, **overrides: Any) -> dict[str, Any]:
    code = overrides.pop("code", CODE)
    row = {"account_id": ALICE, "version": 1, "code": code, "sha256": hashlib.sha256(code).digest(), "size": len(code), "created_execution_id": execution_id, "created_at": NOW}
    return {**row, **overrides}


def _contract_row(execution_id: int, **overrides: Any) -> dict[str, Any]:
    row = {
        "account_id": ALICE,
        "status": ContractStatus.ACTIVE.value,
        "current_version": 1,
        "created_execution_id": execution_id,
        "updated_execution_id": execution_id,
        "created_at": NOW,
        "updated_at": NOW,
    }
    return {**row, **overrides}


async def _insert(engine: AsyncEngine, table: Any, values: dict[str, Any]) -> None:
    async with engine.begin() as conn:
        await conn.execute(insert(table).values(**values))


async def _expect_rejected(engine: AsyncEngine, table: Any, values: dict[str, Any], match: str) -> None:
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match=match):
            await conn.execute(insert(table).values(**values))
        await conn.rollback()


async def _expect_update_rejected(engine: AsyncEngine, values: dict[str, Any], match: str) -> None:
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match=match):
            await conn.execute(update(contract).where(contract.c.account_id == ALICE).values(**values))
        await conn.rollback()


async def _seed_contract(engine: AsyncEngine) -> int:
    execution_id = await _seed(engine)
    await _insert(engine, contract_version, _version_row(execution_id))
    await _insert(engine, contract, _contract_row(execution_id))
    return execution_id


def test_storage_limits() -> None:
    assert CONTRACT_MAX_SIZE == 8000
    assert CONTRACT_STORAGE_MAX_KEYS == 2000
    assert CONTRACT_STORAGE_MAX_BYTES == 256 * 1024
    assert CONTRACT_STORAGE_KEY_MAX == STORAGE_KEY_MAX_LENGTH == 64
    assert CONTRACT_STORAGE_VALUE_MAX == STORAGE_VALUE_MAX_BYTES == 4096
    assert [status.value for status in ContractStatus] == ["active", "paused", "deleted"]


def test_tables_sort_without_cycle_warnings() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        names = [table.name for table in metadata.sorted_tables]
    assert names.index("account") < names.index("contract_version") < names.index("contract") < names.index("contract_storage")
    assert names.index("execution") < names.index("contract_version")


async def test_version_checks(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    await _expect_rejected(db, contract_version, _version_row(execution_id, version=0), "ck_contract_version_version_positive")
    await _expect_rejected(db, contract_version, _version_row(execution_id, size=len(CODE) + 1), "ck_contract_version_size")
    await _expect_rejected(db, contract_version, _version_row(execution_id, code=b"", sha256=hashlib.sha256(b"").digest(), size=0), "ck_contract_version_size")
    await _expect_rejected(db, contract_version, _version_row(execution_id, sha256=hashlib.sha256(b"other").digest()), "ck_contract_version_sha256")
    await _expect_rejected(db, contract_version, _version_row(execution_id, created_execution_id=execution_id + 100), "fk_contract_version_created_execution_id_execution")
    await _expect_rejected(db, contract_version, _version_row(execution_id, account_id=333), "fk_contract_version_account_id_account")
    issuance = (await create_currency(db, currency_id=1, symbol="AAA")).issuance_account_id
    await _expect_rejected(db, contract_version, _version_row(execution_id, account_id=issuance), "ck_contract_version_user_account")
    await _insert(db, contract_version, _version_row(execution_id))
    with pytest.raises(IntegrityError, match="Duplicate entry"):
        await _insert(db, contract_version, _version_row(execution_id))
    async with db.connect() as conn:
        row = (await conn.execute(select(contract_version.c.code, contract_version.c.sha256))).one()
    assert (bytes(row.code), bytes(row.sha256)) == (CODE, hashlib.sha256(CODE).digest())


async def test_binary_is_stored_byte_exact_up_to_the_size_limit(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    code = (bytes(range(256)) * 32)[:CONTRACT_MAX_SIZE]
    assert len(code) == CONTRACT_MAX_SIZE
    await _insert(db, contract_version, _version_row(execution_id, code=code))
    async with db.connect() as conn:
        stored = (await conn.execute(select(contract_version.c.code))).scalar_one()
    assert bytes(stored) == code
    await _expect_rejected(db, contract_version, _version_row(execution_id, version=2, code=code + b"\x00"), "ck_contract_version_size")


async def test_contract_checks(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    await _expect_rejected(db, contract, _contract_row(execution_id), "fk_contract_current_version")
    await _insert(db, contract_version, _version_row(execution_id))
    await _expect_rejected(db, contract, _contract_row(execution_id, current_version=2), "fk_contract_current_version")
    await _expect_rejected(db, contract, _contract_row(execution_id, status="running"), "ck_contract_status")
    await _expect_rejected(db, contract, _contract_row(execution_id, status="deleted"), "ck_contract_deletion_pair")
    await _expect_rejected(db, contract, _contract_row(execution_id, deleted_at=NOW), "ck_contract_deletion_pair")
    await _expect_rejected(db, contract, _contract_row(execution_id, storage_keys=CONTRACT_STORAGE_MAX_KEYS + 1), "ck_contract_storage_keys_range")
    await _expect_rejected(db, contract, _contract_row(execution_id, storage_keys=-1), "ck_contract_storage_keys_range")
    await _expect_rejected(db, contract, _contract_row(execution_id, storage_bytes=CONTRACT_STORAGE_MAX_BYTES + 1), "ck_contract_storage_bytes_range")
    await _expect_rejected(db, contract, _contract_row(execution_id, storage_bytes=-1), "ck_contract_storage_bytes_range")
    await _expect_rejected(db, contract, _contract_row(execution_id, updated_execution_id=execution_id + 100), "fk_contract_updated_execution_id_execution")
    deleted = {"status": "deleted", "deleted_at": NOW}
    await _expect_rejected(db, contract, _contract_row(execution_id, storage_keys=1, storage_bytes=2, **deleted), "ck_contract_deleted_state")
    await _expect_rejected(db, contract, _contract_row(execution_id, locked_until=NOW, **deleted), "ck_contract_deleted_state")
    await _insert(db, contract, _contract_row(execution_id, storage_keys=CONTRACT_STORAGE_MAX_KEYS, storage_bytes=CONTRACT_STORAGE_MAX_BYTES, locked_until=NOW))
    await _expect_update_rejected(db, deleted, "ck_contract_deleted_state")
    async with db.begin() as conn:
        await conn.execute(update(contract).values(storage_keys=0, storage_bytes=0, locked_until=None, **deleted))
    async with db.connect() as conn:
        row = (await conn.execute(select(contract.c.status, contract.c.current_version))).one()
    assert tuple(row) == ("deleted", 1)


async def test_contract_needs_a_user_account(db: AsyncEngine) -> None:
    execution_id = await _seed(db)
    issuance = (await create_currency(db, currency_id=1, symbol="AAA")).issuance_account_id
    async with db.connect() as conn:
        with pytest.raises(DBAPIError, match=r"ck_contract_user_account|fk_contract_current_version"):
            await conn.execute(insert(contract).values(**_contract_row(execution_id, account_id=issuance)))
        await conn.rollback()


def _storage_row(**overrides: Any) -> dict[str, Any]:
    value = overrides.pop("encoded_value", b"\x03")
    row = {"account_id": ALICE, "storage_key": "k", "encoded_value": value, "value_size": len(value), "updated_at": NOW}
    return {**row, **overrides}


async def test_storage_checks(db: AsyncEngine) -> None:
    await _expect_rejected(db, contract_storage, _storage_row(), "fk_contract_storage_account_id_contract")
    await _seed_contract(db)
    await _expect_rejected(db, contract_storage, _storage_row(storage_key=""), "ck_contract_storage_key_not_empty")
    await _expect_rejected(db, contract_storage, _storage_row(storage_key="k" * (CONTRACT_STORAGE_KEY_MAX + 1)), "Data too long")
    await _expect_rejected(db, contract_storage, _storage_row(value_size=2), "ck_contract_storage_value_size")
    await _expect_rejected(db, contract_storage, _storage_row(encoded_value=b""), "ck_contract_storage_value_size")
    await _expect_rejected(db, contract_storage, _storage_row(encoded_value=b"x" * (CONTRACT_STORAGE_VALUE_MAX + 1)), "ck_contract_storage_value_size")
    await _insert(db, contract_storage, _storage_row(storage_key=HIRAGANA_A * CONTRACT_STORAGE_KEY_MAX, encoded_value=b"x" * CONTRACT_STORAGE_VALUE_MAX))
    await _insert(db, contract_storage, _storage_row(storage_key="K"))
    await _insert(db, contract_storage, _storage_row(storage_key="k"))
    with pytest.raises(IntegrityError, match="Duplicate entry"):
        await _insert(db, contract_storage, _storage_row(storage_key="k"))
    async with db.connect() as conn:
        keys = (await conn.execute(select(contract_storage.c.storage_key).order_by(contract_storage.c.storage_key))).scalars().all()
    assert keys == ["K", "k", HIRAGANA_A * CONTRACT_STORAGE_KEY_MAX]


@pytest.mark.parametrize("run", ["first", "second"])
async def test_each_test_starts_with_empty_contract_tables(db: AsyncEngine, run: str) -> None:
    async with db.connect() as conn:
        for name in ("contract", "contract_version", "contract_storage"):
            assert (await conn.execute(text(f"SELECT COUNT(*) FROM {name}"))).scalar_one() == 0
    await _seed_contract(db)
    await _insert(db, contract_storage, _storage_row())


def _downgrade_on(connection: Connection, revision: str) -> None:
    command.downgrade(make_config(connection=connection), revision)


def _upgrade_on(connection: Connection, revision: str) -> None:
    command.upgrade(make_config(connection=connection), revision)


async def _table_names(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set((await conn.execute(text("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"))).scalars().all())


async def test_contract_migration_downgrades_and_upgrades(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await conn.run_sync(_downgrade_on, "0003")
    names = await _table_names(db)
    assert not names & {"contract", "contract_version", "contract_storage"}
    assert {"pool", "pool_share", "discord_permission"} <= names
    async with db.connect() as conn:
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == "0003"
    async with db.begin() as conn:
        await conn.run_sync(_upgrade_on, "head")
    assert {"contract", "contract_version", "contract_storage"} <= await _table_names(db)
    async with db.connect() as conn:
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == head_revision() == "0007"
    await _seed_contract(db)
