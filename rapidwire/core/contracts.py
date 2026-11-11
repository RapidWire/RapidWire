"""Contract management: registering a verified binary as a new version, extending the update lock to a time or by a duration, pausing, resuming and deleting, each a recorded execution initiated by the contract's own account; public reads of contracts, versions, public functions and storage; and the row lock and storage writes the runtime uses inside an open LedgerTx.

A contract belongs to one user account, which is its owner and the only initiator of its management operations.
Only an active contract can be locked, and while locked_until lies in the future the contract cannot be updated, paused, resumed or deleted and the lock can only be extended, so a locked contract is always active.
Deleting clears the storage and keeps the row and every version, so registering again revives the row with the next version; a deleted contract has no lock, since deletion requires an unlocked contract.
Updating a paused contract keeps it paused, and reviving a deleted one makes it active.
No gas is charged for management operations.
"""

from __future__ import annotations

import hashlib
import weakref
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final, Protocol

from sqlalchemy import RowMapping, delete, func, insert, select, update
from sqlalchemy.dialects.mysql import insert as mysql_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract import storage_codec
from rapidwire.contract.errors import VerificationError
from rapidwire.contract.format import decode as decode_binary
from rapidwire.contract.storage_codec import StorageKeyError
from rapidwire.contract.types import Type
from rapidwire.contract.values import Value
from rapidwire.contract.verifier import VerifiedProgram, verify
from rapidwire.core.clock import ensure_utc
from rapidwire.core.db import Db, reader
from rapidwire.core.errors import ContractLocked, ContractNotFound, ContractPaused, ContractStorageLimit, InvalidArgument, InvalidContract, InvalidState
from rapidwire.core.ledger import DUPLICATE_KEY_ERROR, LedgerTx, LedgerUsageError, OperationResult, RerunTransaction, mysql_error_code, run_operation
from rapidwire.core.paging import (
    CONTRACT_STORAGE_PER_PAGE_DEFAULT,
    CONTRACT_STORAGE_PER_PAGE_MAX,
    CONTRACT_VERSIONS_PER_PAGE_DEFAULT,
    CONTRACT_VERSIONS_PER_PAGE_MAX,
    Page,
    read_page,
)
from rapidwire.core.types import ExecutionType
from rapidwire.core.validation import require_int_in_range, require_user_account
from rapidwire.db.tables import (
    CONTRACT_MAX_SIZE,
    CONTRACT_STORAGE_KEY_MAX,
    CONTRACT_STORAGE_MAX_BYTES,
    CONTRACT_STORAGE_MAX_KEYS,
    CONTRACT_STORAGE_VALUE_MAX,
    ContractStatus,
    contract,
    contract_storage,
    contract_version,
)

__all__ = [
    "CONTRACT_MAX_SIZE",
    "CONTRACT_STORAGE_KEY_MAX",
    "CONTRACT_STORAGE_MAX_BYTES",
    "CONTRACT_STORAGE_MAX_KEYS",
    "CONTRACT_STORAGE_PER_PAGE_DEFAULT",
    "CONTRACT_STORAGE_PER_PAGE_MAX",
    "CONTRACT_STORAGE_VALUE_MAX",
    "CONTRACT_VERSIONS_PER_PAGE_DEFAULT",
    "CONTRACT_VERSIONS_PER_PAGE_MAX",
    "LOCK_HOURS_MAX",
    "ContractBinary",
    "ContractDeleted",
    "ContractDetails",
    "ContractInfo",
    "ContractLockExtended",
    "ContractRegistered",
    "ContractStatus",
    "ContractStatusChanged",
    "ContractVersion",
    "ContractVersionSummary",
    "LockedContract",
    "PublicFunction",
    "StorageEntry",
    "StorageWrite",
    "check_binary",
    "contract_lock",
    "delete_contract",
    "delete_contract_in_tx",
    "delete_storage_in_tx",
    "extend_lock_in_tx",
    "get_contract",
    "get_contract_version",
    "get_contract_with_functions",
    "get_storage_entry",
    "is_locked",
    "list_contract_versions",
    "list_public_functions",
    "list_storage",
    "lock_contract",
    "lock_contract_in_tx",
    "pause_contract",
    "pause_contract_in_tx",
    "public_functions",
    "put_storage_in_tx",
    "read_storage",
    "register_contract",
    "register_contract_in_tx",
    "resume_contract",
    "resume_contract_in_tx",
]

# The longest lock a duration can ask for: 100 years of 365 days.
LOCK_HOURS_MAX: Final = 876_000

_OWNER = "contract owner"

# The contract rows each open LedgerTx has locked, mapped to True for FOR UPDATE and False for FOR SHARE; row locks outlive savepoint rollbacks, so entries are never removed.
_ROW_LOCKS: weakref.WeakKeyDictionary[LedgerTx, dict[int, bool]] = weakref.WeakKeyDictionary()


def is_locked(locked_until: datetime | None, now: datetime) -> bool:
    """Whether a lock ending at locked_until is in force at now; a lock ends at the instant it names."""
    return locked_until is not None and locked_until > now


@dataclass(frozen=True)
class ContractBinary:
    """A binary that passed verify(), with its SHA-256 as lowercase hex and its size in bytes."""

    binary: bytes
    sha256: str
    size: int
    program: VerifiedProgram = field(compare=False, repr=False)


def check_binary(binary: bytes, *, max_size: int) -> ContractBinary:
    """Run the registration checks on binary with max_size as the byte limit; raises InvalidContract carrying the verifier's message, or InvalidArgument for a max_size outside 1 to CONTRACT_MAX_SIZE."""
    require_int_in_range("max_size", max_size, 1, CONTRACT_MAX_SIZE)
    if not isinstance(binary, bytes | bytearray | memoryview):
        raise InvalidContract("The contract binary must be bytes.")
    data = bytes(binary)
    try:
        program = verify(data, max_size=max_size)
    except VerificationError as exc:
        raise InvalidContract(str(exc)) from None
    return ContractBinary(binary=data, sha256=hashlib.sha256(data).hexdigest(), size=len(data), program=program)


@dataclass(frozen=True)
class ContractRegistered:
    """A stored version; revived is True when it brought a deleted contract back, and status is the contract's status afterwards (a paused contract stays paused); replayed takes no part in equality."""

    execution_id: int
    account_id: int
    version: int
    sha256: str
    size: int
    status: ContractStatus
    revived: bool
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "version": self.version, "sha256": self.sha256, "size": self.size, "status": self.status.value, "revived": self.revived}

    @classmethod
    def from_operation(cls, op: OperationResult) -> ContractRegistered:
        return cls(
            execution_id=op.execution_id,
            account_id=op.int_field("account_id"),
            version=op.int_field("version"),
            sha256=str(op.output["sha256"]),
            size=op.int_field("size"),
            status=ContractStatus(str(op.output["status"])),
            revived=op.output.get("revived") is True,
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class ContractLockExtended:
    """The new end of the update lock and the end it replaced (None when there was no lock or it had ended); replayed takes no part in equality."""

    execution_id: int
    account_id: int
    locked_until: datetime
    previous_locked_until: datetime | None
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "locked_until": self.locked_until, "previous_locked_until": self.previous_locked_until}

    @classmethod
    def from_operation(cls, op: OperationResult) -> ContractLockExtended:
        previous = op.output.get("previous_locked_until")
        return cls(
            execution_id=op.execution_id,
            account_id=op.int_field("account_id"),
            locked_until=datetime.fromisoformat(str(op.output["locked_until"])),
            previous_locked_until=datetime.fromisoformat(previous) if isinstance(previous, str) else None,
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class ContractStatusChanged:
    """The status after a pause or resume; replayed takes no part in equality."""

    execution_id: int
    account_id: int
    status: ContractStatus
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "status": self.status.value}

    @classmethod
    def from_operation(cls, op: OperationResult) -> ContractStatusChanged:
        return cls(execution_id=op.execution_id, account_id=op.int_field("account_id"), status=ContractStatus(str(op.output["status"])), replayed=op.replayed)


@dataclass(frozen=True)
class ContractDeleted:
    """A deleted contract, the version it ran last and the storage that deletion cleared; replayed takes no part in equality."""

    execution_id: int
    account_id: int
    version: int
    storage_keys_cleared: int
    storage_bytes_cleared: int
    replayed: bool = field(default=False, compare=False)

    def to_output(self) -> dict[str, Any]:
        return {"account_id": self.account_id, "version": self.version, "storage_keys_cleared": self.storage_keys_cleared, "storage_bytes_cleared": self.storage_bytes_cleared}

    @classmethod
    def from_operation(cls, op: OperationResult) -> ContractDeleted:
        return cls(
            execution_id=op.execution_id,
            account_id=op.int_field("account_id"),
            version=op.int_field("version"),
            storage_keys_cleared=op.int_field("storage_keys_cleared"),
            storage_bytes_cleared=op.int_field("storage_bytes_cleared"),
            replayed=op.replayed,
        )


@dataclass(frozen=True)
class LockedContract:
    """A contract row locked by lock_contract_in_tx(); binary and sha256 are those of the current version, None for a deleted contract."""

    account_id: int
    status: ContractStatus
    current_version: int
    locked_until: datetime | None
    storage_keys: int
    storage_bytes: int
    binary: bytes | None
    sha256: str | None


@dataclass(frozen=True)
class ContractInfo:
    """A contract's public state; current_version, sha256 and size describe the latest registered version, which a deleted contract keeps as history but no longer runs."""

    account_id: int
    status: ContractStatus
    current_version: int
    sha256: str
    size: int
    version_count: int
    locked_until: datetime | None
    storage_keys: int
    storage_bytes: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    created_execution_id: int
    updated_execution_id: int

    def locked_at(self, now: datetime) -> bool:
        return is_locked(self.locked_until, now)


@dataclass(frozen=True)
class ContractVersionSummary:
    """A version without its binary, with the execution that registered it."""

    account_id: int
    version: int
    sha256: str
    size: int
    created_execution_id: int
    created_at: datetime


@dataclass(frozen=True)
class ContractVersion:
    """One registered version with its binary exactly as received."""

    account_id: int
    version: int
    binary: bytes
    sha256: str
    size: int
    created_execution_id: int
    created_at: datetime


@dataclass(frozen=True)
class PublicFunction:
    """A public function of a contract's current version; parameters are positional and carry no names in the binary."""

    name: str
    params: tuple[Type, ...]
    returns: Type

    @property
    def signature(self) -> str:
        return f"{self.name}({', '.join(str(param) for param in self.params)}) -> {self.returns}"


@dataclass(frozen=True)
class StorageEntry:
    """A decoded storage entry; size is the length of its encoded value."""

    key: str
    value: Value
    size: int
    updated_at: datetime


@dataclass(frozen=True)
class StorageWrite:
    """The effect of a storage write or delete: the encoded size written (0 for a delete) and replaced (0 when the key was absent), and the contract's totals afterwards."""

    key: str
    size: int
    previous_size: int
    storage_keys: int
    storage_bytes: int


def contract_lock(tx: LedgerTx, account_id: int) -> str | None:
    """How tx holds the contract row of account_id: "update", "share", or None when it has not locked it."""
    held = _ROW_LOCKS.get(tx, {}).get(account_id)
    if held is None:
        return None
    return "update" if held else "share"


def _record_lock(tx: LedgerTx, account_id: int, exclusive: bool) -> None:
    locks = _ROW_LOCKS.setdefault(tx, {})
    locks[account_id] = exclusive or locks.get(account_id, False)


async def _select_row(tx: LedgerTx, account_id: int, *, for_update: bool) -> RowMapping | None:
    """Read and lock the contract row; a row this transaction holds FOR UPDATE is read FOR UPDATE again, and asking FOR UPDATE for one it holds FOR SHARE raises LedgerUsageError, since InnoDB upgrades such locks only through deadlock-prone waits."""
    held = _ROW_LOCKS.get(tx, {}).get(account_id)
    if held is False and for_update:
        raise LedgerUsageError(f"contract {account_id} is already locked FOR SHARE in this transaction; lock it FOR UPDATE on the first call")
    exclusive = for_update or held is True
    stmt = select(contract).where(contract.c.account_id == account_id).with_for_update(read=not exclusive)
    row = (await tx.conn.execute(stmt)).mappings().one_or_none()
    if row is not None:
        _record_lock(tx, account_id, exclusive)
    return row


async def lock_contract_in_tx(tx: LedgerTx, account_id: int, *, for_update: bool) -> LockedContract | None:
    """Lock the contract row of account_id FOR UPDATE or FOR SHARE and return it with the current binary, or None when the account never registered one.

    A contract execution locks the executing contract after the gas currency and before any currency or pool its code reaches, relying on the deadlock retry of the whole transaction for the currencies and pools locked later; management operations lock only the contract row.
    A contract whose storage the execution may write must be locked FOR UPDATE on the first call, because a FOR SHARE lock is never upgraded (LedgerUsageError).
    A deleted contract is returned with binary None; callers decide whether a paused or deleted contract may run.
    """
    row = await _select_row(tx, account_id, for_update=for_update)
    if row is None:
        return None
    status = ContractStatus(row["status"])
    binary: bytes | None = None
    sha256: str | None = None
    if status is not ContractStatus.DELETED:
        stmt = select(contract_version.c.code, contract_version.c.sha256).where(contract_version.c.account_id == account_id, contract_version.c.version == row["current_version"])
        version_row = (await tx.conn.execute(stmt)).one()
        binary, sha256 = bytes(version_row.code), bytes(version_row.sha256).hex()
    return LockedContract(
        account_id=account_id,
        status=status,
        current_version=row["current_version"],
        locked_until=row["locked_until"],
        storage_keys=row["storage_keys"],
        storage_bytes=row["storage_bytes"],
        binary=binary,
        sha256=sha256,
    )


async def _require_live(tx: LedgerTx, account_id: int) -> RowMapping:
    """The non-deleted contract row of account_id locked FOR UPDATE; raises ContractNotFound otherwise."""
    row = await _select_row(tx, account_id, for_update=True)
    if row is None or row["status"] == ContractStatus.DELETED:
        raise ContractNotFound()
    return row


def _require_unlocked(row: RowMapping, now: datetime) -> None:
    if is_locked(row["locked_until"], now):
        raise ContractLocked(f"The contract is locked until {row['locked_until'].isoformat()}.")


async def register_contract_in_tx(tx: LedgerTx, execution_id: int, *, account_id: int, contract_binary: ContractBinary) -> ContractRegistered:
    """Store contract_binary as the next version of account_id's contract, creating or reviving the contract row; raises ContractLocked while a live contract is locked.

    Two first registrations of one account race on the version row's primary key, and the loser reruns its transaction, which then finds the winner's row.
    """
    require_user_account(account_id, _OWNER)
    row = await _select_row(tx, account_id, for_update=True)
    revived = False
    if row is None:
        version = 1
        status = ContractStatus.ACTIVE
    else:
        revived = row["status"] == ContractStatus.DELETED
        if not revived:
            _require_unlocked(row, tx.now)
        version = row["current_version"] + 1
        status = ContractStatus.ACTIVE if revived else ContractStatus(row["status"])
    version_values = {
        "account_id": account_id,
        "version": version,
        "code": contract_binary.binary,
        "sha256": bytes.fromhex(contract_binary.sha256),
        "size": contract_binary.size,
        "created_execution_id": execution_id,
        "created_at": tx.now,
    }
    try:
        await tx.conn.execute(insert(contract_version).values(**version_values))
    except IntegrityError as exc:
        if row is None and mysql_error_code(exc) == DUPLICATE_KEY_ERROR:
            if not tx.rerun_allowed:
                raise InvalidState("Another registration of this contract is in progress; try again.") from None
            raise RerunTransaction(f"contract {account_id} was registered concurrently") from exc
        raise
    if row is None:
        await tx.conn.execute(
            insert(contract).values(
                account_id=account_id,
                status=status.value,
                current_version=version,
                locked_until=None,
                storage_keys=0,
                storage_bytes=0,
                created_execution_id=execution_id,
                updated_execution_id=execution_id,
                created_at=tx.now,
                updated_at=tx.now,
                deleted_at=None,
            )
        )
        _record_lock(tx, account_id, True)
    else:
        values: dict[str, Any] = {"current_version": version, "status": status.value, "updated_execution_id": execution_id, "updated_at": tx.now, "deleted_at": None}
        await tx.conn.execute(update(contract).where(contract.c.account_id == account_id).values(**values))
    return ContractRegistered(
        execution_id=execution_id,
        account_id=account_id,
        version=version,
        sha256=contract_binary.sha256,
        size=contract_binary.size,
        status=status,
        revived=revived,
    )


async def register_contract(
    engine: AsyncEngine,
    *,
    account_id: int,
    binary: bytes,
    max_size: int,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> ContractRegistered:
    """Verify binary against max_size (callers pass Settings.contract_max_size) and store it as a contract_update execution initiated by account_id, whose input records the SHA-256 and size rather than the binary.

    An invalid binary raises InvalidContract and an account ID that is not a user's raises InvalidArgument before anything is recorded; a locked contract is a recorded failure.
    """
    require_user_account(account_id, _OWNER)
    checked = check_binary(binary, max_size=max_size)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await register_contract_in_tx(tx, execution_id, account_id=account_id, contract_binary=checked)).to_output()

    op = await run_operation(
        engine,
        type=ExecutionType.CONTRACT_UPDATE,
        initiator_id=account_id,
        input={"account_id": account_id, "sha256": checked.sha256, "size": checked.size},
        body=body,
        now=now,
        idempotency_key=idempotency_key,
    )
    return ContractRegistered.from_operation(op)


def _lock_end(until: datetime | None, hours: int | None) -> datetime | None:
    """The validated lock end given as a time, or None for a duration, which the transaction resolves against its own time; raises InvalidArgument unless exactly one of an aware until and hours from 1 to LOCK_HOURS_MAX is given."""
    if (until is None) == (hours is None):
        raise InvalidArgument("Give exactly one of a lock end time and a lock duration in hours.")
    if hours is not None:
        require_int_in_range("hours", hours, 1, LOCK_HOURS_MAX)
        return None
    if not isinstance(until, datetime) or until.tzinfo is None or until.utcoffset() is None:
        raise InvalidArgument("The lock end must be a timezone-aware time.")
    return ensure_utc(until)


async def extend_lock_in_tx(tx: LedgerTx, execution_id: int, *, account_id: int, until: datetime | None = None, hours: int | None = None) -> ContractLockExtended:
    """Set the update lock of account_id's active contract to end at until, or hours after the transaction's time, which must be later than both now (InvalidArgument) and the current end (InvalidState); raises ContractNotFound for a missing or deleted contract and InvalidState for a paused one."""
    require_user_account(account_id, _OWNER)
    end = _lock_end(until, hours)
    until = tx.now + timedelta(hours=hours or 0) if end is None else end
    row = await _require_live(tx, account_id)
    if row["status"] != ContractStatus.ACTIVE:
        raise InvalidState("Only an active contract can be locked; resume it first.")
    if until <= tx.now:
        raise InvalidArgument("The lock must end in the future.")
    previous: datetime | None = row["locked_until"]
    if previous is not None and until <= previous:
        raise InvalidState(f"A lock can only be extended; it already lasts until {previous.isoformat()}.")
    await tx.conn.execute(update(contract).where(contract.c.account_id == account_id).values(locked_until=until, updated_execution_id=execution_id, updated_at=tx.now))
    return ContractLockExtended(
        execution_id=execution_id,
        account_id=account_id,
        locked_until=until,
        previous_locked_until=previous if is_locked(previous, tx.now) else None,
    )


async def lock_contract(
    engine: AsyncEngine,
    *,
    account_id: int,
    until: datetime | None = None,
    hours: int | None = None,
    now: datetime | None = None,
    idempotency_key: str | None = None,
) -> ContractLockExtended:
    """Run extend_lock_in_tx as a contract_lock execution initiated by account_id, with exactly one of until (a timezone-aware time) and hours (1 to LOCK_HOURS_MAX), else InvalidArgument before anything is recorded.

    The input records what was asked, {"account_id", "until"} or {"account_id", "hours"}, and a duration ends hours after the execution's time, so repeating a duration request with the same idempotency key replays it instead of conflicting.
    """
    require_user_account(account_id, _OWNER)
    end = _lock_end(until, hours)
    asked: dict[str, Any] = {"until": end} if end is not None else {"hours": hours}

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await extend_lock_in_tx(tx, execution_id, account_id=account_id, until=end, hours=hours if end is None else None)).to_output()

    op = await run_operation(engine, type=ExecutionType.CONTRACT_LOCK, initiator_id=account_id, input={"account_id": account_id, **asked}, body=body, now=now, idempotency_key=idempotency_key)
    return ContractLockExtended.from_operation(op)


async def pause_contract_in_tx(tx: LedgerTx, execution_id: int, *, account_id: int) -> ContractStatusChanged:
    """Pause an active, unlocked contract; raises ContractNotFound, ContractLocked, or InvalidState when it is already paused."""
    require_user_account(account_id, _OWNER)
    row = await _require_live(tx, account_id)
    _require_unlocked(row, tx.now)
    if row["status"] != ContractStatus.ACTIVE:
        raise InvalidState("The contract is already paused.")
    await tx.conn.execute(update(contract).where(contract.c.account_id == account_id).values(status=ContractStatus.PAUSED.value, updated_execution_id=execution_id, updated_at=tx.now))
    return ContractStatusChanged(execution_id=execution_id, account_id=account_id, status=ContractStatus.PAUSED)


async def resume_contract_in_tx(tx: LedgerTx, execution_id: int, *, account_id: int) -> ContractStatusChanged:
    """Resume a paused, unlocked contract; raises ContractNotFound, ContractLocked, or InvalidState when it is not paused."""
    require_user_account(account_id, _OWNER)
    row = await _require_live(tx, account_id)
    _require_unlocked(row, tx.now)
    if row["status"] != ContractStatus.PAUSED:
        raise InvalidState("The contract is not paused.")
    await tx.conn.execute(update(contract).where(contract.c.account_id == account_id).values(status=ContractStatus.ACTIVE.value, updated_execution_id=execution_id, updated_at=tx.now))
    return ContractStatusChanged(execution_id=execution_id, account_id=account_id, status=ContractStatus.ACTIVE)


async def delete_contract_in_tx(tx: LedgerTx, execution_id: int, *, account_id: int) -> ContractDeleted:
    """Delete an unlocked contract, active or paused, and clear its storage; the row and its versions stay; raises ContractNotFound or ContractLocked."""
    require_user_account(account_id, _OWNER)
    row = await _require_live(tx, account_id)
    _require_unlocked(row, tx.now)
    await tx.conn.execute(delete(contract_storage).where(contract_storage.c.account_id == account_id))
    values: dict[str, Any] = {
        "status": ContractStatus.DELETED.value,
        "deleted_at": tx.now,
        "locked_until": None,
        "storage_keys": 0,
        "storage_bytes": 0,
        "updated_execution_id": execution_id,
        "updated_at": tx.now,
    }
    await tx.conn.execute(update(contract).where(contract.c.account_id == account_id).values(**values))
    return ContractDeleted(
        execution_id=execution_id,
        account_id=account_id,
        version=row["current_version"],
        storage_keys_cleared=row["storage_keys"],
        storage_bytes_cleared=row["storage_bytes"],
    )


class _ManagementStep(Protocol):
    async def __call__(self, tx: LedgerTx, execution_id: int, *, account_id: int) -> ContractStatusChanged | ContractDeleted: ...


async def _manage(engine: AsyncEngine, type: ExecutionType, account_id: int, in_tx: _ManagementStep, now: datetime | None, idempotency_key: str | None) -> OperationResult:
    require_user_account(account_id, _OWNER)

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return (await in_tx(tx, execution_id, account_id=account_id)).to_output()

    return await run_operation(engine, type=type, initiator_id=account_id, input={"account_id": account_id}, body=body, now=now, idempotency_key=idempotency_key)


async def pause_contract(engine: AsyncEngine, *, account_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> ContractStatusChanged:
    """Run pause_contract_in_tx as a contract_pause execution initiated by account_id."""
    return ContractStatusChanged.from_operation(await _manage(engine, ExecutionType.CONTRACT_PAUSE, account_id, pause_contract_in_tx, now, idempotency_key))


async def resume_contract(engine: AsyncEngine, *, account_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> ContractStatusChanged:
    """Run resume_contract_in_tx as a contract_resume execution initiated by account_id."""
    return ContractStatusChanged.from_operation(await _manage(engine, ExecutionType.CONTRACT_RESUME, account_id, resume_contract_in_tx, now, idempotency_key))


async def delete_contract(engine: AsyncEngine, *, account_id: int, now: datetime | None = None, idempotency_key: str | None = None) -> ContractDeleted:
    """Run delete_contract_in_tx as a contract_delete execution initiated by account_id."""
    return ContractDeleted.from_operation(await _manage(engine, ExecutionType.CONTRACT_DELETE, account_id, delete_contract_in_tx, now, idempotency_key))


async def _storage_totals(tx: LedgerTx, account_id: int) -> tuple[int, int]:
    """The storage counters of an active contract whose row tx already holds FOR UPDATE; raises LedgerUsageError without that lock, ContractNotFound for a missing or deleted contract and ContractPaused for a paused one."""
    if contract_lock(tx, account_id) != "update":
        raise LedgerUsageError(f"contract {account_id} must be locked FOR UPDATE with lock_contract_in_tx() before its storage is written")
    row = await _require_live(tx, account_id)
    if row["status"] == ContractStatus.PAUSED:
        raise ContractPaused()
    return row["storage_keys"], row["storage_bytes"]


async def _previous_size(tx: LedgerTx, account_id: int, key: str) -> int | None:
    stmt = select(contract_storage.c.value_size).where(contract_storage.c.account_id == account_id, contract_storage.c.storage_key == key).with_for_update()
    return (await tx.conn.execute(stmt)).scalar_one_or_none()


async def _set_totals(tx: LedgerTx, account_id: int, keys: int, total: int) -> None:
    await tx.conn.execute(update(contract).where(contract.c.account_id == account_id).values(storage_keys=keys, storage_bytes=total))


async def put_storage_in_tx(tx: LedgerTx, account_id: int, key: str, data: bytes) -> StorageWrite:
    """Store the encoded value data (from storage_codec.encode or values.encode_value, stored in canonical form of the same length) under key in the storage of account_id's active contract and update its counters.

    The transaction must already hold the contract row FOR UPDATE through lock_contract_in_tx(tx, account_id, for_update=True), else LedgerUsageError.
    Only an active contract's storage changes: a paused contract cannot run, so a write to it raises ContractPaused, and a missing or deleted one raises ContractNotFound.
    Raises StorageKeyError (a contract runtime error) for a bad key, StorageDecodeError for bytes no encoder produces within the value limit, and ContractStorageLimit when the key count or byte total would pass CONTRACT_STORAGE_MAX_KEYS or CONTRACT_STORAGE_MAX_BYTES.
    """
    storage_codec.validate_key(key)
    encoded = storage_codec.canonicalize(data)
    keys, total = await _storage_totals(tx, account_id)
    previous = await _previous_size(tx, account_id, key)
    new_keys = keys + (1 if previous is None else 0)
    new_total = total - (0 if previous is None else storage_codec.entry_size(key, previous)) + storage_codec.entry_size(key, len(encoded))
    if new_keys > CONTRACT_STORAGE_MAX_KEYS:
        raise ContractStorageLimit(f"A contract can store at most {CONTRACT_STORAGE_MAX_KEYS} keys.")
    if new_total > CONTRACT_STORAGE_MAX_BYTES:
        raise ContractStorageLimit(f"A contract can store at most {CONTRACT_STORAGE_MAX_BYTES} bytes.")
    stmt = mysql_insert(contract_storage).values(account_id=account_id, storage_key=key, encoded_value=encoded, value_size=len(encoded), updated_at=tx.now)
    await tx.conn.execute(stmt.on_duplicate_key_update(encoded_value=stmt.inserted.encoded_value, value_size=stmt.inserted.value_size, updated_at=stmt.inserted.updated_at))
    await _set_totals(tx, account_id, new_keys, new_total)
    return StorageWrite(key=key, size=len(encoded), previous_size=previous or 0, storage_keys=new_keys, storage_bytes=new_total)


async def delete_storage_in_tx(tx: LedgerTx, account_id: int, key: str) -> StorageWrite:
    """Remove key from the storage of account_id's active contract and update its counters; previous_size is 0 when the key was absent, and the required lock and the errors are those of put_storage_in_tx()."""
    storage_codec.validate_key(key)
    keys, total = await _storage_totals(tx, account_id)
    previous = await _previous_size(tx, account_id, key)
    if previous is None:
        return StorageWrite(key=key, size=0, previous_size=0, storage_keys=keys, storage_bytes=total)
    await tx.conn.execute(delete(contract_storage).where(contract_storage.c.account_id == account_id, contract_storage.c.storage_key == key))
    new_keys, new_total = keys - 1, total - storage_codec.entry_size(key, previous)
    await _set_totals(tx, account_id, new_keys, new_total)
    return StorageWrite(key=key, size=0, previous_size=previous, storage_keys=new_keys, storage_bytes=new_total)


def _versions_count() -> Any:
    versions = contract_version.alias("versions")
    return select(func.count()).select_from(versions).where(versions.c.account_id == contract.c.account_id).correlate(contract).scalar_subquery()


def _contract_query(*extra: Any) -> Any:
    """The contract row joined with its current version's hash, size and any extra version columns, plus the version count, as one statement."""
    return select(
        contract,
        contract_version.c.sha256,
        contract_version.c.size,
        *extra,
        _versions_count().label("version_count"),
    ).join(contract_version, (contract_version.c.account_id == contract.c.account_id) & (contract_version.c.version == contract.c.current_version))


def _contract_info(row: RowMapping) -> ContractInfo:
    return ContractInfo(
        account_id=row["account_id"],
        status=ContractStatus(row["status"]),
        current_version=row["current_version"],
        sha256=bytes(row["sha256"]).hex(),
        size=row["size"],
        version_count=int(row["version_count"]),
        locked_until=row["locked_until"],
        storage_keys=row["storage_keys"],
        storage_bytes=row["storage_bytes"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        deleted_at=row["deleted_at"],
        created_execution_id=row["created_execution_id"],
        updated_execution_id=row["updated_execution_id"],
    )


async def get_contract(db: Db, account_id: int) -> ContractInfo | None:
    """The contract of account_id, deleted ones included, or None when the account never registered one."""
    async with reader(db) as conn:
        row = (await conn.execute(_contract_query().where(contract.c.account_id == account_id))).mappings().one_or_none()
    return None if row is None else _contract_info(row)


@dataclass(frozen=True)
class ContractDetails:
    """A contract's state with the binary and public functions of exactly the version it describes, read in one statement; binary is None and functions empty for a deleted contract."""

    info: ContractInfo
    functions: tuple[PublicFunction, ...]
    binary: bytes | None


def public_functions(binary: bytes) -> tuple[PublicFunction, ...]:
    """The public functions of a registered (verified) binary in name order."""
    program = decode_binary(binary)
    functions = []
    for entry in program.functions:
        name = program.constants[entry.name_index]
        if entry.public and isinstance(name, str):
            functions.append(PublicFunction(name=name, params=entry.params, returns=entry.returns))
    return tuple(sorted(functions, key=lambda function: function.name))


async def get_contract_with_functions(db: Db, account_id: int) -> ContractDetails | None:
    """The contract of account_id (deleted ones included) with the binary and public functions (in name order) of its current version, all from the same statement, so a concurrent registration never mixes two versions; None when the account never registered one."""
    async with reader(db) as conn:
        row = (await conn.execute(_contract_query(contract_version.c.code).where(contract.c.account_id == account_id))).mappings().one_or_none()
    if row is None:
        return None
    info = _contract_info(row)
    if info.status is ContractStatus.DELETED:
        return ContractDetails(info=info, functions=(), binary=None)
    binary = bytes(row["code"])
    return ContractDetails(info=info, functions=public_functions(binary), binary=binary)


async def get_contract_version(db: Db, account_id: int, version: int | None = None) -> ContractVersion | None:
    """A registered version of account_id's contract with its binary, which anyone may fetch; version None means the current version and finds nothing for a deleted contract, while a numbered version is found whatever the contract's status."""
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
        raise InvalidArgument("The version must be an integer.")
    columns = (
        contract_version.c.account_id,
        contract_version.c.version,
        contract_version.c.code,
        contract_version.c.sha256,
        contract_version.c.size,
        contract_version.c.created_execution_id,
        contract_version.c.created_at,
    )
    if version is None:
        stmt = (
            select(*columns)
            .join(contract, (contract.c.account_id == contract_version.c.account_id) & (contract.c.current_version == contract_version.c.version))
            .where(contract.c.account_id == account_id, contract.c.status != ContractStatus.DELETED.value)
        )
    else:
        stmt = select(*columns).where(contract_version.c.account_id == account_id, contract_version.c.version == version)
    async with reader(db) as conn:
        row = (await conn.execute(stmt)).one_or_none()
    if row is None:
        return None
    return ContractVersion(
        account_id=row.account_id,
        version=row.version,
        binary=bytes(row.code),
        sha256=bytes(row.sha256).hex(),
        size=row.size,
        created_execution_id=row.created_execution_id,
        created_at=row.created_at,
    )


async def list_contract_versions(db: Db, account_id: int, *, page: int = 1, per_page: int = CONTRACT_VERSIONS_PER_PAGE_DEFAULT) -> Page[ContractVersionSummary]:
    """One page of the versions of account_id's contract, newest first, without binaries."""
    stmt = (
        select(
            contract_version.c.account_id,
            contract_version.c.version,
            contract_version.c.sha256,
            contract_version.c.size,
            contract_version.c.created_execution_id,
            contract_version.c.created_at,
        )
        .where(contract_version.c.account_id == account_id)
        .order_by(contract_version.c.version.desc())
    )
    return await read_page(
        db,
        stmt,
        key=contract_version.c.version,
        page=page,
        per_page=per_page,
        per_page_max=CONTRACT_VERSIONS_PER_PAGE_MAX,
        convert=lambda row: ContractVersionSummary(
            account_id=row.account_id,
            version=row.version,
            sha256=bytes(row.sha256).hex(),
            size=row.size,
            created_execution_id=row.created_execution_id,
            created_at=row.created_at,
        ),
    )


async def list_public_functions(db: Db, account_id: int) -> tuple[PublicFunction, ...]:
    """The public functions of the current version of account_id's contract in name order, empty when there is no contract or it is deleted; a paused contract still lists them."""
    found = await get_contract_version(db, account_id)
    return () if found is None else public_functions(found.binary)


async def read_storage(db: Db, account_id: int, key: str) -> Value | None:
    """The decoded value stored under key by account_id's contract, or None when there is none or key cannot name an entry; storage is public."""
    try:
        storage_codec.validate_key(key)
    except StorageKeyError:
        return None
    stmt = select(contract_storage.c.encoded_value).where(contract_storage.c.account_id == account_id, contract_storage.c.storage_key == key)
    async with reader(db) as conn:
        data = (await conn.execute(stmt)).scalar_one_or_none()
    return None if data is None else storage_codec.decode(bytes(data))


async def get_storage_entry(db: Db, account_id: int, key: str) -> StorageEntry | None:
    """The decoded entry stored under key by account_id's contract with its encoded size and last update, or None when there is none or key cannot name an entry; storage is public."""
    try:
        storage_codec.validate_key(key)
    except StorageKeyError:
        return None
    stmt = select(contract_storage.c.encoded_value, contract_storage.c.value_size, contract_storage.c.updated_at).where(
        contract_storage.c.account_id == account_id, contract_storage.c.storage_key == key
    )
    async with reader(db) as conn:
        row = (await conn.execute(stmt)).one_or_none()
    return None if row is None else StorageEntry(key=key, value=storage_codec.decode(bytes(row.encoded_value)), size=row.value_size, updated_at=row.updated_at)


async def list_storage(db: Db, account_id: int, *, page: int = 1, per_page: int = CONTRACT_STORAGE_PER_PAGE_DEFAULT) -> Page[StorageEntry]:
    """One page of the decoded storage of account_id's contract in key order (binary order of the UTF-8 keys)."""
    stmt = (
        select(contract_storage.c.storage_key, contract_storage.c.encoded_value, contract_storage.c.value_size, contract_storage.c.updated_at)
        .where(contract_storage.c.account_id == account_id)
        .order_by(contract_storage.c.storage_key)
    )
    return await read_page(
        db,
        stmt,
        key=contract_storage.c.storage_key,
        page=page,
        per_page=per_page,
        per_page_max=CONTRACT_STORAGE_PER_PAGE_MAX,
        convert=lambda row: StorageEntry(key=row.storage_key, value=storage_codec.decode(bytes(row.encoded_value)), size=row.value_size, updated_at=row.updated_at),
    )
