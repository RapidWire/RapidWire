from __future__ import annotations

import asyncio
import itertools
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.allowances import AllowanceChange, ApproveResult, approve, approve_in_tx, delete_allowances_for_currency, get_allowance, list_allowance_history
from rapidwire.core.errors import (
    AmountOverflow,
    CurrencyNotFound,
    DomainError,
    InsufficientAllowance,
    InsufficientBalance,
    InvalidAmount,
    InvalidArgument,
)
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import LedgerTx, LedgerUsageError, run_child, run_operation
from rapidwire.core.transfers import TransferFromResult, transfer_from, transfer_from_in_tx, transfer_in_tx
from rapidwire.core.types import ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.tables import STAKE_CUSTODY_ACCOUNT_ID, allowance, allowance_history, execution
from rapidwire.db.tables import transfer as transfer_table
from rapidwire.db.types import AMOUNT_MAX
from tests.integration.factories import balance_of, create_currency, mint

NOW = datetime(2026, 5, 2, tzinfo=UTC)
CUR = 9
OTHER_CUR = 10
DELETED_CUR = 13
OWNER = 111
SPENDER = 222
CAROL = 333


@pytest.fixture
async def funded(db: AsyncEngine) -> AsyncEngine:
    await create_currency(db, currency_id=CUR, symbol="ALW", now=NOW)
    await create_currency(db, currency_id=OTHER_CUR, symbol="OTH", now=NOW)
    await create_currency(db, currency_id=DELETED_CUR, symbol="DEL", now=NOW, deleted_at=NOW)
    await mint(db, CUR, OWNER, 1_000, now=NOW)
    return db


async def _execution(engine: AsyncEngine, execution_id: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (await conn.execute(select(execution).where(execution.c.execution_id == execution_id))).mappings().one()
    return dict(row)


async def _count(engine: AsyncEngine, table: Any, *where: Any) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(table).where(*where))).scalar_one())


async def _assert_failed(engine: AsyncEngine, error: DomainError, type: str) -> None:
    assert error.execution_id is not None
    row = await _execution(engine, error.execution_id)
    assert (row["type"], row["status"]) == (type, ExecutionStatus.FAILED)
    assert row["output"] == {"error": {"code": error.code, "message": error.message}}
    assert await _count(engine, transfer_table, transfer_table.c.execution_id == error.execution_id) == 0
    assert await _count(engine, allowance_history, allowance_history.c.execution_id == error.execution_id) == 0


async def _assert_invariants(engine: AsyncEngine) -> None:
    report = await verify_invariants(engine)
    assert report.ok, report.violations


async def test_approve_overwrites_and_records_history(funded: AsyncEngine) -> None:
    first = await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=100, now=NOW)
    second = await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=30, now=NOW + timedelta(seconds=1))
    assert first == ApproveResult(execution_id=first.execution_id, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, old_amount=0, new_amount=100)
    assert (second.old_amount, second.new_amount) == (100, 30)
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == 30
    row = await _execution(funded, second.execution_id)
    assert (row["type"], row["status"], row["initiator_id"]) == (ExecutionType.APPROVE, ExecutionStatus.SUCCESS, OWNER)
    assert row["input"] == {"spender_id": str(SPENDER), "currency_id": str(CUR), "amount": "30"}
    history = (await list_allowance_history(funded, owner_id=OWNER)).items
    assert [(h.execution_id, h.old_amount, h.new_amount) for h in history] == [(second.execution_id, 100, 30), (first.execution_id, 0, 100)]
    assert history[0].created_at == NOW + timedelta(seconds=1)
    assert await _count(funded, transfer_table, transfer_table.c.execution_id.in_([first.execution_id, second.execution_id])) == 0
    await _assert_invariants(funded)


async def test_zero_approve_is_allowed(funded: AsyncEngine) -> None:
    fresh = await approve(funded, owner_id=OWNER, spender_id=CAROL, currency_id=CUR, amount=0, now=NOW)
    assert (fresh.old_amount, fresh.new_amount) == (0, 0)
    await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=50, now=NOW)
    cleared = await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=0, now=NOW)
    assert (cleared.old_amount, cleared.new_amount) == (50, 0)
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == 0
    with pytest.raises(InsufficientAllowance):
        await transfer_from(funded, spender_id=SPENDER, owner_id=OWNER, recipient_id=CAROL, currency_id=CUR, amount=1, now=NOW)


async def test_missing_allowance_reads_as_zero(funded: AsyncEngine) -> None:
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == 0
    async with funded.connect() as conn:
        assert await get_allowance(conn, OWNER, SPENDER, CUR) == 0


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"spender_id": OWNER}, InvalidArgument),
        ({"spender_id": STAKE_CUSTODY_ACCOUNT_ID}, InvalidArgument),
        ({"amount": -1}, InvalidAmount),
        ({"amount": AMOUNT_MAX + 1}, AmountOverflow),
        ({"currency_id": 999}, CurrencyNotFound),
        ({"currency_id": DELETED_CUR}, CurrencyNotFound),
    ],
)
async def test_rejected_approve_is_recorded_and_changes_nothing(funded: AsyncEngine, overrides: dict[str, int], error: type[DomainError]) -> None:
    args: dict[str, Any] = {"owner_id": OWNER, "spender_id": SPENDER, "currency_id": CUR, "amount": 10} | overrides
    with pytest.raises(error) as caught:
        await approve(funded, now=NOW, **args)
    await _assert_failed(funded, caught.value, ExecutionType.APPROVE)
    assert await _count(funded, allowance) == 0
    assert await _count(funded, allowance_history) == 0


async def test_idempotent_approve_replay(funded: AsyncEngine) -> None:
    first = await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=70, now=NOW, idempotency_key="ap-1")
    again = await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=70, now=NOW, idempotency_key="ap-1")
    assert again == first
    assert again.replayed
    assert await _count(funded, allowance_history) == 1
    assert await _count(funded, execution, execution.c.type == ExecutionType.APPROVE) == 1


async def test_replayed_failure_reraises_with_same_execution(funded: AsyncEngine) -> None:
    with pytest.raises(InvalidArgument) as first:
        await approve(funded, owner_id=OWNER, spender_id=OWNER, currency_id=CUR, amount=5, now=NOW, idempotency_key="ap-bad")
    with pytest.raises(InvalidArgument) as again:
        await approve(funded, owner_id=OWNER, spender_id=OWNER, currency_id=CUR, amount=5, now=NOW, idempotency_key="ap-bad")
    assert first.value.execution_id is not None
    assert again.value.execution_id == first.value.execution_id
    assert again.value.message == first.value.message
    assert await _count(funded, execution, execution.c.type == ExecutionType.APPROVE) == 1


async def test_history_is_paged_newest_first_and_filtered(funded: AsyncEngine) -> None:
    for amount in range(1, 5):
        await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=amount, now=NOW)
    await approve(funded, owner_id=OWNER, spender_id=CAROL, currency_id=CUR, amount=7, now=NOW)
    await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=OTHER_CUR, amount=9, now=NOW)
    await approve(funded, owner_id=CAROL, spender_id=SPENDER, currency_id=CUR, amount=11, now=NOW)

    everything = await list_allowance_history(funded, owner_id=OWNER)
    assert [h.new_amount for h in everything.items] == [9, 7, 4, 3, 2, 1]
    assert (everything.total, everything.pages, everything.per_page, everything.total_is_lower_bound) == (6, 1, 15, False)
    assert all(isinstance(h, AllowanceChange) for h in everything.items)
    second_page = await list_allowance_history(funded, owner_id=OWNER, page=2, per_page=4)
    assert ([h.new_amount for h in second_page.items], second_page.total, second_page.pages) == ([2, 1], 6, 2)
    assert (await list_allowance_history(funded, owner_id=OWNER, page=3, per_page=4)).items == ()
    assert [h.new_amount for h in (await list_allowance_history(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR)).items] == [4, 3, 2, 1]
    assert [h.new_amount for h in (await list_allowance_history(funded, owner_id=OWNER, currency_id=OTHER_CUR)).items] == [9]
    assert [h.new_amount for h in (await list_allowance_history(funded, owner_id=OWNER, spender_id=CAROL)).items] == [7]
    for page, per_page in [(0, 15), (1, 0), (1, 101)]:
        with pytest.raises(InvalidArgument):
            await list_allowance_history(funded, owner_id=OWNER, page=page, per_page=per_page)


async def test_transfer_from_spends_allowance_and_moves_owner_funds(funded: AsyncEngine) -> None:
    approved = await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=300, now=NOW)
    result = await transfer_from(funded, spender_id=SPENDER, owner_id=OWNER, recipient_id=CAROL, currency_id=CUR, amount=120, now=NOW)
    assert result == TransferFromResult(
        execution_id=result.execution_id,
        transfer_id=result.transfer_id,
        spender_id=SPENDER,
        owner_id=OWNER,
        recipient_id=CAROL,
        currency_id=CUR,
        amount=120,
        allowance_remaining=180,
    )
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == 180
    assert await balance_of(funded, OWNER, CUR) == 880
    assert await balance_of(funded, CAROL, CUR) == 120
    assert await balance_of(funded, SPENDER, CUR) == 0
    row = await _execution(funded, result.execution_id)
    assert (row["type"], row["status"], row["initiator_id"]) == (ExecutionType.TRANSFER_FROM, ExecutionStatus.SUCCESS, SPENDER)
    assert row["input"] == {"owner_id": str(OWNER), "recipient_id": str(CAROL), "currency_id": str(CUR), "amount": "120"}
    async with funded.connect() as conn:
        moved = (await conn.execute(select(transfer_table).where(transfer_table.c.execution_id == result.execution_id))).mappings().all()
    assert [(m["from_account"], m["to_account"], m["amount"], m["kind"]) for m in moved] == [(OWNER, CAROL, 120, TransferKind.PRINCIPAL)]
    history = (await list_allowance_history(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR)).items
    assert [(h.execution_id, h.old_amount, h.new_amount) for h in history] == [(result.execution_id, 300, 180), (approved.execution_id, 0, 300)]
    await _assert_invariants(funded)


async def test_spender_may_be_the_recipient(funded: AsyncEngine) -> None:
    await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=100, now=NOW)
    result = await transfer_from(funded, spender_id=SPENDER, owner_id=OWNER, recipient_id=SPENDER, currency_id=CUR, amount=100, now=NOW)
    assert result.allowance_remaining == 0
    assert await balance_of(funded, SPENDER, CUR) == 100
    await _assert_invariants(funded)


@pytest.mark.parametrize(
    ("approved", "overrides", "error"),
    [
        (50, {"amount": 60}, InsufficientAllowance),
        (None, {"amount": 1}, InsufficientAllowance),
        (5_000, {"amount": 1_500}, InsufficientBalance),
        (50, {"amount": 0}, InvalidAmount),
        (50, {"recipient_id": OWNER}, InvalidArgument),
        (50, {"recipient_id": STAKE_CUSTODY_ACCOUNT_ID}, InvalidArgument),
        (50, {"currency_id": 999}, CurrencyNotFound),
        (50, {"currency_id": DELETED_CUR}, CurrencyNotFound),
    ],
)
async def test_rejected_transfer_from_leaves_allowance_and_balances_unchanged(funded: AsyncEngine, approved: int | None, overrides: dict[str, int], error: type[DomainError]) -> None:
    if approved is not None:
        await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=approved, now=NOW)
    args: dict[str, Any] = {"spender_id": SPENDER, "owner_id": OWNER, "recipient_id": CAROL, "currency_id": CUR, "amount": 10} | overrides
    with pytest.raises(error) as caught:
        await transfer_from(funded, now=NOW, **args)
    await _assert_failed(funded, caught.value, ExecutionType.TRANSFER_FROM)
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == (approved or 0)
    assert await balance_of(funded, OWNER, CUR) == 1_000
    assert await balance_of(funded, CAROL, CUR) == 0
    await _assert_invariants(funded)


async def test_owner_cannot_be_the_spender(funded: AsyncEngine) -> None:
    with pytest.raises(InvalidArgument) as caught:
        await transfer_from(funded, spender_id=OWNER, owner_id=OWNER, recipient_id=CAROL, currency_id=CUR, amount=1, now=NOW)
    await _assert_failed(funded, caught.value, ExecutionType.TRANSFER_FROM)


async def test_idempotent_transfer_from_replay(funded: AsyncEngine) -> None:
    await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=100, now=NOW)
    first = await transfer_from(funded, spender_id=SPENDER, owner_id=OWNER, recipient_id=CAROL, currency_id=CUR, amount=60, now=NOW, idempotency_key="tf-1")
    again = await transfer_from(funded, spender_id=SPENDER, owner_id=OWNER, recipient_id=CAROL, currency_id=CUR, amount=60, now=NOW, idempotency_key="tf-1")
    assert again == first
    assert again.replayed
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == 40
    assert await balance_of(funded, CAROL, CUR) == 60


async def test_tx_level_functions_compose_in_one_execution(funded: AsyncEngine) -> None:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        sent = await transfer_in_tx(tx, execution_id, sender_id=OWNER, recipient_id=CAROL, currency_id=CUR, amount=100)
        await approve_in_tx(tx, execution_id, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=200)
        spent = await transfer_from_in_tx(tx, execution_id, spender_id=SPENDER, owner_id=OWNER, recipient_id=CAROL, currency_id=CUR, amount=50)
        return {"transfers": [sent.transfer_id, spent.transfer_id]}

    result = await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=OWNER, input={}, body=body, now=NOW)
    assert await balance_of(funded, OWNER, CUR) == 850
    assert await balance_of(funded, CAROL, CUR) == 150
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == 150
    history = (await list_allowance_history(funded, owner_id=OWNER)).items
    assert [(h.execution_id, h.old_amount, h.new_amount) for h in history] == [(result.execution_id, 200, 150), (result.execution_id, 0, 200)]
    assert await _count(funded, transfer_table, transfer_table.c.execution_id == result.execution_id) == 2
    await _assert_invariants(funded)


@pytest.mark.parametrize("for_update", [None, False])
async def test_currency_cascade_needs_the_currency_for_update(funded: AsyncEngine, for_update: bool | None) -> None:
    await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=10, now=NOW)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        if for_update is not None:
            await tx.lock_currencies([CUR], for_update=for_update)
        return {"deleted": await delete_allowances_for_currency(tx, execution_id, currency_id=CUR)}

    with pytest.raises(LedgerUsageError):
        await run_operation(funded, type=ExecutionType.CURRENCY_DELETE_FINALIZE, initiator_id=OWNER, input={}, body=body, now=NOW)
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == 10


async def test_failed_child_transfer_from_leaves_allowance_unchanged(funded: AsyncEngine) -> None:
    await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=5_000, now=NOW)
    child_errors: list[InsufficientBalance] = []

    async def child(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await transfer_from_in_tx(tx, execution_id, spender_id=SPENDER, owner_id=OWNER, recipient_id=CAROL, currency_id=CUR, amount=1_500)).to_output()

    async def parent(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        try:
            await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.TRANSFER_FROM, initiator_id=SPENDER, input={"amount": 1_500}, body=child)
        except InsufficientBalance as error:
            child_errors.append(error)
        return {}

    result = await run_operation(funded, type=ExecutionType.CONTRACT_CALL, initiator_id=SPENDER, input={}, body=parent, now=NOW)
    assert result.status is ExecutionStatus.SUCCESS
    assert len(child_errors) == 1
    await _assert_failed(funded, child_errors[0], ExecutionType.TRANSFER_FROM)
    assert (await _execution(funded, child_errors[0].execution_id or 0))["parent_execution_id"] == result.execution_id
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == 5_000
    assert await _count(funded, allowance_history) == 1
    assert await balance_of(funded, OWNER, CUR) == 1_000
    await _assert_invariants(funded)


async def test_parallel_transfer_from_drains_allowance_exactly(funded: AsyncEngine) -> None:
    await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=500, now=NOW)
    outcomes = await asyncio.gather(
        *(transfer_from(funded, spender_id=SPENDER, owner_id=OWNER, recipient_id=CAROL, currency_id=CUR, amount=75, now=NOW) for _ in range(10)),
        return_exceptions=True,
    )
    succeeded = [o for o in outcomes if isinstance(o, TransferFromResult)]
    failed = [o for o in outcomes if not isinstance(o, TransferFromResult)]
    assert len(succeeded) == 6
    assert all(isinstance(o, InsufficientAllowance) for o in failed)
    assert len(failed) == 4
    assert sorted(o.allowance_remaining for o in succeeded) == [50, 125, 200, 275, 350, 425]
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == 50
    assert await balance_of(funded, CAROL, CUR) == 450
    assert await balance_of(funded, OWNER, CUR) == 550
    assert (await list_allowance_history(funded, owner_id=OWNER)).total == 7
    await _assert_invariants(funded)


async def test_parallel_first_approves_serialize_on_one_row(funded: AsyncEngine) -> None:
    outcomes = await asyncio.gather(*(approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=i, now=NOW) for i in range(1, 21)), return_exceptions=True)
    results = [o for o in outcomes if isinstance(o, ApproveResult)]
    assert len(results) == 20, [o for o in outcomes if not isinstance(o, ApproveResult)]
    assert [r.old_amount for r in results].count(0) == 1
    history = (await list_allowance_history(funded, owner_id=OWNER, per_page=100)).items
    assert len(history) == 20
    chain = list(reversed(history))
    assert chain[0].old_amount == 0
    assert all(later.old_amount == earlier.new_amount for earlier, later in itertools.pairwise(chain))
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == history[0].new_amount


async def test_parallel_approves_and_transfer_from_only_fail_on_allowance(funded: AsyncEngine) -> None:
    await mint(funded, CUR, OWNER, 99_000, now=NOW)
    await approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=1_000, now=NOW)
    calls: list[Any] = []
    for i in range(20):
        if i % 4 == 0:
            calls.append(approve(funded, owner_id=OWNER, spender_id=SPENDER, currency_id=CUR, amount=100, now=NOW))
        else:
            calls.append(transfer_from(funded, spender_id=SPENDER, owner_id=OWNER, recipient_id=5_000 + i, currency_id=CUR, amount=50, now=NOW))
    outcomes = await asyncio.gather(*calls, return_exceptions=True)
    unexpected = [o for o in outcomes if isinstance(o, BaseException) and not isinstance(o, InsufficientAllowance)]
    assert unexpected == []
    spent = sum(o.amount for o in outcomes if isinstance(o, TransferFromResult))
    assert spent > 0
    assert await balance_of(funded, OWNER, CUR) == 100_000 - spent
    history = list(reversed((await list_allowance_history(funded, owner_id=OWNER, per_page=100)).items))
    assert all(later.old_amount == earlier.new_amount for earlier, later in itertools.pairwise(history))
    assert await get_allowance(funded, OWNER, SPENDER, CUR) == history[-1].new_amount
    await _assert_invariants(funded)
