from __future__ import annotations

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core import discord_permissions, notifications
from rapidwire.core import paging as core_paging
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.tokens import issue_token
from rapidwire.core.types import ExecutionType
from tests.integration.api.conftest import AuthHeaders, bearer
from tests.integration.api.test_ledger_writes import count_executions, expect_error

ALICE = 7301
BOB = 7302
CAROL = 7303
GUILD = 6201


async def test_tokens_list_only_the_callers_tokens(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    first = await issue_token(db, account_id=ALICE)
    current = await issue_token(db, account_id=ALICE)
    bob = await issue_token(db, account_id=BOB)
    response = await client.get("/v1/tokens", headers=bearer(current.token))
    assert response.status_code == 200, response.text
    page = response.json()
    assert (page["total"], page["page"], page["per_page"], page["total_is_lower_bound"]) == (2, 1, 15, False)
    assert [(item["token_id"], item["active"]) for item in page["items"]] == [(str(first.token_id), False), (str(current.token_id), True)]
    assert set(page["items"][0]) == {"token_id", "active", "created_at", "revoked_at", "last_used_at"}
    assert page["items"][0]["revoked_at"].endswith("Z") and page["items"][1]["revoked_at"] is None
    assert page["items"][1]["last_used_at"].endswith("Z")
    assert "hash" not in response.text and current.token not in response.text
    active = (await client.get("/v1/tokens", params={"include_revoked": "false"}, headers=bearer(current.token))).json()
    assert [item["token_id"] for item in active["items"]] == [str(current.token_id)]
    of_bob = (await client.get("/v1/tokens", headers=bearer(bob.token))).json()
    assert [item["token_id"] for item in of_bob["items"]] == [str(bob.token_id)]
    expect_error(await client.get("/v1/tokens"), 401, "unauthorized", recorded=False)
    expect_error(await client.get("/v1/tokens", params={"per_page": 101}, headers=bearer(current.token)), 422, "validation_error", recorded=False)


async def test_revoking_the_token_in_use_locks_it_out(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE)
    headers = bearer(issued.token)
    response = await client.post("/v1/tokens/revoke", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["revoked_token_ids"] == [str(issued.token_id)]
    assert response.json()["execution_id"].isdigit()
    for later in (await client.get("/v1/tokens", headers=headers), await client.post("/v1/tokens/revoke", headers=headers)):
        expect_error(later, 401, "unauthorized", recorded=False)
        assert later.headers["www-authenticate"] == "Bearer"
    assert await count_executions(db, ExecutionType.TOKEN_REVOKE) == 1


async def test_revoke_one_token_by_id(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE)
    other = await issue_token(db, account_id=BOB)
    headers = bearer(issued.token)
    expect_error(await client.post("/v1/tokens/revoke", json={"token_id": str(other.token_id)}, headers=headers), 404, "not_found", recorded=True)
    expect_error(await client.post("/v1/tokens/revoke", json={"token_id": int(issued.token_id)}, headers=headers), 422, "validation_error", recorded=False)
    expect_error(await client.post("/v1/tokens/revoke", json={"account_id": str(BOB)}, headers=headers), 422, "validation_error", recorded=False)
    assert (await client.get("/v1/tokens", headers=bearer(other.token))).status_code == 200
    empty = await client.post("/v1/tokens/revoke", json={"token_id": None}, headers=bearer(other.token))
    assert empty.json()["revoked_token_ids"] == [str(other.token_id)]
    revoked = await client.post("/v1/tokens/revoke", json={"token_id": str(issued.token_id)}, headers=headers)
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["revoked_token_ids"] == [str(issued.token_id)]
    expect_error(await client.get("/v1/tokens", headers=headers), 401, "unauthorized", recorded=False)


async def test_revoke_with_an_empty_object_revokes_every_token(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    issued = await issue_token(db, account_id=ALICE)
    response = await client.post("/v1/tokens/revoke", json={}, headers={**bearer(issued.token), "Idempotency-Key": "revoke-all"})
    assert response.status_code == 200, response.text
    assert response.json()["revoked_token_ids"] == [str(issued.token_id)]


async def test_notifications(client: httpx.AsyncClient, db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    alice, bob = await auth_headers(ALICE), await auth_headers(BOB)
    allowed = await client.post("/v1/notifications/allow", json={"user_id": str(BOB)}, headers=alice)
    assert allowed.status_code == 200, allowed.text
    assert {key: allowed.json()[key] for key in ("account_id", "allowed_user_id", "changed")} == {"account_id": str(ALICE), "allowed_user_id": str(BOB), "changed": True}
    assert (await client.post("/v1/notifications/allow", json={"user_id": str(BOB)}, headers=alice)).json()["changed"] is False
    listed = (await client.get("/v1/notifications", headers=alice)).json()
    assert [item["allowed_user_id"] for item in listed["items"]] == [str(BOB)]
    assert listed["items"][0]["created_at"].endswith("Z")
    assert (await client.get("/v1/notifications", headers=bob)).json()["items"] == []
    expect_error(await client.get("/v1/notifications"), 401, "unauthorized", recorded=False)
    denied = await client.post("/v1/notifications/deny", json={"user_id": str(BOB)}, headers=alice)
    assert denied.json()["changed"] is True
    assert (await client.post("/v1/notifications/deny", json={"user_id": str(CAROL)}, headers=alice)).json()["changed"] is False
    assert (await client.get("/v1/notifications", headers=alice)).json()["total"] == 0
    expect_error(await client.post("/v1/notifications/allow", json={"user_id": str(ALICE)}, headers=alice), 422, "invalid_argument", recorded=True)
    expect_error(await client.post("/v1/notifications/allow", json={"user_id": int(BOB)}, headers=alice), 422, "validation_error", recorded=False)


async def test_discord_permissions(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, auth_headers: AuthHeaders) -> None:
    guild_info.add_guild(GUILD, ALICE)
    alice, bob = await auth_headers(ALICE), await auth_headers(BOB)
    base = f"/v1/guilds/{GUILD}/discord-permissions"
    allowed = await client.post(f"{base}/allow", json={"account_id": str(CAROL)}, headers=alice)
    assert allowed.status_code == 200, allowed.text
    assert {key: allowed.json()[key] for key in ("guild_id", "account_id", "changed")} == {"guild_id": str(GUILD), "account_id": str(CAROL), "changed": True}
    assert (await client.post(f"{base}/allow", json={"account_id": str(CAROL)}, headers=alice)).json()["changed"] is False
    listed = await client.get(base, headers=alice)
    assert listed.status_code == 200, listed.text
    [item] = listed.json()["items"]
    assert {key: item[key] for key in ("guild_id", "account_id", "granted_by")} == {"guild_id": str(GUILD), "account_id": str(CAROL), "granted_by": str(ALICE)}

    before = await count_executions(db)
    expect_error(await client.get(base, headers=bob), 403, "permission_denied", recorded=False)
    expect_error(await client.get(base), 401, "unauthorized", recorded=False)
    assert await count_executions(db) == before
    expect_error(await client.post(f"{base}/deny", json={"account_id": str(CAROL)}, headers=bob), 403, "permission_denied", recorded=True)

    guild_info.add_admin(GUILD, BOB)
    assert [entry["account_id"] for entry in (await client.get(base, headers=bob)).json()["items"]] == [str(CAROL)]
    denied = await client.post(f"{base}/deny", json={"account_id": str(CAROL)}, headers=bob)
    assert (denied.status_code, denied.json()["changed"]) == (200, True)
    assert (await client.get(base, headers=alice)).json()["items"] == []

    guild_info.remove_guild(GUILD)
    expect_error(await client.get(base, headers=alice), 409, "guild_unavailable", recorded=False)
    expect_error(await client.post(f"{base}/allow", json={"account_id": str(CAROL)}, headers=alice), 409, "guild_unavailable", recorded=True)
    expect_error(await client.get("/v1/guilds/0/discord-permissions", headers=alice), 422, "validation_error", recorded=False)


@pytest.mark.parametrize("listing", ["tokens", "notifications", "discord_permissions"])
async def test_private_lists_page(client: httpx.AsyncClient, db: AsyncEngine, guild_info: InMemoryGuildInfo, listing: str) -> None:
    guild_info.add_guild(GUILD, ALICE)
    for _ in range(4):
        issued = await issue_token(db, account_id=ALICE)
    headers = bearer(issued.token)
    others = (7311, 7312, 7313, 7314, 7315)
    if listing == "notifications":
        path, field, expected = "/v1/notifications", "allowed_user_id", [str(user_id) for user_id in others]
        for user_id in others:
            await notifications.allow_notifications(db, user_id=ALICE, allowed_user_id=user_id)
    elif listing == "discord_permissions":
        path, field, expected = f"/v1/guilds/{GUILD}/discord-permissions", "account_id", [str(account_id) for account_id in others]
        for account_id in others:
            await discord_permissions.allow_discord(db, guild_info, guild_id=GUILD, account_id=account_id, acting_user_id=ALICE)
    else:
        path, field = "/v1/tokens", "token_id"
        expected = [item["token_id"] for item in (await client.get(path, headers=headers)).json()["items"]]
        assert len(expected) == 4
    default = (await client.get(path, headers=headers)).json()
    assert (default["page"], default["per_page"], default["total"], default["total_is_lower_bound"]) == (1, getattr(core_paging, f"{listing.upper()}_PER_PAGE_DEFAULT"), len(expected), False)
    pages = [(await client.get(path, params={"page": page, "per_page": 2}, headers=headers)).json() for page in (1, 2, 3)]
    assert [item[field] for page in pages for item in page["items"]] == expected
    assert {page["total"] for page in pages} == {len(expected)}
    largest = getattr(core_paging, f"{listing.upper()}_PER_PAGE_MAX")
    assert (await client.get(path, params={"per_page": largest}, headers=headers)).status_code == 200
    for params in ({"per_page": largest + 1}, {"page": 0}, {"page": core_paging.MAX_WINDOW // largest + 1, "per_page": largest}):
        expect_error(await client.get(path, params=params, headers=headers), 422, "validation_error", recorded=False)
