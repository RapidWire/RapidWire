from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Connection, insert, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError, StatementError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.accounts import ensure_user_accounts
from rapidwire.core.ledger import LedgerTx, run_operation
from rapidwire.core.types import ExecutionType
from rapidwire.db.tables import account, allowance, allowance_history, balance, claim, currency, execution, metadata, stake, transfer
from tests.integration.factories import create_currency, insert_transfer, mint

NOW = datetime(2026, 1, 1, tzinfo=UTC)
ALICE = 111
BOB = 222


def _diff(sync_conn: Connection) -> list[Any]:
    context = MigrationContext.configure(sync_conn, opts={"compare_type": True})
    return list(compare_metadata(context, metadata))


async def test_migrated_schema_matches_metadata(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        diff = await conn.run_sync(_diff)
    assert diff == []


async def test_tables_are_innodb_with_binary_collation(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        rows = (await conn.execute(text("SELECT TABLE_NAME, ENGINE, TABLE_COLLATION FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME <> 'alembic_version'"))).all()
    assert {row[0] for row in rows} == {table.name for table in metadata.sorted_tables}
    assert {(row[1], row[2]) for row in rows} == {("InnoDB", "utf8mb4_0900_bin")}


async def _execution(engine: AsyncEngine, **values: Any) -> int:
    async with engine.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=NOW)
        row = {"type": "transfer", "initiator_id": ALICE, "status": "success", "input": {}, "created_at": NOW, **values}
        result = await conn.execute(insert(execution).values(**row))
        assert result.inserted_primary_key is not None
        return int(result.inserted_primary_key[0])


async def _expect_rejected(engine: AsyncEngine, sql: str, params: dict[str, Any], match: str) -> None:
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match=match):
            await conn.execute(text(sql), params)
        await conn.rollback()


async def test_negative_balance_rejected_except_when_allowed(db: AsyncEngine) -> None:
    seeded = await create_currency(db, currency_id=1, symbol="AAA")
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=NOW)
    await _expect_rejected(db, "INSERT INTO balance (account_id, currency_id, amount, allow_negative) VALUES (:a, 1, -1, 0)", {"a": ALICE}, "ck_balance_non_negative")
    async with db.begin() as conn:
        await conn.execute(insert(balance).values(account_id=seeded.issuance_account_id, currency_id=1, amount=-5, allow_negative=True))


async def test_only_the_own_issuance_account_may_go_negative(db: AsyncEngine) -> None:
    first = await create_currency(db, currency_id=1, symbol="AAA")
    await create_currency(db, currency_id=2, symbol="BBB")
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=NOW)
    sql = "INSERT INTO balance (account_id, currency_id, amount, allow_negative) VALUES (:a, :c, -5, 1)"
    # Currency 1's issuance account cannot be negative in currency 2, and a user can never carry the flag.
    await _expect_rejected(db, sql, {"a": first.issuance_account_id, "c": 2}, "fk_balance_negative_issuance")
    await _expect_rejected(db, sql, {"a": ALICE, "c": 1}, "fk_balance_negative_issuance")
    async with db.begin() as conn:
        await conn.execute(insert(balance).values(account_id=first.issuance_account_id, currency_id=2, amount=0, allow_negative=False))
    await _expect_rejected(db, "UPDATE balance SET amount = -1 WHERE account_id = :a AND currency_id = 2", {"a": first.issuance_account_id}, "ck_balance_non_negative")


async def test_currency_must_point_at_its_own_issuance_account(db: AsyncEngine) -> None:
    first = await create_currency(db, currency_id=1, symbol="AAA")
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=NOW)
    sql = "INSERT INTO currency (currency_id, name, symbol, issuance_account_id, created_by, created_at) VALUES (2, 'Other', 'BBB', :i, :a, UTC_TIMESTAMP(6))"
    await _expect_rejected(db, sql, {"i": first.issuance_account_id, "a": ALICE}, "fk_currency_issuance_account")
    await _expect_rejected(db, sql, {"i": ALICE, "a": ALICE}, "fk_currency_issuance_account")


@pytest.mark.parametrize("name", ["A", "Coin", "a1", "My_Coin", "a_b_c_d_e_f", "x" * 24])
async def test_valid_currency_names(db: AsyncEngine, name: str) -> None:
    await create_currency(db, currency_id=1, symbol="AAA", name=name)


@pytest.mark.parametrize("name", ["1Coin", "_coin", "coin_", "co__in", "a_b_c_d_e_f_g", "co-in", "coin!", "x" * 25, ""])
async def test_invalid_currency_names(db: AsyncEngine, name: str) -> None:
    with pytest.raises(DBAPIError, match=r"ck_currency_name_format|Data too long"):
        await create_currency(db, currency_id=1, symbol="AAA", name=name)


@pytest.mark.parametrize("symbol", ["A", "RAPID", "CN-B-AA", "A-B", "ABCDEFGH"])
async def test_valid_currency_symbols(db: AsyncEngine, symbol: str) -> None:
    await create_currency(db, currency_id=1, symbol=symbol)


@pytest.mark.parametrize("symbol", ["rapid", "Rapid", "A--B", "-AB", "AB-", "A-B-C-D", "A1", "ABCDEFGHI", ""])
async def test_invalid_currency_symbols(db: AsyncEngine, symbol: str) -> None:
    with pytest.raises(DBAPIError, match=r"ck_currency_symbol_format|Data too long"):
        await create_currency(db, currency_id=1, symbol=symbol)


async def test_regexp_is_case_sensitive_with_binary_collation(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT REGEXP_LIKE('rapid', '^[A-Z]+$')"))).scalar_one() == 0
        assert (await conn.execute(text("SELECT REGEXP_LIKE('RAPID', '^[A-Z]+$')"))).scalar_one() == 1


async def test_notification_permission_needs_two_users(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=NOW)
    sql = "INSERT INTO notification_permission (user_id, allowed_user_id, created_at) VALUES (:a, :a, UTC_TIMESTAMP(6))"
    await _expect_rejected(db, sql, {"a": ALICE}, "ck_notification_permission_distinct_users")


async def test_loaded_claim_and_allowance_rows_reference_their_executions(db: AsyncEngine) -> None:
    payer, claimant, owner, spender = 960142246257229894, 959782437615976449, 966290198705111051, 1310270419248611390
    await create_currency(db, currency_id=1362038520973103255, symbol="P", name="photon")
    await create_currency(db, currency_id=1138734577360326707, symbol="D", name="Dorago")
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [payer, claimant, owner, spender], now=NOW)
        tx = LedgerTx(conn, NOW)
        claim_execution = await tx.create_execution(type=ExecutionType.CLAIM_CREATE, initiator_id=claimant, input={"payer_id": payer, "amount": 10_000_000})
        await conn.execute(
            insert(claim).values(
                claimant_id=claimant, payer_id=payer, currency_id=1362038520973103255, amount=10_000_000, status="pending", created_at=NOW, updated_at=NOW, created_execution_id=claim_execution
            )
        )
        approve_execution = await tx.create_execution(type=ExecutionType.APPROVE, initiator_id=owner, input={"spender_id": spender, "amount": 5})
        await conn.execute(insert(allowance).values(owner_id=owner, spender_id=spender, currency_id=1138734577360326707, amount=5, updated_at=NOW))
        await conn.execute(
            insert(allowance_history).values(execution_id=approve_execution, owner_id=owner, spender_id=spender, currency_id=1138734577360326707, old_amount=0, new_amount=5, created_at=NOW)
        )
    async with db.connect() as conn:
        rows = (await conn.execute(select(execution.c.type).order_by(execution.c.execution_id))).scalars().all()
    assert rows == ["claim_create", "approve"]


async def test_transfer_checks(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="AAA")
    execution_id = await _execution(db)
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [BOB], now=NOW)
    sql = "INSERT INTO transfer (execution_id, currency_id, from_account, to_account, amount, kind, created_at) VALUES (:e, 1, :f, :t, :amount, 'principal', UTC_TIMESTAMP(6))"
    await _expect_rejected(db, sql, {"e": execution_id, "f": ALICE, "t": BOB, "amount": 0}, "ck_transfer_amount_positive")
    await _expect_rejected(db, sql, {"e": execution_id, "f": ALICE, "t": BOB, "amount": -3}, "ck_transfer_amount_positive")
    await _expect_rejected(db, sql, {"e": execution_id, "f": ALICE, "t": ALICE, "amount": 3}, "ck_transfer_distinct_accounts")


async def test_allowance_owner_must_differ_from_spender(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="AAA")
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=NOW)
    sql = "INSERT INTO allowance (owner_id, spender_id, currency_id, amount, updated_at) VALUES (:o, :s, 1, 5, UTC_TIMESTAMP(6))"
    await _expect_rejected(db, sql, {"o": ALICE, "s": ALICE}, "ck_allowance_distinct_parties")


async def test_claim_description_is_limited_to_50_characters(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="AAA")
    execution_id = await _execution(db)
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [BOB], now=NOW)
    values = {"claimant_id": ALICE, "payer_id": BOB, "currency_id": 1, "amount": 5, "status": "pending", "created_at": NOW, "updated_at": NOW, "created_execution_id": execution_id}
    async with db.begin() as conn:
        await conn.execute(insert(claim).values(description="x" * 50, **values))
    async with db.connect() as conn:
        with pytest.raises(DBAPIError, match="Data too long"):
            await conn.execute(insert(claim).values(description="x" * 51, **values))
        await conn.rollback()
    async with db.connect() as conn:
        with pytest.raises(DBAPIError, match="ck_claim_resolution"):
            await conn.execute(insert(claim).values(description=None, **{**values, "status": "paid"}))
        await conn.rollback()


async def test_rate_is_limited_to_one_million_ppm(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="AAA", hourly_rate_ppm=1_000_000)
    with pytest.raises(DBAPIError, match="ck_currency_hourly_rate_range"):
        await create_currency(db, currency_id=2, symbol="BBB", hourly_rate_ppm=1_000_001)
    async with db.connect() as conn:
        with pytest.raises(DBAPIError, match="ck_currency_pending_rate_range"):
            await conn.execute(update(currency).where(currency.c.currency_id == 1).values(pending_rate_ppm=1_000_001, rate_change_requested_at=NOW))
        await conn.rollback()


async def test_active_symbol_is_unique_and_reusable_after_deletion(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="AAA")
    with pytest.raises(IntegrityError, match="uq_currency_active_symbol"):
        await create_currency(db, currency_id=2, symbol="AAA")
    async with db.begin() as conn:
        await conn.execute(update(currency).where(currency.c.currency_id == 1).values(deleted_at=NOW))
    await create_currency(db, currency_id=3, symbol="AAA")
    async with db.connect() as conn:
        rows = (await conn.execute(select(currency.c.currency_id, currency.c.active_symbol).order_by(currency.c.currency_id))).all()
    assert [tuple(row) for row in rows] == [(1, None), (3, "AAA")]
    async with db.connect() as conn:
        with pytest.raises(IntegrityError, match="uq_currency_active_symbol"):
            await conn.execute(update(currency).where(currency.c.currency_id == 1).values(deleted_at=None))
        await conn.rollback()


async def test_idempotency_key_is_unique_per_initiator(db: AsyncEngine) -> None:
    await _execution(db, idempotency_key="k1")
    with pytest.raises(IntegrityError, match="uq_execution_initiator_id_idempotency_key"):
        await _execution(db, idempotency_key="k1")
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [BOB], now=NOW)
        await conn.execute(insert(execution).values(type="transfer", initiator_id=BOB, status="success", input={}, created_at=NOW, idempotency_key="k1"))
    await _execution(db)
    await _execution(db)


async def test_execution_status_is_constrained(db: AsyncEngine) -> None:
    with pytest.raises(DBAPIError, match="ck_execution_status"):
        await _execution(db, status="pending")
    await _execution(db, status="reverted")


async def test_execution_type_is_an_open_set_without_provenance_columns(db: AsyncEngine) -> None:
    await _execution(db, type="unlisted_operation")
    async with db.connect() as conn:
        columns = {row[0] for row in (await conn.execute(text("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'execution'"))).all()}
        stored = (await conn.execute(select(execution.c.type))).scalar_one()
    assert stored == "unlisted_operation"
    assert columns == {
        "execution_id",
        "type",
        "initiator_id",
        "parent_execution_id",
        "status",
        "input",
        "output",
        "idempotency_key",
        "contract_function",
        "created_at",
        "contract_id",
    }


async def test_account_kind_shape(db: AsyncEngine) -> None:
    sql = "INSERT INTO account (account_id, kind, currency_id, pool_id, created_at) VALUES (:id, :kind, :c, :p, UTC_TIMESTAMP(6))"
    await _expect_rejected(db, sql, {"id": -5, "kind": "user", "c": None, "p": None}, "ck_account_kind_shape")
    await _expect_rejected(db, sql, {"id": 5, "kind": "issuance", "c": 1, "p": None}, "ck_account_kind_shape")
    await _expect_rejected(db, sql, {"id": -5, "kind": "issuance", "c": None, "p": None}, "ck_account_kind_shape")
    await _expect_rejected(db, sql, {"id": -5, "kind": "pool_custody", "c": None, "p": None}, "ck_account_kind_shape")
    await _expect_rejected(db, sql, {"id": -5, "kind": "stake_custody", "c": None, "p": None}, "ck_account_kind_shape")
    await _expect_rejected(db, sql, {"id": -1, "kind": "issuance", "c": 1, "p": None}, "ck_account_kind_shape")
    await _expect_rejected(db, sql, {"id": -5, "kind": "contract", "c": None, "p": None}, "ck_account_kind")


async def test_stake_carry_range(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="AAA")
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=NOW)
        await conn.execute(insert(stake).values(account_id=ALICE, currency_id=1, amount=0, carry=10**18 - 1, last_accrued_at=NOW))
    async with db.connect() as conn:
        with pytest.raises(DBAPIError, match="ck_stake_carry_range"):
            await conn.execute(update(stake).values(carry=10**18))
        await conn.rollback()
    async with db.connect() as conn:
        with pytest.raises(DBAPIError, match="ck_stake_amount_non_negative"):
            await conn.execute(update(stake).values(amount=-1))
        await conn.rollback()


async def test_utc_datetime_round_trip(db: AsyncEngine) -> None:
    tokyo = timezone(timedelta(hours=9))
    local = datetime(2026, 5, 1, 9, 30, 15, 123456, tzinfo=tokyo)
    async with db.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=local)
    async with db.connect() as conn:
        stored = (await conn.execute(select(account.c.created_at).where(account.c.account_id == ALICE))).scalar_one()
        raw = (await conn.execute(text("SELECT DATE_FORMAT(created_at, '%Y-%m-%d %H:%i:%s.%f') FROM account WHERE account_id = :a"), {"a": ALICE})).scalar_one()
    assert stored == local
    assert stored.tzinfo is UTC
    assert raw == "2026-05-01 00:30:15.123456"
    async with db.connect() as conn:
        with pytest.raises(StatementError, match="naive datetime"):
            await conn.execute(update(account).values(created_at=datetime(2026, 1, 1)))
        await conn.rollback()


async def test_explicit_ids_do_not_break_later_allocation(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="AAA")
    await mint(db, 1, ALICE, 1_000, now=NOW)
    big_execution, big_transfer = 9_000_000_000, 7_000_000_000
    earlier = NOW - timedelta(days=400)
    inserted = await insert_transfer(
        db, execution_id=big_execution, transfer_id=big_transfer, type=ExecutionType.TRANSFER, initiator=ALICE, currency_id=1, source=ALICE, destination=BOB, amount=10, at=earlier
    )
    assert inserted == big_execution
    # A smaller explicit ID afterwards must not pull the counter back.
    await _execution(db, execution_id=5_000)
    execution_id = await mint(db, 1, BOB, 5, now=NOW)
    async with db.connect() as conn:
        transfer_id = (await conn.execute(select(transfer.c.transfer_id).where(transfer.c.execution_id == execution_id))).scalar_one()
        explicit_created = (await conn.execute(select(transfer.c.created_at).where(transfer.c.transfer_id == big_transfer))).scalar_one()
    assert execution_id > big_execution
    assert transfer_id > big_transfer
    assert explicit_created == earlier


async def test_operation_after_explicit_id_rows_continues_numbering(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="AAA")

    async def noop(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return {}

    async with db.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=NOW)
        await conn.execute(
            text("INSERT INTO execution (execution_id, type, initiator_id, status, input, created_at) VALUES (123456789, 'mint', :a, 'success', '{}', UTC_TIMESTAMP(6))"),
            {"a": ALICE},
        )
    result = await run_operation(db, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={}, body=noop, now=NOW)
    assert result.execution_id == 123456790
