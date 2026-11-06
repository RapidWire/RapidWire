from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import URL, func, insert, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import accounts as accounts_module
from rapidwire.core.accounts import STAKE_CUSTODY_ACCOUNT_ID, create_pool_custody_account, ensure_user_accounts, get_or_create_issuance_account, pool_custody_account_id
from rapidwire.core.errors import (
    AccountNotFound,
    AmountOverflow,
    CurrencyNotFound,
    DomainError,
    InsufficientBalance,
    InvalidAmount,
    InvalidArgument,
    RevertError,
)
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import LedgerTx, LedgerUsageError, mysql_error_code, retry_delay, run_child, run_operation
from rapidwire.core.types import AccountKind, ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.engine import create_engine
from rapidwire.db.tables import MINIMUM_LIQUIDITY, account, balance, currency, execution, system_account_allocation, transfer
from rapidwire.db.tables import pool as pool_table
from rapidwire.db.types import AMOUNT_MAX
from tests.integration.factories import balance_of, create_currency, mint, supply_of

NOW = datetime(2026, 3, 1, 12, 0, tzinfo=UTC)
CUR = 77
ALICE = 111
BOB = 222
CAROL = 333


async def _transfer(engine: AsyncEngine, sender: int, recipient: int, amount: int, **kw: Any) -> int:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(sender, CUR), (recipient, CUR)])
        return {"transfer_id": await tx.move(execution_id, CUR, sender, recipient, amount, TransferKind.PRINCIPAL)}

    result = await run_operation(engine, type=ExecutionType.TRANSFER, initiator_id=sender, input={"to": recipient, "amount": amount}, body=body, now=NOW, **kw)
    return result.execution_id


async def _executions(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(execution).order_by(execution.c.execution_id))).mappings().all()
    return [dict(row) for row in rows]


async def _transfers(engine: AsyncEngine, execution_id: int | None = None) -> list[dict[str, Any]]:
    stmt = select(transfer).order_by(transfer.c.transfer_id)
    if execution_id is not None:
        stmt = stmt.where(transfer.c.execution_id == execution_id)
    async with engine.connect() as conn:
        rows = (await conn.execute(stmt)).mappings().all()
    return [dict(row) for row in rows]


async def _issuance(engine: AsyncEngine, currency_id: int = CUR) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(currency.c.issuance_account_id).where(currency.c.currency_id == currency_id))).scalar_one())


@pytest.fixture
async def seeded(db: AsyncEngine) -> AsyncEngine:
    await create_currency(db, currency_id=CUR, symbol="LED")
    await mint(db, CUR, ALICE, 1_000, now=NOW)
    return db


async def test_mint_and_transfer_record_executions_and_transfers(seeded: AsyncEngine) -> None:
    execution_id = await _transfer(seeded, ALICE, BOB, 300)
    assert await balance_of(seeded, ALICE, CUR) == 700
    assert await balance_of(seeded, BOB, CUR) == 300
    assert await supply_of(seeded, CUR) == 1_000
    row = (await _executions(seeded))[-1]
    assert row["execution_id"] == execution_id
    assert row["status"] == "success"
    assert row["origin"] == "native"
    assert row["label_basis"] is None
    assert row["input"] == {"to": str(BOB), "amount": "300"}
    assert row["created_at"] == NOW
    moves = await _transfers(seeded, execution_id)
    assert [(m["from_account"], m["to_account"], m["amount"], m["kind"]) for m in moves] == [(ALICE, BOB, 300, "principal")]
    assert row["output"] == {"transfer_id": str(moves[0]["transfer_id"])}


async def test_issuance_balance_goes_negative(seeded: AsyncEngine) -> None:
    issuance = await _issuance(seeded)
    async with seeded.connect() as conn:
        flags = dict((row.account_id, bool(row.allow_negative)) for row in await conn.execute(select(balance.c.account_id, balance.c.allow_negative)))
    assert flags == {issuance: True, ALICE: False}
    assert await balance_of(seeded, issuance, CUR) == -1_000


async def test_insufficient_balance_records_failed_execution_without_transfers(seeded: AsyncEngine) -> None:
    before = len(await _transfers(seeded))
    with pytest.raises(InsufficientBalance) as caught:
        await _transfer(seeded, BOB, ALICE, 1)
    failed_id = caught.value.execution_id
    assert failed_id is not None
    row = (await _executions(seeded))[-1]
    assert row["execution_id"] == failed_id
    assert row["status"] == "failed"
    assert row["output"] == {"error": {"code": "insufficient_balance", "message": "The balance is insufficient."}}
    assert len(await _transfers(seeded)) == before
    assert await _transfers(seeded, failed_id) == []
    assert await balance_of(seeded, ALICE, CUR) == 1_000


async def test_failure_after_partial_moves_rolls_back_the_moves(seeded: AsyncEngine) -> None:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR), (CAROL, CUR)])
        await tx.move(execution_id, CUR, ALICE, BOB, 600, TransferKind.PRINCIPAL)
        await tx.move(execution_id, CUR, ALICE, CAROL, 600, TransferKind.PRINCIPAL)
        return {}

    with pytest.raises(InsufficientBalance):
        await run_operation(seeded, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={}, body=body, now=NOW)
    assert await balance_of(seeded, ALICE, CUR) == 1_000
    assert await balance_of(seeded, BOB, CUR) == 0
    assert await balance_of(seeded, CAROL, CUR) == 0


async def test_on_failure_records_fee_locked_in_prepare_while_body_transfers_vanish(seeded: AsyncEngine) -> None:
    issuance = await _issuance(seeded)

    async def prepare(tx: LedgerTx, execution_id: int) -> None:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(issuance, CUR), (ALICE, CUR)])

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR)])
        await tx.move(execution_id, CUR, ALICE, BOB, 400, TransferKind.PRINCIPAL)
        raise RevertError("contract reverted")

    async def charge_gas(tx: LedgerTx, execution_id: int, error: DomainError) -> None:
        assert isinstance(error, RevertError)
        assert error.execution_id == execution_id
        assert tx.balance(ALICE, CUR) == 1_000
        await tx.move(execution_id, CUR, ALICE, issuance, 7, TransferKind.GAS)

    with pytest.raises(RevertError) as caught:
        await run_operation(seeded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, prepare=prepare, on_failure=charge_gas, now=NOW, contract_function="buy")
    execution_id = caught.value.execution_id
    assert execution_id is not None
    moves = await _transfers(seeded, execution_id)
    assert [(m["from_account"], m["to_account"], m["amount"], m["kind"]) for m in moves] == [(ALICE, issuance, 7, "gas")]
    row = (await _executions(seeded))[-1]
    assert row["status"] == "reverted"
    assert row["contract_function"] == "buy"
    assert row["output"] == {"error": {"code": "reverted", "message": "contract reverted"}}
    assert await balance_of(seeded, ALICE, CUR) == 993
    assert await balance_of(seeded, BOB, CUR) == 0
    assert await supply_of(seeded, CUR) == 993


async def test_domain_error_in_prepare_or_on_failure_rolls_everything_back(seeded: AsyncEngine) -> None:
    async def failing_prepare(tx: LedgerTx, execution_id: int) -> None:
        await tx.lock_currencies([CUR + 1])

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        raise InvalidArgument("body failed")

    async def lock_payer(tx: LedgerTx, execution_id: int) -> None:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR)])

    async def failing_hook(tx: LedgerTx, execution_id: int, error: DomainError) -> None:
        await tx.move(execution_id, CUR, ALICE, BOB, 5, TransferKind.GAS)
        raise error

    before = await _executions(seeded)
    with pytest.raises(CurrencyNotFound) as from_prepare:
        await run_operation(seeded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, prepare=failing_prepare, now=NOW)
    assert from_prepare.value.execution_id is None
    with pytest.raises(InvalidArgument, match="body failed") as from_hook:
        await run_operation(seeded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, prepare=lock_payer, on_failure=failing_hook, now=NOW)
    assert from_hook.value.execution_id is None
    assert await _executions(seeded) == before
    assert await balance_of(seeded, BOB, CUR) == 0


async def test_on_failure_cannot_lock_or_use_rows_locked_by_the_body(seeded: AsyncEngine) -> None:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR)])
        raise InvalidArgument("body failed")

    async def uses_body_rows(tx: LedgerTx, execution_id: int, error: DomainError) -> None:
        await tx.move(execution_id, CUR, ALICE, BOB, 5, TransferKind.GAS)

    async def locks_currency(tx: LedgerTx, execution_id: int, error: DomainError) -> None:
        await tx.lock_currencies([CUR])

    async def locks_balance(tx: LedgerTx, execution_id: int, error: DomainError) -> None:
        await tx.lock_balances([(ALICE, CUR)])

    before = await _executions(seeded)
    with pytest.raises(LedgerUsageError, match="not locked"):
        await run_operation(seeded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, on_failure=uses_body_rows, now=NOW)
    for hook in (locks_currency, locks_balance):
        with pytest.raises(LedgerUsageError, match="on_failure"):
            await run_operation(seeded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, on_failure=hook, now=NOW)
    assert await _executions(seeded) == before
    assert await balance_of(seeded, BOB, CUR) == 0


async def test_child_whose_prepare_or_on_failure_fails_leaves_nothing(seeded: AsyncEngine) -> None:
    issuance = await _issuance(seeded)
    caught: list[DomainError] = []

    async def failing_prepare(tx: LedgerTx, execution_id: int) -> None:
        raise InsufficientBalance("no gas")

    async def lock_gas(tx: LedgerTx, execution_id: int) -> None:
        await tx.lock_balances([(issuance, CUR), (ALICE, CUR)])

    async def failing_body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        raise InvalidArgument("child body failed")

    async def gas_then_fail(tx: LedgerTx, execution_id: int, error: DomainError) -> None:
        await tx.move(execution_id, CUR, ALICE, issuance, 7, TransferKind.GAS)
        raise InvalidArgument("hook failed")

    async def noop(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return {}

    async def parent(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        for prepare, body, hook in ((failing_prepare, noop, None), (lock_gas, failing_body, gas_then_fail)):
            try:
                await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=body, prepare=prepare, on_failure=hook)
            except DomainError as error:
                caught.append(error)
        assert not tx.is_locked(ALICE, CUR)
        return {}

    result = await run_operation(seeded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=parent, now=NOW)
    assert [(type(error).__name__, error.message, error.execution_id) for error in caught] == [("InsufficientBalance", "no gas", None), ("InvalidArgument", "hook failed", None)]
    assert [row for row in await _executions(seeded) if row["parent_execution_id"] == result.execution_id] == []
    assert await balance_of(seeded, ALICE, CUR) == 1_000
    assert await supply_of(seeded, CUR) == 1_000
    assert (await verify_invariants(seeded)).ok


async def test_overflow_is_rejected_before_writing(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR, symbol="LED")
    await mint(db, CUR, ALICE, AMOUNT_MAX, now=NOW)
    with pytest.raises(AmountOverflow) as caught:
        await mint(db, CUR, ALICE, 1, now=NOW)
    assert await balance_of(db, ALICE, CUR) == AMOUNT_MAX
    assert await _transfers(db, caught.value.execution_id) == []
    # The issuance account would fall below AMOUNT_MIN even though the recipient has room.
    with pytest.raises(AmountOverflow):
        await mint(db, CUR, BOB, AMOUNT_MAX, now=NOW)
    assert await balance_of(db, BOB, CUR) == 0
    assert await supply_of(db, CUR) == AMOUNT_MAX


async def test_move_argument_checks(seeded: AsyncEngine) -> None:
    async with seeded.connect() as conn:
        tx = LedgerTx(conn, NOW)
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR)])
        execution_id = await tx.create_execution(type=ExecutionType.TRANSFER, initiator_id=ALICE, input={})
        for bad in (0, -1, True):
            with pytest.raises(InvalidAmount):
                await tx.move(execution_id, CUR, ALICE, BOB, bad, TransferKind.PRINCIPAL)
        with pytest.raises(AmountOverflow):
            await tx.move(execution_id, CUR, ALICE, BOB, AMOUNT_MAX + 1, TransferKind.PRINCIPAL)
        with pytest.raises(InvalidArgument):
            await tx.move(execution_id, CUR, ALICE, ALICE, 1, TransferKind.PRINCIPAL)
        with pytest.raises(LedgerUsageError):
            await tx.move(execution_id, CUR, ALICE, CAROL, 1, TransferKind.PRINCIPAL)
        with pytest.raises(LedgerUsageError):
            await tx.move(execution_id, CUR + 1, ALICE, BOB, 1, TransferKind.PRINCIPAL)
        with pytest.raises(LedgerUsageError):
            tx.balance(CAROL, CUR)
        assert tx.balance(ALICE, CUR) == 1_000
        await conn.rollback()


async def test_balances_require_their_currency_to_be_locked_first(seeded: AsyncEngine) -> None:
    async with seeded.connect() as conn:
        tx = LedgerTx(conn, NOW)
        assert tx.currency_lock(CUR) is None
        with pytest.raises(LedgerUsageError, match="lock_currencies"):
            await tx.lock_balances([(ALICE, CUR)])
        await tx.lock_currencies([CUR])
        assert tx.currency_lock(CUR) == "share"
        await tx.lock_balances([(ALICE, CUR)])
        await conn.rollback()


async def test_share_lock_cannot_be_upgraded(seeded: AsyncEngine) -> None:
    async with seeded.connect() as conn:
        tx = LedgerTx(conn, NOW)
        await tx.lock_currencies([CUR])
        with pytest.raises(LedgerUsageError, match="FOR SHARE"):
            await tx.lock_currencies([CUR], for_update=True)
        await conn.rollback()
    async with seeded.connect() as conn:
        tx = LedgerTx(conn, NOW)
        await tx.lock_currencies([CUR], for_update=True)
        await tx.lock_currencies([CUR])
        assert tx.currency_lock(CUR) == "update"
        await conn.rollback()


async def test_share_lock_blocks_update_lock_elsewhere(seeded: AsyncEngine, migrated_url: URL) -> None:
    impatient = create_engine(migrated_url, lock_wait_timeout=1)
    try:
        async with seeded.connect() as holder, impatient.connect() as waiter:
            await LedgerTx(holder, NOW).lock_currencies([CUR])
            await LedgerTx(waiter, NOW).lock_currencies([CUR])
            with pytest.raises(DBAPIError) as caught:
                await LedgerTx(waiter, NOW).lock_currencies([CUR], for_update=True)
            assert mysql_error_code(caught.value) == 1205
            await waiter.rollback()
            await holder.rollback()
    finally:
        await impatient.dispose()


async def test_issuance_account_only_serves_its_own_currency(seeded: AsyncEngine) -> None:
    other = await create_currency(seeded, currency_id=CUR + 1, symbol="OTH")
    async with seeded.connect() as conn:
        tx = LedgerTx(conn, NOW)
        await tx.lock_currencies([CUR, CUR + 1])
        with pytest.raises(LedgerUsageError, match="belongs to currency"):
            await tx.lock_balances([(other.issuance_account_id, CUR)])
        await conn.rollback()


async def test_lock_currencies_rejects_missing_and_deleted(seeded: AsyncEngine) -> None:
    async with seeded.begin() as conn:
        await conn.execute(update(currency).where(currency.c.currency_id == CUR).values(deleted_at=NOW))
    async with seeded.connect() as conn:
        tx = LedgerTx(conn, NOW)
        with pytest.raises(CurrencyNotFound):
            await tx.lock_currencies([CUR], for_update=True)
        with pytest.raises(CurrencyNotFound):
            await tx.lock_currencies([CUR + 1], include_deleted=True)
        rows = await tx.lock_currencies([CUR], for_update=True, include_deleted=True)
        assert rows[CUR]["deleted_at"] == NOW
        await conn.rollback()


async def _insert_pools(engine: AsyncEngine, currency_a: int, currency_b: int, pools: dict[int, int]) -> None:
    """Insert active pool rows with explicit IDs for the pair, mapping pool ID to fee tier, so custody accounts can reference them."""
    async with engine.begin() as conn:
        await ensure_user_accounts(conn, [ALICE], now=NOW)
        created = await conn.execute(insert(execution).values(type=ExecutionType.POOL_CREATE, origin="native", initiator_id=ALICE, status=ExecutionStatus.SUCCESS, input={}, output={}, created_at=NOW))
        assert created.inserted_primary_key is not None
        for pool_id, fee_bps in pools.items():
            row = {"currency_a": currency_a, "currency_b": currency_b, "fee_bps": fee_bps, "reserve_a": 1, "reserve_b": 1, "total_shares": MINIMUM_LIQUIDITY, "locked_shares": MINIMUM_LIQUIDITY}
            await conn.execute(insert(pool_table).values(pool_id=pool_id, created_by=ALICE, created_execution_id=created.inserted_primary_key[0], created_at=NOW, **row))


async def test_accounts_are_created_lazily_and_system_ids_are_negative(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR, symbol="LED")
    await create_currency(db, currency_id=CUR + 1, symbol="LEE")
    await _insert_pools(db, CUR, CUR + 1, {5: 100, 6: 50, 99: 10})
    async with db.begin() as conn:
        first = await get_or_create_issuance_account(conn, CUR)
        second = await get_or_create_issuance_account(conn, CUR + 1)
        assert await get_or_create_issuance_account(conn, CUR) == first
        pool = await create_pool_custody_account(conn, 5)
        assert await create_pool_custody_account(conn, 5) == pool
        assert await pool_custody_account_id(conn, 5) == pool
        # An account inserted with an explicit ID at the allocator's next value is skipped, not reused.
        next_allocation = (await conn.execute(select(func.max(system_account_allocation.c.allocation_id)))).scalar_one() + 1
        await conn.execute(insert(account).values(account_id=-(next_allocation + 1), kind="pool_custody", pool_id=99, created_at=NOW))
        other_pool = await create_pool_custody_account(conn, 6)
    assert len({first, second, pool, other_pool, STAKE_CUSTODY_ACCOUNT_ID}) == 5
    assert all(account_id < -1 for account_id in (first, second, pool, other_pool))
    assert other_pool < -(next_allocation + 1)
    await mint(db, CUR, CAROL, 5, now=NOW)
    async with db.connect() as conn:
        kinds = {row.account_id: row.kind for row in await conn.execute(select(account.c.account_id, account.c.kind))}
    assert kinds[CAROL] == AccountKind.USER


async def test_ignored_account_insert_is_detected(db: AsyncEngine) -> None:
    async with db.connect() as conn:
        # INSERT IGNORE turns this CHECK violation into a warning, so only the re-read reports it.
        await conn.execute(insert(account).prefix_with("IGNORE").values(account_id=-5, kind="user", created_at=NOW))
        with pytest.raises(RuntimeError, match="could not be created"):
            await accounts_module._confirm_inserted(conn, [-5], AccountKind.USER)
        await conn.rollback()


async def test_lock_balances_requires_known_system_accounts(seeded: AsyncEngine) -> None:
    async with seeded.connect() as conn:
        tx = LedgerTx(conn, NOW)
        await tx.lock_currencies([CUR])
        with pytest.raises(AccountNotFound):
            await tx.lock_balances([(-999, CUR)])
        with pytest.raises(InvalidArgument):
            await tx.lock_balances([(2**63, CUR)])
        await tx.lock_balances([(STAKE_CUSTODY_ACCOUNT_ID, CUR)])
        assert tx.balance(STAKE_CUSTODY_ACCOUNT_ID, CUR) == 0
        await conn.rollback()
    async with seeded.begin() as conn:
        await ensure_user_accounts(conn, [CAROL])


async def test_concurrent_first_touch_of_a_new_user(seeded: AsyncEngine) -> None:
    newcomer = 987_654_321

    async def touch() -> int:
        async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
            await asyncio.sleep(0.2)
            return {}

        return (await run_operation(seeded, type=ExecutionType.NOTIFICATION_ALLOW, initiator_id=newcomer, input={}, body=body, now=NOW, max_attempts=1)).execution_id

    first, second = await asyncio.gather(touch(), touch())
    assert first != second
    async with seeded.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(account).where(account.c.account_id == newcomer))).scalar_one() == 1


async def test_concurrent_issuance_account_creation(db: AsyncEngine) -> None:
    async with db.connect() as first, db.connect() as second:
        created = await get_or_create_issuance_account(first, CUR, now=NOW)
        racing = asyncio.create_task(get_or_create_issuance_account(second, CUR, now=NOW))
        await asyncio.sleep(0.3)
        assert not racing.done()
        await first.commit()
        assert await racing == created
        await second.commit()
    async with db.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(account).where(account.c.currency_id == CUR))).scalar_one() == 1


async def test_child_failure_keeps_parent_transfers(seeded: AsyncEngine) -> None:
    child_ids: list[int] = []

    async def failing_child(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_balances([(BOB, CUR), (CAROL, CUR)])
        await tx.move(execution_id, CUR, BOB, CAROL, 50, TransferKind.PRINCIPAL)
        raise InsufficientBalance("child ran out")

    async def reverting_child(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_balances([(BOB, CUR), (CAROL, CUR)])
        await tx.move(execution_id, CUR, BOB, CAROL, 10, TransferKind.PRINCIPAL)
        raise RevertError()

    async def ok_child(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_balances([(BOB, CUR), (CAROL, CUR)])
        await tx.move(execution_id, CUR, BOB, CAROL, 20, TransferKind.PRINCIPAL)
        return {"ok": True}

    async def parent(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR)])
        await tx.move(execution_id, CUR, ALICE, BOB, 100, TransferKind.PRINCIPAL)
        for child in (failing_child, reverting_child):
            with pytest.raises(DomainError) as caught:
                await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.CONTRACT_CALL, initiator_id=BOB, input={}, body=child, contract_function="f")
            assert caught.value.execution_id is not None
            child_ids.append(caught.value.execution_id)
        result = await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.CONTRACT_CALL, initiator_id=BOB, input={}, body=ok_child)
        child_ids.append(result.execution_id)
        assert tx.balance(BOB, CUR) == 80
        return {}

    result = await run_operation(seeded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=parent, now=NOW)
    rows = {row["execution_id"]: row for row in await _executions(seeded)}
    assert rows[result.execution_id]["status"] == "success"
    statuses = [(rows[i]["status"], rows[i]["parent_execution_id"]) for i in child_ids]
    assert statuses == [("failed", result.execution_id), ("reverted", result.execution_id), ("success", result.execution_id)]
    assert rows[child_ids[0]]["output"] == {"error": {"code": "insufficient_balance", "message": "child ran out"}}
    assert rows[child_ids[0]]["contract_function"] == "f"
    assert [m["amount"] for m in await _transfers(seeded, result.execution_id)] == [100]
    assert await _transfers(seeded, child_ids[0]) == []
    assert await _transfers(seeded, child_ids[1]) == []
    assert [m["amount"] for m in await _transfers(seeded, child_ids[2])] == [20]
    assert (await balance_of(seeded, ALICE, CUR), await balance_of(seeded, BOB, CUR), await balance_of(seeded, CAROL, CUR)) == (900, 80, 20)


async def test_parent_failure_removes_children(seeded: AsyncEngine) -> None:
    async def child(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (CAROL, CUR)])
        await tx.move(execution_id, CUR, ALICE, CAROL, 30, TransferKind.PRINCIPAL)
        return {}

    async def failing_child(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        raise InvalidArgument("nested failure")

    async def parent(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=child)
        with pytest.raises(InvalidArgument):
            await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=failing_child)
        raise RevertError("parent reverted")

    before = len(await _executions(seeded))
    with pytest.raises(RevertError) as caught:
        await run_operation(seeded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=parent, now=NOW)
    rows = await _executions(seeded)
    assert len(rows) == before + 1
    assert rows[-1]["execution_id"] == caught.value.execution_id
    assert rows[-1]["status"] == ExecutionStatus.REVERTED
    assert rows[-1]["output"] == {"error": {"code": "reverted", "message": "parent reverted"}}
    assert await _transfers(seeded, caught.value.execution_id) == []
    assert await balance_of(seeded, CAROL, CUR) == 0
    assert await balance_of(seeded, ALICE, CUR) == 1_000


async def test_propagated_child_error_describes_the_child(seeded: AsyncEngine) -> None:
    async def grandchild(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        raise InsufficientBalance("grandchild ran out")

    async def child(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.CONTRACT_CALL, initiator_id=BOB, input={}, body=grandchild, contract_function="inner")
        return {}

    async def parent(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=child, contract_function="outer")
        return {}

    def call() -> Any:
        return run_operation(seeded, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={}, body=parent, now=NOW, idempotency_key="nested")

    with pytest.raises(InsufficientBalance) as caught:
        await call()
    expected_child = {"type": "contract_call", "contract_function": "outer", "code": "insufficient_balance", "message": "grandchild ran out"}
    rows = await _executions(seeded)
    assert rows[-1]["execution_id"] == caught.value.execution_id
    assert rows[-1]["parent_execution_id"] is None
    assert rows[-1]["output"] == {"error": {"code": "insufficient_balance", "message": "grandchild ran out", "child": expected_child}}
    assert caught.value.child == expected_child
    with pytest.raises(InsufficientBalance) as replayed:
        await call()
    assert replayed.value.execution_id == caught.value.execution_id
    assert replayed.value.child == expected_child


async def test_unexpected_exception_rolls_back_everything(seeded: AsyncEngine) -> None:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR)])
        await tx.move(execution_id, CUR, ALICE, BOB, 5, TransferKind.PRINCIPAL)
        raise ValueError("bug")

    before = await _executions(seeded)
    with pytest.raises(ValueError, match="bug"):
        await run_operation(seeded, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={}, body=body, now=NOW)
    assert await _executions(seeded) == before
    assert await balance_of(seeded, BOB, CUR) == 0


async def test_opposite_concurrent_moves_do_not_deadlock(seeded: AsyncEngine) -> None:
    await _transfer(seeded, ALICE, BOB, 500)

    async def directed(sender: int, recipient: int) -> int:
        async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
            await tx.lock_currencies([CUR])
            # Each side lists its own account first; lock_balances sorts them, so both lock ALICE before BOB.
            await tx.lock_balances([(sender, CUR), (recipient, CUR)])
            await asyncio.sleep(0.05)
            await tx.move(execution_id, CUR, sender, recipient, 1, TransferKind.PRINCIPAL)
            return {}

        result = await run_operation(seeded, type=ExecutionType.TRANSFER, initiator_id=sender, input={}, body=body, now=NOW, max_attempts=1)
        return result.execution_id

    results = await asyncio.gather(*(directed(ALICE, BOB) if i % 2 == 0 else directed(BOB, ALICE) for i in range(20)))
    assert len(set(results)) == 20
    assert await balance_of(seeded, ALICE, CUR) == 500
    assert await balance_of(seeded, BOB, CUR) == 500


async def test_deadlock_is_retried(seeded: AsyncEngine) -> None:
    await _transfer(seeded, ALICE, BOB, 500)
    both_hold_first_lock = asyncio.Event()
    holders = 0
    attempts = {ALICE: 0, BOB: 0}

    async def unordered(sender: int, recipient: int) -> None:
        async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
            nonlocal holders
            attempts[sender] += 1
            await tx.lock_currencies([CUR])
            # Locking one row at a time in opposite orders deliberately breaks the global lock order.
            await tx.lock_balances([(sender, CUR)])
            holders += 1
            if holders >= 2:
                both_hold_first_lock.set()
            await asyncio.wait_for(both_hold_first_lock.wait(), timeout=10)
            await tx.lock_balances([(recipient, CUR)])
            await tx.move(execution_id, CUR, sender, recipient, 1, TransferKind.PRINCIPAL)
            return {}

        await run_operation(seeded, type=ExecutionType.TRANSFER, initiator_id=sender, input={}, body=body, now=NOW)

    await asyncio.gather(unordered(ALICE, BOB), unordered(BOB, ALICE))
    assert sorted(attempts.values()) == [1, 2]
    assert await balance_of(seeded, ALICE, CUR) == 500
    assert await balance_of(seeded, BOB, CUR) == 500
    assert len([row for row in await _executions(seeded) if row["type"] == "transfer"]) == 3


async def test_lock_wait_timeout_is_retried_then_gives_up(seeded: AsyncEngine, migrated_url: URL) -> None:
    impatient = create_engine(migrated_url, lock_wait_timeout=1)
    attempts = 0

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        await tx.lock_currencies([CUR])
        await tx.lock_balances([(ALICE, CUR), (BOB, CUR)])
        await tx.move(execution_id, CUR, ALICE, BOB, 1, TransferKind.PRINCIPAL)
        return {}

    try:
        async with seeded.connect() as holder:
            await holder.execute(select(balance.c.amount).where(balance.c.account_id == ALICE).with_for_update())
            with pytest.raises(DBAPIError) as caught:
                await run_operation(impatient, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={}, body=body, now=NOW, max_attempts=2)
            assert mysql_error_code(caught.value) == 1205
            assert attempts == 2

            async def release_later() -> None:
                await asyncio.sleep(1.5)
                await holder.rollback()

            attempts = 0
            releaser = asyncio.create_task(release_later())
            result = await run_operation(impatient, type=ExecutionType.TRANSFER, initiator_id=ALICE, input={}, body=body, now=NOW, max_attempts=3)
            await releaser
        assert attempts == 2
        assert result.status is ExecutionStatus.SUCCESS
        assert await balance_of(seeded, BOB, CUR) == 1
    finally:
        await impatient.dispose()


async def test_session_lock_wait_timeout_is_short(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        assert (await conn.execute(text("SELECT @@SESSION.innodb_lock_wait_timeout"))).scalar_one() == 5


def test_retry_delay_has_jitter() -> None:
    delays = {retry_delay(2) for _ in range(20)}
    assert len(delays) > 1
    assert all(0.02 <= delay <= 0.06 for delay in delays)
