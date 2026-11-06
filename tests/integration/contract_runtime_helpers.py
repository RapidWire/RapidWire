"""Shared setup for the contract runtime tests: a gas currency, compiled contracts registered through the contracts service, a RuntimeConfig, and readers of the execution, transfer, storage and outbox tables."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.compiler import compile_source
from rapidwire.contract.runtime import RuntimeConfig
from rapidwire.contract.values import Value
from rapidwire.core import contracts
from rapidwire.core.invariants import verify_invariants
from rapidwire.db.tables import execution, outbox, transfer
from tests.integration.factories import create_currency, mint

NOW = datetime(2026, 9, 1, tzinfo=UTC)
RAPID = 900_001
RAPID_SYMBOL = "RAPID"
CALLER = 7001
OTHER_CALLER = 7002
CONTRACT = 8001

PRELUDE = "from rapidwire_contract import *\n"


def config(**overrides: Any) -> RuntimeConfig:
    values: dict[str, Any] = {"gas_currency_id": RAPID, "gas_price": 1, "default_gas": 128, "max_gas": 100_000}
    values.update(overrides)
    return RuntimeConfig(**values)


async def setup_gas(engine: AsyncEngine, funded: dict[int, int] | None = None) -> int:
    """Create the gas currency and mint the given balances; returns the issuance account ID."""
    seeded = await create_currency(engine, currency_id=RAPID, symbol=RAPID_SYMBOL, name="Rapid", now=NOW)
    for account_id, amount in (funded or {CALLER: 100_000}).items():
        await mint(engine, RAPID, account_id, amount, now=NOW)
    return seeded.issuance_account_id


async def deploy(engine: AsyncEngine, account_id: int, source: str) -> None:
    await contracts.register_contract(engine, account_id=account_id, binary=compile_source(PRELUDE + source), max_size=8000, now=NOW)


async def executions(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(execution).where(execution.c.type == "contract_call").order_by(execution.c.execution_id))).mappings().all()]


async def transfers_of(engine: AsyncEngine, execution_id: int | None = None) -> list[dict[str, Any]]:
    stmt = select(transfer).order_by(transfer.c.transfer_id)
    if execution_id is not None:
        stmt = stmt.where(transfer.c.execution_id == execution_id)
    async with engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(stmt)).mappings().all()]


async def outbox_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(outbox).order_by(outbox.c.outbox_id))).mappings().all()]


async def stored(engine: AsyncEngine, account_id: int, key: str) -> Value | None:
    return await contracts.read_storage(engine, account_id, key)


async def assert_clean(engine: AsyncEngine) -> None:
    report = await verify_invariants(engine)
    assert report.violations == []
