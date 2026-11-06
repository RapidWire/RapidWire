"""Allowances: an owner lets a spender move up to an amount of one currency from the owner's balance through transfer_from(); approve() overwrites the amount, a missing row reads as zero, and every change writes an allowance_history row through record_change()."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.db import Db, consistent_snapshot, reader
from rapidwire.core.errors import InvalidArgument
from rapidwire.core.ledger import LedgerTx, LedgerUsageError, OperationResult, run_operation
from rapidwire.core.paging import ALLOWANCE_HISTORY_PER_PAGE_DEFAULT, ALLOWANCE_HISTORY_PER_PAGE_MAX, Page, bounded_count, count_bounded, validate_paging
from rapidwire.core.types import ExecutionType
from rapidwire.core.validation import require_storable_int, require_user_account, validate_non_negative_amount
from rapidwire.db.tables import allowance, allowance_history

__all__ = [
    "AllowanceChange",
    "ApproveResult",
    "approve",
    "approve_in_tx",
    "delete_allowances_for_currency",
    "get_allowance",
    "list_allowance_history",
    "record_change",
]


@dataclass(frozen=True)
class ApproveResult:
    """A completed approve; old_amount is the allowance it replaced (zero when none existed), and replayed takes no part in equality."""

    execution_id: int
    owner_id: int
    spender_id: int
    currency_id: int
    old_amount: int
    new_amount: int
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"owner_id": self.owner_id, "spender_id": self.spender_id, "currency_id": self.currency_id, "old_amount": self.old_amount, "new_amount": self.new_amount}

    @classmethod
    def from_operation(cls, op: OperationResult) -> ApproveResult:
        return cls(
            execution_id=op.execution_id,
            owner_id=op.int_field("owner_id"),
            spender_id=op.int_field("spender_id"),
            currency_id=op.int_field("currency_id"),
            old_amount=op.int_field("old_amount"),
            new_amount=op.int_field("new_amount"),
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class AllowanceChange:
    """One allowance_history row: the allowance of (owner, spender, currency) went from old_amount to new_amount in execution_id."""

    allowance_history_id: int
    execution_id: int
    owner_id: int
    spender_id: int
    currency_id: int
    old_amount: int
    new_amount: int
    created_at: datetime


async def record_change(tx: LedgerTx, execution_id: int, owner_id: int, spender_id: int, currency_id: int, old_amount: int, new_amount: int) -> None:
    """Write the allowance_history row of one allowance change made by execution_id."""
    await tx.conn.execute(
        insert(allowance_history).values(
            execution_id=execution_id,
            owner_id=owner_id,
            spender_id=spender_id,
            currency_id=currency_id,
            old_amount=old_amount,
            new_amount=new_amount,
            created_at=tx.now,
        )
    )


async def approve_in_tx(tx: LedgerTx, execution_id: int, *, owner_id: int, spender_id: int, currency_id: int, amount: int) -> ApproveResult:
    """Set the allowance of spender over owner's funds in the currency to amount and record the change against execution_id; raises InvalidAmount, AmountOverflow, InvalidArgument or CurrencyNotFound."""
    validate_non_negative_amount(amount, "The allowance must be zero or a positive integer number of minimal units.")
    require_user_account(owner_id, "owner")
    require_user_account(spender_id, "spender")
    if owner_id == spender_id:
        raise InvalidArgument("You cannot approve yourself as a spender.")
    await tx.lock_currencies([currency_id])
    await tx.require_accounts([owner_id, spender_id])
    # Inserting a zero row when none exists gives the SELECT FOR UPDATE below a row to lock, so concurrent approvals serialize on it.
    stmt = mysql_insert(allowance).values(owner_id=owner_id, spender_id=spender_id, currency_id=currency_id, amount=0, updated_at=tx.now)
    await tx.conn.execute(stmt.on_duplicate_key_update(owner_id=stmt.inserted.owner_id))
    key = (allowance.c.owner_id == owner_id, allowance.c.spender_id == spender_id, allowance.c.currency_id == currency_id)
    old_amount = int((await tx.conn.execute(select(allowance.c.amount).where(*key).with_for_update())).scalar_one())
    await tx.conn.execute(update(allowance).where(*key).values(amount=amount, updated_at=tx.now))
    await record_change(tx, execution_id, owner_id, spender_id, currency_id, old_amount, amount)
    return ApproveResult(execution_id=execution_id, owner_id=owner_id, spender_id=spender_id, currency_id=currency_id, old_amount=old_amount, new_amount=amount)


async def approve(
    engine: AsyncEngine,
    *,
    owner_id: int,
    spender_id: int,
    currency_id: int,
    amount: int,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> ApproveResult:
    """Set the owner's allowance to the spender as an approve execution initiated by the owner."""
    require_storable_int(amount)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        result = await approve_in_tx(tx, execution_id, owner_id=owner_id, spender_id=spender_id, currency_id=currency_id, amount=amount)
        return result.to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.APPROVE,
        initiator_id=owner_id,
        input={"spender_id": spender_id, "currency_id": currency_id, "amount": amount},
        body=body,
        idempotency_key=idempotency_key,
        now=now,
        user_accounts=[spender_id],
    )
    return ApproveResult.from_operation(op)


async def delete_allowances_for_currency(tx: LedgerTx, execution_id: int, *, currency_id: int) -> int:
    """Lock and delete every allowance of the currency, recording each as an old amount -> 0 change of execution_id, and return how many there were; the transaction must hold the currency FOR UPDATE (LedgerUsageError otherwise)."""
    if tx.currency_lock(currency_id) != "update":
        raise LedgerUsageError(f"currency {currency_id} must be locked FOR UPDATE before its allowances are deleted")
    order = (allowance.c.owner_id, allowance.c.spender_id, allowance.c.currency_id)
    rows = (await tx.conn.execute(select(allowance).where(allowance.c.currency_id == currency_id).order_by(*order).with_for_update())).mappings().all()
    for row in rows:
        await record_change(tx, execution_id, row["owner_id"], row["spender_id"], currency_id, int(row["amount"]), 0)
    if rows:
        await tx.conn.execute(delete(allowance).where(allowance.c.currency_id == currency_id))
    return len(rows)


async def get_allowance(db: Db, owner_id: int, spender_id: int, currency_id: int) -> int:
    """The amount spender may still move from owner's balance in the currency, zero when no allowance row exists."""
    stmt = select(allowance.c.amount).where(allowance.c.owner_id == owner_id, allowance.c.spender_id == spender_id, allowance.c.currency_id == currency_id)
    async with reader(db) as conn:
        amount = (await conn.execute(stmt)).scalar_one_or_none()
    return 0 if amount is None else int(amount)


async def list_allowance_history(
    db: Db,
    *,
    owner_id: int,
    spender_id: int | None = None,
    currency_id: int | None = None,
    page: int = 1,
    per_page: int = ALLOWANCE_HISTORY_PER_PAGE_DEFAULT,
) -> Page[AllowanceChange]:
    """One page of the owner's allowance changes, newest first, optionally narrowed to one spender and/or one currency; paging is checked by validate_paging()."""
    offset = validate_paging(page, per_page, ALLOWANCE_HISTORY_PER_PAGE_MAX)
    conditions = [allowance_history.c.owner_id == owner_id]
    if spender_id is not None:
        conditions.append(allowance_history.c.spender_id == spender_id)
    if currency_id is not None:
        conditions.append(allowance_history.c.currency_id == currency_id)
    page_stmt = select(allowance_history).where(*conditions).order_by(allowance_history.c.allowance_history_id.desc()).limit(per_page).offset(offset)
    async with consistent_snapshot(db) as conn:
        total, bounded = await count_bounded(conn, [bounded_count(select(allowance_history.c.allowance_history_id).where(*conditions))])
        rows = (await conn.execute(page_stmt)).mappings().all()
    return Page(items=tuple(AllowanceChange(**dict(row)) for row in rows), total=total, page=page, per_page=per_page, total_is_lower_bound=bounded)
