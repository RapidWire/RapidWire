from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import insert, text, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.accounts import STAKE_CUSTODY_ACCOUNT_ID
from rapidwire.core.db import consistent_snapshot
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import LedgerTx, run_operation
from rapidwire.core.pools import PoolCreated
from rapidwire.core.types import ExecutionType, TransferKind
from rapidwire.db.tables import balance, stake
from tests.integration.factories import add_liquidity, create_currency, create_pool, mint

NOW = datetime(2026, 2, 1, tzinfo=UTC)
ALICE = 111
BOB = 222


async def _corrupt(engine: AsyncEngine, sql: str, **params: Any) -> None:
    """Run a raw statement with foreign key checks off, to fake corruption the schema would otherwise reject."""
    async with engine.begin() as conn:
        await conn.execute(text("SET FOREIGN_KEY_CHECKS = 0"))
        try:
            await conn.execute(text(sql), params)
        finally:
            await conn.execute(text("SET FOREIGN_KEY_CHECKS = 1"))


async def _stake(engine: AsyncEngine, currency_id: int, amount: int) -> None:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([currency_id])
        await tx.lock_balances([(STAKE_CUSTODY_ACCOUNT_ID, currency_id), (ALICE, currency_id)])
        await tx.move(execution_id, currency_id, ALICE, STAKE_CUSTODY_ACCOUNT_ID, amount, TransferKind.PRINCIPAL)
        await tx.conn.execute(insert(stake).values(account_id=ALICE, currency_id=currency_id, amount=amount, carry=0, last_accrued_at=NOW))
        return {}

    await run_operation(engine, type=ExecutionType.STAKE_DEPOSIT, initiator_id=ALICE, input={}, body=body, now=NOW)


async def test_clean_ledger_passes(db: AsyncEngine) -> None:
    empty = await verify_invariants(db)
    assert empty.ok
    assert empty.currencies == []
    await create_currency(db, currency_id=1, symbol="ONE")
    await create_currency(db, currency_id=2, symbol="TWO", deleted_at=NOW)
    await mint(db, 1, ALICE, 500, now=NOW)
    await mint(db, 1, BOB, 250, now=NOW)
    await _stake(db, 1, 100)
    report = await verify_invariants(db)
    assert report.ok, report.violations
    one, two = report.currencies
    assert (one.currency_id, one.symbol, one.supply, one.balance_sum, one.stake_total, one.stake_custody_balance, one.ok, one.deleted) == (1, "ONE", 750, 0, 100, 100, True, False)
    assert (two.currency_id, two.supply, two.deleted) == (2, 0, True)


async def test_corrupted_balance_is_reported(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="ONE")
    await create_currency(db, currency_id=2, symbol="TWO")
    await mint(db, 1, ALICE, 500, now=NOW)
    await mint(db, 2, ALICE, 500, now=NOW)
    async with db.begin() as conn:
        await conn.execute(update(balance).where(balance.c.account_id == ALICE, balance.c.currency_id == 1).values(amount=501))
    report = await verify_invariants(db)
    assert not report.ok
    assert {(v.check, v.currency_id, v.account_id) for v in report.violations} == {("balance_transfer_mismatch", 1, ALICE), ("sum_not_zero", 1, None)}
    assert [c.ok for c in report.currencies] == [False, True]
    assert report.currencies[0].balance_sum == 1


async def test_negative_user_balance_and_flag_mismatch_are_reported(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="ONE")
    await mint(db, 1, ALICE, 5, now=NOW)
    await _corrupt(db, "UPDATE balance SET allow_negative = 1, amount = -1 WHERE account_id = :a", a=ALICE)
    checks = {v.check for v in (await verify_invariants(db)).violations}
    assert {"allow_negative_flag", "negative_balance", "balance_transfer_mismatch", "sum_not_zero"} <= checks


async def test_stake_custody_mismatch_is_reported(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="ONE")
    await mint(db, 1, ALICE, 500, now=NOW)
    await _stake(db, 1, 100)
    async with db.begin() as conn:
        await conn.execute(update(stake).values(amount=101))
    violations = (await verify_invariants(db)).violations
    assert [(v.check, v.currency_id, v.account_id) for v in violations] == [("stake_custody_mismatch", 1, STAKE_CUSTODY_ACCOUNT_ID)]


async def test_wrong_issuance_pointer_is_reported(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="ONE", created_by=ALICE)
    await _corrupt(db, "UPDATE currency SET issuance_account_id = :a WHERE currency_id = 1", a=ALICE)
    violations = (await verify_invariants(db)).violations
    assert [(v.check, v.currency_id, v.account_id) for v in violations] == [("issuance_account", 1, ALICE)]


async def test_positive_issuance_balance_is_reported(db: AsyncEngine) -> None:
    seeded = await create_currency(db, currency_id=1, symbol="ONE")
    await mint(db, 1, ALICE, 5, now=NOW)
    await _corrupt(db, "UPDATE balance SET amount = 3 WHERE account_id = :i", i=seeded.issuance_account_id)
    assert "positive_issuance" in {v.check for v in (await verify_invariants(db)).violations}


async def test_unsuccessful_executions_keep_only_gas_and_no_children(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="ONE")
    execution_id = await mint(db, 1, ALICE, 5, now=NOW)
    async with db.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO execution (type, origin, initiator_id, parent_execution_id, status, input, output, created_at)"
                " VALUES ('contract_call', 'native', :a, :p, 'success', '{}', '{}', UTC_TIMESTAMP(6))"
            ),
            {"a": ALICE, "p": execution_id},
        )
    assert (await verify_invariants(db)).ok
    await _corrupt(db, "UPDATE execution SET status = 'failed' WHERE execution_id = :e", e=execution_id)
    checks = sorted(v.check for v in (await verify_invariants(db)).violations)
    assert checks == ["child_of_unsuccessful_execution", "execution_output", "failed_execution_transfer"]


async def test_executions_must_record_their_outcome(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="ONE")
    execution_id = await mint(db, 1, ALICE, 5, now=NOW)
    assert (await verify_invariants(db)).ok
    for output in ("NULL", "'null'"):
        await _corrupt(db, f"UPDATE execution SET output = {output} WHERE execution_id = :e", e=execution_id)
        assert [(v.check, v.message) for v in (await verify_invariants(db)).violations] == [("execution_output", f"execution {execution_id} with status success has no recorded output")]
    await _corrupt(db, "UPDATE execution SET status = 'reverted', output = '{\"note\": \"x\"}' WHERE execution_id = :e", e=execution_id)
    checks = sorted(v.check for v in (await verify_invariants(db)).violations)
    assert checks == ["execution_output", "failed_execution_transfer"]
    await _corrupt(db, 'UPDATE execution SET output = \'{"error": {"code": "reverted", "message": "m"}}\' WHERE execution_id = :e', e=execution_id)
    assert sorted(v.check for v in (await verify_invariants(db)).violations) == ["failed_execution_transfer"]


async def test_execution_contract_id_must_follow_type_and_input(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="ONE")
    minted = await mint(db, 1, ALICE, 5, now=NOW)
    async with db.begin() as conn:
        tx = LedgerTx(conn, NOW)
        await tx.require_accounts([ALICE, BOB])
        call = await tx.create_execution(type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={"contract_id": BOB, "function": "f"}, output={})
        refused = await tx.create_execution(type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={"contract_id": "x"}, output={})
        managed = await tx.create_execution(type=ExecutionType.CONTRACT_PAUSE, initiator_id=BOB, input={"account_id": BOB}, output={})
    assert (await verify_invariants(db)).ok
    await _corrupt(db, "UPDATE execution SET contract_id = :c WHERE execution_id = :e", c=ALICE, e=call)
    await _corrupt(db, "UPDATE execution SET contract_id = :c WHERE execution_id = :e", c=BOB, e=refused)
    await _corrupt(db, "UPDATE execution SET contract_id = NULL WHERE execution_id = :e", e=managed)
    await _corrupt(db, "UPDATE execution SET contract_id = :c WHERE execution_id = :e", c=BOB, e=minted)
    assert [(v.check, v.message) for v in (await verify_invariants(db)).violations] == [
        ("execution_contract_id", f"execution {minted} (mint) has contract_id {BOB} but its type and input give None"),
        ("execution_contract_id", f"execution {call} (contract_call) has contract_id {ALICE} but its type and input give {BOB}"),
        ("execution_contract_id", f"execution {refused} (contract_call) has contract_id {BOB} but its type and input give None"),
        ("execution_contract_id", f"execution {managed} (contract_pause) has contract_id None but its type and input give {BOB}"),
    ]


async def test_deleted_currency_must_be_empty(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="ONE")
    await mint(db, 1, ALICE, 500, now=NOW)
    await _stake(db, 1, 100)
    execution_id = await mint(db, 1, BOB, 1, now=NOW)
    async with db.begin() as conn:
        await conn.execute(text("INSERT INTO allowance (owner_id, spender_id, currency_id, amount, updated_at) VALUES (:a, :b, 1, 5, UTC_TIMESTAMP(6))"), {"a": ALICE, "b": BOB})
        await conn.execute(
            text(
                "INSERT INTO claim (claimant_id, payer_id, currency_id, amount, status, created_at, updated_at, created_execution_id) VALUES (:a, :b, 1, 5, 'pending', UTC_TIMESTAMP(6), UTC_TIMESTAMP(6), :e)"
            ),
            {"a": ALICE, "b": BOB, "e": execution_id},
        )
        await conn.execute(text("UPDATE currency SET deleted_at = UTC_TIMESTAMP(6) WHERE currency_id = 1"))
    checks = {v.check for v in (await verify_invariants(db)).violations}
    assert checks == {"deleted_currency_balance", "deleted_currency_stake", "deleted_currency_claim", "deleted_currency_allowance"}


async def _pooled(engine: AsyncEngine) -> PoolCreated:
    for currency_id, symbol in ((1, "ONE"), (2, "TWO"), (3, "THR")):
        await create_currency(engine, currency_id=currency_id, symbol=symbol)
    created = await create_pool(engine, user_id=ALICE, currency_x=1, currency_y=2, amount_x=4_000, amount_y=9_000, now=NOW)
    await add_liquidity(engine, user_id=BOB, currency_x=1, currency_y=2, amount_x=400, amount_y=900, now=NOW)
    return created


async def test_pools_are_reported(db: AsyncEngine) -> None:
    created = await _pooled(db)
    report = await verify_invariants(db)
    assert report.ok, report.violations
    (entry,) = report.pools
    assert (entry.pool_id, entry.currency_a, entry.currency_b, entry.fee_bps, entry.active, entry.custody_account_id) == (created.pool_id, 1, 2, 100, True, created.custody_account_id)
    assert (entry.reserve_a, entry.reserve_b, entry.custody_a, entry.custody_b) == (4_400, 9_900, 4_400, 9_900)
    assert (entry.total_shares, entry.locked_shares, entry.provider_shares, entry.ok) == (6_600, 1_000, 5_600, True)


@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("UPDATE pool SET reserve_a = reserve_a + 1", {("pool_custody_reserve", 1)}),
        ("UPDATE pool_share SET shares = shares + 1 WHERE account_id = :b", {("pool_share_total", None)}),
        ("UPDATE pool SET total_shares = total_shares - 1", {("pool_share_total", None)}),
        ("INSERT INTO balance (account_id, currency_id, amount, allow_negative) VALUES (:c, 3, 5, 0)", {("pool_custody_foreign", 3), ("balance_transfer_mismatch", 3), ("sum_not_zero", 3)}),
        ("UPDATE currency SET deleted_at = UTC_TIMESTAMP(6) WHERE currency_id = 2", {("deleted_currency_pool", 2), ("deleted_currency_balance", 2)}),
        (
            "UPDATE pool SET reserve_a = 0, reserve_b = 0, total_shares = 0, locked_shares = 0, dissolved_at = UTC_TIMESTAMP(6), dissolved_execution_id = created_execution_id",
            {("dissolved_pool_custody", 1), ("dissolved_pool_custody", 2), ("dissolved_pool_shares", None), ("pool_share_total", None)},
        ),
    ],
)
async def test_pool_corruption_is_reported(db: AsyncEngine, sql: str, expected: set[tuple[str, int | None]]) -> None:
    created = await _pooled(db)
    await _corrupt(db, sql, b=BOB, c=created.custody_account_id)
    report = await verify_invariants(db)
    found = {(v.check, v.currency_id) for v in report.violations}
    assert found == expected
    assert all(v.pool_id == created.pool_id for v in report.violations if v.check.startswith(("pool_", "dissolved_pool", "deleted_currency_pool")))
    assert [entry.ok for entry in report.pools] == [False]


async def test_pool_without_custody_account_is_reported(db: AsyncEngine) -> None:
    created = await _pooled(db)
    await _corrupt(
        db,
        "INSERT INTO pool (currency_a, currency_b, fee_bps, reserve_a, reserve_b, total_shares, locked_shares, created_by, created_execution_id, created_at)"
        " VALUES (1, 3, 100, 5, 5, 1000, 1000, :a, :e, UTC_TIMESTAMP(6))",
        a=ALICE,
        e=created.execution_id,
    )
    report = await verify_invariants(db)
    assert {(v.check, v.currency_id, v.account_id) for v in report.violations} == {("pool_custody_account", None, None), ("pool_custody_reserve", 1, None), ("pool_custody_reserve", 3, None)}
    assert [entry.ok for entry in report.pools] == [True, False]
    assert report.pools[1].custody_account_id is None


async def test_connection_needs_a_consistent_snapshot(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=1, symbol="ONE")
    await mint(db, 1, ALICE, 5, now=NOW)
    async with db.connect() as conn:
        assert (await verify_invariants(conn)).ok
        assert not conn.in_transaction()
        assert (await conn.execute(text("SELECT @@SESSION.transaction_isolation"))).scalar_one() == "READ-COMMITTED"
        assert conn.in_transaction()
        with pytest.raises(ValueError, match="consistent_snapshot"):
            await verify_invariants(conn)
        await conn.rollback()
        async with consistent_snapshot(conn) as snapshot:
            assert (await snapshot.execute(text("SELECT @@SESSION.transaction_isolation"))).scalar_one() == "REPEATABLE-READ"
            await mint(db, 1, BOB, 7, now=NOW)
            report = await verify_invariants(snapshot)
            assert report.currencies[0].supply == 5
        assert (await verify_invariants(conn)).currencies[0].supply == 12
        await conn.rollback()
