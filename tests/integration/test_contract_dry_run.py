"""Dry runs: the call's code path in a transaction that is always rolled back, reporting the value or error, the gas and the would-be transfers and outbox rows while leaving every table exactly as it was."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.values import Attachment, Currency
from rapidwire.core import contract_calls
from rapidwire.core.contract_calls import call_contract, dry_run_contract
from rapidwire.core.discord_permissions import allow_discord
from rapidwire.core.errors import ContractReverted, DomainError, InsufficientBalance, InvalidArgument
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.types import OutboxKind, TransferKind
from rapidwire.db.tables import metadata
from tests.integration.contract_runtime_helpers import CALLER, NOW, RAPID, config, deploy, setup_gas

A = 8001
B = 8002
GUILD = 600_001
NEW_USER = 7_777

CALLER_SOURCE = """
@public
def work(x: int) -> str:
    storage["n"] = x
    transfer(Account(7777), currency("RAPID"), 3)
    discord.send(600001, 600003, "hi")
    v: int = call(Account(8002), "double", [x])
    return str(v)

@public
def refuse() -> int:
    storage["n"] = 1
    revert("nope")

@public
def keep() -> str:
    return "kept"

@public
def shielded() -> str:
    r = try_call(Account(8002), "flaky", [], attach=(currency("RAPID"), 4))
    return r.error + ":" + r.message
"""

CALLEE_SOURCE = """
@public
def double(x: int) -> int:
    storage["seen"] = x
    return x * 2 + random(0, 0)

@public
def flaky() -> int:
    v: int = random(7, 7)
    storage["seen"] = v
    revert("flaky")
"""


async def _snapshot(engine: AsyncEngine) -> dict[str, list[tuple[Any, ...]]]:
    """Every row of every table, in a stable order."""
    result: dict[str, list[tuple[Any, ...]]] = {}
    async with engine.connect() as conn:
        for table in metadata.sorted_tables:
            rows = (await conn.execute(select(table))).all()
            result[table.name] = sorted((tuple(row) for row in rows), key=repr)
    return result


async def _setup(engine: AsyncEngine) -> None:
    await setup_gas(engine, {CALLER: 100_000, A: 100})
    await deploy(engine, A, CALLER_SOURCE)
    await deploy(engine, B, CALLEE_SOURCE)
    guilds = InMemoryGuildInfo()
    guilds.add_guild(GUILD, CALLER)
    await allow_discord(engine, guilds, guild_id=GUILD, account_id=A, acting_user_id=CALLER, now=NOW)


async def test_dry_run_leaves_no_trace_and_matches_the_real_call(db: AsyncEngine) -> None:
    await _setup(db)
    before = await _snapshot(db)
    dry = await dry_run_contract(db, None, caller_id=CALLER, contract_id=A, function="work", args=[21], attach=Attachment(Currency(RAPID), 10), gas_limit=2_000, config=config(), now=NOW)
    assert await _snapshot(db) == before
    assert dry.ok
    assert (dry.value, dry.error, dry.gas_limit) == ("42", None, 2_000)
    kinds = [(item.kind, item.from_account, item.to_account, item.amount) for item in dry.transfers]
    assert kinds[:2] == [(TransferKind.PRINCIPAL, CALLER, A, 10), (TransferKind.PRINCIPAL, A, NEW_USER, 3)]
    assert kinds[2][:2] == (TransferKind.GAS, CALLER)
    assert kinds[2][3] == dry.gas_used
    assert [(item.kind, item.payload["message"]) for item in dry.outbox] == [(OutboxKind.DISCORD_SEND, "hi")]

    real = await call_contract(db, None, caller_id=CALLER, contract_id=A, function="work", args=[21], attach=Attachment(Currency(RAPID), 10), gas_limit=2_000, config=config(), now=NOW)
    assert (real.value, real.gas_used) == (dry.value, dry.gas_used)


async def test_dry_run_of_a_failure_reports_the_error_and_the_gas_it_would_collect(db: AsyncEngine) -> None:
    await _setup(db)
    before = await _snapshot(db)
    dry = await dry_run_contract(db, None, caller_id=CALLER, contract_id=A, function="refuse", args=[], config=config(), now=NOW)
    assert await _snapshot(db) == before
    assert not dry.ok
    assert dry.error is not None
    assert (dry.error.code, dry.error.kind, dry.error.message, dry.value) == ("contract_reverted", "REVERTED", "nope", None)
    [gas] = dry.transfers
    assert (gas.kind, gas.amount) == (TransferKind.GAS, dry.gas_used)
    with pytest.raises(ContractReverted):
        await call_contract(db, None, caller_id=CALLER, contract_id=A, function="refuse", args=[], config=config(), now=NOW)


async def test_dry_run_of_a_call_that_cannot_start(db: AsyncEngine) -> None:
    await _setup(db)
    before = await _snapshot(db)
    dry = await dry_run_contract(db, None, caller_id=NEW_USER, contract_id=A, function="keep", args=[], config=config(), now=NOW)
    assert await _snapshot(db) == before
    assert dry.error is not None
    assert (dry.error.code, dry.error.kind, dry.error.reason, dry.gas_used, dry.transfers) == ("insufficient_balance", "CALL_FAILED", "INSUFFICIENT_BALANCE", 0, ())
    missing = await dry_run_contract(db, None, caller_id=CALLER, contract_id=9_999, function="keep", args=[], config=config(), now=NOW)
    assert missing.error is not None
    assert missing.error.code == "contract_not_found"
    with pytest.raises(InvalidArgument):
        await dry_run_contract(db, None, caller_id=CALLER, contract_id=A, function="keep", args=[1.5], config=config(), now=NOW)
    assert await _snapshot(db) == before


async def test_dry_run_reraises_an_error_from_the_failure_hook(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    await _setup(db)

    async def broken(self: object, tx: object, execution_id: int, error: DomainError) -> None:
        raise InsufficientBalance("collection failed")

    monkeypatch.setattr(contract_calls._TopLevelCall, "on_failure", broken)
    before = await _snapshot(db)
    with pytest.raises(InsufficientBalance, match="collection failed"):
        await dry_run_contract(db, None, caller_id=CALLER, contract_id=A, function="refuse", args=[], config=config(), now=NOW)
    assert await _snapshot(db) == before


async def test_dry_run_reports_the_random_values_drawn(db: AsyncEngine) -> None:
    await _setup(db)
    dry = await dry_run_contract(db, None, caller_id=CALLER, contract_id=B, function="double", args=[4], config=config(), now=NOW)
    assert (dry.value, dry.random_values) == (8, (0,))
    nested = await dry_run_contract(db, None, caller_id=CALLER, contract_id=A, function="work", args=[3], gas_limit=2_000, config=config(), now=NOW)
    assert nested.random_values == ()
    assert [(draw.order, draw.depth, draw.contract_id, draw.function, draw.values) for draw in nested.random_tree] == [(1, 2, B, "double", (0,))]


async def test_dry_run_of_a_caught_nested_failure_with_an_attachment(db: AsyncEngine) -> None:
    await _setup(db)
    before = await _snapshot(db)
    dry = await dry_run_contract(db, None, caller_id=CALLER, contract_id=A, function="shielded", args=[], attach=Attachment(Currency(RAPID), 10), gas_limit=2_000, config=config(), now=NOW)
    assert await _snapshot(db) == before
    assert (dry.value, dry.error) == ("REVERTED:flaky", None)
    assert [(draw.contract_id, draw.function, draw.values) for draw in dry.random_tree] == [(B, "flaky", (7,))]
    kinds = [(item.kind, item.from_account, item.to_account, item.amount) for item in dry.transfers]
    assert kinds[0] == (TransferKind.PRINCIPAL, CALLER, A, 10)
    assert [kind for kind, *_ in kinds] == [TransferKind.PRINCIPAL, TransferKind.GAS]
    real = await call_contract(db, None, caller_id=CALLER, contract_id=A, function="shielded", args=[], attach=Attachment(Currency(RAPID), 10), gas_limit=2_000, config=config(), now=NOW)
    assert (real.value, real.gas_used) == (dry.value, dry.gas_used)
