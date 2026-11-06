"""Idempotent replay over every write route: each is sent twice with one Idempotency-Key, and the second answer must repeat the first with Idempotency-Replayed: true while exactly one execution is recorded."""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api.app import create_app
from rapidwire.api.auth import get_writer
from rapidwire.config import Settings
from rapidwire.core import allowances, claims, contracts, staking
from rapidwire.core import currency as currencies
from rapidwire.core.currency import DELETE_TIMELOCK, RATE_CHANGE_TIMELOCK
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.tokens import issue_token
from rapidwire.core.types import ExecutionType
from rapidwire.db.tables import execution
from tests.integration.api.conftest import bearer
from tests.integration.factories import contract_binary, create_currency, create_pool, mint, register_contract

ALICE = 7401
BOB = 7402
CAROL = 7403
CUR = 4401
CUR2 = 4402
GUILD_ID = 7_700_101
KEY = "replay-every-route"
CONTRACT_SOURCE_V2 = """
.function ping public returns=int
  PUSH_INT 2
  RET
.end
"""


@dataclass(frozen=True)
class World:
    db: AsyncEngine
    guild_info: InMemoryGuildInfo
    settings: Settings


type Request = tuple[str, dict[str, Any] | None]
type Setup = Callable[[World], Awaitable[Request]]


def _fixed(path: str, payload: dict[str, Any] | None = None) -> Setup:
    async def setup(world: World) -> Request:
        return path, payload

    return setup


async def _transfer_from(world: World) -> Request:
    await mint(world.db, CUR, BOB, 5000)
    await allowances.approve(world.db, owner_id=BOB, spender_id=ALICE, currency_id=CUR, amount=3000)
    return "/v1/transfers/from", {"source_id": str(BOB), "recipient_id": str(CAROL), "currency_id": str(CUR), "amount": "1"}


async def _claim_path(world: World, action: str) -> Request:
    created = await claims.create_claim(world.db, claimant_id=BOB, payer_id=ALICE, currency_id=CUR, amount=1000)
    return f"/v1/claims/{created.claim_id}/{action}", None


async def _claim_pay(world: World) -> Request:
    return await _claim_path(world, "pay")


async def _claim_cancel(world: World) -> Request:
    return await _claim_path(world, "cancel")


async def _withdraw(world: World) -> Request:
    await staking.deposit(world.db, account_id=ALICE, currency_id=CUR, amount=2000)
    return "/v1/stakes/withdraw", {"currency_id": str(CUR), "amount": "1"}


async def _rate_change_apply(world: World) -> Request:
    past = datetime.now(UTC) - RATE_CHANGE_TIMELOCK - timedelta(minutes=1)
    await currencies.request_rate_change(world.db, world.guild_info, currency_id=CUR, user_id=ALICE, new_rate_ppm=5, now=past)
    return f"/v1/currencies/{CUR}/rate-change/apply", None


async def _delete_finalize(world: World) -> Request:
    past = datetime.now(UTC) - DELETE_TIMELOCK - timedelta(minutes=1)
    await currencies.request_delete(world.db, world.guild_info, currency_id=CUR, user_id=ALICE, now=past)
    return f"/v1/currencies/{CUR}/delete/finalize", None


async def _pool_of_alice(world: World) -> None:
    await create_pool(world.db, user_id=ALICE, currency_x=CUR, currency_y=CUR2, amount_x=100_000, amount_y=100_000, fee_bps=100)


async def _swap(world: World) -> Request:
    await _pool_of_alice(world)
    return "/v1/swap", {"from_currency": str(CUR), "to_currency": str(CUR2), "amount_in": "1", "min_out": "0"}


async def _add_liquidity(world: World) -> Request:
    await _pool_of_alice(world)
    return "/v1/pools/liquidity/add", {"currency_x": str(CUR), "currency_y": str(CUR2), "fee_bps": 100, "amount_x": "5", "amount_y": "5"}


async def _remove_liquidity(world: World) -> Request:
    await _pool_of_alice(world)
    return "/v1/pools/liquidity/remove", {"currency_x": str(CUR2), "currency_y": str(CUR), "fee_bps": 100, "shares": "1000"}


async def _contract_path(world: World, action: str) -> Request:
    await register_contract(world.db, ALICE)
    if action == "resume":
        await contracts.pause_contract(world.db, account_id=ALICE)
    return f"/v1/contracts/{ALICE}/{action}", {"hours": 1} if action == "lock" else None


async def _contract_lock(world: World) -> Request:
    return await _contract_path(world, "lock")


async def _contract_pause(world: World) -> Request:
    return await _contract_path(world, "pause")


async def _contract_resume(world: World) -> Request:
    return await _contract_path(world, "resume")


async def _contract_delete(world: World) -> Request:
    return await _contract_path(world, "delete")


async def _contract_update(world: World) -> Request:
    await register_contract(world.db, ALICE)
    return f"/v1/contracts/{ALICE}/versions", {"binary": base64.b64encode(contract_binary(CONTRACT_SOURCE_V2)).decode("ascii")}


async def _contract_call(world: World) -> Request:
    gas = world.settings.gas_currency_id
    await create_currency(world.db, currency_id=gas, symbol="RAPID", name="Rapid")
    await mint(world.db, gas, ALICE, 1_000_000)
    await register_contract(world.db, BOB)
    return f"/v1/contracts/{BOB}/call", {"function": "ping"}


# Every endpoint that depends on the Writer, with the state that lets its first request succeed.
WRITES: dict[str, Setup] = {
    "/v1/transfers": _fixed("/v1/transfers", {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1"}),
    "/v1/transfers/from": _transfer_from,
    "/v1/allowances": _fixed("/v1/allowances", {"spender_id": str(BOB), "currency_id": str(CUR), "amount": "1"}),
    "/v1/claims": _fixed("/v1/claims", {"payer_id": str(BOB), "currency_id": str(CUR), "amount": "1", "description": "replayed"}),
    "/v1/claims/{claim_id}/pay": _claim_pay,
    "/v1/claims/{claim_id}/cancel": _claim_cancel,
    "/v1/stakes/deposit": _fixed("/v1/stakes/deposit", {"currency_id": str(CUR), "amount": "1"}),
    "/v1/stakes/withdraw": _withdraw,
    "/v1/tokens/revoke": _fixed("/v1/tokens/revoke"),
    "/v1/currencies": _fixed("/v1/currencies", {"guild_id": str(GUILD_ID), "name": "Replayed", "symbol": "rpl", "supply": "1"}),
    "/v1/currencies/{currency_id}/mint": _fixed(f"/v1/currencies/{CUR}/mint", {"amount": "1"}),
    "/v1/currencies/{currency_id}/burn": _fixed(f"/v1/currencies/{CUR}/burn", {"amount": "1"}),
    "/v1/currencies/{currency_id}/renounce": _fixed(f"/v1/currencies/{CUR}/renounce"),
    "/v1/currencies/{currency_id}/rate-change": _fixed(f"/v1/currencies/{CUR}/rate-change", {"hourly_rate_ppm": "5"}),
    "/v1/currencies/{currency_id}/rate-change/apply": _rate_change_apply,
    "/v1/currencies/{currency_id}/delete": _fixed(f"/v1/currencies/{CUR}/delete"),
    "/v1/currencies/{currency_id}/delete/finalize": _delete_finalize,
    "/v1/currencies/{currency_id}/delegation/enable": _fixed(f"/v1/currencies/{CUR}/delegation/enable"),
    "/v1/currencies/{currency_id}/delegation/disable": _fixed(f"/v1/currencies/{CUR}/delegation/disable"),
    "/v1/notifications/allow": _fixed("/v1/notifications/allow", {"user_id": str(BOB)}),
    "/v1/notifications/deny": _fixed("/v1/notifications/deny", {"user_id": str(BOB)}),
    "/v1/guilds/{guild_id}/discord-permissions/allow": _fixed(f"/v1/guilds/{GUILD_ID}/discord-permissions/allow", {"account_id": str(BOB)}),
    "/v1/guilds/{guild_id}/discord-permissions/deny": _fixed(f"/v1/guilds/{GUILD_ID}/discord-permissions/deny", {"account_id": str(BOB)}),
    "/v1/swap": _swap,
    "/v1/pools": _fixed("/v1/pools", {"currency_x": str(CUR), "currency_y": str(CUR2), "fee_bps": 10, "amount_x": "5", "amount_y": "5"}),
    "/v1/pools/liquidity/add": _add_liquidity,
    "/v1/pools/liquidity/remove": _remove_liquidity,
    "/v1/contracts/{account_id}/versions": _contract_update,
    "/v1/contracts/{account_id}/lock": _contract_lock,
    "/v1/contracts/{account_id}/pause": _contract_pause,
    "/v1/contracts/{account_id}/resume": _contract_resume,
    "/v1/contracts/{account_id}/delete": _contract_delete,
    "/v1/contracts/{account_id}/call": _contract_call,
}


def _depends_on(dependant: Dependant, call: object) -> bool:
    return dependant.call is call or any(_depends_on(sub, call) for sub in dependant.dependencies)


def test_every_writer_route_is_replayed(api_settings: Settings) -> None:
    app = create_app(api_settings)
    writer_paths = {str(context.path) for context in iter_route_contexts(app.routes) if isinstance(context.original_route, APIRoute) and _depends_on(context.original_route.dependant, get_writer)}
    assert writer_paths == set(WRITES)


async def _executions(engine: AsyncEngine) -> list[tuple[int, str, str | None]]:
    # Token issues are left out because the revoke case issues a new token between its two requests.
    stmt = select(execution.c.execution_id, execution.c.type, execution.c.idempotency_key).where(execution.c.type != ExecutionType.TOKEN_ISSUE).order_by(execution.c.execution_id)
    async with engine.connect() as conn:
        return [(row.execution_id, row.type, row.idempotency_key) for row in await conn.execute(stmt)]


async def _send(client: httpx.AsyncClient, request: Request, headers: dict[str, str]) -> httpx.Response:
    path, payload = request
    return await client.post(path, headers=headers) if payload is None else await client.post(path, json=payload, headers=headers)


@pytest.mark.parametrize("template", sorted(WRITES))
async def test_every_write_replays_under_one_key(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, api_settings: Settings, template: str) -> None:
    for currency_id, symbol in ((CUR, "RPA"), (CUR2, "RPB")):
        await create_currency(db, currency_id=currency_id, symbol=symbol)
        await mint(db, currency_id, ALICE, 1_000_000)
    guild_info.add_guild(CUR, ALICE)
    guild_info.add_guild(GUILD_ID, ALICE)
    request = await WRITES[template](World(db=db, guild_info=guild_info, settings=api_settings))
    before = await _executions(db)
    issued = await issue_token(db, account_id=ALICE)
    first = await _send(client, request, {**bearer(issued.token), "Idempotency-Key": KEY})
    assert first.status_code == 200, first.text
    assert "idempotency-replayed" not in first.headers
    recorded = await _executions(db)
    if template == "/v1/tokens/revoke":
        # The first request revoked the token that sent it, so the repeat comes with a newer token of the same account.
        issued = await issue_token(db, account_id=ALICE)
    again = await _send(client, request, {**bearer(issued.token), "Idempotency-Key": KEY})
    assert again.status_code == first.status_code, again.text
    assert again.json() == first.json()
    assert again.headers["idempotency-replayed"] == "true"
    new = recorded[len(before) :]
    assert recorded[: len(before)] == before
    assert [row[2] for row in new] == [KEY], new
    assert str(new[0][0]) == first.json()["execution_id"]
    assert await _executions(db) == recorded
