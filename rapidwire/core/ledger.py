"""The ledger primitive and the only code that writes balance and transfer rows: run_operation() runs one execution in one transaction, run_child() nests one inside an open LedgerTx, and LedgerTx.lock_currencies(), lock_balances() and move() are the building blocks, with the lock order stated on those methods."""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any

from sqlalchemy import RowMapping, insert, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from rapidwire.core import accounts
from rapidwire.core.accounts import AccountInfo
from rapidwire.core.clock import ensure_utc, resolve_now
from rapidwire.core.errors import (
    AmountOverflow,
    CurrencyNotFound,
    DomainError,
    IdempotencyConflict,
    InsufficientBalance,
    InvalidAmount,
    InvalidArgument,
    error_from_code,
)
from rapidwire.core.types import AccountKind, ExecutionStatus, ExecutionType, JsonObject, to_json_object
from rapidwire.core.validation import BIGINT_MAX, is_user_account_id, validate_user_account_id
from rapidwire.db.tables import IDEMPOTENCY_KEY_MAX, balance, currency, execution, transfer
from rapidwire.db.types import AMOUNT_MAX, AMOUNT_MIN

__all__ = [
    "CONTRACT_MANAGEMENT_TYPES",
    "DUPLICATE_KEY_ERROR",
    "IDEMPOTENCY_KEY_MAX",
    "MAX_ATTEMPTS",
    "MAX_RERUNS",
    "RETRY_BASE_DELAY_SECONDS",
    "BalanceKey",
    "Before",
    "Body",
    "CurrencyLockEscalation",
    "FailureHook",
    "Inspect",
    "LedgerTx",
    "LedgerUsageError",
    "OperationResult",
    "Prepare",
    "RerunTransaction",
    "WriteGuard",
    "execution_contract_id",
    "failure_output",
    "is_replayed_error",
    "is_retryable",
    "mysql_error_code",
    "retry_delay",
    "run_child",
    "run_operation",
    "set_failure_output",
    "write_guard",
]

MAX_ATTEMPTS = 5
MAX_RERUNS = 2
RETRY_BASE_DELAY_SECONDS = 0.02
RETRYABLE_MYSQL_ERRORS = frozenset({1205, 1213})
DUPLICATE_KEY_ERROR = 1062
_KEY_RACE_ATTEMPTS = 3
_RANDOM = secrets.SystemRandom()

type BalanceKey = tuple[int, int]
type Body = Callable[[LedgerTx, int], Awaitable[Mapping[str, Any]]]
type Prepare = Callable[[LedgerTx, int], Awaitable[None]]
type FailureHook = Callable[[LedgerTx, int, DomainError], Awaitable[None]]
type Before = Callable[[], Awaitable[None]]
type Inspect = Callable[[LedgerTx, int], Awaitable[None]]
type WriteGuard = Callable[[AsyncConnection, int], Awaitable[None]]
type _Snapshot = tuple[dict[BalanceKey, int], dict[BalanceKey, bool], dict[int, AccountInfo]]

_write_guard: ContextVar[WriteGuard | None] = ContextVar("rapidwire_write_guard", default=None)

# Contract management executions, which act on the contract of their initiator's account.
CONTRACT_MANAGEMENT_TYPES = frozenset({ExecutionType.CONTRACT_UPDATE, ExecutionType.CONTRACT_LOCK, ExecutionType.CONTRACT_PAUSE, ExecutionType.CONTRACT_RESUME, ExecutionType.CONTRACT_DELETE})


def execution_contract_id(execution_type: str, initiator_id: int, execution_input: Mapping[str, Any]) -> int | None:
    """The execution.contract_id of an execution: the initiator for a contract management type, execution_input["contract_id"] for a contract_call when it is a decimal string (or int) from 1 to BIGINT_MAX, else None; migration 0007 fills existing rows and the invariant verifier checks them by the same rule."""
    if execution_type in CONTRACT_MANAGEMENT_TYPES:
        return initiator_id
    if execution_type != ExecutionType.CONTRACT_CALL:
        return None
    value = execution_input.get("contract_id")
    if isinstance(value, int) and not isinstance(value, bool):
        number = value
    elif isinstance(value, str) and value.isascii() and value.isdigit() and not value.startswith("0") and len(value) <= 19:
        number = int(value)
    else:
        return None
    return number if 1 <= number <= BIGINT_MAX else None


@contextmanager
def write_guard(guard: WriteGuard) -> Iterator[None]:
    """Make every run_operation() started in this context await guard(conn, initiator_id) inside each transaction attempt, before any account or execution row is written and never for an idempotent replay.

    An exception from guard rolls the attempt back with nothing recorded and propagates; the API uses it to re-check the caller's token under a row lock in the write's own transaction.
    """
    reset = _write_guard.set(guard)
    try:
        yield
    finally:
        _write_guard.reset(reset)


class LedgerUsageError(RuntimeError):
    """The ledger was called incorrectly (for example move() on a balance row that was not locked); this is a programming error, not a domain failure."""


class RerunTransaction(Exception):
    """The transaction must be rolled back and run again from the start with the currencies in exclusive taken FOR UPDATE on first lock and those in pre_lock locked before prepare runs.

    It is not a DomainError, so neither a savepoint nor run_child() contains it, and any code between the raise and run_operation() (a contract runtime, a wrapper) must re-raise it unchanged; run_operation() reruns at most MAX_RERUNS times and sets LedgerTx.rerun_allowed to False on the last run, where the body reports a failure instead.
    """

    def __init__(self, message: str, *, exclusive: Iterable[int] = (), pre_lock: Iterable[int] = ()) -> None:
        super().__init__(message)
        self.exclusive = frozenset(exclusive)
        self.pre_lock = frozenset(pre_lock)


class CurrencyLockEscalation(RerunTransaction):
    """The transaction holds currency_id FOR SHARE but needs it FOR UPDATE, as when a stake accrual reaching the supply cap must mark the currency interest_halted; each escalation adds a new exclusive currency, so it does not count against MAX_RERUNS, and code that drives a LedgerTx itself reruns the same way."""

    def __init__(self, currency_id: int) -> None:
        super().__init__(f"currency {currency_id} must be locked FOR UPDATE", exclusive={currency_id})
        self.currency_id = currency_id


@dataclass(frozen=True)
class OperationResult:
    """The outcome of a successful execution; output is the stored JSON object, and replayed is True when an idempotency key returned an earlier result."""

    execution_id: int
    status: ExecutionStatus
    output: JsonObject
    replayed: bool = False

    def int_field(self, key: str) -> int:
        """An integer stored in output under key (integers are stored as decimal strings)."""
        value = self.output[key]
        if not isinstance(value, str):
            raise KeyError(f"output field {key!r} is not an integer string")
        return int(value)


def mysql_error_code(exc: BaseException) -> int | None:
    """The MySQL error number carried by a DBAPIError, or None."""
    orig = getattr(exc, "orig", None)
    args = getattr(orig, "args", ())
    if args and isinstance(args[0], int):
        return args[0]
    return None


def is_retryable(exc: BaseException) -> bool:
    """True for a deadlock or lock wait timeout, after which the whole transaction must be run again."""
    return isinstance(exc, DBAPIError) and mysql_error_code(exc) in RETRYABLE_MYSQL_ERRORS


def retry_delay(attempt: int) -> float:
    """Seconds to wait before retry number attempt (1-based): linear backoff with random jitter so colliding transactions do not retry in lockstep."""
    return RETRY_BASE_DELAY_SECONDS * attempt * (0.5 + _RANDOM.random())


class _Savepoint:
    """A SAVEPOINT that is released on success, rolled back on DomainError together with the LedgerTx caches, and left to the outer rollback on any other exception."""

    def __init__(self, tx: LedgerTx) -> None:
        self._tx = tx
        self._transaction: AsyncTransaction | None = None
        self._snapshot: _Snapshot | None = None

    async def __aenter__(self) -> None:
        self._snapshot = self._tx._snapshot()
        self._transaction = await self._tx.conn.begin_nested()

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> bool:
        if self._transaction is None or self._snapshot is None:
            raise LedgerUsageError("savepoint was not entered")
        if exc is None:
            await self._transaction.commit()
        elif isinstance(exc, DomainError):
            await self._transaction.rollback()
            self._tx._restore(self._snapshot)
        return False


class LedgerTx:
    """Ledger operations bound to one open transaction and one fixed time; it tracks the currency and balance rows it has locked, and locks the currencies in exclusive_currencies FOR UPDATE whenever it locks them.

    late_currency_locks lets the body lock new currencies after balance rows outside run_child(), and rerun_allowed says whether raising RerunTransaction will rerun the transaction.
    """

    def __init__(self, conn: AsyncConnection, now: datetime, *, exclusive_currencies: Iterable[int] = (), late_currency_locks: bool = False, rerun_allowed: bool = False) -> None:
        self.conn = conn
        self.now = ensure_utc(now)
        self.exclusive_currencies = frozenset(exclusive_currencies)
        self.late_currency_locks = late_currency_locks
        self.rerun_allowed = rerun_allowed
        self._balances: dict[BalanceKey, int] = {}
        self._allow_negative: dict[BalanceKey, bool] = {}
        self._accounts: dict[int, AccountInfo] = {}
        # Row locks outlive a savepoint rollback, so these lock records are not restored with the caches; _currency_locks maps currency_id to True for FOR UPDATE and False for FOR SHARE.
        self._currency_locks: dict[int, bool] = {}
        self._balance_locked = False
        self._child_depth = 0
        self._in_failure_hook = False

    def _check_locking_allowed(self) -> None:
        if self._in_failure_hook:
            raise LedgerUsageError("an on_failure hook may only move balances locked in prepare; it cannot lock new rows")

    def _snapshot(self) -> _Snapshot:
        return dict(self._balances), dict(self._allow_negative), dict(self._accounts)

    def _restore(self, snapshot: _Snapshot) -> None:
        # Rows locked after the savepoint stay locked in InnoDB, but rows inserted after it are gone, so they are forgotten and locked again on next use.
        balances, allow_negative, infos = snapshot
        self._balances, self._allow_negative, self._accounts = dict(balances), dict(allow_negative), dict(infos)

    def savepoint(self) -> _Savepoint:
        """An async context manager running its block in a SAVEPOINT that a DomainError rolls back (the error still propagates)."""
        return _Savepoint(self)

    async def create_execution(
        self,
        *,
        type: str,
        initiator_id: int,
        input: Mapping[str, Any],
        parent_execution_id: int | None = None,
        idempotency_key: str | None = None,
        contract_function: str | None = None,
        status: ExecutionStatus = ExecutionStatus.SUCCESS,
        output: Mapping[str, Any] | None = None,
    ) -> int:
        """Insert an execution row and return its ID; the initiator account must exist (see require_accounts()), and contract_id is derived by execution_contract_id()."""
        stored_input = to_json_object(input)
        values: dict[str, Any] = {
            "type": str(type),
            "initiator_id": initiator_id,
            "parent_execution_id": parent_execution_id,
            "status": status.value,
            "input": stored_input,
            "output": to_json_object(output) if output is not None else None,
            "idempotency_key": idempotency_key,
            "contract_function": contract_function,
            "created_at": self.now,
            "contract_id": execution_contract_id(str(type), initiator_id, stored_input),
        }
        result = await self.conn.execute(insert(execution).values(**values))
        key = result.inserted_primary_key
        if key is None:
            raise LedgerUsageError("execution insert returned no primary key")
        return int(key[0])

    async def finish_execution(self, execution_id: int, *, status: ExecutionStatus, output: Mapping[str, Any] | None) -> None:
        """Set the final status and output of an execution."""
        stored = to_json_object(output) if output is not None else None
        await self.conn.execute(update(execution).where(execution.c.execution_id == execution_id).values(status=status.value, output=stored))

    async def lock_currencies(self, currency_ids: Iterable[int], *, for_update: bool = False, include_deleted: bool = False) -> dict[int, RowMapping]:
        """Lock currency rows in ascending ID order and return them; currencies are the first rows any transaction locks, before domain rows (claim, allowance, stake, then later pools and contracts, ascending key within each table) and balance rows.

        Ordinary operations lock FOR SHARE and administrative or cascading ones FOR UPDATE, as do currencies in exclusive_currencies; upgrading a row this transaction already holds FOR SHARE raises LedgerUsageError, so request FOR UPDATE on the first call.
        Once a balance row is locked, locking a further currency raises LedgerUsageError except inside run_child() or with late_currency_locks: contract execution is the one allowed exception to the ascending, currency-before-balance order, and it relies on the deadlock retry of the whole transaction.
        A missing currency, or a deleted one unless include_deleted, raises CurrencyNotFound.
        """
        self._check_locking_allowed()
        rows: dict[int, RowMapping] = {}
        for currency_id in sorted(set(currency_ids)):
            held = self._currency_locks.get(currency_id)
            if held is None and self._balance_locked and self._child_depth == 0 and not self.late_currency_locks:
                raise LedgerUsageError(f"currency {currency_id} cannot be locked after balance rows outside run_child()")
            if held is False and for_update:
                raise LedgerUsageError(f"currency {currency_id} is already locked FOR SHARE in this transaction; lock it FOR UPDATE on the first call")
            exclusive = for_update or held is True or currency_id in self.exclusive_currencies
            stmt = select(currency).where(currency.c.currency_id == currency_id).with_for_update(read=not exclusive)
            row = (await self.conn.execute(stmt)).mappings().one_or_none()
            if row is None or (row["deleted_at"] is not None and not include_deleted):
                raise CurrencyNotFound(f"Currency {currency_id} was not found.")
            self._currency_locks[currency_id] = exclusive
            rows[currency_id] = row
        return rows

    def currency_lock(self, currency_id: int) -> str | None:
        """How this transaction holds the currency row: "update", "share", or None when it has not locked it."""
        held = self._currency_locks.get(currency_id)
        if held is None:
            return None
        return "update" if held else "share"

    async def require_accounts(self, account_ids: Iterable[int]) -> dict[int, AccountKind]:
        """Make sure the accounts exist (users are created lazily, other system accounts must exist) and return their kinds."""
        return {account_id: info.kind for account_id, info in (await self._require_infos(account_ids)).items()}

    async def _require_infos(self, account_ids: Iterable[int]) -> dict[int, AccountInfo]:
        ids = set(account_ids)
        missing = ids - self._accounts.keys()
        if missing:
            self._accounts.update(await accounts.require_account_infos(self.conn, missing, now=self.now))
        return {account_id: self._accounts[account_id] for account_id in ids}

    async def lock_balances(self, keys: Iterable[BalanceKey]) -> dict[BalanceKey, int]:
        """Lock (account_id, currency_id) balance rows FOR UPDATE in ascending order, after their currencies, creating missing rows, and return the amounts; lock every row an operation needs in one call when the set is known in advance.

        The currency of every key must already be locked by lock_currencies(), and an issuance account may only be used for its own currency, which is the only balance allowed to go negative; either violation raises LedgerUsageError.
        An on_failure hook may lock balances only under late_currency_locks, and only of currencies this transaction already holds, as a failed contract call does to collect its gas into the issuance account.
        """
        wanted = sorted(set(keys))
        if not (self._in_failure_hook and self.late_currency_locks):
            self._check_locking_allowed()
        for _, currency_id in wanted:
            if currency_id not in self._currency_locks:
                raise LedgerUsageError(f"currency {currency_id} must be locked with lock_currencies() before its balances")
        infos = await self._require_infos({account_id for account_id, _ in wanted})
        for account_id, currency_id in wanted:
            info = infos[account_id]
            if info.kind is AccountKind.ISSUANCE and info.currency_id != currency_id:
                raise LedgerUsageError(f"issuance account {account_id} belongs to currency {info.currency_id}, not {currency_id}")
        if wanted:
            self._balance_locked = True
        for key in wanted:
            if key in self._balances:
                continue
            account_id, currency_id = key
            row = await self._select_balance(account_id, currency_id)
            if row is None:
                allow_negative = infos[account_id].kind is AccountKind.ISSUANCE
                stmt = mysql_insert(balance).values(account_id=account_id, currency_id=currency_id, amount=0, allow_negative=allow_negative)
                await self.conn.execute(stmt.on_duplicate_key_update(account_id=stmt.inserted.account_id))
                row = await self._select_balance(account_id, currency_id)
                if row is None:
                    raise LedgerUsageError(f"balance row {key} vanished after insert")
            self._balances[key] = row[0]
            self._allow_negative[key] = row[1]
        return {key: self._balances[key] for key in wanted}

    async def _select_balance(self, account_id: int, currency_id: int) -> tuple[int, bool] | None:
        stmt = select(balance.c.amount, balance.c.allow_negative).where(balance.c.account_id == account_id, balance.c.currency_id == currency_id).with_for_update()
        row = (await self.conn.execute(stmt)).one_or_none()
        return None if row is None else (row.amount, bool(row.allow_negative))

    def is_locked(self, account_id: int, currency_id: int) -> bool:
        return (account_id, currency_id) in self._balances

    def balance(self, account_id: int, currency_id: int) -> int:
        """The current amount of a balance row locked by this transaction."""
        key = (account_id, currency_id)
        if key not in self._balances:
            raise LedgerUsageError(f"balance {key} was not locked by this transaction")
        return self._balances[key]

    async def move(
        self,
        execution_id: int,
        currency_id: int,
        from_account: int,
        to_account: int,
        amount: int,
        kind: str,
    ) -> int:
        """Move amount between two balance rows locked by this transaction and record the transfer row, returning its ID; raises InvalidAmount, InvalidArgument, InsufficientBalance or AmountOverflow before writing anything."""
        if isinstance(amount, bool) or not isinstance(amount, int) or amount <= 0:
            raise InvalidAmount()
        if amount > AMOUNT_MAX:
            raise AmountOverflow()
        if from_account == to_account:
            raise InvalidArgument("The source and destination accounts must differ.")
        if currency_id not in self._currency_locks:
            raise LedgerUsageError(f"currency {currency_id} was not locked by this transaction")
        from_key, to_key = (from_account, currency_id), (to_account, currency_id)
        for key in (from_key, to_key):
            if key not in self._balances:
                raise LedgerUsageError(f"balance {key} was not locked by this transaction")
        new_from = self._balances[from_key] - amount
        new_to = self._balances[to_key] + amount
        if new_from < 0 and not self._allow_negative[from_key]:
            raise InsufficientBalance()
        if new_from < AMOUNT_MIN or new_to > AMOUNT_MAX:
            raise AmountOverflow()
        for (account_id, cur), value in ((from_key, new_from), (to_key, new_to)):
            await self.conn.execute(update(balance).where(balance.c.account_id == account_id, balance.c.currency_id == cur).values(amount=value))
        values: dict[str, Any] = {
            "execution_id": execution_id,
            "currency_id": currency_id,
            "from_account": from_account,
            "to_account": to_account,
            "amount": amount,
            "kind": str(kind),
            "created_at": self.now,
        }
        result = await self.conn.execute(insert(transfer).values(**values))
        self._balances[from_key] = new_from
        self._balances[to_key] = new_to
        inserted = result.inserted_primary_key
        if inserted is None:
            raise LedgerUsageError("transfer insert returned no primary key")
        return int(inserted[0])


_OUTPUT_FIELDS_ATTRIBUTE = "_ledger_output_fields"
_REPLAYED_ATTRIBUTE = "_ledger_replayed"


def set_failure_output(error: DomainError, fields: Mapping[str, Any]) -> None:
    """Attach fields that the output of the execution error fails is to record beside "error" (a failed contract call stores the gas it used and the random values it drew); the key "error" itself cannot be set this way."""
    if "error" in fields:
        raise LedgerUsageError('the "error" field of a failed execution is written by the ledger')
    setattr(error, _OUTPUT_FIELDS_ATTRIBUTE, dict(fields))


def failure_output(error: DomainError) -> dict[str, Any]:
    """The fields attached to error by set_failure_output(), empty when there are none; an error re-raised by an idempotent replay carries the stored fields beside "error" in their stored JSON form."""
    fields = getattr(error, _OUTPUT_FIELDS_ATTRIBUTE, None)
    return dict(fields) if isinstance(fields, dict) else {}


def is_replayed_error(error: BaseException) -> bool:
    """True when error was rebuilt from a failed execution stored under a repeated idempotency key rather than raised by running the operation."""
    return getattr(error, _REPLAYED_ATTRIBUTE, False) is True


def _error_output(error: DomainError) -> dict[str, Any]:
    details: dict[str, Any] = {"code": error.code, "message": error.message}
    if error.child is not None:
        details["child"] = dict(error.child)
    return {**failure_output(error), "error": details}


async def _record_failure(tx: LedgerTx, execution_id: int, error: DomainError, on_failure: FailureHook | None) -> None:
    await tx.finish_execution(execution_id, status=error.status, output=_error_output(error))
    error.execution_id = execution_id
    if on_failure is None:
        return
    tx._in_failure_hook = True
    try:
        await on_failure(tx, execution_id, error)
    finally:
        tx._in_failure_hook = False


async def _run_body(tx: LedgerTx, execution_id: int, body: Body, prepare: Prepare | None, on_failure: FailureHook | None) -> JsonObject | DomainError:
    """Run prepare before and body inside a savepoint, so rows prepare locks (a fee payer and the issuance account) stay locked when the body fails, and on_failure may move only balances locked there.

    A DomainError from the body rolls back its writes, records the execution as failed (reverted for RevertError) with output {"error": {"code", "message"}} and on_failure's writes, and is returned; errors from prepare and on_failure propagate and roll everything back.
    """
    if prepare is not None:
        await prepare(tx, execution_id)
    try:
        async with tx.savepoint():
            output = to_json_object(await body(tx, execution_id))
    except DomainError as error:
        await _record_failure(tx, execution_id, error, on_failure)
        return error
    await tx.finish_execution(execution_id, status=ExecutionStatus.SUCCESS, output=output)
    return output


async def _create_accounts(tx: LedgerTx, initiator_id: int, user_accounts: Iterable[int]) -> None:
    """Create the initiator and the valid user account IDs among user_accounts together in ascending ID order before the execution row, so two operations creating the same new accounts cannot deadlock on them.

    These rows persist even when the body fails, and values that are not user account IDs are skipped so the body can reject them.
    """
    await tx.require_accounts({initiator_id, *(account_id for account_id in user_accounts if is_user_account_id(account_id))})


class _KeyTaken(Exception):
    """Another transaction committed an execution with the same idempotency key while this one was inserting."""


def _validate_key(idempotency_key: str | None) -> None:
    if idempotency_key is None:
        return
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= IDEMPOTENCY_KEY_MAX:
        raise InvalidArgument(f"The idempotency key must be 1 to {IDEMPOTENCY_KEY_MAX} characters.")


async def _find_keyed(conn: AsyncConnection, initiator_id: int, idempotency_key: str) -> RowMapping | None:
    stmt = select(execution).where(execution.c.initiator_id == initiator_id, execution.c.idempotency_key == idempotency_key)
    return (await conn.execute(stmt)).mappings().one_or_none()


async def _stored_result(engine: AsyncEngine, *, initiator_id: int, idempotency_key: str, type: str, input: JsonObject, contract_function: str | None) -> OperationResult | None:
    """The replay of an execution already committed under (initiator_id, idempotency_key), read outside any transaction, or None when there is none."""
    async with engine.connect() as conn:
        stored = await _find_keyed(conn, initiator_id, idempotency_key)
    return None if stored is None else _replay(stored, type=type, input=input, contract_function=contract_function)


def _replay(row: RowMapping, *, type: str, input: JsonObject, contract_function: str | None) -> OperationResult:
    """Return the stored result for a repeated idempotency key, re-raise its stored error, or raise IdempotencyConflict when the type, input or contract_function differs.

    The first request for (initiator_id, key) runs once; a repeat already committed when run_operation() starts is answered here before its before hook is awaited, and one committed while the call is under way is answered here inside the call's transaction.
    """
    if row["type"] != str(type) or row["input"] != input or row["contract_function"] != contract_function:
        raise IdempotencyConflict()
    status = ExecutionStatus(row["status"])
    output: JsonObject = row["output"] or {}
    if status is ExecutionStatus.SUCCESS:
        return OperationResult(execution_id=row["execution_id"], status=status, output=output, replayed=True)
    stored = output.get("error")
    code = stored.get("code") if isinstance(stored, dict) else None
    message = stored.get("message") if isinstance(stored, dict) else None
    raw_child = stored.get("child") if isinstance(stored, dict) else None
    child = {key: value for key, value in raw_child.items() if value is None or isinstance(value, str)} if isinstance(raw_child, dict) else None
    error = error_from_code(code if isinstance(code, str) else "domain_error", message if isinstance(message, str) else "The operation failed.", execution_id=row["execution_id"], child=child)
    set_failure_output(error, {key: value for key, value in output.items() if key != "error"})
    setattr(error, _REPLAYED_ATTRIBUTE, True)
    raise error


async def _run_once(
    engine: AsyncEngine,
    *,
    type: str,
    initiator_id: int,
    input: JsonObject,
    body: Body,
    prepare: Prepare | None,
    user_accounts: tuple[int, ...],
    idempotency_key: str | None,
    now: datetime,
    on_failure: FailureHook | None,
    contract_function: str | None,
    exclusive_currencies: frozenset[int],
    pre_lock: frozenset[int],
    late_currency_locks: bool,
    rerun_allowed: bool,
    rollback_inspect: Inspect | None,
) -> OperationResult:
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            tx = LedgerTx(conn, now, exclusive_currencies=exclusive_currencies, late_currency_locks=late_currency_locks, rerun_allowed=rerun_allowed)
            if idempotency_key is not None:
                stored = await _find_keyed(conn, initiator_id, idempotency_key)
                if stored is not None:
                    await trans.rollback()
                    return _replay(stored, type=type, input=input, contract_function=contract_function)
            guard = _write_guard.get()
            if guard is not None:
                await guard(conn, initiator_id)
            await _create_accounts(tx, initiator_id, user_accounts)
            try:
                execution_id = await tx.create_execution(type=type, initiator_id=initiator_id, input=input, idempotency_key=idempotency_key, contract_function=contract_function)
            except IntegrityError as exc:
                # The insert waits on the unique (initiator_id, idempotency_key) index until a concurrent holder commits, then fails here.
                if idempotency_key is not None and mysql_error_code(exc) == DUPLICATE_KEY_ERROR:
                    await trans.rollback()
                    raise _KeyTaken from exc
                raise
            if pre_lock:
                await tx.lock_currencies(pre_lock, include_deleted=True)
            outcome = await _run_body(tx, execution_id, body, prepare, on_failure)
            if rollback_inspect is not None:
                await rollback_inspect(tx, execution_id)
                await trans.rollback()
                if isinstance(outcome, DomainError):
                    outcome.execution_id = None
                    raise outcome
                return OperationResult(execution_id=execution_id, status=ExecutionStatus.SUCCESS, output=outcome)
            await trans.commit()
            if isinstance(outcome, DomainError):
                raise outcome
            return OperationResult(execution_id=execution_id, status=ExecutionStatus.SUCCESS, output=outcome)
        except BaseException as exc:
            if trans.is_active:
                await trans.rollback()
                if isinstance(exc, DomainError):
                    # Nothing was committed, so the error names no execution.
                    exc.execution_id = None
            raise


async def run_operation(
    engine: AsyncEngine,
    *,
    type: str,
    initiator_id: int,
    input: Mapping[str, Any],
    body: Body,
    idempotency_key: str | None = None,
    now: datetime | None = None,
    prepare: Prepare | None = None,
    on_failure: FailureHook | None = None,
    contract_function: str | None = None,
    user_accounts: Iterable[int] = (),
    exclusive_currencies: Iterable[int] = (),
    late_currency_locks: bool = False,
    before: Before | None = None,
    max_attempts: int = MAX_ATTEMPTS,
    rollback_inspect: Inspect | None = None,
) -> OperationResult:
    """Run body(tx, execution_id) as one execution in one transaction and return its result, raising the body's DomainError with execution_id set; initiator_id must be a user account ID (InvalidArgument before anything is written or asked).

    prepare and on_failure work as in _run_body(), user_accounts as in _create_accounts(), idempotency_key as in _replay(), before is awaited once ahead of the first transaction unless a replay answers, exclusive_currencies and late_currency_locks (the opt-in of a top-level contract call whose gas balances prepare locks, relying on deadlock retry) configure LedgerTx, and the whole transaction is rerun on deadlock or lock wait timeout (up to max_attempts) and on RerunTransaction.
    With rollback_inspect (a dry run, which takes no idempotency key) the transaction is never committed: after the body and any on_failure, rollback_inspect(tx, execution_id) may read what the operation wrote, then everything is rolled back and the result is returned or the DomainError raised with execution_id None.
    """
    validate_user_account_id(initiator_id)
    _validate_key(idempotency_key)
    if rollback_inspect is not None and idempotency_key is not None:
        raise LedgerUsageError("a dry run cannot take an idempotency key")
    at = resolve_now(now)
    stored_input = to_json_object(input)
    touched = tuple(user_accounts)
    exclusive = set(exclusive_currencies)
    pre_lock: set[int] = set()
    reruns = 0
    if before is not None:
        if idempotency_key is not None:
            replayed = await _stored_result(engine, initiator_id=initiator_id, idempotency_key=idempotency_key, type=type, input=stored_input, contract_function=contract_function)
            if replayed is not None:
                return replayed
        await before()
    attempt = 0
    key_races = 0
    while True:
        try:
            return await _run_once(
                engine,
                type=type,
                initiator_id=initiator_id,
                input=stored_input,
                body=body,
                prepare=prepare,
                user_accounts=touched,
                idempotency_key=idempotency_key,
                now=at,
                on_failure=on_failure,
                contract_function=contract_function,
                exclusive_currencies=frozenset(exclusive),
                pre_lock=frozenset(pre_lock),
                late_currency_locks=late_currency_locks,
                rerun_allowed=reruns < MAX_RERUNS,
                rollback_inspect=rollback_inspect,
            )
        except CurrencyLockEscalation as escalation:
            if escalation.exclusive <= exclusive:
                raise
            exclusive |= escalation.exclusive
        except RerunTransaction as rerun:
            reruns += 1
            if reruns > MAX_RERUNS:
                raise
            exclusive |= rerun.exclusive
            pre_lock |= rerun.pre_lock
        except _KeyTaken:
            key_races += 1
            if key_races >= _KEY_RACE_ATTEMPTS:
                raise RuntimeError("idempotency key kept colliding without a readable stored execution") from None
        except DBAPIError as exc:
            attempt += 1
            if not is_retryable(exc) or attempt >= max_attempts:
                raise
            await asyncio.sleep(retry_delay(attempt))


async def run_child(
    tx: LedgerTx,
    *,
    parent_execution_id: int,
    type: str,
    initiator_id: int,
    input: Mapping[str, Any],
    body: Body,
    prepare: Prepare | None = None,
    on_failure: FailureHook | None = None,
    contract_function: str | None = None,
    user_accounts: Iterable[int] = (),
) -> OperationResult:
    """Run body as a child execution of parent_execution_id inside tx, with prepare, on_failure and user_accounts as in run_operation() and the same InvalidArgument for an initiator that is not a user account.

    A nested contract execution is the one allowed exception to the ascending, currency-before-balance lock order: inside it lock_currencies() may lock a currency after balances are locked, and a resulting deadlock is resolved by rerunning the whole transaction; a RerunTransaction from the child propagates unchanged.
    On a DomainError from the body, the child's writes are rolled back, the child is recorded as failed (reverted for RevertError) with on_failure's writes, and the error is raised with the child's execution_id and with error.child describing the child (type, contract_function, code, message), which the parent stores if the error fails it too.
    A DomainError from prepare or on_failure removes the child row and all its writes and is raised with execution_id None; the child's record also disappears whenever the enclosing transaction rolls back.
    """
    validate_user_account_id(initiator_id)
    tx._child_depth += 1
    try:
        async with tx.savepoint():
            await _create_accounts(tx, initiator_id, user_accounts)
            execution_id = await tx.create_execution(type=type, initiator_id=initiator_id, input=input, parent_execution_id=parent_execution_id, contract_function=contract_function)
            outcome = await _run_body(tx, execution_id, body, prepare, on_failure)
    except RerunTransaction:
        raise
    except DomainError as error:
        error.execution_id = None
        raise
    finally:
        tx._child_depth -= 1
    if isinstance(outcome, DomainError):
        outcome.child = {"type": str(type), "contract_function": contract_function, "code": outcome.code, "message": outcome.message}
        raise outcome
    return OperationResult(execution_id=execution_id, status=ExecutionStatus.SUCCESS, output=outcome)
