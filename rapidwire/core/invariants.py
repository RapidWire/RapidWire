"""Ledger invariant verifier used by the verify-invariants command, tests, migration checks and backup checks; it reads one consistent snapshot, never writes, and checks what INVARIANTS_SUMMARY states."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import BigInteger, Numeric, and_, case, cast, func, null, or_, select, type_coerce, union_all
from sqlalchemy.ext.asyncio import AsyncConnection

from rapidwire.contract import storage_codec
from rapidwire.contract.storage_codec import StorageDecodeError
from rapidwire.core.db import Db, consistent_snapshot
from rapidwire.core.ledger import CONTRACT_MANAGEMENT_TYPES
from rapidwire.core.types import AccountKind, ClaimStatus, ExecutionStatus, ExecutionType, TransferKind
from rapidwire.core.validation import BIGINT_MAX
from rapidwire.db.tables import (
    STAKE_CUSTODY_ACCOUNT_ID,
    ContractStatus,
    account,
    allowance,
    balance,
    claim,
    contract,
    contract_storage,
    contract_version,
    currency,
    execution,
    pool,
    pool_share,
    stake,
    transfer,
)

__all__ = ["INVARIANTS_SUMMARY", "CurrencyReport", "InvariantReport", "PoolReport", "Violation", "verify_invariants"]

INVARIANTS_SUMMARY: Final = (
    "balances sum to zero per currency and equal the net of their transfers;"
    " only a currency's own issuance account is negative in it and never positive, and allow_negative says exactly that;"
    " stake custody equals the stakes; allowances are non-negative; each currency points at its own issuance account;"
    " failed and reverted executions moved only gas, have no children and record their error, while successful ones record an output;"
    " every execution's contract_id is its initiator for a contract management type, the input's contract_id for a contract_call, and null otherwise;"
    " a deleted currency has only zero balances and no stakes, pending claims, allowances or active pools;"
    " every pool has exactly one custody account, an active pool's custody holds exactly its reserves of its two currencies and nothing else, and a dissolved pool's custody holds nothing;"
    " a pool's total_shares equals its locked_shares plus its providers' shares, and only active pools have providers;"
    " a contract's versions run from 1 to its current_version without gaps, versions exist only for accounts with a contract, and each was created by a successful contract_update execution of that account;"
    " a contract's storage_keys and storage_bytes equal the count and accounted size (key bytes plus value bytes) of its storage rows, which a deleted contract has none of, and every stored value is a canonical encoding."
)

_WIDE = Numeric(65, 0)

type _Add = Callable[..., None]


@dataclass(frozen=True)
class Violation:
    check: str
    message: str
    currency_id: int | None = None
    account_id: int | None = None
    pool_id: int | None = None


@dataclass(frozen=True)
class PoolReport:
    """A pool's stored reserves and shares next to what its custody account and share rows hold; custody_account_id is None when the pool has no custody account."""

    pool_id: int
    currency_a: int
    currency_b: int
    fee_bps: int
    active: bool
    custody_account_id: int | None
    reserve_a: int
    reserve_b: int
    custody_a: int
    custody_b: int
    total_shares: int
    locked_shares: int
    provider_shares: int
    ok: bool


@dataclass(frozen=True)
class CurrencyReport:
    currency_id: int
    symbol: str
    deleted: bool
    supply: int
    balance_sum: int
    stake_total: int
    stake_custody_balance: int
    ok: bool


@dataclass(frozen=True)
class InvariantReport:
    currencies: list[CurrencyReport] = field(default_factory=list)
    violations: list[Violation] = field(default_factory=list)
    pools: list[PoolReport] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations


def _int(value: Any) -> int:
    if value is None:
        return 0
    decimal = value if isinstance(value, Decimal) else Decimal(str(value))
    return int(decimal)


def _sum(column: Any) -> Any:
    return type_coerce(func.sum(column), _WIDE)


async def _collect(conn: AsyncConnection) -> InvariantReport:
    violations: list[Violation] = []

    def add(check: str, message: str, currency_id: int | None = None, account_id: int | None = None, *, pool_id: int | None = None) -> None:
        violations.append(Violation(check, message, currency_id, account_id, pool_id))

    currencies = (await conn.execute(select(currency.c.currency_id, currency.c.symbol, currency.c.deleted_at, currency.c.issuance_account_id).order_by(currency.c.currency_id))).all()
    deleted = {row.currency_id for row in currencies if row.deleted_at is not None}
    owners = {row.account_id: (AccountKind(row.kind), row.currency_id) for row in await conn.execute(select(account.c.account_id, account.c.kind, account.c.currency_id))}
    balances = {
        (row.account_id, row.currency_id): (_int(row.amount), bool(row.allow_negative))
        for row in await conn.execute(select(balance.c.account_id, balance.c.currency_id, type_coerce(balance.c.amount, _WIDE).label("amount"), balance.c.allow_negative))
    }

    sums: dict[int, int] = defaultdict(int)
    for (account_id, currency_id), (amount, allow_negative) in balances.items():
        sums[currency_id] += amount
        kind, owned_currency = owners[account_id]
        own_issuance = kind is AccountKind.ISSUANCE and owned_currency == currency_id
        if allow_negative != own_issuance:
            add("allow_negative_flag", f"balance of account {account_id} ({kind}) in currency {currency_id} has allow_negative={allow_negative}", currency_id, account_id)
        if amount < 0 and not own_issuance:
            add("negative_balance", f"account {account_id} ({kind}) has negative balance {amount} in currency {currency_id}", currency_id, account_id)
        if amount > 0 and own_issuance:
            add("positive_issuance", f"issuance account {account_id} has positive balance {amount} in currency {currency_id}", currency_id, account_id)
        if amount != 0 and currency_id in deleted:
            add("deleted_currency_balance", f"account {account_id} still holds {amount} of deleted currency {currency_id}", currency_id, account_id)

    moves = union_all(
        select(transfer.c.to_account.label("account_id"), transfer.c.currency_id, type_coerce(transfer.c.amount, _WIDE).label("delta")),
        select(transfer.c.from_account.label("account_id"), transfer.c.currency_id, type_coerce(-transfer.c.amount, _WIDE).label("delta")),
    ).subquery()
    nets = {
        (row.account_id, row.currency_id): _int(row.net)
        for row in await conn.execute(select(moves.c.account_id, moves.c.currency_id, _sum(moves.c.delta).label("net")).group_by(moves.c.account_id, moves.c.currency_id))
    }
    for key in sorted(balances.keys() | nets.keys()):
        recorded = balances.get(key, (0, False))[0]
        net = nets.get(key, 0)
        if recorded != net:
            add("balance_transfer_mismatch", f"balance of account {key[0]} in currency {key[1]} is {recorded} but its transfers net to {net}", key[1], key[0])

    stake_totals = {row.currency_id: _int(row.total) for row in await conn.execute(select(stake.c.currency_id, _sum(stake.c.amount).label("total")).group_by(stake.c.currency_id))}
    custody = {currency_id: amount for (account_id, currency_id), (amount, _) in balances.items() if account_id == STAKE_CUSTODY_ACCOUNT_ID}
    for currency_id in sorted(stake_totals.keys() | custody.keys()):
        if stake_totals.get(currency_id, 0) != custody.get(currency_id, 0):
            add(
                "stake_custody_mismatch",
                f"stake custody holds {custody.get(currency_id, 0)} of currency {currency_id} but stakes total {stake_totals.get(currency_id, 0)}",
                currency_id,
                STAKE_CUSTODY_ACCOUNT_ID,
            )

    allowance_rows = await conn.execute(select(allowance.c.owner_id, allowance.c.spender_id, allowance.c.currency_id, type_coerce(allowance.c.amount, _WIDE).label("amount")))
    for row in allowance_rows:
        amount = _int(row.amount)
        if amount < 0:
            add("negative_allowance", f"allowance from {row.owner_id} to {row.spender_id} in currency {row.currency_id} is {amount}", row.currency_id, row.owner_id)
        if row.currency_id in deleted:
            add("deleted_currency_allowance", f"allowance from {row.owner_id} to {row.spender_id} remains for deleted currency {row.currency_id}", row.currency_id, row.owner_id)

    if deleted:
        for row in await conn.execute(select(stake.c.account_id, stake.c.currency_id).where(stake.c.currency_id.in_(sorted(deleted)))):
            add("deleted_currency_stake", f"account {row.account_id} still has a stake row in deleted currency {row.currency_id}", row.currency_id, row.account_id)
        pending = select(claim.c.claim_id, claim.c.currency_id).where(claim.c.currency_id.in_(sorted(deleted)), claim.c.status == ClaimStatus.PENDING.value)
        for row in await conn.execute(pending):
            add("deleted_currency_claim", f"claim {row.claim_id} is still pending in deleted currency {row.currency_id}", row.currency_id)

    failed_moves = (
        select(transfer.c.transfer_id, transfer.c.currency_id, transfer.c.execution_id, transfer.c.kind)
        .join(execution, execution.c.execution_id == transfer.c.execution_id)
        .where(execution.c.status != ExecutionStatus.SUCCESS.value, transfer.c.kind != TransferKind.GAS.value)
    )
    for row in await conn.execute(failed_moves):
        add("failed_execution_transfer", f"transfer {row.transfer_id} of kind {row.kind} belongs to unsuccessful execution {row.execution_id}", row.currency_id)

    parent = execution.alias("parent")
    orphans = (
        select(execution.c.execution_id, parent.c.execution_id.label("parent_id"), parent.c.status)
        .join(parent, parent.c.execution_id == execution.c.parent_execution_id)
        .where(parent.c.status != ExecutionStatus.SUCCESS.value)
    )
    for row in await conn.execute(orphans):
        add("child_of_unsuccessful_execution", f"execution {row.execution_id} is a child of execution {row.parent_id}, which has status {row.status}")

    # A JSON column stores Python None as the JSON literal null, so both SQL NULL and JSON null count as missing.
    # JSON_TYPE returns utf8mb4_bin text, so the comparison names that collation explicitly.
    missing_output = or_(execution.c.output.is_(None), func.json_type(execution.c.output).collate("utf8mb4_bin") == "NULL")
    succeeded = execution.c.status == ExecutionStatus.SUCCESS.value
    unrecorded = select(execution.c.execution_id, execution.c.status).where(
        or_(and_(succeeded, missing_output), and_(~succeeded, or_(missing_output, func.json_extract(execution.c.output, "$.error.code").is_(None))))
    )
    for row in await conn.execute(unrecorded):
        add("execution_output", f"execution {row.execution_id} with status {row.status} has no recorded output" + ("" if row.status == ExecutionStatus.SUCCESS else " error"))

    await _check_execution_contract_ids(conn, add)
    pool_reports = await _check_pools(conn, balances, deleted, add)
    await _check_contracts(conn, add)

    reports: list[CurrencyReport] = []
    for row in currencies:
        if owners.get(row.issuance_account_id) != (AccountKind.ISSUANCE, row.currency_id):
            add("issuance_account", f"currency {row.currency_id} points at account {row.issuance_account_id}, which is not its issuance account", row.currency_id, row.issuance_account_id)
        if sums.get(row.currency_id, 0) != 0:
            add("sum_not_zero", f"balances of currency {row.currency_id} sum to {sums[row.currency_id]}", row.currency_id)
        reports.append(
            CurrencyReport(
                currency_id=row.currency_id,
                symbol=row.symbol,
                deleted=row.deleted_at is not None,
                supply=-balances.get((row.issuance_account_id, row.currency_id), (0, True))[0],
                balance_sum=sums.get(row.currency_id, 0),
                stake_total=stake_totals.get(row.currency_id, 0),
                stake_custody_balance=custody.get(row.currency_id, 0),
                ok=not any(violation.currency_id == row.currency_id for violation in violations),
            )
        )
    pools_checked = [replace(item, ok=not any(violation.pool_id == item.pool_id for violation in violations)) for item in pool_reports]
    return InvariantReport(currencies=reports, violations=violations, pools=pools_checked)


def _expected_contract_id() -> Any:
    """execution_contract_id() as a SQL expression: the initiator for a contract management type, a contract_call's input.contract_id when it is a decimal of 1 to 19 digits without a leading zero up to the BIGINT maximum, else NULL."""
    called = func.json_unquote(func.json_extract(execution.c.input, "$.contract_id")).collate("utf8mb4_bin")
    in_range = case((cast(called, Numeric(19, 0)) <= BIGINT_MAX, cast(called, BigInteger)), else_=null())
    from_input = case((func.regexp_like(called, "^[1-9][0-9]{0,18}$"), in_range), else_=null())
    return case(
        (execution.c.type.in_(sorted(CONTRACT_MANAGEMENT_TYPES)), execution.c.initiator_id),
        (execution.c.type == ExecutionType.CONTRACT_CALL.value, from_input),
        else_=null(),
    )


async def _check_execution_contract_ids(conn: AsyncConnection, add: _Add) -> None:
    """Report every execution whose contract_id differs from what its type, initiator and input give (see execution_contract_id())."""
    expected = _expected_contract_id().label("expected")
    wrong = (
        select(execution.c.execution_id, execution.c.type, execution.c.contract_id, expected)
        .where(execution.c.contract_id.is_distinct_from(_expected_contract_id()))
        .order_by(execution.c.execution_id)
    )
    for row in await conn.execute(wrong):
        add("execution_contract_id", f"execution {row.execution_id} ({row.type}) has contract_id {row.contract_id} but its type and input give {row.expected}", account_id=row.contract_id)


async def _check_pools(conn: AsyncConnection, balances: dict[tuple[int, int], tuple[int, bool]], deleted: set[int], add: _Add) -> list[PoolReport]:
    """Check every pool against its custody balances and share rows and return one report per pool in ascending pool_id, with ok filled in by the caller."""
    custody_accounts: dict[int, list[int]] = defaultdict(list)
    for row in await conn.execute(select(account.c.account_id, account.c.pool_id).where(account.c.kind == AccountKind.POOL_CUSTODY.value).order_by(account.c.account_id)):
        custody_accounts[row.pool_id].append(row.account_id)
    held: dict[int, dict[int, int]] = defaultdict(dict)
    owner_pool = {account_id: pool_id for pool_id, ids in custody_accounts.items() for account_id in ids}
    for (account_id, currency_id), (amount, _) in balances.items():
        if account_id in owner_pool and amount != 0:
            held[account_id][currency_id] = amount
    shares = {
        row.pool_id: (_int(row.total), int(row.providers))
        for row in await conn.execute(select(pool_share.c.pool_id, _sum(pool_share.c.shares).label("total"), func.count().label("providers")).group_by(pool_share.c.pool_id))
    }
    reports: list[PoolReport] = []
    for row in await conn.execute(select(pool).order_by(pool.c.pool_id)):
        pool_id, currency_a, currency_b = row.pool_id, row.currency_a, row.currency_b
        active = row.dissolved_at is None
        accounts = custody_accounts.get(pool_id, [])
        if len(accounts) != 1:
            add("pool_custody_account", f"pool {pool_id} has {len(accounts)} custody accounts instead of one", pool_id=pool_id)
        custody = accounts[0] if accounts else None
        holdings = {currency_id: amount for account_id in accounts for currency_id, amount in held.get(account_id, {}).items()}
        reserve_a, reserve_b = _int(row.reserve_a), _int(row.reserve_b)
        provider_shares, providers = shares.get(pool_id, (0, 0))
        if active:
            for currency_id, reserve in ((currency_a, reserve_a), (currency_b, reserve_b)):
                found = holdings.get(currency_id, 0)
                if found != reserve:
                    add("pool_custody_reserve", f"custody of pool {pool_id} holds {found} of currency {currency_id} but its reserve is {reserve}", currency_id, custody, pool_id=pool_id)
            for currency_id in sorted(holdings.keys() - {currency_a, currency_b}):
                add("pool_custody_foreign", f"custody of pool {pool_id} holds {holdings[currency_id]} of currency {currency_id}, which is not in the pool", currency_id, custody, pool_id=pool_id)
            for currency_id in sorted({currency_a, currency_b} & deleted):
                add("deleted_currency_pool", f"pool {pool_id} is still active with deleted currency {currency_id}", currency_id, pool_id=pool_id)
        else:
            for currency_id in sorted(holdings):
                add("dissolved_pool_custody", f"custody of dissolved pool {pool_id} still holds {holdings[currency_id]} of currency {currency_id}", currency_id, custody, pool_id=pool_id)
            if providers:
                add("dissolved_pool_shares", f"dissolved pool {pool_id} still has {providers} share rows", pool_id=pool_id)
        total, locked = _int(row.total_shares), _int(row.locked_shares)
        if total != locked + provider_shares:
            add("pool_share_total", f"pool {pool_id} has total_shares {total} but locked_shares {locked} plus provider shares {provider_shares}", pool_id=pool_id)
        reports.append(
            PoolReport(
                pool_id=pool_id,
                currency_a=currency_a,
                currency_b=currency_b,
                fee_bps=row.fee_bps,
                active=active,
                custody_account_id=custody,
                reserve_a=reserve_a,
                reserve_b=reserve_b,
                custody_a=holdings.get(currency_a, 0),
                custody_b=holdings.get(currency_b, 0),
                total_shares=total,
                locked_shares=locked,
                provider_shares=provider_shares,
                ok=True,
            )
        )
    return reports


async def _check_contracts(conn: AsyncConnection, add: _Add) -> None:
    """Check every contract's versions, their creating executions, its storage counters and stored values, reporting violations under the contract's account_id."""
    versions = {
        row.account_id: (int(row.version_count), int(row.highest))
        for row in await conn.execute(
            select(contract_version.c.account_id, func.count().label("version_count"), func.max(contract_version.c.version).label("highest")).group_by(contract_version.c.account_id)
        )
    }
    stored = {
        row.account_id: (int(row.key_count), _int(row.byte_total))
        for row in await conn.execute(
            select(
                contract_storage.c.account_id,
                func.count().label("key_count"),
                _sum(func.length(contract_storage.c.storage_key) + contract_storage.c.value_size).label("byte_total"),
            ).group_by(contract_storage.c.account_id)
        )
    }
    columns = (contract.c.account_id, contract.c.status, contract.c.current_version, contract.c.storage_keys, contract.c.storage_bytes)
    contracts = (await conn.execute(select(*columns).order_by(contract.c.account_id))).all()
    known = {row.account_id for row in contracts}
    for account_id in sorted(versions.keys() - known):
        add("contract_version_orphan", f"account {account_id} has contract versions but no contract", account_id=account_id)
    for row in contracts:
        count, highest = versions.get(row.account_id, (0, 0))
        if count != row.current_version or highest != row.current_version:
            add("contract_versions", f"contract {row.account_id} has current_version {row.current_version} but {count} versions up to {highest}", account_id=row.account_id)
        keys, size = stored.get(row.account_id, (0, 0))
        if (keys, size) != (row.storage_keys, row.storage_bytes):
            add(
                "contract_storage_totals",
                f"contract {row.account_id} records {row.storage_keys} keys and {row.storage_bytes} bytes but its storage holds {keys} keys and {size} bytes",
                account_id=row.account_id,
            )
        if row.status == ContractStatus.DELETED and keys:
            add("deleted_contract_storage", f"deleted contract {row.account_id} still has {keys} storage rows", account_id=row.account_id)
    creators = (
        select(contract_version.c.account_id, contract_version.c.version, execution.c.execution_id, execution.c.type, execution.c.status, execution.c.initiator_id)
        .join(execution, execution.c.execution_id == contract_version.c.created_execution_id)
        .where(
            or_(
                execution.c.type != ExecutionType.CONTRACT_UPDATE.value,
                execution.c.status != ExecutionStatus.SUCCESS.value,
                execution.c.initiator_id != contract_version.c.account_id,
            )
        )
        .order_by(contract_version.c.account_id, contract_version.c.version)
    )
    for creator in await conn.execute(creators):
        add(
            "contract_version_execution",
            f"version {creator.version} of contract {creator.account_id} was created by execution {creator.execution_id} ({creator.type}, {creator.status}, initiated by {creator.initiator_id})",
            account_id=creator.account_id,
        )
    entries = select(contract_storage.c.account_id, contract_storage.c.storage_key, contract_storage.c.encoded_value).order_by(contract_storage.c.account_id, contract_storage.c.storage_key)
    for entry in await conn.execute(entries):
        try:
            storage_codec.decode(bytes(entry.encoded_value))
        except StorageDecodeError as exc:
            add("contract_storage_value", f"storage key {entry.storage_key!r} of contract {entry.account_id} is not a canonical value: {exc}", account_id=entry.account_id)


async def verify_invariants(db: Db) -> InvariantReport:
    """Check every ledger invariant on one consistent snapshot and return the report; a connection inside consistent_snapshot() is read as is, so a backup can export and verify the same snapshot, and one in any other transaction raises ValueError."""
    async with consistent_snapshot(db, allow_open_transaction=False) as conn:
        return await _collect(conn)
