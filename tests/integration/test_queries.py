from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Select, select, text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.core import paging
from rapidwire.core.db import consistent_snapshot
from rapidwire.core.errors import CurrencyNotFound, InsufficientBalance, InvalidArgument
from rapidwire.core.ledger import LedgerTx, run_child, run_operation
from rapidwire.core.queries import (
    HISTORY_PER_PAGE_MAX,
    MAX_WINDOW,
    PRIVATE_EXECUTION_TYPES,
    AccountStats,
    ExecutionDetail,
    Page,
    TransferEntry,
    currency_supplies,
    currency_supply,
    execution_list_statements,
    get_balance,
    get_execution,
    get_transfer,
    history_statements,
    list_balances,
    list_currencies,
    list_executions,
    search_history,
    user_stats,
    user_stats_count_statements,
)
from rapidwire.core.types import ExecutionStatus, ExecutionType, LabelBasis, Origin, TransferKind
from rapidwire.db.tables import transfer
from tests.integration.factories import balance_of, create_currency, mint, supply_of

T0 = datetime(2026, 1, 1, tzinfo=UTC)
CUR_A = 10
CUR_B = 20
CUR_GONE = 30
ALICE = 111
BOB = 222
CAROL = 333
DAVE = 444
NOBODY = 999


@dataclass(frozen=True)
class Seed:
    legacy_mint: int
    legacy_transfer: int
    native_mint: int
    call: int
    child: int
    grandchild: int
    failed_child: int
    approve: int
    failed: int
    roundtrip: int


async def _legacy(engine: AsyncEngine, *, execution_id: int, transfer_id: int, type: str, initiator: int, currency_id: int, source: int, destination: int, amount: int, at: datetime) -> int:
    async with engine.begin() as conn:
        tx = LedgerTx(conn, at)
        await tx.require_accounts([initiator])
        await tx.lock_currencies([currency_id])
        await tx.lock_balances([(source, currency_id), (destination, currency_id)])
        created = await tx.create_execution(
            type=type,
            initiator_id=initiator,
            input={"amount": amount},
            output={},
            origin=Origin.LEGACY,
            label_basis=LabelBasis.ROW_SHAPE,
            execution_id=execution_id,
            created_at=at,
        )
        await tx.move(created, currency_id, source, destination, amount, TransferKind.PRINCIPAL, transfer_id=transfer_id, created_at=at)
    return created


async def _moves(engine: AsyncEngine, *, type: str, initiator: int, moves: list[tuple[int, int, int, int]], now: datetime, contract_function: str | None = None) -> int:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies({move[0] for move in moves})
        await tx.lock_balances([key for cur, src, dst, _ in moves for key in ((src, cur), (dst, cur))])
        for cur, src, dst, amount in moves:
            await tx.move(execution_id, cur, src, dst, amount, TransferKind.PRINCIPAL)
        return {}

    result = await run_operation(engine, type=type, initiator_id=initiator, input={"n": len(moves)}, body=body, now=now, contract_function=contract_function)
    return result.execution_id


async def _seed(engine: AsyncEngine) -> Seed:
    a = await create_currency(engine, currency_id=CUR_A, symbol="AAA", name="Alpha", now=T0)
    await create_currency(engine, currency_id=CUR_B, symbol="BBB", name="Beta", now=T0)
    await create_currency(engine, currency_id=CUR_GONE, symbol="GONE", name="Gone", now=T0, deleted_at=T0)
    legacy_mint = await _legacy(engine, execution_id=1, transfer_id=1, type=ExecutionType.MINT, initiator=ALICE, currency_id=CUR_A, source=a.issuance_account_id, destination=ALICE, amount=1000, at=T0)
    legacy_transfer = await _legacy(
        engine, execution_id=2, transfer_id=2, type=ExecutionType.TRANSFER, initiator=ALICE, currency_id=CUR_A, source=ALICE, destination=BOB, amount=100, at=T0 + timedelta(days=1)
    )
    native_mint = await mint(engine, CUR_B, BOB, 500, now=T0 + timedelta(days=2))
    ids: dict[str, int] = {}

    async def call_body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR_A, CUR_B])
        await tx.lock_balances([(ALICE, CUR_A), (BOB, CUR_A), (BOB, CUR_B), (CAROL, CUR_B)])
        await tx.move(execution_id, CUR_B, BOB, CAROL, 30, TransferKind.PRINCIPAL)

        async def child_body(ctx: LedgerTx, child_id: int) -> dict[str, Any]:
            await ctx.move(child_id, CUR_A, ALICE, BOB, 20, TransferKind.PRINCIPAL)

            async def grandchild_body(gtx: LedgerTx, grandchild_id: int) -> dict[str, Any]:
                await gtx.move(grandchild_id, CUR_A, ALICE, BOB, 20, TransferKind.PRINCIPAL)
                return {}

            ids["grandchild"] = (await run_child(ctx, parent_execution_id=child_id, type=ExecutionType.TRANSFER, initiator_id=CAROL, input={}, body=grandchild_body)).execution_id
            return {}

        ids["child"] = (await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.TRANSFER, initiator_id=CAROL, input={}, body=child_body)).execution_id

        async def failing_body(ctx: LedgerTx, child_id: int) -> dict[str, Any]:
            await ctx.move(child_id, CUR_A, ALICE, BOB, 1, TransferKind.PRINCIPAL)
            raise InsufficientBalance()

        try:
            await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.TRANSFER, initiator_id=CAROL, input={}, body=failing_body)
        except InsufficientBalance as error:
            assert error.execution_id is not None
            ids["failed_child"] = error.execution_id
        return {}

    call = (
        await run_operation(
            engine, type=ExecutionType.CONTRACT_CALL, initiator_id=CAROL, input={"f": "pay"}, body=call_body, now=T0 + timedelta(days=3), contract_function="pay", idempotency_key="secret-key"
        )
    ).execution_id

    async def approve_body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return {"amount": 5}

    approve = (await run_operation(engine, type=ExecutionType.APPROVE, initiator_id=ALICE, input={"spender": BOB}, body=approve_body, now=T0 + timedelta(days=4))).execution_id

    async def failing(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR_A])
        await tx.lock_balances([(CAROL, CUR_A), (ALICE, CUR_A)])
        await tx.move(execution_id, CUR_A, CAROL, ALICE, 10**6, TransferKind.PRINCIPAL)
        return {}

    with pytest.raises(InsufficientBalance) as raised:
        await run_operation(engine, type=ExecutionType.TRANSFER, initiator_id=CAROL, input={"amount": 10**6}, body=failing, now=T0 + timedelta(days=5))
    assert raised.value.execution_id is not None
    roundtrip = await _moves(engine, type=ExecutionType.TRANSFER, initiator=ALICE, moves=[(CUR_A, ALICE, DAVE, 50), (CUR_A, DAVE, ALICE, 50)], now=T0 + timedelta(days=6))
    return Seed(
        legacy_mint=legacy_mint,
        legacy_transfer=legacy_transfer,
        native_mint=native_mint,
        call=call,
        child=ids["child"],
        grandchild=ids["grandchild"],
        failed_child=ids["failed_child"],
        approve=approve,
        failed=raised.value.execution_id,
        roundtrip=roundtrip,
    )


@pytest.fixture
async def seed(db: AsyncEngine) -> Seed:
    return await _seed(db)


async def _all_entries(engine: AsyncEngine) -> list[TransferEntry]:
    async with engine.connect() as conn:
        ids = (await conn.execute(select(transfer.c.transfer_id).order_by(transfer.c.transfer_id))).scalars().all()
    entries = []
    for transfer_id in ids:
        entry = await get_transfer(engine, transfer_id)
        assert entry is not None
        entries.append(entry)
    return entries


def _oracle(entries: list[TransferEntry], filters: dict[str, Any], sort_by: str = "transfer_id", sort_order: str = "desc") -> list[int]:
    checks: dict[str, Callable[[TransferEntry, Any], bool]] = {
        "transfer_id": lambda e, v: e.transfer_id == v,
        "user_id": lambda e, v: v in (e.from_account, e.to_account),
        "source_id": lambda e, v: e.from_account == v,
        "destination_id": lambda e, v: e.to_account == v,
        "currency_id": lambda e, v: e.currency_id == v,
        "start": lambda e, v: e.created_at >= v,
        "end": lambda e, v: e.created_at < v,
        "min_amount": lambda e, v: e.amount >= v,
        "max_amount": lambda e, v: e.amount <= v,
        "execution_type": lambda e, v: e.execution_type == v,
        "contract_function": lambda e, v: e.contract_function == v,
    }
    matched = [e for e in entries if all(checks[name](e, value) for name, value in filters.items())]
    keys: dict[str, Callable[[TransferEntry], tuple[Any, ...]]] = {
        "transfer_id": lambda e: (e.transfer_id,),
        "timestamp": lambda e: (e.created_at, e.transfer_id),
        "amount": lambda e: (e.amount, e.transfer_id),
    }
    matched.sort(key=keys[sort_by], reverse=sort_order.lower() == "desc")
    return [e.transfer_id for e in matched]


FILTER_VALUES: dict[str, Any] = {
    "transfer_id": 2,
    "user_id": BOB,
    "source_id": ALICE,
    "destination_id": BOB,
    "currency_id": CUR_A,
    "start": T0 + timedelta(days=1),
    "end": T0 + timedelta(days=3),
    "min_amount": 20,
    "max_amount": 100,
    "execution_type": ExecutionType.TRANSFER.value,
    "contract_function": "pay",
}


async def test_history_matches_oracle_for_every_filter_alone_and_in_pairs(db: AsyncEngine, seed: Seed) -> None:
    entries = await _all_entries(db)
    combos: list[dict[str, Any]] = [{}]
    combos += [{name: value} for name, value in FILTER_VALUES.items()]
    combos += [dict(pair) for pair in itertools.combinations(FILTER_VALUES.items(), 2)]
    combos.append({"user_id": ALICE, "currency_id": CUR_A, "start": T0, "end": T0 + timedelta(days=6), "min_amount": 1, "max_amount": 1000})
    combos.append({"user_id": BOB, "execution_type": "transfer", "source_id": ALICE})
    non_empty = 0
    for filters in combos:
        expected = _oracle(entries, filters)
        result = await search_history(db, per_page=HISTORY_PER_PAGE_MAX, **filters)
        assert [item.transfer_id for item in result.items] == expected, filters
        assert result.total == len(expected), filters
        non_empty += bool(expected)
    assert non_empty > len(FILTER_VALUES)


async def test_history_single_filters_hand_checked(db: AsyncEngine, seed: Seed) -> None:
    by_user = await search_history(db, user_id=BOB)
    assert {item.transfer_id for item in by_user.items} >= {2, 3}
    assert all(BOB in (item.from_account, item.to_account) for item in by_user.items)
    legacy = await search_history(db, transfer_id=2)
    (row,) = legacy.items
    assert row.origin is Origin.LEGACY and row.label_basis is LabelBasis.ROW_SHAPE
    assert (row.from_account, row.to_account, row.amount, row.currency_symbol) == (ALICE, BOB, 100, "AAA")
    assert row.created_at == T0 + timedelta(days=1) and row.created_at.tzinfo is UTC
    by_function = await search_history(db, contract_function="pay")
    assert [item.execution_id for item in by_function.items] == [seed.call]
    assert by_function.items[0].amount == 30 and by_function.items[0].execution_type == ExecutionType.CONTRACT_CALL
    one_day = await search_history(db, start=T0 + timedelta(days=1), end=T0 + timedelta(days=2))
    assert [item.transfer_id for item in one_day.items] == [2]
    assert (await search_history(db, start=T0 + timedelta(days=1), end=T0 + timedelta(days=1))).total == 0
    amounts = await search_history(db, min_amount=20, max_amount=20)
    assert [item.amount for item in amounts.items] == [20, 20]
    nested = await search_history(db, execution_type="transfer", user_id=ALICE, destination_id=BOB)
    assert {item.execution_id for item in nested.items} == {seed.legacy_transfer, seed.child, seed.grandchild}
    assert (await search_history(db, user_id=NOBODY)).total == 0


async def test_history_sorting_and_paging(db: AsyncEngine, seed: Seed) -> None:
    entries = await _all_entries(db)
    filter_sets: tuple[dict[str, Any], ...] = ({}, {"user_id": ALICE}, {"user_id": BOB, "currency_id": CUR_A})
    for sort_by, sort_order, filters in itertools.product(("transfer_id", "timestamp", "amount"), ("asc", "DESC"), filter_sets):
        expected = _oracle(entries, filters, sort_by, sort_order)
        collected: list[int] = []
        for page in range(1, len(expected) + 2):
            result = await search_history(db, page=page, per_page=2, sort_by=sort_by, sort_order=sort_order, **filters)
            assert result.total == len(expected)
            assert result.pages == (len(expected) + 1) // 2
            assert len(result.items) <= 2
            collected += [item.transfer_id for item in result.items]
        assert collected == expected, (sort_by, sort_order, filters)


async def test_history_rejects_invalid_arguments(db: AsyncEngine) -> None:
    bad: list[dict[str, Any]] = [
        {"page": 0},
        {"page": -1},
        {"per_page": 0},
        {"per_page": HISTORY_PER_PAGE_MAX + 1},
        {"page": True},
        {"sort_by": "created_at"},
        {"sort_order": "up"},
        {"start": datetime(2026, 1, 1)},
        {"user_id": 2**63},
        {"min_amount": 10**38},
        {"execution_type": ""},
        {"contract_function": "x" * 65},
    ]
    for kwargs in bad:
        with pytest.raises(InvalidArgument):
            await search_history(db, **kwargs)


async def test_get_transfer(db: AsyncEngine, seed: Seed) -> None:
    entry = await get_transfer(db, 3)
    assert entry is not None
    assert (entry.execution_id, entry.execution_type, entry.execution_status, entry.origin, entry.label_basis) == (seed.native_mint, "mint", ExecutionStatus.SUCCESS, Origin.NATIVE, None)
    assert (entry.to_account, entry.amount, entry.kind, entry.initiator_id, entry.currency_symbol) == (BOB, 500, TransferKind.PRINCIPAL, BOB, "BBB")
    assert await get_transfer(db, 10**9) is None


async def test_get_execution_with_nested_children(db: AsyncEngine, seed: Seed) -> None:
    detail = await get_execution(db, seed.call)
    assert isinstance(detail, ExecutionDetail)
    root = detail.execution
    assert (root.type, root.status, root.origin, root.label_basis, root.initiator_id, root.parent_execution_id) == ("contract_call", ExecutionStatus.SUCCESS, Origin.NATIVE, None, CAROL, None)
    assert (root.contract_function, root.input, root.output, root.created_at) == ("pay", {"f": "pay"}, {}, T0 + timedelta(days=3))
    assert root.has_idempotency_key is True
    assert "secret-key" not in repr(detail)
    assert [(t.from_account, t.to_account, t.amount, t.currency_id) for t in detail.transfers] == [(BOB, CAROL, 30, CUR_B)]
    assert detail.child_execution_ids == (seed.child, seed.failed_child)
    child, failed_child = detail.children
    assert child.execution.parent_execution_id == seed.call and child.child_execution_ids == (seed.grandchild,)
    assert [(t.from_account, t.to_account, t.amount) for t in child.transfers] == [(ALICE, BOB, 20)]
    assert failed_child.execution.status is ExecutionStatus.FAILED and failed_child.transfers == () and failed_child.child_execution_ids == ()
    assert failed_child.execution.output == {"error": {"code": "insufficient_balance", "message": "The balance is insufficient."}}
    deeper = await get_execution(db, seed.grandchild)
    assert deeper is not None and deeper.children == () and deeper.execution.parent_execution_id == seed.child
    assert [t.amount for t in deeper.transfers] == [20]
    legacy = await get_execution(db, seed.legacy_mint)
    assert legacy is not None and legacy.execution.origin is Origin.LEGACY and legacy.execution.label_basis is LabelBasis.ROW_SHAPE
    assert legacy.execution.has_idempotency_key is False and [t.transfer_id for t in legacy.transfers] == [1]
    assert await get_execution(db, 10**9) is None


async def test_list_executions(db: AsyncEngine, seed: Seed) -> None:
    everything = await list_executions(db, per_page=20)
    ids = [item.execution_id for item in everything.items]
    assert ids == sorted(ids, reverse=True) and everything.total == len(ids) == 10
    failed = await list_executions(db, status=ExecutionStatus.FAILED)
    assert {item.execution_id for item in failed.items} == {seed.failed, seed.failed_child}
    assert (await search_history(db, user_id=CAROL, execution_type="transfer")).items == ()
    by_carol = await list_executions(db, initiator_id=CAROL, status="failed", execution_type="transfer")
    assert [item.execution_id for item in by_carol.items] == [seed.failed, seed.failed_child]
    approve = await list_executions(db, initiator_id=ALICE, execution_type="approve")
    assert [item.execution_id for item in approve.items] == [seed.approve] and approve.items[0].output == {"amount": "5"}
    paged = [item.execution_id for page in (1, 2, 3, 4) for item in (await list_executions(db, page=page, per_page=3)).items]
    assert paged == ids
    beyond = await list_executions(db, page=5, per_page=3)
    assert beyond.items == () and beyond.total == 10 and beyond.pages == 4
    for kwargs in ({"status": "done"}, {"page": 0}, {"per_page": 21}, {"initiator_id": "1"}):
        with pytest.raises(InvalidArgument):
            await list_executions(db, **kwargs)


async def test_balances(db: AsyncEngine, seed: Seed) -> None:
    for account_id, currency_id in itertools.product((ALICE, BOB, CAROL, DAVE), (CUR_A, CUR_B, CUR_GONE)):
        assert await get_balance(db, account_id, currency_id) == await balance_of(db, account_id, currency_id)
    assert await get_balance(db, ALICE, CUR_A) == 1000 - 100 - 20 - 20
    assert await get_balance(db, NOBODY, CUR_A) == 0
    assert await get_balance(db, ALICE, CUR_GONE) == 0
    with pytest.raises(CurrencyNotFound):
        await get_balance(db, ALICE, 12345)
    bob = await list_balances(db, BOB)
    assert [(b.currency_id, b.symbol, b.name, b.amount) for b in bob.items] == [(CUR_A, "AAA", "Alpha", 140), (CUR_B, "BBB", "Beta", 470)]
    assert (bob.total, bob.pages, bob.page, bob.per_page) == (2, 1, 1, 15)
    assert await get_balance(db, DAVE, CUR_A) == 0
    dave = await list_balances(db, DAVE)
    assert dave.items == () and dave.total == 0 and dave.pages == 0
    second = await list_balances(db, BOB, page=2, per_page=1)
    assert [b.currency_id for b in second.items] == [CUR_B] and second.pages == 2
    out_of_range = await list_balances(db, BOB, page=3, per_page=1)
    assert out_of_range.items == () and out_of_range.pages == 2
    for kwargs in ({"page": 0}, {"per_page": 0}, {"per_page": 101}):
        with pytest.raises(InvalidArgument):
            await list_balances(db, BOB, **kwargs)


async def test_user_stats(db: AsyncEngine, seed: Seed) -> None:
    alice = await user_stats(db, ALICE)
    expected = [e for e in await _all_entries(db) if ALICE in (e.from_account, e.to_account)]
    assert alice == AccountStats(total_transfers=len(expected), first_transfer_at=T0, last_transfer_at=T0 + timedelta(days=6))
    assert alice.first_transfer_at is not None and alice.first_transfer_at.tzinfo is UTC
    dave = await user_stats(db, DAVE)
    assert dave.total_transfers == 2
    assert await user_stats(db, NOBODY) == AccountStats(total_transfers=0, first_transfer_at=None, last_transfer_at=None)


async def test_user_stats_counts_come_from_the_account_indexes_alone(db: AsyncEngine, seed: Seed) -> None:
    async with db.connect() as conn:
        for stmt, index in zip(user_stats_count_statements(ALICE), ("ix_transfer_from_account_transfer_id", "ix_transfer_to_account_transfer_id"), strict=True):
            compiled = stmt.compile(conn.sync_engine, compile_kwargs={"literal_binds": True})
            plan = (await conn.execute(text(f"EXPLAIN FORMAT=JSON {compiled}"))).scalar_one()
            table = json.loads(plan)["query_block"]["table"]
            assert table["key"] == index
            assert table["using_index"] is True


async def test_contract_execution_lists_use_the_contract_index(db: AsyncEngine, seed: Seed) -> None:
    statements = execution_list_statements(contract_id=DAVE, execution_type=ExecutionType.CONTRACT_CALL, per_page=20)
    async with db.connect() as conn:
        for stmt in (statements.page_statement, statements.count_statement):
            compiled = stmt.compile(conn.sync_engine, compile_kwargs={"literal_binds": True})
            plan = (await conn.execute(text(f"EXPLAIN {compiled}"))).mappings().all()
            keys = {row["key"] for row in plan if row["table"] == "execution"}
            assert keys == {"ix_execution_contract_id_execution_id"}, plan
            assert all("filesort" not in (row["Extra"] or "") for row in plan if row["table"] == "execution"), plan


async def test_list_executions_by_contract(db: AsyncEngine) -> None:
    async with db.begin() as conn:
        tx = LedgerTx(conn, T0)
        await tx.require_accounts([ALICE])
        call = await tx.create_execution(type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={"contract_id": DAVE, "function": "f"})
        nested = await tx.create_execution(type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={"contract_id": BOB}, parent_execution_id=call)
        managed = await tx.create_execution(type=ExecutionType.CONTRACT_PAUSE, initiator_id=ALICE, input={"account_id": ALICE})
        other = await tx.create_execution(type=ExecutionType.TRANSFER, initiator_id=ALICE, input={})
    by_dave = await list_executions(db, contract_id=DAVE)
    assert [item.execution_id for item in by_dave.items] == [call]
    assert by_dave.items[0].contract_id == DAVE and by_dave.items[0].parent_origin is None
    by_bob = await list_executions(db, contract_id=BOB)
    assert [(item.execution_id, item.parent_execution_id, item.parent_origin) for item in by_bob.items] == [(nested, call, Origin.NATIVE)]
    assert [item.execution_id for item in (await list_executions(db, contract_id=ALICE)).items] == [managed]
    assert [item.execution_id for item in (await list_executions(db, contract_id=ALICE, execution_type=ExecutionType.CONTRACT_CALL)).items] == []
    assert next(item for item in (await list_executions(db)).items if item.execution_id == other).contract_id is None
    with pytest.raises(InvalidArgument):
        await list_executions(db, contract_id=2**63)


async def test_currencies_and_supply(db: AsyncEngine, seed: Seed) -> None:
    assert await currency_supply(db, CUR_A) == await supply_of(db, CUR_A) == 1000
    assert await currency_supply(db, CUR_B) == 500
    assert await currency_supply(db, CUR_GONE) == 0
    with pytest.raises(CurrencyNotFound):
        await currency_supply(db, 12345)
    active = await list_currencies(db)
    assert [(c.currency_id, c.symbol, c.name, c.supply, c.deleted) for c in active] == [(CUR_A, "AAA", "Alpha", 1000, False), (CUR_B, "BBB", "Beta", 500, False)]
    every = await list_currencies(db, include_deleted=True)
    assert [(c.symbol, c.deleted) for c in every] == [("AAA", False), ("BBB", False), ("GONE", True)]
    assert await currency_supplies(db, [CUR_B, CUR_A, CUR_GONE, 12345, CUR_A]) == {CUR_A: 1000, CUR_B: 500, CUR_GONE: 0}
    assert await currency_supplies(db, []) == {}
    with pytest.raises(InvalidArgument):
        await currency_supplies(db, [2**63])


async def test_queries_accept_an_idle_or_open_connection(db: AsyncEngine, seed: Seed) -> None:
    async with db.connect() as conn:
        assert (await search_history(conn, user_id=BOB)).total == (await search_history(db, user_id=BOB)).total
        assert (await get_execution(conn, seed.call)) == (await get_execution(db, seed.call))
        assert isinstance(await list_balances(conn, BOB), Page)
        assert not conn.in_transaction()
        assert (await conn.execute(text("SELECT @@SESSION.transaction_isolation"))).scalar_one() == "READ-COMMITTED"
        await conn.rollback()
        assert await get_balance(conn, BOB, CUR_B) == 470
        assert not conn.in_transaction()
        async with conn.begin():
            assert await get_balance(conn, BOB, CUR_B) == 470
            assert isinstance(await list_balances(conn, BOB), Page)
            assert conn.in_transaction()


async def _explain(conn: AsyncConnection, stmt: Select[Any]) -> list[dict[str, Any]]:
    sql = str(stmt.compile(dialect=conn.dialect, compile_kwargs={"literal_binds": True}))
    return [dict(row) for row in (await conn.exec_driver_sql("EXPLAIN " + sql)).mappings().all()]


async def test_history_statements_use_transfer_indexes(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR_A, symbol="AAA", now=T0)
    await create_currency(db, currency_id=CUR_B, symbol="BBB", now=T0)
    users = list(range(1, 41))
    await mint(db, CUR_A, users[0], 10**9, now=T0)
    await mint(db, CUR_B, users[0], 10**9, now=T0)
    for step in range(80):
        cur = CUR_A if step % 2 else CUR_B
        moves = [(cur, users[0], users[(step * 5 + k) % 39 + 1], 1 + step + k) for k in range(5)]
        kind = ExecutionType.TRANSFER if step % 4 else ExecutionType.CONTRACT_CALL
        await _moves(db, type=kind, initiator=users[0], moves=moves, now=T0 + timedelta(hours=step), contract_function="pay" if kind is ExecutionType.CONTRACT_CALL else None)
    queries: list[dict[str, Any]] = [
        {},
        {"user_id": 7},
        {"user_id": 7, "currency_id": CUR_A},
        {"user_id": 7, "sort_by": "timestamp", "sort_order": "asc"},
        {"source_id": 7},
        {"destination_id": 7},
        {"currency_id": CUR_A},
        {"start": T0 + timedelta(hours=10), "end": T0 + timedelta(hours=12)},
        {"min_amount": 80},
        {"transfer_id": 5},
        {"execution_type": "contract_call"},
        {"contract_function": "pay"},
        {"sort_by": "amount"},
    ]
    async with db.connect() as conn:
        await conn.exec_driver_sql("ANALYZE TABLE transfer, execution")
        for filters in queries:
            query = history_statements(**filters)
            filtered = bool(filters.keys() - {"sort_by", "sort_order"})
            full_scans = ("ALL", "index") if filtered else ("ALL",)
            for stmt in (*query.count_statements, query.page_statement):
                plan = await _explain(conn, stmt)
                scans = [row for row in plan if row["table"] in ("transfer", "execution") and row["type"] in full_scans]
                assert scans == [], (filters, plan)
                assert any(row["table"] == "transfer" and row["key"] is not None for row in plan), (filters, plan)
        for side in (transfer.c.from_account, transfer.c.to_account):
            (row,) = await _explain(conn, select(transfer.c.created_at).where(side == 7).order_by(transfer.c.created_at.desc()).limit(1))
            assert row["type"] == "ref" and row["key"] is not None and "created_at" in row["key"], row
            assert "filesort" not in (row["Extra"] or ""), row
        rows = (await conn.execute(text("SELECT COUNT(*) FROM transfer"))).scalar_one()
    assert rows == 402


async def test_history_default_paging(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR_A, symbol="AAA", now=T0)
    await mint(db, CUR_A, ALICE, 10**6, now=T0)
    await _moves(db, type=ExecutionType.TRANSFER, initiator=ALICE, moves=[(CUR_A, ALICE, BOB, n) for n in range(1, 24)], now=T0)
    first = await search_history(db)
    assert (first.page, first.per_page, first.total, first.pages, first.total_is_lower_bound) == (1, 10, 24, 3, False)
    assert [item.transfer_id for item in first.items] == list(range(24, 14, -1))
    last = await search_history(db, page=3)
    assert [item.transfer_id for item in last.items] == [4, 3, 2, 1]
    assert (await search_history(db, page=4)).items == ()


async def test_private_execution_types_are_redacted(db: AsyncEngine) -> None:
    secrets = {"allowed_user_id": 987654321012, "guild_id": 424242424242, "api_token_id": 13131313131}

    async def reveal(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        return dict(secrets)

    private_ids = []
    for kind in sorted(PRIVATE_EXECUTION_TYPES):
        result = await run_operation(db, type=kind, initiator_id=ALICE, input=secrets, body=reveal, now=T0)
        private_ids.append(result.execution_id)

    async def parent(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.DISCORD_PERMISSION_ALLOW, initiator_id=ALICE, input=secrets, body=reveal)
        return {"visible": 1}

    parent_id = (await run_operation(db, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={"visible": 1}, body=parent, now=T0)).execution_id
    seen: list[Any] = []
    for execution_id in private_ids:
        detail = await get_execution(db, execution_id)
        assert detail is not None
        assert (detail.execution.input, detail.execution.output, detail.execution.status, detail.execution.initiator_id) == ({}, None, ExecutionStatus.SUCCESS, ALICE)
        assert detail.execution.created_at == T0
        seen.append(detail)
    parent_detail = await get_execution(db, parent_id)
    assert parent_detail is not None and parent_detail.execution.input == {"visible": "1"} and parent_detail.execution.output == {"visible": "1"}
    (child,) = parent_detail.children
    assert (child.execution.type, child.execution.input, child.execution.output) == (ExecutionType.DISCORD_PERMISSION_ALLOW, {}, None)
    listed = await list_executions(db, per_page=20)
    assert len(listed.items) == len(PRIVATE_EXECUTION_TYPES) + 2
    seen += [parent_detail, listed]
    text_seen = repr(seen)
    for value in secrets.values():
        assert str(value) not in text_seen


async def test_paging_window_is_capped(db: AsyncEngine) -> None:
    assert (await search_history(db, page=MAX_WINDOW // 20, per_page=20)).items == ()
    assert (await list_executions(db, page=MAX_WINDOW // 20, per_page=20)).items == ()
    assert (await list_balances(db, ALICE, page=MAX_WINDOW // 100, per_page=100)).items == ()
    with pytest.raises(InvalidArgument):
        await search_history(db, page=MAX_WINDOW // 20 + 1, per_page=20)
    with pytest.raises(InvalidArgument):
        await search_history(db, page=MAX_WINDOW // 10, per_page=11)
    with pytest.raises(InvalidArgument):
        await list_executions(db, page=MAX_WINDOW // 20 + 1, per_page=20)
    with pytest.raises(InvalidArgument):
        await list_balances(db, ALICE, page=MAX_WINDOW // 100 + 1, per_page=100)


async def test_counts_are_bounded(db: AsyncEngine, seed: Seed, monkeypatch: pytest.MonkeyPatch) -> None:
    exact = await search_history(db, user_id=ALICE)
    assert exact.total > 4 and not exact.total_is_lower_bound
    monkeypatch.setattr(paging, "MAX_WINDOW", 4)
    history = await search_history(db, per_page=2)
    assert (history.total, history.total_is_lower_bound, history.pages) == (4, True, 2)
    by_user = await search_history(db, user_id=ALICE, per_page=2, page=2)
    assert (by_user.total, by_user.total_is_lower_bound, len(by_user.items)) == (4, True, 2)
    executions = await list_executions(db, per_page=4)
    assert (executions.total, executions.total_is_lower_bound) == (4, True)
    # The account statistics are exact, so the window does not cap them.
    assert (await user_stats(db, ALICE)).total_transfers == exact.total
    small = await search_history(db, transfer_id=2, per_page=4)
    assert (small.total, small.total_is_lower_bound) == (1, False)
    with pytest.raises(InvalidArgument):
        await search_history(db, page=3, per_page=2)


async def test_snapshot_reads_are_consistent(db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR_A, symbol="AAA", now=T0)
    async with consistent_snapshot(db) as conn:
        assert (await conn.execute(text("SELECT @@transaction_isolation"))).scalar_one() == "REPEATABLE-READ"
        assert len(await list_currencies(conn)) == 1
        await create_currency(db, currency_id=CUR_B, symbol="BBB", now=T0)
        assert len(await list_currencies(conn)) == 1
    assert len(await list_currencies(db)) == 2
