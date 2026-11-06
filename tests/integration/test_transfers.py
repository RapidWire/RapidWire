from __future__ import annotations

import asyncio
import functools
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import allowances, claims
from rapidwire.core import transfers as transfers_module
from rapidwire.core.allowances import ApproveResult, approve
from rapidwire.core.claims import CreateClaimResult, create_claim
from rapidwire.core.errors import AmountOverflow, CurrencyNotFound, DomainError, InsufficientBalance, InvalidAmount, InvalidArgument
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import run_operation
from rapidwire.core.transfers import TransferResult, transfer
from rapidwire.core.types import ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.tables import STAKE_CUSTODY_ACCOUNT_ID, execution
from rapidwire.db.tables import transfer as transfer_table
from rapidwire.db.types import AMOUNT_MAX
from tests.integration.factories import balance_of, create_currency, mint

NOW = datetime(2026, 5, 1, tzinfo=UTC)
CUR = 7
DELETED_CUR = 8
ALICE = 111
BOB = 222


@pytest.fixture
async def funded(db: AsyncEngine) -> AsyncEngine:
    await create_currency(db, currency_id=CUR, symbol="TRF", now=NOW)
    await mint(db, CUR, ALICE, 1_000, now=NOW)
    return db


async def _transfer_rows(engine: AsyncEngine, execution_id: int) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(transfer_table).where(transfer_table.c.execution_id == execution_id))).mappings().all()
    return [dict(row) for row in rows]


async def _execution(engine: AsyncEngine, execution_id: int) -> dict[str, Any]:
    async with engine.connect() as conn:
        row = (await conn.execute(select(execution).where(execution.c.execution_id == execution_id))).mappings().one()
    return dict(row)


async def _count_executions(engine: AsyncEngine, type: str) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(execution).where(execution.c.type == type))).scalar_one())


async def _assert_failed(engine: AsyncEngine, error: DomainError, type: str) -> None:
    assert error.execution_id is not None
    row = await _execution(engine, error.execution_id)
    assert row["type"] == type
    assert row["status"] == ExecutionStatus.FAILED
    assert row["output"] == {"error": {"code": error.code, "message": error.message}}
    assert await _transfer_rows(engine, error.execution_id) == []


async def _assert_invariants(engine: AsyncEngine) -> None:
    report = await verify_invariants(engine)
    assert report.ok, report.violations


async def test_transfer_moves_funds_as_one_principal_transfer(funded: AsyncEngine) -> None:
    result = await transfer(funded, sender_id=ALICE, recipient_id=BOB, currency_id=CUR, amount=250, now=NOW)
    assert result == TransferResult(execution_id=result.execution_id, transfer_id=result.transfer_id, sender_id=ALICE, recipient_id=BOB, currency_id=CUR, amount=250)
    assert not result.replayed
    assert await balance_of(funded, ALICE, CUR) == 750
    assert await balance_of(funded, BOB, CUR) == 250
    row = await _execution(funded, result.execution_id)
    assert (row["type"], row["status"], row["initiator_id"]) == (ExecutionType.TRANSFER, ExecutionStatus.SUCCESS, ALICE)
    assert row["input"] == {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "250"}
    rows = await _transfer_rows(funded, result.execution_id)
    assert [(r["transfer_id"], r["from_account"], r["to_account"], r["amount"], r["kind"]) for r in rows] == [(result.transfer_id, ALICE, BOB, 250, TransferKind.PRINCIPAL)]
    await _assert_invariants(funded)


async def test_smallest_unit_and_whole_balance_are_allowed(funded: AsyncEngine) -> None:
    await transfer(funded, sender_id=ALICE, recipient_id=BOB, currency_id=CUR, amount=1, now=NOW)
    await transfer(funded, sender_id=ALICE, recipient_id=BOB, currency_id=CUR, amount=999, now=NOW)
    assert await balance_of(funded, ALICE, CUR) == 0
    assert await balance_of(funded, BOB, CUR) == 1_000
    await _assert_invariants(funded)


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        ({"amount": 0}, InvalidAmount),
        ({"amount": -5}, InvalidAmount),
        ({"amount": AMOUNT_MAX + 1}, AmountOverflow),
        ({"amount": 1_001}, InsufficientBalance),
        ({"recipient_id": ALICE}, InvalidArgument),
        ({"recipient_id": 0}, InvalidArgument),
        ({"recipient_id": STAKE_CUSTODY_ACCOUNT_ID}, InvalidArgument),
        ({"recipient_id": 2**63}, InvalidArgument),
        ({"currency_id": 999}, CurrencyNotFound),
        ({"currency_id": DELETED_CUR}, CurrencyNotFound),
    ],
)
async def test_rejected_transfer_is_recorded_as_failed_and_moves_nothing(funded: AsyncEngine, overrides: dict[str, int], error: type[DomainError]) -> None:
    await create_currency(funded, currency_id=DELETED_CUR, symbol="GONE", now=NOW, deleted_at=NOW)
    args: dict[str, Any] = {"sender_id": ALICE, "recipient_id": BOB, "currency_id": CUR, "amount": 100} | overrides
    with pytest.raises(error) as caught:
        await transfer(funded, now=NOW, **args)
    await _assert_failed(funded, caught.value, ExecutionType.TRANSFER)
    assert await balance_of(funded, ALICE, CUR) == 1_000
    assert await balance_of(funded, BOB, CUR) == 0
    await _assert_invariants(funded)


@pytest.mark.parametrize("amount", [1.5, True, "10"])
async def test_non_integer_amount_is_rejected_before_any_execution(funded: AsyncEngine, amount: object) -> None:
    with pytest.raises(InvalidAmount) as caught:
        await transfer(funded, sender_id=ALICE, recipient_id=BOB, currency_id=CUR, amount=amount, now=NOW)  # type: ignore[arg-type]
    assert caught.value.execution_id is None
    assert await _count_executions(funded, ExecutionType.TRANSFER) == 0


async def test_idempotent_replay_returns_same_result_without_moving_twice(funded: AsyncEngine) -> None:
    first = await transfer(funded, sender_id=ALICE, recipient_id=BOB, currency_id=CUR, amount=40, now=NOW, idempotency_key="pay-1")
    again = await transfer(funded, sender_id=ALICE, recipient_id=BOB, currency_id=CUR, amount=40, now=NOW, idempotency_key="pay-1")
    assert again == first
    assert again.replayed
    assert not first.replayed
    assert await balance_of(funded, BOB, CUR) == 40
    assert await _count_executions(funded, ExecutionType.TRANSFER) == 1
    await _assert_invariants(funded)


async def test_parallel_transfers_never_overdraw(funded: AsyncEngine) -> None:
    recipients = [5_000 + i for i in range(10)]
    outcomes = await asyncio.gather(*(transfer(funded, sender_id=ALICE, recipient_id=r, currency_id=CUR, amount=150, now=NOW) for r in recipients), return_exceptions=True)
    succeeded = [o for o in outcomes if isinstance(o, TransferResult)]
    failed = [o for o in outcomes if not isinstance(o, TransferResult)]
    assert len(succeeded) == 6
    assert len(failed) == 4
    assert all(isinstance(o, InsufficientBalance) for o in failed)
    assert await balance_of(funded, ALICE, CUR) == 100
    assert sum([await balance_of(funded, r, CUR) for r in recipients]) == 900
    await _assert_invariants(funded)


async def test_first_ever_opposite_operations_do_not_deadlock(funded: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    single_attempt = functools.partial(run_operation, max_attempts=1)
    for module in (transfers_module, allowances, claims):
        monkeypatch.setattr(module, "run_operation", single_attempt)
    pairs = [(9_000 + 2 * index, 9_001 + 2 * index) for index in range(8)]

    sent = await asyncio.gather(*(transfer(funded, sender_id=a, recipient_id=b, currency_id=CUR, amount=1, now=NOW) for pair in pairs for a, b in (pair, pair[::-1])), return_exceptions=True)
    assert all(isinstance(outcome, InsufficientBalance) and outcome.execution_id is not None for outcome in sent), sent

    fresh = [(a + 1_000, b + 1_000) for a, b in pairs]
    approved = await asyncio.gather(*(approve(funded, owner_id=a, spender_id=b, currency_id=CUR, amount=5, now=NOW) for pair in fresh for a, b in (pair, pair[::-1])), return_exceptions=True)
    assert all(isinstance(outcome, ApproveResult) for outcome in approved), approved

    fresh = [(a + 2_000, b + 2_000) for a, b in pairs]
    claimed = await asyncio.gather(*(create_claim(funded, claimant_id=a, payer_id=b, currency_id=CUR, amount=5, now=NOW) for pair in fresh for a, b in (pair, pair[::-1])), return_exceptions=True)
    assert all(isinstance(outcome, CreateClaimResult) for outcome in claimed), claimed
    await _assert_invariants(funded)
