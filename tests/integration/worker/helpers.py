"""Setup for the worker tests: contracts that queue Discord operations, a claim with a notification, readers of outbox rows and of the ledger tables the dispatcher must never change."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import claims, notifications
from rapidwire.core.contract_calls import ContractCallResult, call_contract
from rapidwire.core.discord_permissions import allow_discord
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.db.tables import balance, execution, outbox, transfer
from tests.integration.contract_runtime_helpers import CALLER, NOW, config, deploy, setup_gas
from tests.integration.factories import create_currency, mint

GUILD = 600_001
OWNER = 600_002
CHANNEL = 600_003
ROLE = 600_004
OTHER_GUILD = 600_099
CONTRACT_A = 8001
CONTRACT_B = 8002
CLAIMANT = 7101
PAYER = 7102
CLAIM_CURRENCY = 910_001
# Rows are queued at NOW; the dispatcher's clock starts a minute later, so they are due.
DISPATCH_AT = NOW + timedelta(minutes=1)

SOURCE = """
GUILD = 600001

@public
def shout(text: str) -> str:
    discord.send(GUILD, 600003, text)
    return "sent"

@public
def shout_at(channel: int, text: str) -> str:
    discord.send(GUILD, channel, text)
    return "sent"

@public
def grant(user: Account) -> str:
    discord.add_role(GUILD, user, 600004)
    discord.remove_role(GUILD, user, 600005)
    return "done"

@public
def relay(target: Account, text: str) -> str:
    discord.send(GUILD, 600003, "outer")
    r = try_call(target, "shout", [text])
    if r.ok:
        return "ok"
    return r.error
"""


def guilds() -> InMemoryGuildInfo:
    info = InMemoryGuildInfo()
    info.add_guild(GUILD, OWNER)
    return info


async def setup_contracts(engine: AsyncEngine) -> InMemoryGuildInfo:
    """Gas for CALLER, contracts A and B with SOURCE, both allowed to use Discord in GUILD."""
    info = guilds()
    await setup_gas(engine, {CALLER: 1_000_000})
    for account_id in (CONTRACT_A, CONTRACT_B):
        await deploy(engine, account_id, SOURCE)
        await allow_discord(engine, info, guild_id=GUILD, account_id=account_id, acting_user_id=OWNER, now=NOW)
    return info


async def call(engine: AsyncEngine, function: str, args: list[Any]) -> ContractCallResult:
    return await call_contract(engine, None, caller_id=CALLER, contract_id=CONTRACT_A, function=function, args=args, gas_limit=5_000, config=config(), now=NOW)


async def create_notified_claim(engine: AsyncEngine, *, notify: bool = True) -> claims.CreateClaimResult:
    """A claim of 12345 units from CLAIMANT to PAYER, who holds enough to pay it and, with notify, allows notifications from CLAIMANT."""
    await create_currency(engine, currency_id=CLAIM_CURRENCY, symbol="CLM", name="Claim", now=NOW)
    await mint(engine, CLAIM_CURRENCY, PAYER, 1_000_000, now=NOW)
    if notify:
        await notifications.allow_notifications(engine, user_id=PAYER, allowed_user_id=CLAIMANT, now=NOW)
    return await claims.create_claim(engine, claimant_id=CLAIMANT, payer_id=PAYER, currency_id=CLAIM_CURRENCY, amount=12_345, description="lunch", now=NOW)


async def outbox_rows(engine: AsyncEngine) -> list[dict[str, Any]]:
    async with engine.connect() as conn:
        return [dict(row) for row in (await conn.execute(select(outbox).order_by(outbox.c.outbox_id))).mappings().all()]


async def ledger_snapshot(engine: AsyncEngine) -> dict[str, list[dict[str, Any]]]:
    """Every row of the execution, balance and transfer tables."""
    snapshot: dict[str, list[dict[str, Any]]] = {}
    async with engine.connect() as conn:
        for table in (execution, balance, transfer):
            rows = (await conn.execute(select(table).order_by(*table.primary_key.columns))).mappings().all()
            snapshot[table.name] = [dict(row) for row in rows]
    return snapshot
