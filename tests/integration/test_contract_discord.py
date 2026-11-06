"""Discord builtins of contracts: send, add_role and remove_role queue outbox rows only for an allowed contract and only when its execution commits, and has_role answers from GuildInfo with a timeout."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.contract.values import Account
from rapidwire.core.contract_calls import ContractCallResult, call_contract
from rapidwire.core.discord_permissions import allow_discord
from rapidwire.core.errors import ContractReverted, ContractRuntimeFailure
from rapidwire.core.permissions import GuildInfo, InMemoryGuildInfo
from rapidwire.core.types import OutboxKind
from tests.integration.contract_runtime_helpers import CALLER, NOW, assert_clean, config, deploy, executions, outbox_rows, setup_gas

GUILD = 600_001
OWNER = 600_002
CHANNEL = 600_003
ROLE = 600_004
A = 8001
B = 8002

SOURCE = """
GUILD = 600001

@public
def shout(text: str) -> str:
    discord.send(GUILD, 600003, text)
    return "sent"

@public
def grant(user: Account) -> str:
    discord.add_role(GUILD, user, 600004)
    discord.remove_role(GUILD, user, 600005)
    return "done"

@public
def grant_then_revert(user: Account) -> str:
    discord.add_role(GUILD, user, 600004)
    revert("no")

@public
def check(guild: int, user: Account, role: int) -> bool:
    return discord.has_role(guild, user, role)

@public
def bad_channel() -> str:
    discord.send(GUILD, 0, "x")
    return "x"

@public
def long_message(text: str) -> str:
    discord.send(GUILD, 600003, text)
    return "x"

@public
def relay(target: Account, fn: str) -> str:
    discord.send(GUILD, 600003, "before")
    r = try_call(target, fn, [sender])
    if r.ok:
        return "ok"
    return r.error
"""


def _guilds() -> InMemoryGuildInfo:
    guilds = InMemoryGuildInfo()
    guilds.add_guild(GUILD, OWNER)
    guilds.add_role(GUILD, CALLER, ROLE)
    return guilds


async def _setup(engine: AsyncEngine, guilds: InMemoryGuildInfo, *allowed: int) -> None:
    await setup_gas(engine, {CALLER: 1_000_000})
    await deploy(engine, A, SOURCE)
    await deploy(engine, B, SOURCE)
    for account_id in allowed:
        await allow_discord(engine, guilds, guild_id=GUILD, account_id=account_id, acting_user_id=OWNER, now=NOW)


async def _call(engine: AsyncEngine, function: str, args: list[Any], guild_info: GuildInfo | None = None, **kwargs: Any) -> ContractCallResult:
    return await call_contract(engine, guild_info, caller_id=CALLER, contract_id=A, function=function, args=args, gas_limit=5_000, config=config(**kwargs), now=NOW)


async def test_send_and_role_changes_queue_outbox_rows(db: AsyncEngine) -> None:
    guilds = _guilds()
    await _setup(db, guilds, A)
    sent = await _call(db, "shout", ["hello"])
    granted = await _call(db, "grant", [Account(CALLER)])
    rows = await outbox_rows(db)
    assert [(row["kind"], row["execution_id"], row["status"]) for row in rows] == [
        (OutboxKind.DISCORD_SEND, sent.execution_id, "pending"),
        (OutboxKind.DISCORD_ADD_ROLE, granted.execution_id, "pending"),
        (OutboxKind.DISCORD_REMOVE_ROLE, granted.execution_id, "pending"),
    ]
    assert rows[0]["payload"] == {"contract_id": str(A), "guild_id": str(GUILD), "channel_id": str(CHANNEL), "message": "hello"}
    assert rows[1]["payload"] == {"contract_id": str(A), "guild_id": str(GUILD), "user_id": str(CALLER), "role_id": str(ROLE)}
    assert rows[2]["payload"]["role_id"] == str(ROLE + 1)
    await assert_clean(db)


async def test_missing_permission_is_a_runtime_failure(db: AsyncEngine) -> None:
    guilds = _guilds()
    await _setup(db, guilds, B)
    with pytest.raises(ContractRuntimeFailure) as info:
        await _call(db, "shout", ["hello"])
    [row] = [row for row in await executions(db) if row["execution_id"] == info.value.execution_id]
    assert row["output"]["host_code"] == "permission_denied"
    assert await outbox_rows(db) == []


@pytest.mark.parametrize(("function", "args", "host_code"), [("bad_channel", [], None), ("long_message", ["x" * 2001], "invalid_argument"), ("long_message", [""], "invalid_argument")])
async def test_invalid_ids_and_message_lengths_are_rejected(db: AsyncEngine, function: str, args: list[Any], host_code: str | None) -> None:
    guilds = _guilds()
    await _setup(db, guilds, A)
    assert (await _call(db, "long_message", ["x" * 2000])).value == "x"
    with pytest.raises(ContractRuntimeFailure) as info:
        await _call(db, function, args)
    [row] = [row for row in await executions(db) if row["execution_id"] == info.value.execution_id]
    assert row["output"]["kind"] == "RUNTIME"
    assert row["output"].get("host_code") == host_code
    assert len(await outbox_rows(db)) == 1


async def test_failed_execution_leaves_no_outbox_row(db: AsyncEngine) -> None:
    guilds = _guilds()
    await _setup(db, guilds, A)
    with pytest.raises(ContractReverted):
        await _call(db, "grant_then_revert", [Account(CALLER)])
    assert await outbox_rows(db) == []


async def test_caught_callee_failure_removes_only_the_callees_rows(db: AsyncEngine) -> None:
    guilds = _guilds()
    await _setup(db, guilds, A, B)
    failed = await _call(db, "relay", [Account(B), "grant_then_revert"])
    assert failed.value == "REVERTED"
    assert [(row["kind"], row["execution_id"]) for row in await outbox_rows(db)] == [(OutboxKind.DISCORD_SEND, failed.execution_id)]
    succeeded = await _call(db, "relay", [Account(B), "grant"])
    assert succeeded.value == "ok"
    [child] = [row for row in await executions(db) if row["parent_execution_id"] == succeeded.execution_id]
    rows = (await outbox_rows(db))[1:]
    assert [(row["kind"], row["execution_id"]) for row in rows] == [
        (OutboxKind.DISCORD_SEND, succeeded.execution_id),
        (OutboxKind.DISCORD_ADD_ROLE, child["execution_id"]),
        (OutboxKind.DISCORD_REMOVE_ROLE, child["execution_id"]),
    ]
    assert rows[1]["payload"]["contract_id"] == str(B)


class _SlowGuildInfo(InMemoryGuildInfo):
    async def has_role(self, guild_id: int, user_id: int, role_id: int) -> bool:
        await asyncio.sleep(5)
        return True


class _BrokenGuildInfo(InMemoryGuildInfo):
    async def has_role(self, guild_id: int, user_id: int, role_id: int) -> bool:
        raise ConnectionError("gateway down")


async def test_has_role_needs_the_discord_permission(db: AsyncEngine) -> None:
    guilds = _guilds()
    await _setup(db, guilds, B)
    with pytest.raises(ContractRuntimeFailure) as info:
        await _call(db, "check", [GUILD, Account(CALLER), ROLE], guilds)
    [row] = [row for row in await executions(db) if row["execution_id"] == info.value.execution_id]
    assert row["output"]["host_code"] == "permission_denied"


async def test_has_role_answers(db: AsyncEngine) -> None:
    guilds = _guilds()
    await _setup(db, guilds, A)
    guilds.add_guild(GUILD + 99, OWNER)
    await allow_discord(db, guilds, guild_id=GUILD + 99, account_id=A, acting_user_id=OWNER, now=NOW)
    guilds.remove_guild(GUILD + 99)
    assert (await _call(db, "check", [GUILD, Account(CALLER), ROLE], guilds)).value is True
    assert (await _call(db, "check", [GUILD, Account(CALLER), ROLE + 1], guilds)).value is False
    assert (await _call(db, "check", [GUILD + 99, Account(CALLER), ROLE], guilds)).value is False
    with pytest.raises(ContractRuntimeFailure):
        await _call(db, "check", [-1, Account(CALLER), ROLE], guilds)
    assert (await _call(db, "check", [GUILD, Account(CALLER), ROLE], None)).value is False
    assert (await _call(db, "check", [GUILD, Account(CALLER), ROLE], _BrokenGuildInfo())).value is False
    slow = _SlowGuildInfo()
    slow.add_guild(GUILD, OWNER)
    async with asyncio.timeout(3):
        result = await _call(db, "check", [GUILD, Account(CALLER), ROLE], slow, has_role_timeout_seconds=0.05)
    assert result.value is False


async def test_has_role_never_writes(db: AsyncEngine) -> None:
    guilds = _guilds()
    await _setup(db, guilds, A)
    before = await outbox_rows(db)
    await _call(db, "check", [GUILD, Account(CALLER), ROLE], guilds)
    assert await outbox_rows(db) == before
