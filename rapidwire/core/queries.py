"""Public read-only queries over accounts, balances, executions and transfers, single statements through reader() and multi-statement reads from one consistent_snapshot(); execution input and output are returned as stored except for PRIVATE_EXECUTION_TYPES, and an idempotency key is never returned, only whether one was given."""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from sqlalchemy import ColumnElement, RowMapping, Select, func, select, union_all
from sqlalchemy.ext.asyncio import AsyncConnection

from rapidwire.core.db import Db, consistent_snapshot, reader
from rapidwire.core.errors import CurrencyNotFound, InvalidArgument
from rapidwire.core.outbox import CONTRACT_DISCORD_KINDS
from rapidwire.core.paging import (
    BALANCES_PER_PAGE_DEFAULT,
    BALANCES_PER_PAGE_MAX,
    EXECUTIONS_PER_PAGE_DEFAULT,
    EXECUTIONS_PER_PAGE_MAX,
    HISTORY_PER_PAGE_DEFAULT,
    HISTORY_PER_PAGE_MAX,
    MAX_WINDOW,
    Page,
    bounded_count,
    count_bounded,
    validate_paging,
)
from rapidwire.core.types import AccountKind, ExecutionStatus, ExecutionType, JsonObject, LabelBasis, Origin, OutboxStatus
from rapidwire.core.validation import BIGINT_MAX, require_int_in_range, validate_bigint_id
from rapidwire.db.tables import CONTRACT_FUNCTION_MAX, ENUM_LENGTH, account, balance, currency, execution, outbox, transfer
from rapidwire.db.types import AMOUNT_MAX, AMOUNT_MIN

__all__ = [
    "BALANCES_PER_PAGE_DEFAULT",
    "BALANCES_PER_PAGE_MAX",
    "EXECUTIONS_PER_PAGE_DEFAULT",
    "EXECUTIONS_PER_PAGE_MAX",
    "HISTORY_PER_PAGE_DEFAULT",
    "HISTORY_PER_PAGE_MAX",
    "HISTORY_SORT_KEYS",
    "MAX_WINDOW",
    "PRIVATE_EXECUTION_TYPES",
    "AccountRecord",
    "AccountStats",
    "BalanceEntry",
    "CurrencySummary",
    "CurrencySymbol",
    "Db",
    "Delivery",
    "DeliveryError",
    "Execution",
    "ExecutionDetail",
    "ExecutionListStatements",
    "ExecutionNode",
    "HistoryStatements",
    "Page",
    "Transfer",
    "TransferEntry",
    "currency_supplies",
    "currency_supply",
    "execution_list_statements",
    "get_account",
    "get_balance",
    "get_execution",
    "get_transfer",
    "history_statements",
    "list_balances",
    "list_currencies",
    "list_executions",
    "search_currency_symbols",
    "search_history",
    "user_stats",
    "user_stats_count_statements",
]

HISTORY_SORT_KEYS: Final = ("transfer_id", "timestamp", "amount")

# Executions that change notification permissions, Discord permissions or API tokens; these settings are private, so public reads hide their input and output.
PRIVATE_EXECUTION_TYPES: Final = frozenset(
    {
        ExecutionType.NOTIFICATION_ALLOW,
        ExecutionType.NOTIFICATION_DENY,
        ExecutionType.DISCORD_PERMISSION_ALLOW,
        ExecutionType.DISCORD_PERMISSION_DENY,
        ExecutionType.TOKEN_ISSUE,
        ExecutionType.TOKEN_REVOKE,
    }
)

_SORT_ORDERS: Final = ("asc", "desc")


@dataclass(frozen=True)
class BalanceEntry:
    currency_id: int
    symbol: str
    name: str
    amount: int


@dataclass(frozen=True)
class Execution:
    """An execution row without its idempotency key, with the origin and label basis of its parent (None without a parent); type is an open set of strings (see ExecutionType), and contract_id is the contract a contract_call called or a contract management execution acted on."""

    execution_id: int
    type: str
    origin: Origin
    label_basis: LabelBasis | None
    initiator_id: int
    parent_execution_id: int | None
    parent_origin: Origin | None
    parent_label_basis: LabelBasis | None
    status: ExecutionStatus
    input: JsonObject
    output: JsonObject | None
    has_idempotency_key: bool
    contract_function: str | None
    contract_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class Transfer:
    transfer_id: int
    execution_id: int
    currency_id: int
    from_account: int
    to_account: int
    amount: int
    kind: str
    created_at: datetime


@dataclass(frozen=True)
class TransferEntry(Transfer):
    """A transfer with its currency symbol and a summary of the execution it belongs to."""

    currency_symbol: str
    execution_type: str
    execution_status: ExecutionStatus
    origin: Origin
    label_basis: LabelBasis | None
    initiator_id: int
    parent_execution_id: int | None
    parent_origin: Origin | None
    parent_label_basis: LabelBasis | None
    contract_function: str | None


@dataclass(frozen=True)
class DeliveryError:
    """Why a delivery failed: reason is RapidWire's code (such as permission_revoked, channel_not_in_guild, message_rejected or retries_exhausted), and http_status, code and message are Discord's answer when it gave one."""

    reason: str
    http_status: int | None
    code: str | None
    message: str | None


@dataclass(frozen=True)
class Delivery:
    """A Discord operation a contract execution queued and the worker's progress on it, without the message text; status is pending, sent or failed (the dispatcher never skips contract operations), and the target IDs that do not apply to the kind are None."""

    outbox_id: int
    kind: str
    guild_id: int | None
    channel_id: int | None
    user_id: int | None
    role_id: int | None
    status: OutboxStatus
    attempts: int
    created_at: datetime
    next_attempt_at: datetime | None
    completed_at: datetime | None
    message_id: int | None
    error: DeliveryError | None


@dataclass(frozen=True)
class ExecutionNode:
    """An execution with its transfers (ascending transfer_id), the IDs of its direct children (ascending), which get_execution() fetches in turn, and the contract Discord operations it queued (ascending outbox_id)."""

    execution: Execution
    transfers: tuple[Transfer, ...]
    child_execution_ids: tuple[int, ...]
    deliveries: tuple[Delivery, ...]


@dataclass(frozen=True)
class ExecutionDetail(ExecutionNode):
    """An execution node plus its direct children as nodes, in ascending execution_id order."""

    children: tuple[ExecutionNode, ...]


@dataclass(frozen=True)
class AccountRecord:
    """An account row: its kind, the currency of an issuance account and the pool of a pool custody account (None otherwise), and when it was created."""

    account_id: int
    kind: AccountKind
    currency_id: int | None
    pool_id: int | None
    created_at: datetime


@dataclass(frozen=True)
class AccountStats:
    """Transfer statistics of an account counting both sides; total_transfers is exact, and the timestamps are None when it has no transfers."""

    total_transfers: int
    first_transfer_at: datetime | None
    last_transfer_at: datetime | None


@dataclass(frozen=True)
class CurrencySummary:
    currency_id: int
    symbol: str
    name: str
    supply: int
    deleted: bool


def _check_time(name: str, value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise InvalidArgument(f"{name} must be a timezone-aware datetime.")
    return value


def _check_text(name: str, value: object, max_length: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length:
        raise InvalidArgument(f"{name} must be 1 to {max_length} characters.")
    return value


def _origin(value: str | None) -> Origin | None:
    return Origin(value) if value is not None else None


def _label_basis(value: str | None) -> LabelBasis | None:
    return LabelBasis(value) if value is not None else None


# The parent execution, outer-joined on its primary key to give every execution or transfer row the origin of its parent.
_PARENT: Final = execution.alias("parent")
_PARENT_COLUMNS: Final = (_PARENT.c.origin.label("parent_origin"), _PARENT.c.label_basis.label("parent_label_basis"))


def _executions_with_parent() -> Select[Any]:
    """Every execution column plus parent_origin and parent_label_basis."""
    return select(execution, *_PARENT_COLUMNS).select_from(execution.outerjoin(_PARENT, _PARENT.c.execution_id == execution.c.parent_execution_id))


def _execution(row: RowMapping) -> Execution:
    private = row["type"] in PRIVATE_EXECUTION_TYPES
    return Execution(
        execution_id=row["execution_id"],
        type=row["type"],
        origin=Origin(row["origin"]),
        label_basis=_label_basis(row["label_basis"]),
        initiator_id=row["initiator_id"],
        parent_execution_id=row["parent_execution_id"],
        parent_origin=_origin(row["parent_origin"]),
        parent_label_basis=_label_basis(row["parent_label_basis"]),
        status=ExecutionStatus(row["status"]),
        input={} if private else row["input"],
        output=None if private else row["output"],
        has_idempotency_key=row["idempotency_key"] is not None,
        contract_function=row["contract_function"],
        contract_id=row["contract_id"],
        created_at=row["created_at"],
    )


def _transfer(row: RowMapping) -> Transfer:
    return Transfer(
        transfer_id=row["transfer_id"],
        execution_id=row["execution_id"],
        currency_id=row["currency_id"],
        from_account=row["from_account"],
        to_account=row["to_account"],
        amount=row["amount"],
        kind=row["kind"],
        created_at=row["created_at"],
    )


_ENTRY_COLUMNS: Final = (
    *transfer.c,
    currency.c.symbol.label("currency_symbol"),
    execution.c.type.label("execution_type"),
    execution.c.status.label("execution_status"),
    execution.c.origin,
    execution.c.label_basis,
    execution.c.initiator_id,
    execution.c.parent_execution_id,
    *_PARENT_COLUMNS,
    execution.c.contract_function,
)


def _transfer_entry(row: RowMapping) -> TransferEntry:
    return TransferEntry(
        transfer_id=row["transfer_id"],
        execution_id=row["execution_id"],
        currency_id=row["currency_id"],
        from_account=row["from_account"],
        to_account=row["to_account"],
        amount=row["amount"],
        kind=row["kind"],
        created_at=row["created_at"],
        currency_symbol=row["currency_symbol"],
        execution_type=row["execution_type"],
        execution_status=ExecutionStatus(row["execution_status"]),
        origin=Origin(row["origin"]),
        label_basis=_label_basis(row["label_basis"]),
        initiator_id=row["initiator_id"],
        parent_execution_id=row["parent_execution_id"],
        parent_origin=_origin(row["parent_origin"]),
        parent_label_basis=_label_basis(row["parent_label_basis"]),
        contract_function=row["contract_function"],
    )


async def get_balance(db: Db, account_id: int, currency_id: int) -> int:
    """The balance of the account in the currency (deleted currencies included), 0 when no balance row exists; a missing currency raises CurrencyNotFound."""
    validate_bigint_id("account_id", account_id)
    validate_bigint_id("currency_id", currency_id)
    joined = currency.outerjoin(balance, (balance.c.currency_id == currency.c.currency_id) & (balance.c.account_id == account_id))
    stmt = select(balance.c.amount).select_from(joined).where(currency.c.currency_id == currency_id)
    async with reader(db) as conn:
        row = (await conn.execute(stmt)).one_or_none()
    if row is None:
        raise CurrencyNotFound(f"Currency {currency_id} was not found.")
    return 0 if row.amount is None else int(row.amount)


async def list_balances(db: Db, account_id: int, *, page: int = 1, per_page: int = BALANCES_PER_PAGE_DEFAULT) -> Page[BalanceEntry]:
    """The account's non-zero balances in ascending currency_id order, with each currency's symbol and name."""
    validate_bigint_id("account_id", account_id)
    offset = validate_paging(page, per_page, BALANCES_PER_PAGE_MAX)
    where = (balance.c.account_id == account_id, balance.c.amount != 0)
    page_stmt = (
        select(balance.c.currency_id, currency.c.symbol, currency.c.name, balance.c.amount)
        .join(currency, currency.c.currency_id == balance.c.currency_id)
        .where(*where)
        .order_by(balance.c.currency_id)
        .limit(per_page)
        .offset(offset)
    )
    async with consistent_snapshot(db) as conn:
        total, bounded = await count_bounded(conn, [bounded_count(select(balance.c.currency_id).where(*where))])
        rows = (await conn.execute(page_stmt)).mappings().all()
    items = tuple(BalanceEntry(currency_id=row["currency_id"], symbol=row["symbol"], name=row["name"], amount=row["amount"]) for row in rows)
    return Page(items=items, total=total, page=page, per_page=per_page, total_is_lower_bound=bounded)


def _optional_id(source: Mapping[str, Any] | None, key: str) -> int | None:
    value = source.get(key) if source is not None else None
    if not isinstance(value, str) or not value.isascii() or not value.isdigit():
        return None
    number = int(value)
    return number if 1 <= number <= BIGINT_MAX else None


def _delivery(row: RowMapping) -> Delivery:
    payload = row["payload"] if isinstance(row["payload"], Mapping) else None
    result = row["result"] if isinstance(row["result"], Mapping) else None
    status = OutboxStatus(row["status"])
    error = None
    if status is OutboxStatus.FAILED and result is not None:
        http_status = _optional_id(result, "http_status")
        code, message = result.get("code"), result.get("message")
        error = DeliveryError(
            reason=str(result.get("reason") or "unknown"),
            http_status=http_status,
            code=code if isinstance(code, str) else None,
            message=message if isinstance(message, str) else None,
        )
    return Delivery(
        outbox_id=row["outbox_id"],
        kind=row["kind"],
        guild_id=_optional_id(payload, "guild_id"),
        channel_id=_optional_id(result, "channel_id") or _optional_id(payload, "channel_id"),
        user_id=_optional_id(payload, "user_id"),
        role_id=_optional_id(payload, "role_id"),
        status=status,
        attempts=row["attempts"],
        created_at=row["created_at"],
        next_attempt_at=row["available_at"] if status is OutboxStatus.PENDING else None,
        completed_at=row["completed_at"],
        message_id=_optional_id(result, "message_id"),
        error=error,
    )


async def _execution_nodes(conn: AsyncConnection, rows: Sequence[RowMapping]) -> list[ExecutionNode]:
    """Load the transfers, child IDs and contract Discord deliveries of the given execution rows with one query each; claim notifications are never included, because they would reveal the payer's notification setting."""
    ids = [row["execution_id"] for row in rows]
    transfers: dict[int, list[Transfer]] = {execution_id: [] for execution_id in ids}
    children: dict[int, list[int]] = {execution_id: [] for execution_id in ids}
    deliveries: dict[int, list[Delivery]] = {execution_id: [] for execution_id in ids}
    if ids:
        transfer_rows = (await conn.execute(select(transfer).where(transfer.c.execution_id.in_(ids)).order_by(transfer.c.transfer_id))).mappings().all()
        for transfer_row in transfer_rows:
            transfers[transfer_row["execution_id"]].append(_transfer(transfer_row))
        child_stmt = select(execution.c.execution_id, execution.c.parent_execution_id).where(execution.c.parent_execution_id.in_(ids)).order_by(execution.c.execution_id)
        for child in (await conn.execute(child_stmt)).mappings().all():
            children[child["parent_execution_id"]].append(child["execution_id"])
        delivery_stmt = select(outbox).where(outbox.c.execution_id.in_(ids), outbox.c.kind.in_(sorted(CONTRACT_DISCORD_KINDS))).order_by(outbox.c.outbox_id)
        for delivery_row in (await conn.execute(delivery_stmt)).mappings().all():
            deliveries[delivery_row["execution_id"]].append(_delivery(delivery_row))
    return [
        ExecutionNode(
            execution=_execution(row),
            transfers=tuple(transfers[row["execution_id"]]),
            child_execution_ids=tuple(children[row["execution_id"]]),
            deliveries=tuple(deliveries[row["execution_id"]]),
        )
        for row in rows
    ]


async def get_execution(db: Db, execution_id: int) -> ExecutionDetail | None:
    """The execution with its transfers and its direct children (each with its own transfers and child IDs), or None when it does not exist."""
    validate_bigint_id("execution_id", execution_id)
    async with consistent_snapshot(db) as conn:
        row = (await conn.execute(_executions_with_parent().where(execution.c.execution_id == execution_id))).mappings().one_or_none()
        if row is None:
            return None
        (node,) = await _execution_nodes(conn, [row])
        child_stmt = _executions_with_parent().where(execution.c.parent_execution_id == execution_id).order_by(execution.c.execution_id)
        child_rows = (await conn.execute(child_stmt)).mappings().all()
        children = await _execution_nodes(conn, child_rows)
    return ExecutionDetail(execution=node.execution, transfers=node.transfers, child_execution_ids=node.child_execution_ids, deliveries=node.deliveries, children=tuple(children))


async def get_transfer(db: Db, transfer_id: int) -> TransferEntry | None:
    """The transfer with its execution summary, or None when it does not exist."""
    validate_bigint_id("transfer_id", transfer_id)
    stmt = (
        select(*_ENTRY_COLUMNS)
        .join(execution, execution.c.execution_id == transfer.c.execution_id)
        .join(currency, currency.c.currency_id == transfer.c.currency_id)
        .outerjoin(_PARENT, _PARENT.c.execution_id == execution.c.parent_execution_id)
        .where(transfer.c.transfer_id == transfer_id)
    )
    async with reader(db) as conn:
        row = (await conn.execute(stmt)).mappings().one_or_none()
    return None if row is None else _transfer_entry(row)


@dataclass(frozen=True)
class HistoryStatements:
    """The bounded count statements (summed) and the page statement of one history search."""

    count_statements: tuple[Select[Any], ...]
    page_statement: Select[Any]


def history_statements(
    *,
    transfer_id: int | None = None,
    user_id: int | None = None,
    source_id: int | None = None,
    destination_id: int | None = None,
    currency_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    min_amount: int | None = None,
    max_amount: int | None = None,
    execution_type: str | None = None,
    contract_function: str | None = None,
    page: int = 1,
    per_page: int = HISTORY_PER_PAGE_DEFAULT,
    sort_by: str = "transfer_id",
    sort_order: str = "desc",
) -> HistoryStatements:
    """Validate the arguments of search_history() and build its statements; exposed so tests can inspect the query plans.

    A user_id search is a UNION ALL of a from_account branch and a to_account branch (a transfer never has the same account on both sides), so each branch uses its (account, transfer_id) index.
    Each branch keeps only its first offset + per_page rows in the requested order, and the outer query re-sorts those, pages them and joins the execution and currency rows.
    """
    offset = validate_paging(page, per_page, HISTORY_PER_PAGE_MAX)
    if sort_by not in HISTORY_SORT_KEYS:
        raise InvalidArgument(f"sort_by must be one of {', '.join(HISTORY_SORT_KEYS)}.")
    order = sort_order.lower() if isinstance(sort_order, str) else None
    if order not in _SORT_ORDERS:
        raise InvalidArgument("sort_order must be asc or desc.")

    conditions: list[ColumnElement[bool]] = []
    for name, value, column in (
        ("transfer_id", transfer_id, transfer.c.transfer_id),
        ("source_id", source_id, transfer.c.from_account),
        ("destination_id", destination_id, transfer.c.to_account),
        ("currency_id", currency_id, transfer.c.currency_id),
    ):
        if value is not None:
            conditions.append(column == validate_bigint_id(name, value))
    if start is not None:
        conditions.append(transfer.c.created_at >= _check_time("start", start))
    if end is not None:
        conditions.append(transfer.c.created_at < _check_time("end", end))
    if min_amount is not None:
        conditions.append(transfer.c.amount >= require_int_in_range("min_amount", min_amount, AMOUNT_MIN, AMOUNT_MAX))
    if max_amount is not None:
        conditions.append(transfer.c.amount <= require_int_in_range("max_amount", max_amount, AMOUNT_MIN, AMOUNT_MAX))
    execution_conditions: list[ColumnElement[bool]] = []
    if execution_type is not None:
        execution_conditions.append(execution.c.type == _check_text("execution_type", execution_type, ENUM_LENGTH))
    if contract_function is not None:
        execution_conditions.append(execution.c.contract_function == _check_text("contract_function", contract_function, CONTRACT_FUNCTION_MAX))
    if user_id is not None:
        user = validate_bigint_id("user_id", user_id)
        sides: list[ColumnElement[bool] | None] = [transfer.c.from_account == user, transfer.c.to_account == user]
    else:
        sides = [None]

    sort_keys = {"transfer_id": [transfer.c.transfer_id], "timestamp": [transfer.c.created_at, transfer.c.transfer_id], "amount": [transfer.c.amount, transfer.c.transfer_id]}[sort_by]
    ordering = [key.asc() if order == "asc" else key.desc() for key in sort_keys]

    def branch(side: ColumnElement[bool] | None) -> Select[Any]:
        stmt = select(transfer.c.transfer_id).select_from(transfer)
        if execution_conditions:
            stmt = stmt.join(execution, execution.c.execution_id == transfer.c.execution_id).where(*execution_conditions)
        return stmt.where(*conditions, *([side] if side is not None else []))

    counts = tuple(bounded_count(branch(side)) for side in sides)
    branches = [branch(side).order_by(*ordering).limit(offset + per_page) for side in sides]
    matched = (branches[0] if len(branches) == 1 else union_all(*branches)).subquery("matched")
    page_statement = (
        select(*_ENTRY_COLUMNS)
        .select_from(matched)
        .join(transfer, transfer.c.transfer_id == matched.c.transfer_id)
        .join(execution, execution.c.execution_id == transfer.c.execution_id)
        .join(currency, currency.c.currency_id == transfer.c.currency_id)
        .outerjoin(_PARENT, _PARENT.c.execution_id == execution.c.parent_execution_id)
        .order_by(*ordering)
        .limit(per_page)
        .offset(offset)
    )
    return HistoryStatements(count_statements=counts, page_statement=page_statement)


async def search_history(
    db: Db,
    *,
    transfer_id: int | None = None,
    user_id: int | None = None,
    source_id: int | None = None,
    destination_id: int | None = None,
    currency_id: int | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    min_amount: int | None = None,
    max_amount: int | None = None,
    execution_type: str | None = None,
    contract_function: str | None = None,
    page: int = 1,
    per_page: int = HISTORY_PER_PAGE_DEFAULT,
    sort_by: str = "transfer_id",
    sort_order: str = "desc",
) -> Page[TransferEntry]:
    """Search transfers of native and legacy executions alike; every given filter must match.

    user_id matches either side, start is an inclusive and end an exclusive bound on the transfer time (a date range ends at the next midnight), min_amount and max_amount are inclusive, and execution_type and contract_function filter on the owning execution.
    sort_by is transfer_id, timestamp or amount and sort_order is asc or desc (any case); ties on timestamp or amount are ordered by transfer_id in the same direction.
    """
    statements = history_statements(
        transfer_id=transfer_id,
        user_id=user_id,
        source_id=source_id,
        destination_id=destination_id,
        currency_id=currency_id,
        start=start,
        end=end,
        min_amount=min_amount,
        max_amount=max_amount,
        execution_type=execution_type,
        contract_function=contract_function,
        page=page,
        per_page=per_page,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    async with consistent_snapshot(db) as conn:
        total, bounded = await count_bounded(conn, statements.count_statements)
        rows = (await conn.execute(statements.page_statement)).mappings().all()
    return Page(items=tuple(_transfer_entry(row) for row in rows), total=total, page=page, per_page=per_page, total_is_lower_bound=bounded)


@dataclass(frozen=True)
class ExecutionListStatements:
    """The bounded count statement and the page statement of one execution list."""

    count_statement: Select[Any]
    page_statement: Select[Any]


# A contract's executions are read through this index, whose order is the list's order, rather than through the type index, which would scan every execution of the type.
_CONTRACT_INDEX_HINT: Final = "FORCE INDEX (ix_execution_contract_id_execution_id)"


def execution_list_statements(
    *,
    initiator_id: int | None = None,
    execution_type: str | None = None,
    status: ExecutionStatus | str | None = None,
    contract_id: int | None = None,
    page: int = 1,
    per_page: int = EXECUTIONS_PER_PAGE_DEFAULT,
) -> ExecutionListStatements:
    """Validate the arguments of list_executions() and build its statements; exposed so tests can inspect the query plans."""
    offset = validate_paging(page, per_page, EXECUTIONS_PER_PAGE_MAX)
    conditions: list[ColumnElement[bool]] = []
    if initiator_id is not None:
        conditions.append(execution.c.initiator_id == validate_bigint_id("initiator_id", initiator_id))
    if contract_id is not None:
        conditions.append(execution.c.contract_id == validate_bigint_id("contract_id", contract_id))
    if execution_type is not None:
        conditions.append(execution.c.type == _check_text("execution_type", execution_type, ENUM_LENGTH))
    if status is not None:
        try:
            wanted = ExecutionStatus(status)
        except ValueError:
            raise InvalidArgument(f"status must be one of {', '.join(ExecutionStatus)}.") from None
        conditions.append(execution.c.status == wanted.value)
    page_stmt = _executions_with_parent().where(*conditions).order_by(execution.c.execution_id.desc()).limit(per_page).offset(offset)
    count_source = select(execution.c.execution_id).where(*conditions)
    if contract_id is not None:
        page_stmt = page_stmt.with_hint(execution, _CONTRACT_INDEX_HINT, "mysql")
        count_source = count_source.with_hint(execution, _CONTRACT_INDEX_HINT, "mysql")
    return ExecutionListStatements(count_statement=bounded_count(count_source), page_statement=page_stmt)


async def list_executions(
    db: Db,
    *,
    initiator_id: int | None = None,
    execution_type: str | None = None,
    status: ExecutionStatus | str | None = None,
    contract_id: int | None = None,
    page: int = 1,
    per_page: int = EXECUTIONS_PER_PAGE_DEFAULT,
) -> Page[Execution]:
    """Executions newest first (descending execution_id), including failed ones and ones without transfers; every given filter must match, and contract_id matches the calls of that contract (nested ones included) and its management executions."""
    statements = execution_list_statements(initiator_id=initiator_id, execution_type=execution_type, status=status, contract_id=contract_id, page=page, per_page=per_page)
    async with consistent_snapshot(db) as conn:
        total, bounded = await count_bounded(conn, [statements.count_statement])
        rows = (await conn.execute(statements.page_statement)).mappings().all()
    return Page(items=tuple(_execution(row) for row in rows), total=total, page=page, per_page=per_page, total_is_lower_bound=bounded)


async def get_account(db: Db, account_id: int) -> AccountRecord | None:
    """The account row, or None when it does not exist; user accounts are inserted lazily on their first ledger operation, so a valid user ID can be absent."""
    validate_bigint_id("account_id", account_id)
    async with reader(db) as conn:
        row = (await conn.execute(select(account).where(account.c.account_id == account_id))).one_or_none()
    if row is None:
        return None
    return AccountRecord(account_id=row.account_id, kind=AccountKind(row.kind), currency_id=row.currency_id, pool_id=row.pool_id, created_at=row.created_at)


def user_stats_count_statements(account_id: int) -> tuple[Select[Any], Select[Any]]:
    """The exact counts of the account's outgoing and incoming transfers, each answered from its (account, transfer_id) index alone; their sum counts every transfer once, because the distinct_accounts CHECK keeps an account off both sides of one transfer."""
    validate_bigint_id("account_id", account_id)
    outgoing = select(func.count()).select_from(transfer).where(transfer.c.from_account == account_id)
    incoming = select(func.count()).select_from(transfer).where(transfer.c.to_account == account_id)
    return outgoing, incoming


async def user_stats(db: Db, account_id: int) -> AccountStats:
    """The exact number of transfers with the account on either side and the times of its first and last transfer."""
    counts = user_stats_count_statements(account_id)
    sides = (transfer.c.from_account, transfer.c.to_account)
    times: list[datetime] = []
    total = 0
    async with consistent_snapshot(db) as conn:
        for stmt in counts:
            total += int((await conn.execute(stmt)).scalar_one())
        for side in sides:
            for key in (transfer.c.created_at.asc(), transfer.c.created_at.desc()):
                found = (await conn.execute(select(transfer.c.created_at).where(side == account_id).order_by(key).limit(1))).scalar_one_or_none()
                if found is not None:
                    times.append(found)
    return AccountStats(total_transfers=total, first_transfer_at=min(times, default=None), last_transfer_at=max(times, default=None))


def _currency_with_supply() -> Select[Any]:
    joined = currency.outerjoin(balance, (balance.c.account_id == currency.c.issuance_account_id) & (balance.c.currency_id == currency.c.currency_id))
    return select(currency.c.currency_id, currency.c.symbol, currency.c.name, currency.c.deleted_at, balance.c.amount).select_from(joined)


async def currency_supply(db: Db, currency_id: int) -> int:
    """The supply of the currency (the negated balance of its issuance account), deleted currencies included; a missing currency raises CurrencyNotFound."""
    validate_bigint_id("currency_id", currency_id)
    async with reader(db) as conn:
        row = (await conn.execute(_currency_with_supply().where(currency.c.currency_id == currency_id))).one_or_none()
    if row is None:
        raise CurrencyNotFound(f"Currency {currency_id} was not found.")
    return -(row.amount or 0)


async def currency_supplies(db: Db, currency_ids: Iterable[int]) -> dict[int, int]:
    """The supply of each given currency that exists, deleted ones included, from one statement; IDs without a currency are absent from the result."""
    ids = sorted({validate_bigint_id("currency_id", currency_id) for currency_id in currency_ids})
    if not ids:
        return {}
    async with reader(db) as conn:
        rows = (await conn.execute(_currency_with_supply().where(currency.c.currency_id.in_(ids)))).all()
    return {row.currency_id: -(row.amount or 0) for row in rows}


async def list_currencies(db: Db, *, include_deleted: bool = False) -> tuple[CurrencySummary, ...]:
    """Every currency with its supply, ordered by symbol then currency_id; deleted currencies only when include_deleted."""
    stmt = _currency_with_supply()
    if not include_deleted:
        stmt = stmt.where(currency.c.deleted_at.is_(None))
    async with consistent_snapshot(db) as conn:
        rows = (await conn.execute(stmt.order_by(currency.c.symbol, currency.c.currency_id))).all()
    return tuple(CurrencySummary(currency_id=row.currency_id, symbol=row.symbol, name=row.name, supply=-(row.amount or 0), deleted=row.deleted_at is not None) for row in rows)


@dataclass(frozen=True)
class CurrencySymbol:
    currency_id: int
    symbol: str
    name: str


async def search_currency_symbols(db: Db, text: str, *, limit: int) -> tuple[CurrencySymbol, ...]:
    """Live currencies whose symbol contains text (matched as given, so callers uppercase it), those starting with it first and each group in symbol order, at most limit of them; empty text matches every live currency."""
    require_int_in_range("limit", limit, 1, MAX_WINDOW)
    symbol = currency.c.active_symbol
    stmt = select(currency.c.currency_id, symbol, currency.c.name).where(symbol.is_not(None))
    if text:
        stmt = stmt.where(symbol.contains(text, autoescape=True))
        stmt = stmt.order_by(symbol.startswith(text, autoescape=True).desc())
    stmt = stmt.order_by(symbol).limit(limit)
    async with reader(db) as conn:
        rows = (await conn.execute(stmt)).all()
    return tuple(CurrencySymbol(currency_id=row.currency_id, symbol=row.active_symbol, name=row.name) for row in rows)
