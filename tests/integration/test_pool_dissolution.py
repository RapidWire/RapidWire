from __future__ import annotations

import asyncio
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import currency as cur
from rapidwire.core import pools
from rapidwire.core.errors import CurrencyNotFound, PoolNotFound
from rapidwire.core.invariants import verify_invariants
from rapidwire.core.ledger import LedgerTx, run_operation
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.types import ExecutionStatus, ExecutionType, TransferKind
from rapidwire.db.tables import execution, pool, pool_share
from rapidwire.db.tables import transfer as transfer_table
from tests.integration.factories import add_liquidity, balance_of, create_currency, create_pool, mint, supply_of

T0 = datetime(2026, 9, 1, tzinfo=UTC)
AT = T0 + timedelta(days=7)
OWNER = 100
ALICE = 111
BOB = 222
CAROL = 333
DAVE = 444
LOW, DELETED, PARTNER, OTHER = 5, 10, 20, 30


def test_importing_currency_registers_the_dex_dissolver() -> None:
    code = "import rapidwire.core.currency\nfrom rapidwire.core.hooks import get_pool_dissolver\nprint(type(get_pool_dissolver()).__module__, type(get_pool_dissolver()).__name__)"
    result = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True, timeout=60)
    assert result.stdout.split() == ["rapidwire.core.pools", "DexPoolDissolver"]


@pytest.fixture
def guilds() -> InMemoryGuildInfo:
    info = InMemoryGuildInfo()
    for guild_id in (LOW, DELETED, PARTNER, OTHER):
        info.add_guild(guild_id, OWNER)
    return info


async def _seed(engine: AsyncEngine) -> None:
    for currency_id, symbol in ((LOW, "LOW"), (DELETED, "DEL"), (PARTNER, "PAR"), (OTHER, "OTH")):
        await create_currency(engine, currency_id=currency_id, symbol=symbol, now=T0)


async def _assert_invariants(engine: AsyncEngine) -> None:
    report = await verify_invariants(engine)
    assert report.ok, report.violations


async def _delete(engine: AsyncEngine, guilds: InMemoryGuildInfo, currency_id: int = DELETED) -> cur.CurrencyDeleted:
    await cur.request_delete(engine, guilds, currency_id=currency_id, user_id=OWNER, now=T0)
    return await cur.finalize_delete(engine, guilds, currency_id=currency_id, user_id=OWNER, now=AT)


async def test_deletion_dissolves_every_pool_of_the_currency(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _seed(db)
    # Pool 1 (DEL/PAR, 1%): providers hold 19000, 2000 and 1000 shares of 23000 over a PAR reserve of 46000.
    main = await create_pool(db, user_id=ALICE, currency_x=DELETED, currency_y=PARTNER, amount_x=10_000, amount_y=40_000, now=T0)
    await add_liquidity(db, user_id=BOB, currency_x=DELETED, currency_y=PARTNER, amount_x=1_000, amount_y=4_000, now=T0)
    await add_liquidity(db, user_id=CAROL, currency_x=PARTNER, currency_y=DELETED, amount_x=2_000, amount_y=500, now=T0)
    # Pool 2 (LOW/DEL, 0.5%): the deleted currency is currency_b and Bob is the only provider.
    low = await create_pool(db, user_id=BOB, currency_x=LOW, currency_y=DELETED, amount_x=3_000, amount_y=3_000, fee_bps=50, now=T0)
    # Pool 3 (DEL/PAR, 0.1%): Bob created it, Alice holds as many shares, and the remainder ties go to the lower account ID.
    tie = await create_pool(db, user_id=BOB, currency_x=DELETED, currency_y=PARTNER, amount_x=2_000, amount_y=2_000, fee_bps=10, now=T0)
    await add_liquidity(db, user_id=ALICE, currency_x=DELETED, currency_y=PARTNER, amount_x=1_000, amount_y=1_000, fee_bps=10, now=T0)
    await add_liquidity(db, user_id=CAROL, currency_x=DELETED, currency_y=PARTNER, amount_x=1, amount_y=1, fee_bps=10, now=T0)
    # Pool 4 (DEL/OTH, 1%): its only provider withdrew, so only the locked minimum liquidity remains.
    empty = await create_pool(db, user_id=DAVE, currency_x=DELETED, currency_y=OTHER, amount_x=2_000, amount_y=2_000, now=T0)
    await pools.remove_liquidity(db, user_id=DAVE, currency_x=DELETED, currency_y=OTHER, fee_bps=100, shares=1_000, now=T0)
    # An unrelated pool of the partners stays untouched.
    untouched = await create_pool(db, user_id=CAROL, currency_x=PARTNER, currency_y=OTHER, amount_x=5_000, amount_y=5_000, now=T0)
    await _assert_invariants(db)
    supplies = {currency_id: await supply_of(db, currency_id) for currency_id in (LOW, DELETED, PARTNER, OTHER)}

    deleted = await _delete(db, guilds)
    # The four custody accounts and Dave's wallet (the 1000 he withdrew) held the deleted currency.
    assert (deleted.pools_dissolved, deleted.balances_burned, deleted.burned_amount) == (4, 5, supplies[DELETED])

    assert await supply_of(db, DELETED) == 0
    assert await supply_of(db, PARTNER) == supplies[PARTNER]
    assert await supply_of(db, LOW) == supplies[LOW]
    assert await supply_of(db, OTHER) == supplies[OTHER] - 1_000
    # Pool 1: floor(46000 * 19000 / 22000) = 39727 + remainder 2, floor(46000 * 2000 / 22000) = 4181, floor(46000 * 1000 / 22000) = 2090.
    # Pool 3: PAR reserve 3001 over 2001 provider shares: 1499 each for Bob and Alice + remainder 2 to Alice (lower ID), 1 to Carol.
    assert await balance_of(db, ALICE, PARTNER) == 39_729 + 1_501
    assert await balance_of(db, BOB, PARTNER) == 4_181 + 1_499
    assert await balance_of(db, CAROL, PARTNER) == 2_090 + 1
    assert await balance_of(db, BOB, LOW) == 3_000
    assert await balance_of(db, DAVE, OTHER) == 1_000
    for holder in (ALICE, BOB, CAROL, DAVE):
        assert await balance_of(db, holder, DELETED) == 0

    async with db.connect() as conn:
        moves = (await conn.execute(select(transfer_table).where(transfer_table.c.execution_id == deleted.execution_id).order_by(transfer_table.c.transfer_id))).mappings().all()
        rows = {row["pool_id"]: row for row in (await conn.execute(select(pool))).mappings().all()}
        share_rows = (await conn.execute(select(pool_share.c.pool_id, pool_share.c.account_id))).all()
    issuance = {currency_id: (await cur.get_currency(db, currency_id, include_deleted=True)) for currency_id in (DELETED, PARTNER, OTHER)}
    burns = [(m["from_account"], m["amount"]) for m in moves if m["currency_id"] == DELETED]
    deleted_issuance = issuance[DELETED]
    assert deleted_issuance is not None
    assert all(m["to_account"] == deleted_issuance.issuance_account_id for m in moves if m["currency_id"] == DELETED)
    # Each pool's reserve of the deleted currency is burned straight from its custody.
    for created, reserve in ((main, 11_500), (low, 3_000), (tie, 3_001), (empty, 1_000)):
        assert (created.custody_account_id, reserve) in burns
    other_issuance = issuance[OTHER]
    assert other_issuance is not None
    assert [(m["from_account"], m["to_account"], m["amount"]) for m in moves if m["currency_id"] == OTHER] == [(empty.custody_account_id, other_issuance.issuance_account_id, 1_000)]
    paid = [(m["from_account"], m["to_account"], m["amount"]) for m in moves if m["currency_id"] == PARTNER]
    assert paid == [
        (main.custody_account_id, ALICE, 39_729),
        (main.custody_account_id, BOB, 4_181),
        (main.custody_account_id, CAROL, 2_090),
        (tie.custody_account_id, ALICE, 1_501),
        (tie.custody_account_id, BOB, 1_499),
        (tie.custody_account_id, CAROL, 1),
    ]
    assert [(m["from_account"], m["to_account"], m["amount"]) for m in moves if m["currency_id"] == LOW] == [(low.custody_account_id, BOB, 3_000)]

    for created in (main, low, tie, empty):
        row = rows[created.pool_id]
        assert (row["dissolved_at"], row["dissolved_execution_id"], row["active"]) == (AT, deleted.execution_id, None)
        assert (row["reserve_a"], row["reserve_b"], row["total_shares"], row["locked_shares"]) == (0, 0, 0, 0)
        for currency_id in (LOW, DELETED, PARTNER, OTHER):
            assert await balance_of(db, created.custody_account_id, currency_id) == 0
    assert rows[untouched.pool_id]["dissolved_at"] is None
    assert {pool_id for pool_id, _ in share_rows} == {untouched.pool_id}
    info = await pools.get_pool(db, main.pool_id)
    assert info is not None and not info.active and info.dissolved_execution_id == deleted.execution_id
    assert [item.pool_id for item in (await pools.list_pools(db)).items] == [untouched.pool_id]
    assert await pools.get_pools_for_pair(db, DELETED, PARTNER) == ()
    assert [item.pool.pool_id for item in (await pools.list_provider_positions(db, ALICE)).items] == []
    await _assert_invariants(db)

    # A revived currency can pool with the same partner and tier again, as a new pool.
    await cur.create_currency(db, guilds, guild_id=DELETED, user_id=OWNER, name="Again", symbol="DEL", initial_supply=0, now=AT)
    again = await create_pool(db, user_id=ALICE, currency_x=PARTNER, currency_y=DELETED, amount_x=3_000, amount_y=3_000, now=AT)
    assert again.pool_id not in rows
    assert [item.pool_id for item in await pools.get_pools_for_pair(db, DELETED, PARTNER)] == [again.pool_id]
    await _assert_invariants(db)


async def test_deletion_of_the_partner_side_and_a_pool_free_currency(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _seed(db)
    created = await create_pool(db, user_id=ALICE, currency_x=LOW, currency_y=PARTNER, amount_x=4_000, amount_y=9_000, now=T0)
    await mint(db, OTHER, BOB, 77, now=T0)
    # OTH has no pools, so its deletion moves only its own balances.
    gone = await _delete(db, guilds, OTHER)
    assert (gone.balances_burned, gone.burned_amount, gone.pools_dissolved) == (1, 77, 0)
    assert (await pools.get_pool(db, created.pool_id)) is not None
    # Deleting the pool's higher currency pays the lower one out.
    await _delete(db, guilds, PARTNER)
    assert await balance_of(db, ALICE, LOW) == 4_000
    info = await pools.get_pool(db, created.pool_id)
    assert info is not None and not info.active
    await _assert_invariants(db)


async def test_provider_whose_payout_floors_to_zero(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _seed(db)
    created = await create_pool(db, user_id=ALICE, currency_x=DELETED, currency_y=PARTNER, amount_x=10_000_000, amount_y=1_000, now=T0)
    await add_liquidity(db, user_id=BOB, currency_x=DELETED, currency_y=PARTNER, amount_x=10_000, amount_y=1, now=T0)
    assert await _shares_of(db, created.pool_id) == {ALICE: 99_000, BOB: 100}
    await mint(db, DELETED, CAROL, 1_000, now=T0)

    # A swap-like move drains the partner reserve to 2 while the shares stay, so Bob's floor(2 * 100 / 99100) is 0.
    async def drain(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([DELETED, PARTNER])
        locked = (await pools.lock_pools(tx, [created.pool_id]))[created.pool_id]
        custody = locked.custody_account_id
        await tx.lock_balances([(CAROL, DELETED), (CAROL, PARTNER), (custody, DELETED), (custody, PARTNER)])
        await tx.move(execution_id, DELETED, CAROL, custody, 1_000, TransferKind.PRINCIPAL)
        await tx.move(execution_id, PARTNER, custody, CAROL, 999, TransferKind.PRINCIPAL)
        await pools.set_reserves(tx, created.pool_id, locked.reserve_a + 1_000, locked.reserve_b - 999)
        return {}

    await run_operation(db, type=ExecutionType.SWAP, initiator_id=CAROL, input={}, body=drain, now=T0)
    await _assert_invariants(db)
    deleted = await _delete(db, guilds)
    async with db.connect() as conn:
        moves = (await conn.execute(select(transfer_table).where(transfer_table.c.execution_id == deleted.execution_id, transfer_table.c.currency_id == PARTNER))).mappings().all()
    # Alice gets floor(2 * 99000 / 99100) = 1 plus the remainder 1; Bob's zero payout records no transfer.
    assert [(m["from_account"], m["to_account"], m["amount"]) for m in moves] == [(created.custody_account_id, ALICE, 2)]
    assert await balance_of(db, BOB, PARTNER) == 0
    assert await _shares_of(db, created.pool_id) == {}
    await _assert_invariants(db)


async def test_pool_created_while_its_currency_is_being_deleted(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _seed(db)
    await mint(db, DELETED, ALICE, 5_000, now=T0)
    await mint(db, PARTNER, ALICE, 5_000, now=T0)
    await cur.request_delete(db, guilds, currency_id=DELETED, user_id=OWNER, now=T0)
    # The creation holds both currencies FOR SHARE until it commits, so the deletion reads no partner, waits for its FOR UPDATE lock, and then finds an unlocked partner.
    async with db.connect() as other:
        await other.begin()
        tx = LedgerTx(other, T0)
        await tx.require_accounts([ALICE])
        execution_id = await tx.create_execution(type=ExecutionType.POOL_CREATE, initiator_id=ALICE, input={})
        created = await pools.create_pool_in_tx(tx, execution_id, user_id=ALICE, currency_x=DELETED, currency_y=PARTNER, fee_bps=100, amount_x=3_000, amount_y=3_000)
        await tx.finish_execution(execution_id, status=ExecutionStatus.SUCCESS, output=created.to_output())
        task = asyncio.create_task(cur.finalize_delete(db, guilds, currency_id=DELETED, user_id=OWNER, now=AT))
        await _wait_for_lock_wait(db)
        await other.commit()
    deleted = await task
    # The first run rolled back with RerunTransaction and the rerun, with the partner pre-locked, dissolved the new pool.
    assert deleted.pools_dissolved == 1
    async with db.connect() as conn:
        finals = (await conn.execute(select(execution.c.execution_id).where(execution.c.type == ExecutionType.CURRENCY_DELETE_FINALIZE))).all()
    assert [row.execution_id for row in finals] == [deleted.execution_id]
    info = await pools.get_pool(db, created.pool_id)
    assert info is not None and not info.active and info.dissolved_execution_id == deleted.execution_id
    assert (info.deleted_a, info.deleted_b, info.symbol_a) == (True, False, "DEL")
    assert await balance_of(db, ALICE, PARTNER) == 5_000
    await _assert_invariants(db)


async def _wait_for_lock_wait(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        for _ in range(200):
            if (await conn.execute(text("SELECT COUNT(*) FROM performance_schema.data_lock_waits"))).scalar_one():
                return
            await conn.rollback()
            await asyncio.sleep(0.05)
    raise AssertionError("no transaction started waiting for a lock")


async def _shares_of(engine: AsyncEngine, pool_id: int) -> dict[int, int]:
    async with engine.connect() as conn:
        rows = await conn.execute(select(pool_share.c.account_id, pool_share.c.shares).where(pool_share.c.pool_id == pool_id))
        return {row.account_id: int(row.shares) for row in rows}


async def test_deletion_racing_liquidity_changes(db: AsyncEngine, guilds: InMemoryGuildInfo) -> None:
    await _seed(db)
    await create_pool(db, user_id=ALICE, currency_x=DELETED, currency_y=PARTNER, amount_x=100_000, amount_y=100_000, now=T0)
    for user in (BOB, CAROL):
        await mint(db, DELETED, user, 10_000, now=T0)
        await mint(db, PARTNER, user, 10_000, now=T0)
    await cur.request_delete(db, guilds, currency_id=DELETED, user_id=OWNER, now=T0)

    async def noise(index: int) -> None:
        user = BOB if index % 2 else CAROL
        try:
            if index % 3 == 2:
                await pools.remove_liquidity(db, user_id=ALICE, currency_x=DELETED, currency_y=PARTNER, fee_bps=100, shares=100, now=AT)
            else:
                await pools.add_liquidity(db, user_id=user, currency_x=PARTNER, currency_y=DELETED, fee_bps=100, amount_x_desired=500, amount_y_desired=500, now=AT)
        except (CurrencyNotFound, PoolNotFound):
            pass

    tasks: list[Any] = [noise(index) for index in range(12)]
    tasks.insert(6, cur.finalize_delete(db, guilds, currency_id=DELETED, user_id=OWNER, now=AT))
    results = await asyncio.gather(*tasks, return_exceptions=True)
    assert not [result for result in results if isinstance(result, BaseException)]
    assert await supply_of(db, DELETED) == 0
    assert (await pools.list_pools(db)).total == 0
    await _assert_invariants(db)
