"""Staking: deposits into and withdrawals from the stake custody account, with interest compounded continuously at the currency's hourly rate and accrued, as its own interest execution, before every deposit, withdrawal and rate change apply and by the worker for stale stakes."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import MAX_EMAX, MIN_EMIN, ROUND_FLOOR, Context, Decimal, DivisionByZero, InvalidOperation, Overflow, localcontext
from enum import StrEnum
from typing import Any

from sqlalchemy import RowMapping, and_, delete, select, tuple_, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.core.clock import ensure_utc, resolve_now
from rapidwire.core.db import Db, consistent_snapshot, reader
from rapidwire.core.errors import CurrencyNotFound, InsufficientStake, InvalidArgument
from rapidwire.core.ledger import MAX_ATTEMPTS, CurrencyLockEscalation, LedgerTx, LedgerUsageError, OperationResult, is_retryable, retry_delay, run_operation
from rapidwire.core.paging import STAKES_PER_PAGE_DEFAULT, STAKES_PER_PAGE_MAX, Page, bounded_count, count_bounded, validate_paging
from rapidwire.core.types import ExecutionStatus, ExecutionType, TransferKind
from rapidwire.core.validation import require_storable_int, validate_positive_amount, validate_user_account_id
from rapidwire.db.tables import CARRY_SCALE, RATE_PPM_MAX, STAKE_CUSTODY_ACCOUNT_ID, currency, stake
from rapidwire.db.types import AMOUNT_MAX

__all__ = [
    "DEFAULT_ACCRUAL_BATCH_SIZE",
    "MICROSECONDS_PER_HOUR",
    "Accrual",
    "AccrualTrigger",
    "StakeAccrual",
    "StakeChanged",
    "StakeInfo",
    "StaleAccrualResult",
    "accrue_currency_stakes_in_tx",
    "accrue_stake_in_tx",
    "accrue_stale_stakes",
    "compute_accrual",
    "deposit",
    "deposit_in_tx",
    "elapsed_microseconds",
    "get_stake",
    "list_stakes",
    "projected_amount",
    "withdraw",
    "withdraw_in_tx",
]

_logger = logging.getLogger(__name__)

DEFAULT_ACCRUAL_BATCH_SIZE = 500
PPM_SCALE = 1_000_000
MICROSECONDS_PER_HOUR = 3_600_000_000
# Whole hours are compounded in exact integers, at most this many per step, so a stake left alone for decades costs a bounded number of cheap steps.
_EXACT_STEP_HOURS = 8760
_SATURATED_UNITS = (AMOUNT_MAX + 1) * CARRY_SCALE
_ONE_MICROSECOND = timedelta(microseconds=1)
_CONTEXT = Context(prec=60, rounding=ROUND_FLOOR, Emax=MAX_EMAX, Emin=MIN_EMIN, traps=[InvalidOperation, DivisionByZero, Overflow])


class AccrualTrigger(StrEnum):
    DEPOSIT = "deposit"
    WITHDRAW = "withdraw"
    RATE_APPLY = "rate_apply"
    WORKER = "worker"


@dataclass(frozen=True)
class StaleAccrualResult:
    """What accrue_stale_stakes() did: stakes credited with interest (one interest execution each), stakes whose accrual minted nothing (less than one minimal unit, or no room left under the supply cap; only their accrual time and carry changed, no execution), and stakes whose accrual raised (each failure is logged)."""

    credited: int
    without_interest: int
    failed: int


@dataclass(frozen=True)
class Accrual:
    """The result of compute_accrual(); a saturated result exceeded AMOUNT_MAX and reports amount AMOUNT_MAX + 1 with carry 0."""

    amount: int
    carry: int
    interest: int
    saturated: bool = False


def elapsed_microseconds(start: datetime, end: datetime) -> int:
    """Whole microseconds from start to end; negative spans are 0."""
    return max(0, (ensure_utc(end) - ensure_utc(start)) // _ONE_MICROSECOND)


def compute_accrual(amount: int, carry: int, rate_ppm: int, elapsed_us: int) -> Accrual:
    """Grow amount + carry / 10**18 by (1 + rate_ppm / 10**6) ** (elapsed_us / one hour) and split the floored result into whole units and carry.

    Up to a year of whole hours is compounded in exact integers, so with zero carry the amount equals floor(amount * (1 + r) ** hours); longer spans compound year by year and a sub-hour remainder uses a 60-digit decimal power, each step flooring at the carry scale.
    """
    if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
        raise ValueError("amount must be a non-negative int")
    if isinstance(carry, bool) or not isinstance(carry, int) or not 0 <= carry < CARRY_SCALE:
        raise ValueError("carry must be an int in [0, 10**18)")
    if isinstance(rate_ppm, bool) or not isinstance(rate_ppm, int) or not 0 <= rate_ppm <= RATE_PPM_MAX:
        raise ValueError(f"rate_ppm must be an int in [0, {RATE_PPM_MAX}]")
    if isinstance(elapsed_us, bool) or not isinstance(elapsed_us, int):
        raise ValueError("elapsed_us must be an int")
    if amount == 0 or rate_ppm == 0 or elapsed_us <= 0:
        return Accrual(amount=amount, carry=carry, interest=0)
    hours, rest_us = divmod(elapsed_us, MICROSECONDS_PER_HOUR)
    growth = PPM_SCALE + rate_ppm
    units = amount * CARRY_SCALE + carry
    full_step: tuple[int, int] | None = None
    while hours and units < _SATURATED_UNITS:
        step = min(hours, _EXACT_STEP_HOURS)
        if step == _EXACT_STEP_HOURS:
            if full_step is None:
                full_step = (pow(growth, step), pow(PPM_SCALE, step))
            numerator, denominator = full_step
        else:
            numerator, denominator = pow(growth, step), pow(PPM_SCALE, step)
        units = units * numerator // denominator
        hours -= step
    if rest_us and units < _SATURATED_UNITS:
        with localcontext(_CONTEXT):
            factor = (Decimal(growth) / Decimal(PPM_SCALE)) ** (Decimal(rest_us) / Decimal(MICROSECONDS_PER_HOUR))
            units = int((Decimal(units) * factor).to_integral_value(rounding=ROUND_FLOOR))
    if units >= _SATURATED_UNITS:
        return Accrual(amount=AMOUNT_MAX + 1, carry=0, interest=AMOUNT_MAX + 1 - amount, saturated=True)
    new_amount, new_carry = divmod(units, CARRY_SCALE)
    return Accrual(amount=new_amount, carry=new_carry, interest=new_amount - amount)


@dataclass(frozen=True)
class StakeAccrual:
    """What one accrual did to a stake; interest_execution_id is None when no interest was minted."""

    account_id: int
    currency_id: int
    old_amount: int
    new_amount: int
    interest: int
    interest_execution_id: int | None
    halted: bool


@dataclass(frozen=True)
class StakeChanged:
    """The result of a deposit or withdrawal: the moved amount, the stake afterwards (0 when the row was removed) and the interest accrued just before; replayed takes no part in equality."""

    execution_id: int
    account_id: int
    currency_id: int
    amount: int
    stake_amount: int
    transfer_id: int
    interest: int
    interest_execution_id: int | None
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "currency_id": self.currency_id,
            "amount": self.amount,
            "stake_amount": self.stake_amount,
            "transfer_id": self.transfer_id,
            "interest": self.interest,
            "interest_execution_id": self.interest_execution_id,
        }

    @classmethod
    def from_operation(cls, op: OperationResult) -> StakeChanged:
        interest_execution = op.output.get("interest_execution_id")
        return cls(
            execution_id=op.execution_id,
            account_id=op.int_field("account_id"),
            currency_id=op.int_field("currency_id"),
            amount=op.int_field("amount"),
            stake_amount=op.int_field("stake_amount"),
            transfer_id=op.int_field("transfer_id"),
            interest=op.int_field("interest"),
            interest_execution_id=int(interest_execution) if isinstance(interest_execution, str) else None,
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class StakeInfo:
    account_id: int
    currency_id: int
    symbol: str
    name: str
    amount: int
    carry: int
    hourly_rate_ppm: int
    interest_halted: bool
    last_accrued_at: datetime


async def _select_stake(conn: AsyncConnection, account_id: int, currency_id: int, *, for_update: bool) -> RowMapping | None:
    stmt = select(stake).where(stake.c.account_id == account_id, stake.c.currency_id == currency_id)
    if for_update:
        stmt = stmt.with_for_update()
    return (await conn.execute(stmt)).mappings().one_or_none()


class _CurrencyState:
    """The accrual-relevant fields of a locked currency row; halted is updated when an accrual hits the supply cap."""

    def __init__(self, row: RowMapping) -> None:
        self.currency_id: int = row["currency_id"]
        self.issuance_account_id: int = row["issuance_account_id"]
        self.rate_ppm: int = row["hourly_rate_ppm"]
        self.halted: bool = bool(row["interest_halted"])


async def _accrue_locked(tx: LedgerTx, state: _CurrencyState, row: RowMapping, trigger: AccrualTrigger, trigger_execution_id: int | None) -> StakeAccrual:
    """Accrue a stake row the caller has locked FOR UPDATE; interest beyond the supply cap is cut to what fits and marks the currency interest_halted.

    Interest is recorded as its own top-level interest execution with parent_execution_id None, and its input.trigger_execution_id links it to the triggering operation (None for the worker).
    """
    account_id, currency_id = row["account_id"], state.currency_id
    old_amount, old_carry, last = int(row["amount"]), int(row["carry"]), row["last_accrued_at"]
    accrued_at = max(tx.now, last)
    elapsed = elapsed_microseconds(last, tx.now)
    result = compute_accrual(old_amount, old_carry, 0 if state.halted else state.rate_ppm, elapsed)
    interest, new_carry, halted_now = result.interest, result.carry, False
    interest_execution_id: int | None = None
    if interest > 0:
        issuance = state.issuance_account_id
        balances = await tx.lock_balances([(issuance, currency_id), (STAKE_CUSTODY_ACCOUNT_ID, currency_id)])
        room = AMOUNT_MAX + balances[(issuance, currency_id)]
        if interest > room:
            lock = tx.currency_lock(currency_id)
            if lock == "share":
                raise CurrencyLockEscalation(currency_id)
            if lock != "update":
                raise LedgerUsageError(f"currency {currency_id} must be locked FOR UPDATE before an accrual can halt its interest")
            interest, new_carry, halted_now = max(0, room), 0, True
    new_amount = old_amount + interest
    if interest > 0:
        interest_execution_id = await tx.create_execution(
            type=ExecutionType.INTEREST,
            initiator_id=account_id,
            input={
                "trigger": trigger.value,
                "trigger_execution_id": trigger_execution_id,
                "currency_id": currency_id,
                "account_id": account_id,
                "rate_ppm": state.rate_ppm,
                "elapsed_microseconds": elapsed,
                "old_amount": old_amount,
                "new_amount": new_amount,
                "old_carry": old_carry,
                "new_carry": new_carry,
            },
        )
        transfer_id = await tx.move(interest_execution_id, currency_id, state.issuance_account_id, STAKE_CUSTODY_ACCOUNT_ID, interest, TransferKind.INTEREST)
        await tx.finish_execution(interest_execution_id, status=ExecutionStatus.SUCCESS, output={"transfer_id": transfer_id, "interest": interest, "halted": halted_now})
    if halted_now:
        await tx.conn.execute(update(currency).where(currency.c.currency_id == currency_id).values(interest_halted=True))
        state.halted = True
    await tx.conn.execute(update(stake).where(stake.c.account_id == account_id, stake.c.currency_id == currency_id).values(amount=new_amount, carry=new_carry, last_accrued_at=accrued_at))
    return StakeAccrual(
        account_id=account_id,
        currency_id=currency_id,
        old_amount=old_amount,
        new_amount=new_amount,
        interest=interest,
        interest_execution_id=interest_execution_id,
        halted=halted_now,
    )


async def accrue_stake_in_tx(tx: LedgerTx, execution_id: int | None, *, currency_row: RowMapping, account_id: int, trigger: AccrualTrigger) -> StakeAccrual | None:
    """Accrue one stake up to tx.now under a currency row the caller has locked, locking the stake row FOR UPDATE; execution_id is the triggering execution (None for the worker), recorded as input.trigger_execution_id of the interest execution.

    Returns None when there is no stake; reaching the supply cap needs the currency FOR UPDATE, and under FOR SHARE it raises CurrencyLockEscalation.
    """
    row = await _select_stake(tx.conn, account_id, currency_row["currency_id"], for_update=True)
    if row is None:
        return None
    return await _accrue_locked(tx, _CurrencyState(currency_row), row, trigger, execution_id)


async def accrue_currency_stakes_in_tx(tx: LedgerTx, execution_id: int | None, *, currency_id: int, trigger: AccrualTrigger) -> list[StakeAccrual]:
    """Accrue every stake of the currency up to tx.now at its current rate as triggered by execution_id; locks the currency row FOR UPDATE, then all its stake rows.

    Stakes accrue in ascending account order, so when the supply cap is reached the stake that hits it gets what fits and the later ones get no interest.
    """
    currency_row = (await tx.lock_currencies([currency_id], for_update=True))[currency_id]
    rows = (await tx.conn.execute(select(stake).where(stake.c.currency_id == currency_id).order_by(stake.c.account_id).with_for_update())).mappings().all()
    if not rows:
        return []
    state = _CurrencyState(currency_row)
    await tx.lock_balances([(state.issuance_account_id, currency_id), (STAKE_CUSTODY_ACCOUNT_ID, currency_id)])
    return [await _accrue_locked(tx, state, row, trigger, execution_id) for row in rows]


async def _lock_for_stake_change(tx: LedgerTx, account_id: int, currency_id: int, *, create: bool) -> tuple[RowMapping, RowMapping | None]:
    """Lock the currency (FOR SHARE unless tx.exclusive_currencies names it), the stake row and the user, issuance and custody balances in the ledger's order; with create, a missing stake row is first inserted as a zero placeholder."""
    validate_user_account_id(account_id)
    currency_row = (await tx.lock_currencies([currency_id]))[currency_id]
    if create:
        await tx.require_accounts([account_id])
        placeholder = mysql_insert(stake).values(account_id=account_id, currency_id=currency_id, amount=0, carry=0, last_accrued_at=tx.now)
        await tx.conn.execute(placeholder.on_duplicate_key_update(account_id=placeholder.inserted.account_id))
    row = await _select_stake(tx.conn, account_id, currency_id, for_update=True)
    await tx.lock_balances([(account_id, currency_id), (currency_row["issuance_account_id"], currency_id), (STAKE_CUSTODY_ACCOUNT_ID, currency_id)])
    return currency_row, row


def _changed(execution_id: int, *, account_id: int, currency_id: int, amount: int, stake_amount: int, transfer_id: int, accrual: StakeAccrual) -> StakeChanged:
    return StakeChanged(
        execution_id=execution_id,
        account_id=account_id,
        currency_id=currency_id,
        amount=amount,
        stake_amount=stake_amount,
        transfer_id=transfer_id,
        interest=accrual.interest,
        interest_execution_id=accrual.interest_execution_id,
    )


async def deposit_in_tx(tx: LedgerTx, execution_id: int, *, account_id: int, currency_id: int, amount: int) -> StakeChanged:
    """Accrue the existing stake, then move amount from the user's wallet into stake custody and add it to the stake.

    A first deposit inserts the stake row as a zero placeholder before locking balances; a failed deposit's savepoint rollback removes it again.
    An accrual reaching the supply cap while the currency is held FOR SHARE raises CurrencyLockEscalation, which run_operation() answers by rerunning the transaction with the currency exclusive.
    """
    validate_positive_amount(amount)
    currency_row, row = await _lock_for_stake_change(tx, account_id, currency_id, create=True)
    if row is None:
        raise LedgerUsageError(f"stake placeholder for account {account_id} in currency {currency_id} is missing")
    accrual = await _accrue_locked(tx, _CurrencyState(currency_row), row, AccrualTrigger.DEPOSIT, execution_id)
    transfer_id = await tx.move(execution_id, currency_id, account_id, STAKE_CUSTODY_ACCOUNT_ID, amount, TransferKind.PRINCIPAL)
    stake_amount = accrual.new_amount + amount
    await tx.conn.execute(update(stake).where(stake.c.account_id == account_id, stake.c.currency_id == currency_id).values(amount=stake_amount))
    return _changed(execution_id, account_id=account_id, currency_id=currency_id, amount=amount, stake_amount=stake_amount, transfer_id=transfer_id, accrual=accrual)


async def withdraw_in_tx(tx: LedgerTx, execution_id: int, *, account_id: int, currency_id: int, amount: int) -> StakeChanged:
    """Accrue the stake, then move amount from stake custody back to the user's wallet; a stake reaching 0 is removed together with its carry.

    Locking and CurrencyLockEscalation are as in deposit_in_tx(), and withdrawals keep working after the currency's interest is halted.
    """
    validate_positive_amount(amount)
    currency_row, row = await _lock_for_stake_change(tx, account_id, currency_id, create=False)
    accrual = None if row is None else await _accrue_locked(tx, _CurrencyState(currency_row), row, AccrualTrigger.WITHDRAW, execution_id)
    if accrual is None or accrual.new_amount < amount:
        raise InsufficientStake("Withdrawal amount exceeds staked balance.")
    transfer_id = await tx.move(execution_id, currency_id, STAKE_CUSTODY_ACCOUNT_ID, account_id, amount, TransferKind.PRINCIPAL)
    stake_amount = accrual.new_amount - amount
    where = and_(stake.c.account_id == account_id, stake.c.currency_id == currency_id)
    if stake_amount == 0:
        await tx.conn.execute(delete(stake).where(where))
    else:
        await tx.conn.execute(update(stake).where(where).values(amount=stake_amount))
    return _changed(execution_id, account_id=account_id, currency_id=currency_id, amount=amount, stake_amount=stake_amount, transfer_id=transfer_id, accrual=accrual)


async def deposit(engine: AsyncEngine, *, account_id: int, currency_id: int, amount: int, now: datetime | None = None, idempotency_key: str | None = None) -> StakeChanged:
    """Stake amount of the currency from the account's wallet as one stake_deposit execution."""
    require_storable_int(amount)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await deposit_in_tx(tx, execution_id, account_id=account_id, currency_id=currency_id, amount=amount)).to_output()

    op = await run_operation(
        engine, type=ExecutionType.STAKE_DEPOSIT, initiator_id=account_id, input={"currency_id": currency_id, "amount": amount}, body=body, now=now, idempotency_key=idempotency_key
    )
    return StakeChanged.from_operation(op)


async def withdraw(engine: AsyncEngine, *, account_id: int, currency_id: int, amount: int, now: datetime | None = None, idempotency_key: str | None = None) -> StakeChanged:
    """Withdraw amount of the account's stake back to its wallet as one stake_withdraw execution; raises InsufficientStake when the stake is smaller."""
    require_storable_int(amount)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await withdraw_in_tx(tx, execution_id, account_id=account_id, currency_id=currency_id, amount=amount)).to_output()

    op = await run_operation(
        engine, type=ExecutionType.STAKE_WITHDRAW, initiator_id=account_id, input={"currency_id": currency_id, "amount": amount}, body=body, now=now, idempotency_key=idempotency_key
    )
    return StakeChanged.from_operation(op)


async def _accrue_one_stale(engine: AsyncEngine, account_id: int, currency_id: int, older_than: timedelta, now: datetime | None) -> StakeAccrual | None:
    """Accrue one stake in its own transaction when it is still stale under its lock, retrying deadlocks and rerunning with the currency exclusive on CurrencyLockEscalation; returns the accrual, or None when the stake was skipped."""
    attempt = 0
    exclusive: set[int] = set()
    while True:
        at = resolve_now(now)
        try:
            async with engine.begin() as conn:
                tx = LedgerTx(conn, at, exclusive_currencies=exclusive)
                try:
                    currency_row = (await tx.lock_currencies([currency_id]))[currency_id]
                except CurrencyNotFound:
                    return None
                if currency_row["interest_halted"] or currency_row["hourly_rate_ppm"] == 0:
                    return None
                row = await _select_stake(conn, account_id, currency_id, for_update=True)
                if row is None or row["last_accrued_at"] > at - older_than:
                    return None
                return await _accrue_locked(tx, _CurrencyState(currency_row), row, AccrualTrigger.WORKER, None)
        except CurrencyLockEscalation as escalation:
            if escalation.currency_id in exclusive:
                raise
            exclusive.add(escalation.currency_id)
        except DBAPIError as exc:
            attempt += 1
            if not is_retryable(exc) or attempt >= MAX_ATTEMPTS:
                raise
            await asyncio.sleep(retry_delay(attempt))


async def accrue_stale_stakes(engine: AsyncEngine, *, older_than: timedelta, now: datetime | None = None, batch_size: int = DEFAULT_ACCRUAL_BATCH_SIZE) -> StaleAccrualResult:
    """Accrue every stake not accrued for at least older_than, each in its own transaction; with now None each stake uses the clock's time when it is processed.

    A stake whose accrual mints nothing is counted as without_interest: its interest came to less than one minimal unit, or the supply was already at the cap (which halts the currency), and no interest execution is recorded.
    A stake whose accrual raises is logged with its IDs, counted as failed and skipped, and the run continues with the next stake.
    Stakes of deleted, rate-0 and halted currencies are skipped because accruing them mints nothing; their timestamps advance on the next deposit, withdrawal or rate change apply, which accrue first.
    """
    if older_than < timedelta(0):
        raise InvalidArgument("older_than must not be negative.")
    if batch_size <= 0:
        raise InvalidArgument("batch_size must be positive.")
    cutoff = resolve_now(now) - older_than
    credited = without_interest = failed = 0
    after: tuple[int, int] | None = None
    while True:
        stmt = (
            select(stake.c.currency_id, stake.c.account_id)
            .join(currency, currency.c.currency_id == stake.c.currency_id)
            .where(stake.c.last_accrued_at <= cutoff, currency.c.deleted_at.is_(None), currency.c.hourly_rate_ppm > 0, currency.c.interest_halted.is_(False))
            .order_by(stake.c.currency_id, stake.c.account_id)
            .limit(batch_size)
        )
        if after is not None:
            stmt = stmt.where(tuple_(stake.c.currency_id, stake.c.account_id) > tuple_(*after))
        async with engine.connect() as conn:
            keys = [(row.currency_id, row.account_id) for row in await conn.execute(stmt)]
        for currency_id, account_id in keys:
            try:
                accrual = await _accrue_one_stale(engine, account_id, currency_id, older_than, now)
            except Exception:
                failed += 1
                _logger.exception("stake accrual failed for account %d in currency %d", account_id, currency_id)
                continue
            if accrual is None:
                continue
            if accrual.interest_execution_id is not None:
                credited += 1
            else:
                without_interest += 1
        if len(keys) < batch_size:
            return StaleAccrualResult(credited=credited, without_interest=without_interest, failed=failed)
        after = keys[-1]


def _stake_query() -> Any:
    return select(
        stake.c.account_id,
        stake.c.currency_id,
        currency.c.symbol,
        currency.c.name,
        stake.c.amount,
        stake.c.carry,
        currency.c.hourly_rate_ppm,
        currency.c.interest_halted,
        stake.c.last_accrued_at,
    ).join(currency, currency.c.currency_id == stake.c.currency_id)


def _stake_info(row: Any) -> StakeInfo:
    return StakeInfo(
        account_id=row.account_id,
        currency_id=row.currency_id,
        symbol=row.symbol,
        name=row.name,
        amount=int(row.amount),
        carry=int(row.carry),
        hourly_rate_ppm=row.hourly_rate_ppm,
        interest_halted=bool(row.interest_halted),
        last_accrued_at=row.last_accrued_at,
    )


async def get_stake(db: Db, account_id: int, currency_id: int) -> StakeInfo | None:
    """The stake of the account in the currency as last accrued, or None."""
    async with reader(db) as conn:
        row = (await conn.execute(_stake_query().where(stake.c.account_id == account_id, stake.c.currency_id == currency_id))).one_or_none()
    return None if row is None else _stake_info(row)


async def list_stakes(db: Db, account_id: int, *, page: int = 1, per_page: int = STAKES_PER_PAGE_DEFAULT) -> Page[StakeInfo]:
    """One page of the account's stakes ordered by currency ID; paging is checked by validate_paging() and a page past the end is empty."""
    offset = validate_paging(page, per_page, STAKES_PER_PAGE_MAX)
    page_stmt = _stake_query().where(stake.c.account_id == account_id).order_by(stake.c.currency_id).limit(per_page).offset(offset)
    async with consistent_snapshot(db) as conn:
        total, bounded = await count_bounded(conn, [bounded_count(select(stake.c.currency_id).where(stake.c.account_id == account_id))])
        items = tuple(_stake_info(row) for row in await conn.execute(page_stmt))
    return Page(items=items, total=total, page=page, per_page=per_page, total_is_lower_bound=bounded)


def projected_amount(info: StakeInfo, now: datetime | None = None, *, supply: int | None = None) -> int:
    """The stake amount an accrual at now would produce, computed without writing; given the currency's supply, the interest is capped at the supply headroom as an accrual would cap it, and without it the result is capped at AMOUNT_MAX."""
    if info.interest_halted:
        return info.amount
    result = compute_accrual(info.amount, info.carry, info.hourly_rate_ppm, elapsed_microseconds(info.last_accrued_at, resolve_now(now)))
    if supply is None:
        return int(min(result.amount, AMOUNT_MAX))
    headroom: int = max(0, AMOUNT_MAX - supply)
    return info.amount + min(result.interest, headroom)
