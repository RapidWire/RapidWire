from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract import storage_codec
from rapidwire.contract.errors import VerificationError
from rapidwire.contract.format import encode_uvarint
from rapidwire.contract.types import ACCOUNT, BOOL, CURRENCY, INT, STR
from rapidwire.contract.values import Account, Currency, Value, encode_value
from rapidwire.contract.verifier import verify
from rapidwire.core import contracts
from rapidwire.core.accounts import ensure_user_account
from rapidwire.core.contracts import ContractStatus
from rapidwire.core.errors import ContractLocked, ContractNotFound, ContractPaused, ContractStorageLimit, IdempotencyConflict, InvalidArgument, InvalidContract, InvalidState
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import LedgerTx, LedgerUsageError, RerunTransaction
from rapidwire.core.types import ExecutionType
from rapidwire.db.tables import (
    CONTRACT_MAX_SIZE,
    CONTRACT_STORAGE_MAX_BYTES,
    CONTRACT_STORAGE_MAX_KEYS,
    CONTRACT_STORAGE_VALUE_MAX,
    account,
    contract,
    contract_storage,
    contract_version,
    execution,
)
from tests.integration.factories import SHOP_CONTRACT, contract_binary, contract_binary_of_size, delete_storage, put_storage, register_contract

NOW = datetime(2026, 3, 1, tzinfo=UTC)
HIRAGANA_A = chr(0x3042)
OWNER = 4242
OTHER = 5151
MAX_SIZE = 8000


async def _executions(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        rows = (await conn.execute(select(execution).order_by(execution.c.execution_id))).mappings().all()
    return [dict(row) for row in rows]


async def _count(engine: AsyncEngine, table: Any) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(table))).scalar_one())


async def _assert_clean(engine: AsyncEngine) -> None:
    report = await verify_invariants(engine)
    assert report.violations == []


async def test_first_registration_stores_the_binary_exactly(db: AsyncEngine) -> None:
    binary = contract_binary()
    result = await contracts.register_contract(db, account_id=OWNER, binary=binary, max_size=MAX_SIZE, now=NOW)
    sha = hashlib.sha256(binary).hexdigest()
    assert (result.account_id, result.version, result.sha256, result.size, result.status, result.revived, result.replayed) == (OWNER, 1, sha, len(binary), ContractStatus.ACTIVE, False, False)
    [row] = await _executions(db)
    assert row["type"] == ExecutionType.CONTRACT_UPDATE
    assert row["status"] == "success"
    assert row["initiator_id"] == OWNER
    assert row["input"] == {"account_id": str(OWNER), "sha256": sha, "size": str(len(binary))}
    assert row["output"] == {"account_id": str(OWNER), "version": "1", "sha256": sha, "size": str(len(binary)), "status": "active", "revived": False}
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert (info.status, info.current_version, info.sha256, info.size, info.version_count) == (ContractStatus.ACTIVE, 1, sha, len(binary), 1)
    assert (info.locked_until, info.storage_keys, info.storage_bytes) == (None, 0, 0)
    assert (info.created_at, info.updated_at, info.deleted_at, info.created_execution_id, info.updated_execution_id) == (NOW, NOW, None, result.execution_id, result.execution_id)
    version = await contracts.get_contract_version(db, OWNER)
    assert version is not None
    assert (version.binary, version.sha256, version.version, version.size, version.created_execution_id, version.created_at) == (binary, sha, 1, len(binary), result.execution_id, NOW)
    assert await contracts.get_contract(db, OTHER) is None
    assert await contracts.get_contract_version(db, OTHER) is None
    await _assert_clean(db)


async def test_updates_keep_every_version(db: AsyncEngine) -> None:
    binaries = [contract_binary(), contract_binary(SHOP_CONTRACT), contract_binary_of_size(500)]
    results = [await register_contract(db, OWNER, binary, now=NOW + timedelta(minutes=index)) for index, binary in enumerate(binaries)]
    assert [result.version for result in results] == [1, 2, 3]
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert (info.current_version, info.version_count, info.sha256, info.created_at, info.updated_at) == (3, 3, hashlib.sha256(binaries[2]).hexdigest(), NOW, NOW + timedelta(minutes=2))
    assert info.created_execution_id == results[0].execution_id
    assert info.updated_execution_id == results[2].execution_id
    for number, binary in enumerate(binaries, start=1):
        version = await contracts.get_contract_version(db, OWNER, number)
        assert version is not None
        assert (version.binary, version.sha256, version.version) == (binary, hashlib.sha256(binary).hexdigest(), number)
    assert await contracts.get_contract_version(db, OWNER, 4) is None
    page = await contracts.list_contract_versions(db, OWNER, per_page=2)
    assert (page.total, page.pages) == (3, 2)
    assert [(item.version, item.sha256, item.size) for item in page.items] == [(3, hashlib.sha256(binaries[2]).hexdigest(), 500), (2, hashlib.sha256(binaries[1]).hexdigest(), len(binaries[1]))]
    second = await contracts.list_contract_versions(db, OWNER, page=2, per_page=2)
    assert [(item.version, item.created_execution_id) for item in second.items] == [(1, results[0].execution_id)]
    assert (await contracts.list_contract_versions(db, OTHER)).total == 0
    with pytest.raises(InvalidArgument):
        await contracts.list_contract_versions(db, OWNER, per_page=contracts.CONTRACT_VERSIONS_PER_PAGE_MAX + 1)
    with pytest.raises(InvalidArgument):
        await contracts.get_contract_version(db, OWNER, True)
    await _assert_clean(db)


@pytest.mark.parametrize("binary", [b"", b"not a contract", b"RWC1" + b"\x00" * 20, contract_binary()[:-1], contract_binary() + b"\x00"])
async def test_invalid_binary_rejected_before_anything_is_recorded(db: AsyncEngine, binary: bytes) -> None:
    with pytest.raises(VerificationError) as expected:
        verify(binary, max_size=MAX_SIZE)
    with pytest.raises(InvalidContract) as raised:
        await contracts.register_contract(db, account_id=OWNER, binary=binary, max_size=MAX_SIZE, now=NOW)
    assert raised.value.message == str(expected.value)
    assert raised.value.execution_id is None
    assert await _executions(db) == []
    assert await _count(db, account) == 0
    assert await _count(db, contract_version) == 0


async def test_non_bytes_and_bad_arguments_rejected_before_anything_is_recorded(db: AsyncEngine) -> None:
    with pytest.raises(InvalidContract, match="must be bytes"):
        await contracts.register_contract(db, account_id=OWNER, binary="text", max_size=MAX_SIZE)  # type: ignore[arg-type]
    with pytest.raises(InvalidArgument):
        await contracts.register_contract(db, account_id=-5, binary=contract_binary(), max_size=MAX_SIZE)
    with pytest.raises(InvalidArgument):
        await contracts.register_contract(db, account_id=OWNER, binary=contract_binary(), max_size=0)
    with pytest.raises(InvalidArgument):
        await contracts.lock_contract(db, account_id=OWNER, until=datetime(2030, 1, 1))
    for operation in (contracts.pause_contract, contracts.resume_contract, contracts.delete_contract):
        with pytest.raises(InvalidArgument):
            await operation(db, account_id=0)
    assert await _executions(db) == []


async def test_size_limit_is_inclusive(db: AsyncEngine) -> None:
    exact, over = contract_binary_of_size(MAX_SIZE), contract_binary_of_size(MAX_SIZE + 1)
    assert (len(exact), len(over)) == (MAX_SIZE, MAX_SIZE + 1)
    result = await contracts.register_contract(db, account_id=OWNER, binary=exact, max_size=MAX_SIZE, now=NOW)
    assert result.size == MAX_SIZE
    with pytest.raises(InvalidContract, match="8001 bytes, larger than the 8000-byte limit"):
        await contracts.register_contract(db, account_id=OWNER, binary=over, max_size=MAX_SIZE, now=NOW)
    assert len(await _executions(db)) == 1
    with pytest.raises(InvalidContract, match="larger than the 100-byte limit"):
        await contracts.register_contract(db, account_id=OTHER, binary=contract_binary_of_size(101), max_size=100)
    stored = await contracts.get_contract_version(db, OWNER)
    assert stored is not None
    assert stored.binary == exact
    with pytest.raises(InvalidArgument, match="max_size"):
        await contracts.register_contract(db, account_id=OWNER, binary=exact, max_size=CONTRACT_MAX_SIZE + 1, now=NOW)
    assert len(await _executions(db)) == 1


async def test_only_an_active_contract_can_be_locked_and_a_lock_allows_only_extension(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    await contracts.pause_contract(db, account_id=OWNER, now=NOW)
    until = NOW + timedelta(days=30)
    with pytest.raises(InvalidState, match="Only an active contract"):
        await contracts.lock_contract(db, account_id=OWNER, until=until, now=NOW)
    await contracts.resume_contract(db, account_id=OWNER, now=NOW)
    locked = await contracts.lock_contract(db, account_id=OWNER, until=until, now=NOW)
    assert (locked.locked_until, locked.previous_locked_until) == (until, None)
    later = NOW + timedelta(days=1)
    with pytest.raises(ContractLocked) as update_error:
        await register_contract(db, OWNER, contract_binary(SHOP_CONTRACT), now=later)
    assert update_error.value.execution_id is not None
    for operation in (contracts.pause_contract, contracts.resume_contract, contracts.delete_contract):
        with pytest.raises(ContractLocked):
            await operation(db, account_id=OWNER, now=later)
    extended = await contracts.lock_contract(db, account_id=OWNER, until=until + timedelta(days=1), now=later)
    assert extended.previous_locked_until == until
    until = extended.locked_until
    rows = await _executions(db)
    failed = [row for row in rows if row["status"] == "failed"]
    assert [(row["type"], row["output"]["error"]["code"]) for row in failed] == [
        (ExecutionType.CONTRACT_LOCK, "invalid_state"),
        (ExecutionType.CONTRACT_UPDATE, "contract_locked"),
        (ExecutionType.CONTRACT_PAUSE, "contract_locked"),
        (ExecutionType.CONTRACT_RESUME, "contract_locked"),
        (ExecutionType.CONTRACT_DELETE, "contract_locked"),
    ]
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert (info.current_version, info.status, info.locked_until) == (1, ContractStatus.ACTIVE, until)
    assert info.locked_at(later) and not info.locked_at(until)
    # The lock ends at the instant it names, so the contract can be updated and deleted exactly then.
    updated = await register_contract(db, OWNER, contract_binary(SHOP_CONTRACT), now=until)
    assert updated.version == 2
    deleted = await contracts.delete_contract(db, account_id=OWNER, now=until)
    assert deleted.version == 2
    await _assert_clean(db)


async def test_lock_can_only_be_extended(db: AsyncEngine) -> None:
    with pytest.raises(ContractNotFound) as missing:
        await contracts.lock_contract(db, account_id=OWNER, until=NOW + timedelta(days=1), now=NOW)
    assert missing.value.execution_id is not None
    await register_contract(db, OWNER, now=NOW)
    with pytest.raises(InvalidArgument, match="future"):
        await contracts.lock_contract(db, account_id=OWNER, until=NOW, now=NOW)
    first = NOW + timedelta(days=10)
    await contracts.lock_contract(db, account_id=OWNER, until=first, now=NOW)
    for until in (first, first - timedelta(microseconds=1), NOW + timedelta(days=1)):
        with pytest.raises(InvalidState, match="only be extended"):
            await contracts.lock_contract(db, account_id=OWNER, until=until, now=NOW)
    tokyo = first.astimezone(timezone(timedelta(hours=9)))
    with pytest.raises(InvalidState):
        await contracts.lock_contract(db, account_id=OWNER, until=tokyo, now=NOW)
    second = first + timedelta(microseconds=1)
    extended = await contracts.lock_contract(db, account_id=OWNER, until=second, now=NOW)
    assert (extended.locked_until, extended.previous_locked_until) == (second, first)
    # After a lock has ended any future time is an extension, and the ended lock is not reported as the previous one.
    after = second + timedelta(days=1)
    renewed = await contracts.lock_contract(db, account_id=OWNER, until=after + timedelta(hours=1), now=after)
    assert renewed.previous_locked_until is None
    rows = await _executions(db)
    assert [row["type"] for row in rows].count(ExecutionType.CONTRACT_LOCK) == 9
    lock_inputs = [row["input"] for row in rows if row["type"] == ExecutionType.CONTRACT_LOCK and row["status"] == "success"]
    assert lock_inputs[0] == {"account_id": str(OWNER), "until": first.isoformat()}
    failures = [row["output"]["error"]["code"] for row in rows if row["status"] == "failed"]
    assert failures == ["contract_not_found", "invalid_argument", *["invalid_state"] * 4]
    await _assert_clean(db)


async def test_pause_and_resume(db: AsyncEngine) -> None:
    for operation in (contracts.pause_contract, contracts.resume_contract, contracts.delete_contract):
        with pytest.raises(ContractNotFound):
            await operation(db, account_id=OWNER, now=NOW)
    await register_contract(db, OWNER, now=NOW)
    with pytest.raises(InvalidState, match="not paused"):
        await contracts.resume_contract(db, account_id=OWNER, now=NOW)
    paused = await contracts.pause_contract(db, account_id=OWNER, now=NOW)
    assert paused.status is ContractStatus.PAUSED
    with pytest.raises(InvalidState, match="already paused"):
        await contracts.pause_contract(db, account_id=OWNER, now=NOW)
    # Updating a paused contract keeps it paused.
    updated = await register_contract(db, OWNER, contract_binary(SHOP_CONTRACT), now=NOW)
    assert (updated.version, updated.status) == (2, ContractStatus.PAUSED)
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert info.status is ContractStatus.PAUSED
    assert [function.name for function in await contracts.list_public_functions(db, OWNER)] == ["buy", "count", "flag"]
    resumed = await contracts.resume_contract(db, account_id=OWNER, now=NOW)
    assert resumed.status is ContractStatus.ACTIVE
    rows = await _executions(db)
    assert [(row["type"], row["status"]) for row in rows] == [
        (ExecutionType.CONTRACT_PAUSE, "failed"),
        (ExecutionType.CONTRACT_RESUME, "failed"),
        (ExecutionType.CONTRACT_DELETE, "failed"),
        (ExecutionType.CONTRACT_UPDATE, "success"),
        (ExecutionType.CONTRACT_RESUME, "failed"),
        (ExecutionType.CONTRACT_PAUSE, "success"),
        (ExecutionType.CONTRACT_PAUSE, "failed"),
        (ExecutionType.CONTRACT_UPDATE, "success"),
        (ExecutionType.CONTRACT_RESUME, "success"),
    ]
    assert all(row["initiator_id"] == OWNER and row["input"] == {"account_id": str(OWNER)} for row in rows if row["type"] != ExecutionType.CONTRACT_UPDATE)
    await _assert_clean(db)


async def test_delete_clears_storage_and_registering_again_revives(db: AsyncEngine) -> None:
    first = await register_contract(db, OWNER, now=NOW)
    await put_storage(db, OWNER, "a", 1, now=NOW)
    await put_storage(db, OWNER, "b", {"x": [True, Account(7)]}, now=NOW)
    before = await contracts.get_contract(db, OWNER)
    assert before is not None
    await contracts.lock_contract(db, account_id=OWNER, until=NOW + timedelta(hours=1), now=NOW)
    later = NOW + timedelta(hours=2)
    deleted = await contracts.delete_contract(db, account_id=OWNER, now=later)
    assert (deleted.version, deleted.storage_keys_cleared, deleted.storage_bytes_cleared) == (1, 2, before.storage_bytes)
    assert before.storage_bytes == storage_codec.entry_size("a", len(storage_codec.encode(1))) + storage_codec.entry_size("b", len(storage_codec.encode({"x": [True, Account(7)]})))
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert (info.status, info.deleted_at, info.locked_until, info.storage_keys, info.storage_bytes, info.current_version) == (ContractStatus.DELETED, later, None, 0, 0, 1)
    assert await _count(db, contract_storage) == 0
    assert await contracts.read_storage(db, OWNER, "a") is None
    assert await contracts.get_contract_version(db, OWNER) is None
    old = await contracts.get_contract_version(db, OWNER, 1)
    assert old is not None
    assert old.binary == contract_binary()
    assert await contracts.list_public_functions(db, OWNER) == ()
    for operation in (contracts.pause_contract, contracts.resume_contract, contracts.delete_contract):
        with pytest.raises(ContractNotFound):
            await operation(db, account_id=OWNER, now=later)
    with pytest.raises(ContractNotFound):
        await contracts.lock_contract(db, account_id=OWNER, until=later + timedelta(days=1), now=later)
    await _assert_clean(db)
    revived = await register_contract(db, OWNER, contract_binary(SHOP_CONTRACT), now=later)
    assert (revived.version, revived.status, revived.revived) == (2, ContractStatus.ACTIVE, True)
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert (info.status, info.deleted_at, info.current_version, info.version_count, info.created_execution_id) == (ContractStatus.ACTIVE, None, 2, 2, first.execution_id)
    assert await contracts.read_storage(db, OWNER, "a") is None
    await _assert_clean(db)


async def test_paused_contract_can_be_deleted(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    await contracts.pause_contract(db, account_id=OWNER, now=NOW)
    await contracts.delete_contract(db, account_id=OWNER, now=NOW)
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert info.status is ContractStatus.DELETED


async def test_list_public_functions(db: AsyncEngine) -> None:
    assert await contracts.list_public_functions(db, OWNER) == ()
    await register_contract(db, OWNER, contract_binary(SHOP_CONTRACT), now=NOW)
    functions = await contracts.list_public_functions(db, OWNER)
    assert [(function.name, function.params, function.returns) for function in functions] == [
        ("buy", (INT, ACCOUNT), STR),
        ("count", (STR,), INT),
        ("flag", (BOOL, CURRENCY), BOOL),
    ]
    assert [function.signature for function in functions] == ["buy(int, Account) -> str", "count(str) -> int", "flag(bool, Currency) -> bool"]
    await register_contract(db, OWNER, now=NOW)
    assert [function.signature for function in await contracts.list_public_functions(db, OWNER)] == ["ping() -> int"]


async def test_idempotent_replay(db: AsyncEngine) -> None:
    binary = contract_binary()
    first = await contracts.register_contract(db, account_id=OWNER, binary=binary, max_size=MAX_SIZE, now=NOW, idempotency_key="k1")
    again = await contracts.register_contract(db, account_id=OWNER, binary=binary, max_size=MAX_SIZE, now=NOW, idempotency_key="k1")
    assert again == first
    assert again.replayed and not first.replayed
    with pytest.raises(IdempotencyConflict):
        await contracts.register_contract(db, account_id=OWNER, binary=contract_binary(SHOP_CONTRACT), max_size=MAX_SIZE, now=NOW, idempotency_key="k1")
    until = NOW + timedelta(days=1)
    locked = await contracts.lock_contract(db, account_id=OWNER, until=until, now=NOW, idempotency_key="k2")
    assert await contracts.lock_contract(db, account_id=OWNER, until=until, now=NOW, idempotency_key="k2") == locked
    with pytest.raises(ContractLocked) as failure:
        await contracts.pause_contract(db, account_id=OWNER, now=NOW, idempotency_key="k3")
    with pytest.raises(ContractLocked) as replayed_failure:
        await contracts.pause_contract(db, account_id=OWNER, now=NOW + timedelta(days=2), idempotency_key="k3")
    assert replayed_failure.value.execution_id == failure.value.execution_id
    later = NOW + timedelta(days=2)
    deleted = await contracts.delete_contract(db, account_id=OWNER, now=later, idempotency_key="k4")
    assert await contracts.delete_contract(db, account_id=OWNER, now=later, idempotency_key="k4") == deleted
    assert len(await _executions(db)) == 4
    assert (await contracts.get_contract(db, OWNER)) is not None


@pytest.mark.parametrize("account_exists", [True, False])
async def test_concurrent_first_registrations_both_succeed(db: AsyncEngine, account_exists: bool) -> None:
    # With the account present both transactions reach the version insert and race on its primary key; without it they first race on creating the account.
    if account_exists:
        async with db.begin() as conn:
            await ensure_user_account(conn, OWNER, now=NOW)
    binaries = [contract_binary(), contract_binary(SHOP_CONTRACT)]
    results = await asyncio.gather(*(contracts.register_contract(db, account_id=OWNER, binary=binary, max_size=MAX_SIZE, now=NOW) for binary in binaries))
    assert sorted(result.version for result in results) == [1, 2]
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert (info.current_version, info.version_count) == (2, 2)
    winner = next(result for result in results if result.version == 2)
    assert info.sha256 == winner.sha256
    await _assert_clean(db)


async def _orphan_version(engine: AsyncEngine) -> None:
    """Commit version 1 of OWNER without a contract row, the state a concurrent first registration holds just before it inserts the row."""
    async with engine.begin() as conn:
        await ensure_user_account(conn, OWNER, now=NOW)
        execution_id = await LedgerTx(conn, NOW).create_execution(type=ExecutionType.CONTRACT_UPDATE, initiator_id=OWNER, input={})
        binary = contract_binary()
        values = {"account_id": OWNER, "version": 1, "code": binary, "sha256": hashlib.sha256(binary).digest(), "size": len(binary), "created_execution_id": execution_id, "created_at": NOW}
        await conn.execute(insert(contract_version).values(**values))


@pytest.mark.parametrize("rerun_allowed", [True, False])
async def test_losing_a_first_registration_race(db: AsyncEngine, rerun_allowed: bool) -> None:
    await _orphan_version(db)
    checked = contracts.check_binary(contract_binary(SHOP_CONTRACT), max_size=MAX_SIZE)
    async with db.connect() as conn:
        await conn.begin()
        tx = LedgerTx(conn, NOW, rerun_allowed=rerun_allowed)
        execution_id = await tx.create_execution(type=ExecutionType.CONTRACT_UPDATE, initiator_id=OWNER, input={})
        if rerun_allowed:
            with pytest.raises(RerunTransaction, match="registered concurrently"):
                await contracts.register_contract_in_tx(tx, execution_id, account_id=OWNER, contract_binary=checked)
        else:
            with pytest.raises(InvalidState, match="Another registration of this contract is in progress"):
                await contracts.register_contract_in_tx(tx, execution_id, account_id=OWNER, contract_binary=checked)
        await conn.rollback()


async def test_lock_contract_in_tx_returns_the_current_binary(db: AsyncEngine) -> None:
    binary = contract_binary(SHOP_CONTRACT)
    await register_contract(db, OWNER, binary, now=NOW)
    async with db.begin() as conn:
        tx = LedgerTx(conn, NOW)
        assert await contracts.lock_contract_in_tx(tx, OTHER, for_update=False) is None
        locked = await contracts.lock_contract_in_tx(tx, OWNER, for_update=False)
    assert locked is not None
    assert (locked.status, locked.current_version, locked.binary, locked.sha256, locked.locked_until) == (ContractStatus.ACTIVE, 1, binary, hashlib.sha256(binary).hexdigest(), None)
    await contracts.delete_contract(db, account_id=OWNER, now=NOW)
    async with db.begin() as conn:
        deleted = await contracts.lock_contract_in_tx(LedgerTx(conn, NOW), OWNER, for_update=True)
    assert deleted is not None
    assert (deleted.status, deleted.binary, deleted.sha256) == (ContractStatus.DELETED, None, None)


async def test_storage_writes_need_the_contract_locked_for_update(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    data = storage_codec.encode(1)
    async with db.connect() as conn:
        await conn.begin()
        tx = LedgerTx(conn, NOW)
        with pytest.raises(LedgerUsageError, match="must be locked FOR UPDATE"):
            await contracts.put_storage_in_tx(tx, OWNER, "a", data)
        with pytest.raises(LedgerUsageError, match="must be locked FOR UPDATE"):
            await contracts.delete_storage_in_tx(tx, OWNER, "a")
        assert contracts.contract_lock(tx, OWNER) is None
        await contracts.lock_contract_in_tx(tx, OWNER, for_update=False)
        assert contracts.contract_lock(tx, OWNER) == "share"
        with pytest.raises(LedgerUsageError, match="must be locked FOR UPDATE"):
            await contracts.put_storage_in_tx(tx, OWNER, "a", data)
        with pytest.raises(LedgerUsageError, match="already locked FOR SHARE"):
            await contracts.lock_contract_in_tx(tx, OWNER, for_update=True)
        await conn.rollback()
    async with db.connect() as conn:
        await conn.begin()
        tx = LedgerTx(conn, NOW)
        await contracts.lock_contract_in_tx(tx, OWNER, for_update=True)
        # Asking FOR SHARE afterwards keeps the FOR UPDATE lock.
        await contracts.lock_contract_in_tx(tx, OWNER, for_update=False)
        assert contracts.contract_lock(tx, OWNER) == "update"
        assert (await contracts.put_storage_in_tx(tx, OWNER, "a", data)).storage_keys == 1
        assert contracts.contract_lock(LedgerTx(conn, NOW), OWNER) is None
        await conn.commit()
    assert await contracts.read_storage(db, OWNER, "a") == 1


async def test_storage_of_paused_and_deleted_contracts_cannot_change(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    await put_storage(db, OWNER, "a", 1, now=NOW)
    await contracts.pause_contract(db, account_id=OWNER, now=NOW)
    with pytest.raises(ContractPaused):
        await put_storage(db, OWNER, "a", 2, now=NOW)
    with pytest.raises(ContractPaused):
        await delete_storage(db, OWNER, "a")
    assert await contracts.read_storage(db, OWNER, "a") == 1
    await contracts.delete_contract(db, account_id=OWNER, now=NOW)
    with pytest.raises(ContractNotFound):
        await put_storage(db, OWNER, "a", 2, now=NOW)
    with pytest.raises(ContractNotFound):
        await delete_storage(db, OWNER, "a")
    await _assert_clean(db)


async def test_storage_reads_writes_and_totals(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    nested: Value = {"z": 1, "a": [Currency(3), {"k": False}], "m": "text"}
    write = await put_storage(db, OWNER, "config", nested, now=NOW)
    canonical = storage_codec.encode(nested)
    # The runtime passes the VM's insertion-ordered encoding; storage keeps the canonical form of the same length.
    assert encode_value(nested) != canonical
    assert len(encode_value(nested)) == len(canonical)
    assert (write.size, write.previous_size, write.storage_keys, write.storage_bytes) == (len(canonical), 0, 1, len("config") + len(canonical))
    async with db.connect() as conn:
        stored = (await conn.execute(select(contract_storage.c.encoded_value).where(contract_storage.c.storage_key == "config"))).scalar_one()
    assert bytes(stored) == canonical
    value = await contracts.read_storage(db, OWNER, "config")
    assert value == nested
    assert isinstance(value, dict)
    assert list(value) == ["a", "m", "z"]
    replaced = await put_storage(db, OWNER, "config", 5, now=NOW)
    assert (replaced.previous_size, replaced.storage_keys, replaced.storage_bytes) == (len(canonical), 1, len("config") + len(storage_codec.encode(5)))
    for key, item in ((HIRAGANA_A, True), ("b", Account(9)), ("a", "x" * 100)):
        await put_storage(db, OWNER, key, item, now=NOW)
    page = await contracts.list_storage(db, OWNER, per_page=3)
    assert page.total == 4
    assert [(entry.key, entry.value) for entry in page.items] == [("a", "x" * 100), ("b", Account(9)), ("config", 5)]
    assert page.items[0].size == len(storage_codec.encode("x" * 100))
    rest = await contracts.list_storage(db, OWNER, page=2, per_page=3)
    assert [(entry.key, entry.value) for entry in rest.items] == [(HIRAGANA_A, True)]
    async with db.begin() as conn:
        tx = LedgerTx(conn, NOW)
        await contracts.lock_contract_in_tx(tx, OWNER, for_update=True)
        removed = await contracts.delete_storage_in_tx(tx, OWNER, "b")
        absent = await contracts.delete_storage_in_tx(tx, OWNER, "nothing")
    assert (removed.previous_size, removed.storage_keys) == (len(storage_codec.encode(Account(9))), 3)
    assert (absent.previous_size, absent.storage_keys, absent.storage_bytes) == (0, 3, removed.storage_bytes)
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert (info.storage_keys, info.storage_bytes) == (3, removed.storage_bytes)
    assert await contracts.read_storage(db, OWNER, "b") is None
    assert await contracts.read_storage(db, OWNER, "") is None
    assert await contracts.read_storage(db, OTHER, "a") is None
    with pytest.raises(storage_codec.StorageKeyError):
        await put_storage(db, OWNER, "k" * 65, 1, now=NOW)
    # An account without a contract has no row to lock, so a write to it is a usage error.
    with pytest.raises(LedgerUsageError):
        await put_storage(db, OTHER, "a", 1, now=NOW)
    await _assert_clean(db)


async def test_get_storage_entry(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    nested: Value = {"z": 1, "a": [Currency(3)]}
    await put_storage(db, OWNER, "config", nested, now=NOW)
    later = NOW + timedelta(minutes=5)
    await put_storage(db, OWNER, "a/b", "slash", now=later)
    entry = await contracts.get_storage_entry(db, OWNER, "config")
    assert entry == contracts.StorageEntry(key="config", value=nested, size=len(storage_codec.encode(nested)), updated_at=NOW)
    slash = await contracts.get_storage_entry(db, OWNER, "a/b")
    assert slash is not None and (slash.value, slash.updated_at) == ("slash", later)
    assert [entry.key for entry in (await contracts.list_storage(db, OWNER)).items] == ["a/b", "config"]
    for absent in ((OWNER, "missing"), (OWNER, ""), (OWNER, "k" * 65), (OTHER, "config")):
        assert await contracts.get_storage_entry(db, *absent) is None


async def test_contract_with_functions_describes_one_version(db: AsyncEngine) -> None:
    assert await contracts.get_contract_with_functions(db, OWNER) is None
    await register_contract(db, OWNER, contract_binary(SHOP_CONTRACT), now=NOW)
    first = await contracts.get_contract_with_functions(db, OWNER)
    assert first is not None
    assert first.info == await contracts.get_contract(db, OWNER)
    assert [function.signature for function in first.functions] == ["buy(int, Account) -> str", "count(str) -> int", "flag(bool, Currency) -> bool"]
    await register_contract(db, OWNER, now=NOW)
    second = await contracts.get_contract_with_functions(db, OWNER)
    assert second is not None
    assert (second.info.current_version, [function.signature for function in second.functions]) == (2, ["ping() -> int"])
    await contracts.pause_contract(db, account_id=OWNER, now=NOW)
    paused = await contracts.get_contract_with_functions(db, OWNER)
    assert paused is not None and paused.info.status is ContractStatus.PAUSED and paused.functions == second.functions
    await contracts.delete_contract(db, account_id=OWNER, now=NOW)
    deleted = await contracts.get_contract_with_functions(db, OWNER)
    assert deleted is not None and deleted.info.status is ContractStatus.DELETED and deleted.functions == ()


async def test_concurrent_registration_never_mixes_versions_in_one_read(db: AsyncEngine) -> None:
    shop, ping = contract_binary(SHOP_CONTRACT), contract_binary()
    expected = {2: ["buy", "count", "flag"], 3: ["ping"]}
    await register_contract(db, OWNER, shop, now=NOW)
    await register_contract(db, OWNER, shop, now=NOW)
    done = asyncio.Event()
    seen: set[tuple[int, tuple[str, ...]]] = set()

    async def read() -> None:
        while not done.is_set():
            details = await contracts.get_contract_with_functions(db, OWNER)
            assert details is not None
            names = tuple(function.name for function in details.functions)
            assert list(names) == expected[details.info.current_version]
            seen.add((details.info.current_version, names))
            await asyncio.sleep(0)

    async def update_once() -> None:
        try:
            await register_contract(db, OWNER, ping, now=NOW)
        finally:
            done.set()

    await asyncio.gather(read(), update_once())
    final = await contracts.get_contract_with_functions(db, OWNER)
    assert final is not None and final.info.current_version == 3


async def test_lock_for_a_duration_ends_after_the_execution_time(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    locked = await contracts.lock_contract(db, account_id=OWNER, hours=24, now=NOW, idempotency_key="day")
    assert (locked.locked_until, locked.previous_locked_until) == (NOW + timedelta(hours=24), None)
    # The same key a minute later replays the first lock instead of conflicting or extending it.
    again = await contracts.lock_contract(db, account_id=OWNER, hours=24, now=NOW + timedelta(minutes=1), idempotency_key="day")
    assert again == locked and again.replayed
    with pytest.raises(IdempotencyConflict):
        await contracts.lock_contract(db, account_id=OWNER, hours=25, now=NOW, idempotency_key="day")
    with pytest.raises(IdempotencyConflict):
        await contracts.lock_contract(db, account_id=OWNER, until=NOW + timedelta(hours=24), now=NOW, idempotency_key="day")
    [row] = [row for row in await _executions(db) if row["type"] == ExecutionType.CONTRACT_LOCK]
    assert row["input"] == {"account_id": str(OWNER), "hours": "24"}
    later = NOW + timedelta(hours=1)
    extended = await contracts.lock_contract(db, account_id=OWNER, hours=48, now=later)
    assert (extended.locked_until, extended.previous_locked_until) == (later + timedelta(hours=48), NOW + timedelta(hours=24))
    with pytest.raises(InvalidState):
        await contracts.lock_contract(db, account_id=OWNER, hours=1, now=later)


@pytest.mark.parametrize("arguments", [{}, {"hours": 1, "until": NOW + timedelta(days=1)}, {"hours": 0}, {"hours": contracts.LOCK_HOURS_MAX + 1}, {"hours": True}, {"until": datetime(2030, 1, 1)}])
async def test_lock_arguments_are_checked_before_anything_is_recorded(db: AsyncEngine, arguments: dict[str, Any]) -> None:
    await register_contract(db, OWNER, now=NOW)
    with pytest.raises(InvalidArgument):
        await contracts.lock_contract(db, account_id=OWNER, now=NOW, **arguments)
    assert [row for row in await _executions(db) if row["type"] == ExecutionType.CONTRACT_LOCK] == []
    longest = await contracts.lock_contract(db, account_id=OWNER, hours=contracts.LOCK_HOURS_MAX, now=NOW)
    assert longest.locked_until == NOW + timedelta(hours=contracts.LOCK_HOURS_MAX)


async def test_storage_keys_differ_by_trailing_space(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    await put_storage(db, OWNER, "a", 1, now=NOW)
    await put_storage(db, OWNER, "a ", 2, now=NOW)
    assert (await contracts.read_storage(db, OWNER, "a"), await contracts.read_storage(db, OWNER, "a ")) == (1, 2)
    page = await contracts.list_storage(db, OWNER)
    assert [(entry.key, entry.value) for entry in page.items] == [("a", 1), ("a ", 2)]
    await delete_storage(db, OWNER, "a ")
    assert (await contracts.read_storage(db, OWNER, "a"), await contracts.read_storage(db, OWNER, "a ")) == (1, None)
    await _assert_clean(db)


async def test_largest_value_is_stored(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    # A str of n >= 128 ASCII characters encodes to a tag byte, a 2-byte length and n bytes.
    largest = "x" * (CONTRACT_STORAGE_VALUE_MAX - 3)
    write = await put_storage(db, OWNER, "big", largest, now=NOW)
    assert write.size == CONTRACT_STORAGE_VALUE_MAX
    assert await contracts.read_storage(db, OWNER, "big") == largest
    oversized = b"\x02" + encode_uvarint(CONTRACT_STORAGE_VALUE_MAX - 2) + b"x" * (CONTRACT_STORAGE_VALUE_MAX - 2)
    assert len(oversized) == CONTRACT_STORAGE_VALUE_MAX + 1
    async with db.begin() as conn:
        tx = LedgerTx(conn, NOW)
        await contracts.lock_contract_in_tx(tx, OWNER, for_update=True)
        with pytest.raises(storage_codec.StorageDecodeError, match="larger than"):
            await contracts.put_storage_in_tx(tx, OWNER, "too big", oversized)
    await _assert_clean(db)


async def test_storage_byte_limit(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    big = "x" * 4000
    entry = storage_codec.entry_size("k000", len(storage_codec.encode(big)))
    fitting = CONTRACT_STORAGE_MAX_BYTES // entry
    async with db.begin() as conn:
        tx = LedgerTx(conn, NOW)
        await contracts.lock_contract_in_tx(tx, OWNER, for_update=True)
        for index in range(fitting):
            await contracts.put_storage_in_tx(tx, OWNER, f"k{index:03d}", storage_codec.encode(big))
    with pytest.raises(ContractStorageLimit, match="bytes"):
        await put_storage(db, OWNER, "overflow", big, now=NOW)
    remaining = CONTRACT_STORAGE_MAX_BYTES - fitting * entry
    small_key = "s"
    # The entry costs the key byte, the str tag byte, a 2-byte length (the filler has at least 128 characters) and the filler.
    assert remaining - 4 >= 128
    filler = "y" * (remaining - 4)
    exact = await put_storage(db, OWNER, small_key, filler, now=NOW)
    assert exact.storage_bytes == CONTRACT_STORAGE_MAX_BYTES
    with pytest.raises(ContractStorageLimit):
        await put_storage(db, OWNER, "t", 0, now=NOW)
    # Replacing a value with a smaller one is always possible at the limit.
    shrunk = await put_storage(db, OWNER, small_key, 0, now=NOW)
    assert shrunk.storage_bytes < CONTRACT_STORAGE_MAX_BYTES
    await _assert_clean(db)


async def test_storage_key_limit(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    async with db.begin() as conn:
        tx = LedgerTx(conn, NOW)
        await contracts.lock_contract_in_tx(tx, OWNER, for_update=True)
        for index in range(CONTRACT_STORAGE_MAX_KEYS):
            await contracts.put_storage_in_tx(tx, OWNER, f"{index}", b"\x03")
    info = await contracts.get_contract(db, OWNER)
    assert info is not None
    assert info.storage_keys == CONTRACT_STORAGE_MAX_KEYS
    with pytest.raises(ContractStorageLimit, match="keys"):
        await put_storage(db, OWNER, "one more", True, now=NOW)
    overwritten = await put_storage(db, OWNER, "0", False, now=NOW)
    assert overwritten.storage_keys == CONTRACT_STORAGE_MAX_KEYS
    await _assert_clean(db)


async def test_invariants_detect_contract_inconsistencies(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    await register_contract(db, OWNER, contract_binary(SHOP_CONTRACT), now=NOW)
    await put_storage(db, OWNER, "a", 1, now=NOW)
    await _assert_clean(db)
    async with db.begin() as conn:
        await conn.execute(update(contract).values(storage_bytes=1))
    report = await verify_invariants(db)
    assert [(violation.check, violation.account_id) for violation in report.violations] == [("contract_storage_totals", OWNER)]
    async with db.begin() as conn:
        await conn.execute(update(contract).values(storage_bytes=len("a") + len(storage_codec.encode(1)), current_version=1))
    report = await verify_invariants(db)
    assert [(violation.check, violation.account_id) for violation in report.violations] == [("contract_versions", OWNER)]


def _contract_violations(report: Any) -> list[tuple[str, int | None]]:
    return [(violation.check, violation.account_id) for violation in report.violations if violation.check.startswith(("contract", "deleted_contract"))]


async def test_invariants_detect_versions_without_a_contract(db: AsyncEngine) -> None:
    await _orphan_version(db)
    assert _contract_violations(await verify_invariants(db)) == [("contract_version_orphan", OWNER)]


async def test_invariants_detect_storage_of_a_deleted_contract(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    await contracts.delete_contract(db, account_id=OWNER, now=NOW)
    encoded = storage_codec.encode(1)
    async with db.begin() as conn:
        await conn.execute(insert(contract_storage).values(account_id=OWNER, storage_key="a", encoded_value=encoded, value_size=len(encoded), updated_at=NOW))
    assert _contract_violations(await verify_invariants(db)) == [("contract_storage_totals", OWNER), ("deleted_contract_storage", OWNER)]


@pytest.mark.parametrize("change", [{"type": ExecutionType.TRANSFER.value}, {"initiator_id": OTHER}])
async def test_invariants_detect_a_version_not_created_by_its_owners_update(db: AsyncEngine, change: dict[str, Any]) -> None:
    created = await register_contract(db, OWNER, now=NOW)
    async with db.begin() as conn:
        await ensure_user_account(conn, OTHER, now=NOW)
        await conn.execute(update(execution).where(execution.c.execution_id == created.execution_id).values(**change))
    assert _contract_violations(await verify_invariants(db)) == [("contract_version_execution", OWNER)]


async def test_invariants_detect_a_non_canonical_stored_value(db: AsyncEngine) -> None:
    await register_contract(db, OWNER, now=NOW)
    value: Value = {"b": 2, "a": 1}
    await put_storage(db, OWNER, "d", value, now=NOW)
    insertion_ordered = encode_value(value)
    assert insertion_ordered != storage_codec.encode(value)
    async with db.begin() as conn:
        await conn.execute(update(contract_storage).values(encoded_value=insertion_ordered))
    assert _contract_violations(await verify_invariants(db)) == [("contract_storage_value", OWNER)]
