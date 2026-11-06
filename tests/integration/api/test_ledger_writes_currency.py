from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.currency import DELETE_EXPIRY, DELETE_TIMELOCK, RATE_CHANGE_TIMELOCK, get_currency
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.types import ExecutionType
from rapidwire.db.tables import RATE_PPM_MAX, currency
from tests.integration.api.conftest import AuthHeaders
from tests.integration.api.test_ledger_writes import count_executions, expect_error
from tests.integration.factories import balance_of, create_currency, mint, supply_of

OWNER = 7201
ADMIN = 7202
HOLDER = 7203
GUILD = 6101
OTHER_GUILD = 6102


def _parse_time(value: str) -> datetime:
    assert value.endswith("Z"), value
    return datetime.fromisoformat(value)


async def _create(client: httpx.AsyncClient, headers: dict[str, str], **fields: object) -> httpx.Response:
    body = {"guild_id": str(GUILD), "name": "Gold", "symbol": "GLD", "supply": "100", "hourly_rate_ppm": "1000", **fields}
    return await client.post("/v1/currencies", json=body, headers=headers)


async def test_owner_creates_the_currency(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    owner = await auth_headers(OWNER)
    response = await _create(client, owner)
    assert response.status_code == 200, response.text
    body = response.json()
    assert {key: body[key] for key in ("currency_id", "name", "symbol", "hourly_rate_ppm", "initial_supply", "revived")} == {
        "currency_id": str(GUILD),
        "name": "Gold",
        "symbol": "GLD",
        "hourly_rate_ppm": "1000",
        "initial_supply": "100.000",
        "revived": False,
    }
    assert body["execution_id"].isdigit() and body["transfer_id"].isdigit()
    assert await balance_of(db, OWNER, GUILD) == 100_000
    expect_error(await _create(client, owner, symbol="GLDX"), 409, "already_exists", recorded=True)


async def test_create_without_rate_and_supply_zero(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    body = {"guild_id": str(GUILD), "name": "Gold", "symbol": "GLD", "supply": "0"}
    response = await client.post("/v1/currencies", json=body, headers=await auth_headers(OWNER))
    assert response.status_code == 200, response.text
    assert (response.json()["hourly_rate_ppm"], response.json()["initial_supply"], response.json()["transfer_id"]) == ("0", "0.000", None)


async def test_create_is_refused_to_others_and_without_the_guild(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    guild_info.add_admin(GUILD, ADMIN)
    expect_error(await _create(client, await auth_headers(ADMIN)), 403, "permission_denied", recorded=True)
    expect_error(await _create(client, await auth_headers(OWNER), guild_id=str(OTHER_GUILD)), 409, "guild_unavailable", recorded=True)
    assert await get_currency(db, GUILD) is None
    assert await get_currency(db, OTHER_GUILD) is None


async def test_create_validates_the_body(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    owner = await auth_headers(OWNER)
    invalid: tuple[dict[str, object], ...] = (
        {"hourly_rate_ppm": 1000},
        {"hourly_rate_ppm": "-1"},
        {"hourly_rate_ppm": str(RATE_PPM_MAX + 1)},
        {"supply": 100},
        {"supply": "-1"},
        {"guild_id": "0"},
        {"name": "N" * 25},
        {"symbol": "ABCDEFGHI"},
    )
    for fields in invalid:
        expect_error(await _create(client, owner, **fields), 422, "validation_error", recorded=False)
    assert await count_executions(db, ExecutionType.CURRENCY_CREATE) == 0
    expect_error(await _create(client, owner, symbol="G_D"), 422, "invalid_argument", recorded=True)
    assert await count_executions(db, ExecutionType.CURRENCY_CREATE) == 1
    created = await _create(client, owner, hourly_rate_ppm=str(RATE_PPM_MAX))
    assert created.status_code == 200, created.text
    assert created.json()["hourly_rate_ppm"] == str(RATE_PPM_MAX)


async def test_create_uppercases_the_symbol(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    response = await _create(client, await auth_headers(OWNER), symbol="gld-x")
    assert response.status_code == 200, response.text
    assert response.json()["symbol"] == "GLD-X"
    info = await get_currency(db, GUILD)
    assert info is not None and info.symbol == "GLD-X"
    assert (await client.get("/v1/currencies/symbol/gld-x")).json()["currency_id"] == str(GUILD)


async def test_create_replays(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    headers = {**await auth_headers(OWNER), "Idempotency-Key": "create-gold"}
    first = await _create(client, headers)
    guild_info.remove_guild(GUILD)
    again = await _create(client, headers)
    assert (first.status_code, again.status_code) == (200, 200), again.text
    assert again.json() == first.json()
    assert again.headers["idempotency-replayed"] == "true"
    assert await count_executions(db, ExecutionType.CURRENCY_CREATE) == 1


async def test_mint_burn_and_delegation(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    guild_info.add_admin(GUILD, ADMIN)
    await create_currency(db, currency_id=GUILD, symbol="GLD")
    owner, admin = await auth_headers(OWNER), await auth_headers(ADMIN)
    minted = await client.post(f"/v1/currencies/{GUILD}/mint", json={"amount": "5"}, headers=owner)
    assert minted.status_code == 200, minted.text
    assert {key: minted.json()[key] for key in ("currency_id", "account_id", "amount")} == {"currency_id": str(GUILD), "account_id": str(OWNER), "amount": "5.000"}
    expect_error(await client.post(f"/v1/currencies/{GUILD}/mint", json={"amount": "1"}, headers=admin), 403, "permission_denied", recorded=True)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/delegation/enable", headers=admin), 403, "permission_denied", recorded=True)

    enabled = await client.post(f"/v1/currencies/{GUILD}/delegation/enable", headers=owner)
    assert enabled.status_code == 200, enabled.text
    assert {key: enabled.json()[key] for key in ("currency_id", "enabled", "changed")} == {"currency_id": str(GUILD), "enabled": True, "changed": True}
    assert (await client.post(f"/v1/currencies/{GUILD}/delegation/enable", headers=owner)).json()["changed"] is False
    by_admin = await client.post(f"/v1/currencies/{GUILD}/mint", json={"amount": "2"}, headers=admin)
    assert by_admin.status_code == 200, by_admin.text
    assert await balance_of(db, ADMIN, GUILD) == 2000

    disabled = await client.post(f"/v1/currencies/{GUILD}/delegation/disable", headers=owner)
    assert (disabled.json()["enabled"], disabled.json()["changed"]) == (False, True)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/mint", json={"amount": "1"}, headers=admin), 403, "permission_denied", recorded=True)

    await mint(db, GUILD, HOLDER, 3000)
    guild_info.remove_guild(GUILD)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/mint", json={"amount": "1"}, headers=owner), 409, "guild_unavailable", recorded=True)
    burned = await client.post(f"/v1/currencies/{GUILD}/burn", json={"amount": "1"}, headers=await auth_headers(HOLDER))
    assert burned.status_code == 200, burned.text
    assert {key: burned.json()[key] for key in ("account_id", "amount")} == {"account_id": str(HOLDER), "amount": "1.000"}
    assert await balance_of(db, HOLDER, GUILD) == 2000
    assert await supply_of(db, GUILD) == 5000 + 2000 + 2000
    expect_error(await client.post(f"/v1/currencies/{GUILD}/burn", json={"amount": "2.001"}, headers=await auth_headers(HOLDER)), 409, "insufficient_balance", recorded=True)


async def test_missing_currency_is_404(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    owner = await auth_headers(OWNER)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/mint", json={"amount": "1"}, headers=owner), 404, "currency_not_found", recorded=True)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/burn", json={"amount": "1"}, headers=owner), 404, "currency_not_found", recorded=True)
    expect_error(await client.post("/v1/currencies/abc/renounce", headers=owner), 422, "validation_error", recorded=False)


async def test_renounce(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    await create_currency(db, currency_id=GUILD, symbol="GLD")
    owner = await auth_headers(OWNER)
    renounced = await client.post(f"/v1/currencies/{GUILD}/renounce", headers=owner)
    assert renounced.status_code == 200, renounced.text
    assert renounced.json()["currency_id"] == str(GUILD)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/renounce", headers=owner), 409, "currency_renounced", recorded=True)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/mint", json={"amount": "1"}, headers=owner), 409, "currency_renounced", recorded=True)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/rate-change", json={"hourly_rate_ppm": "5"}, headers=owner), 409, "currency_renounced", recorded=True)


async def test_rate_change_request_and_apply(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    await create_currency(db, currency_id=GUILD, symbol="GLD", hourly_rate_ppm=100)
    headers = {**await auth_headers(OWNER), "Idempotency-Key": "rate-1"}
    requested = await client.post(f"/v1/currencies/{GUILD}/rate-change", json={"hourly_rate_ppm": "2500"}, headers=headers)
    assert requested.status_code == 200, requested.text
    body = requested.json()
    assert (body["currency_id"], body["new_rate_ppm"]) == (str(GUILD), "2500")
    assert _parse_time(body["apply_after"]) - _parse_time(body["requested_at"]) == RATE_CHANGE_TIMELOCK
    replayed = await client.post(f"/v1/currencies/{GUILD}/rate-change", json={"hourly_rate_ppm": "2500"}, headers=headers)
    assert replayed.json() == body
    assert replayed.headers["idempotency-replayed"] == "true"
    expect_error(await client.post(f"/v1/currencies/{GUILD}/rate-change", json={"hourly_rate_ppm": "3000"}, headers=await auth_headers(OWNER)), 409, "invalid_state", recorded=True)
    owner = await auth_headers(OWNER)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/rate-change/apply", headers=owner), 409, "timelock_active", recorded=True)
    for rate in (10, "-5", str(RATE_PPM_MAX + 1)):
        expect_error(await client.post(f"/v1/currencies/{GUILD}/rate-change", json={"hourly_rate_ppm": rate}, headers=owner), 422, "validation_error", recorded=False)

    async with db.begin() as conn:
        await conn.execute(currency.update().where(currency.c.currency_id == GUILD).values(rate_change_requested_at=datetime.now(UTC) - RATE_CHANGE_TIMELOCK - timedelta(minutes=1)))
    applied = await client.post(f"/v1/currencies/{GUILD}/rate-change/apply", headers=owner)
    assert applied.status_code == 200, applied.text
    assert {key: applied.json()[key] for key in ("currency_id", "old_rate_ppm", "new_rate_ppm", "stakes_accrued", "interest_execution_ids")} == {
        "currency_id": str(GUILD),
        "old_rate_ppm": "100",
        "new_rate_ppm": "2500",
        "stakes_accrued": 0,
        "interest_execution_ids": [],
    }
    expect_error(await client.post(f"/v1/currencies/{GUILD}/rate-change/apply", headers=owner), 409, "no_pending_request", recorded=True)


async def test_delete_request_and_finalize(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    await create_currency(db, currency_id=GUILD, symbol="GLD")
    await mint(db, GUILD, HOLDER, 4000)
    owner = await auth_headers(OWNER)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/delete/finalize", headers=owner), 409, "no_pending_request", recorded=True)
    requested = await client.post(f"/v1/currencies/{GUILD}/delete", headers=owner)
    assert requested.status_code == 200, requested.text
    body = requested.json()
    start = _parse_time(body["requested_at"])
    assert (_parse_time(body["finalize_from"]) - start, _parse_time(body["finalize_until"]) - start) == (DELETE_TIMELOCK, DELETE_EXPIRY)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/delete", headers=owner), 409, "invalid_state", recorded=True)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/delete/finalize", headers=owner), 409, "timelock_active", recorded=True)
    expect_error(await client.post(f"/v1/currencies/{GUILD}/delete/finalize", headers=await auth_headers(HOLDER)), 403, "permission_denied", recorded=True)

    async with db.begin() as conn:
        await conn.execute(currency.update().where(currency.c.currency_id == GUILD).values(delete_requested_at=datetime.now(UTC) - DELETE_TIMELOCK - timedelta(minutes=1)))
    finalized = await client.post(f"/v1/currencies/{GUILD}/delete/finalize", headers=owner)
    assert finalized.status_code == 200, finalized.text
    assert finalized.json() == {
        "execution_id": finalized.json()["execution_id"],
        "currency_id": str(GUILD),
        "burned_amount": "4.000",
        "balances_burned": 1,
        "stakes_burned": 0,
        "claims_canceled": 0,
        "allowances_deleted": 0,
        "pools_dissolved": 0,
    }
    async with db.connect() as conn:
        assert (await conn.execute(select(currency.c.deleted_at).where(currency.c.currency_id == GUILD))).scalar_one() is not None
    assert await balance_of(db, HOLDER, GUILD) == 0


async def test_paying_a_claim_of_a_deleted_currency_is_invalid_state(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, OWNER)
    await create_currency(db, currency_id=GUILD, symbol="GLD")
    await mint(db, GUILD, HOLDER, 5000)
    created = await client.post("/v1/claims", json={"payer_id": str(HOLDER), "currency_id": str(GUILD), "amount": "1"}, headers=await auth_headers(OWNER))
    assert created.status_code == 200, created.text
    owner = await auth_headers(OWNER)
    assert (await client.post(f"/v1/currencies/{GUILD}/delete", headers=owner)).status_code == 200
    async with db.begin() as conn:
        await conn.execute(currency.update().where(currency.c.currency_id == GUILD).values(delete_requested_at=datetime.now(UTC) - DELETE_TIMELOCK - timedelta(minutes=1)))
    finalized = await client.post(f"/v1/currencies/{GUILD}/delete/finalize", headers=owner)
    assert finalized.json()["claims_canceled"] == 1
    expect_error(await client.post(f"/v1/claims/{created.json()['claim_id']}/pay", headers=await auth_headers(HOLDER)), 409, "invalid_state", recorded=True)
