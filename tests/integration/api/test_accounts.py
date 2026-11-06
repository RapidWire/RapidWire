from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api.app import create_app
from rapidwire.api.routers.accounts import DISCORD_UNAVAILABLE_CODE, DISCORD_UNAVAILABLE_MESSAGE
from rapidwire.config import Settings
from rapidwire.core import allowances, claims, paging, staking, transfers
from rapidwire.core.accounts import ensure_stake_custody_account
from rapidwire.core.ledger import LedgerTx, run_child, run_operation
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.tokens import issue_token
from rapidwire.core.types import ExecutionType, LabelBasis, Origin, TransferKind
from tests.integration.api.conftest import API_TEST_DISCORD_TOKEN, API_TEST_PASSWORD, DiscordStub
from tests.integration.factories import create_currency, create_pool, mint

T0 = datetime(2026, 1, 1, tzinfo=UTC)
CUR_A = 10
CUR_B = 20
CUR_GONE = 30
ALICE = 111
BOB = 222
CAROL = 333
NOBODY = 999
# IDs above 2**53 lose precision as JSON numbers, so they prove the string format.
BIG = 2**60
BIG_USER = 2**62 + 7
SECRET_KEY = "idem-key-31415"


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _envelope(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    error: dict[str, Any] = body["error"]
    return error


def _details(response: httpx.Response) -> list[list[str | int]]:
    return [detail["loc"] for detail in _envelope(response, 422, "validation_error")["details"]]


async def _legacy(engine: AsyncEngine, *, execution_id: int, type: str, initiator: int, currency_id: int, source: int, destination: int, amount: int, at: datetime) -> None:
    async with engine.begin() as conn:
        tx = LedgerTx(conn, at)
        await tx.require_accounts([initiator, destination])
        await tx.lock_currencies([currency_id])
        await tx.lock_balances([(source, currency_id), (destination, currency_id)])
        created = await tx.create_execution(
            type=type, initiator_id=initiator, input={"amount": str(amount)}, output={}, origin=Origin.LEGACY, label_basis=LabelBasis.ROW_SHAPE, execution_id=execution_id, created_at=at
        )
        await tx.move(created, currency_id, source, destination, amount, TransferKind.PRINCIPAL, transfer_id=execution_id, created_at=at)


async def _move(engine: AsyncEngine, *, type: str, initiator: int, currency_id: int, source: int, destination: int, amount: int, now: datetime, contract_function: str | None = None) -> int:
    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([currency_id])
        await tx.lock_balances([(source, currency_id), (destination, currency_id)])
        await tx.move(execution_id, currency_id, source, destination, amount, TransferKind.PRINCIPAL)
        return {}

    result = await run_operation(engine, type=type, initiator_id=initiator, input={"amount": amount}, body=body, now=now, contract_function=contract_function)
    return result.execution_id


@dataclass(frozen=True)
class History:
    legacy_mint: int
    legacy_transfer: int
    native_mint: int
    keyed_transfer: int
    call: int


async def _seed_history(db: AsyncEngine) -> History:
    a = await create_currency(db, currency_id=CUR_A, symbol="AAA", name="Alpha", now=T0)
    await create_currency(db, currency_id=CUR_B, symbol="BBB", name="Beta", now=T0)
    await _legacy(db, execution_id=BIG, type=ExecutionType.MINT, initiator=ALICE, currency_id=CUR_A, source=a.issuance_account_id, destination=ALICE, amount=1000, at=T0)
    await _legacy(db, execution_id=BIG + 1, type=ExecutionType.TRANSFER, initiator=ALICE, currency_id=CUR_A, source=ALICE, destination=BIG_USER, amount=100, at=T0 + timedelta(days=1))
    native_mint = await mint(db, CUR_B, BOB, 5000, now=T0 + timedelta(days=2))
    keyed = await transfers.transfer(db, sender_id=BOB, recipient_id=ALICE, currency_id=CUR_B, amount=1500, now=T0 + timedelta(days=3), idempotency_key=SECRET_KEY)
    call = await _move(db, type=ExecutionType.CONTRACT_CALL, initiator=ALICE, currency_id=CUR_A, source=ALICE, destination=BOB, amount=200, now=T0 + timedelta(days=4), contract_function="buy")
    return History(legacy_mint=BIG, legacy_transfer=BIG + 1, native_mint=native_mint, keyed_transfer=keyed.execution_id, call=call)


async def _history_ids(client: httpx.AsyncClient, path: str = "/v1/history", **params: str) -> list[str]:
    response = await client.get(path, params=params)
    assert response.status_code == 200, response.text
    return [item["execution_id"] for item in response.json()["items"]]


async def test_user_name_is_looked_up_once_and_cached(client: httpx.AsyncClient, discord: DiscordStub) -> None:
    discord.users[BIG_USER] = "big-user"
    for _ in range(3):
        response = await client.get(f"/v1/users/{BIG_USER}/name")
        assert response.status_code == 200, response.text
        assert response.json() == {"account_id": str(BIG_USER), "username": "big-user"}
    [request] = discord.requests
    assert request.url.path == f"/api/v10/users/{BIG_USER}"
    assert request.headers["authorization"] == f"Bot {API_TEST_DISCORD_TOKEN}"
    assert request.headers["user-agent"].startswith("DiscordBot (")


async def test_unknown_discord_user_is_404(client: httpx.AsyncClient, discord: DiscordStub) -> None:
    error = _envelope(await client.get(f"/v1/users/{ALICE}/name"), 404, "user_not_found")
    assert error["message"] == "The user was not found on Discord."
    assert len(discord.requests) == 1


@pytest.mark.parametrize("user_id", ["0", "-5", "abc", "01", str(2**63), "1.5"])
async def test_invalid_user_ids_are_rejected_before_discord(client: httpx.AsyncClient, discord: DiscordStub, user_id: str) -> None:
    assert _details(await client.get(f"/v1/users/{user_id}/name")) == [["path", "account_id"]]
    assert discord.requests == []


async def test_rate_limited_discord_is_503_and_pauses_lookups(client: httpx.AsyncClient, discord: DiscordStub) -> None:
    discord.handler = lambda request: httpx.Response(429, json={"message": "You are being rate limited.", "retry_after": 11.2, "global": False})
    response = await client.get(f"/v1/users/{ALICE}/name")
    error = _envelope(response, 503, DISCORD_UNAVAILABLE_CODE)
    assert error == {"code": DISCORD_UNAVAILABLE_CODE, "message": DISCORD_UNAVAILABLE_MESSAGE}
    assert 1 <= int(response.headers["retry-after"]) <= 12
    assert "rate limited" not in response.text
    _envelope(await client.get(f"/v1/users/{BOB}/name"), 503, DISCORD_UNAVAILABLE_CODE)
    assert len(discord.requests) == 1


async def test_discord_failures_are_503_without_detail(client: httpx.AsyncClient, discord: DiscordStub) -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("leaky-upstream-detail")

    discord.handler = broken
    response = await client.get(f"/v1/users/{ALICE}/name")
    _envelope(response, 503, DISCORD_UNAVAILABLE_CODE)
    assert "leaky" not in response.text
    assert "retry-after" not in response.headers
    discord.handler = lambda request: httpx.Response(500, text="leaky-server-detail")
    response = await client.get(f"/v1/users/{ALICE}/name")
    _envelope(response, 503, DISCORD_UNAVAILABLE_CODE)
    assert "leaky" not in response.text


async def test_cached_name_survives_a_discord_outage(client: httpx.AsyncClient, discord: DiscordStub) -> None:
    discord.users[ALICE] = "alice"
    assert (await client.get(f"/v1/users/{ALICE}/name")).json()["username"] == "alice"
    discord.handler = lambda request: httpx.Response(500)
    assert (await client.get(f"/v1/users/{ALICE}/name")).json()["username"] == "alice"
    assert len(discord.requests) == 1


async def test_user_name_without_a_discord_token_is_503(db: AsyncEngine) -> None:
    stub = DiscordStub()
    stub.users[ALICE] = "alice"
    app = create_app(Settings(_env_file=None, db_password=API_TEST_PASSWORD), engine=db, guild_info=InMemoryGuildInfo(), discord_transport=httpx.MockTransport(stub))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        _envelope(await http.get(f"/v1/users/{ALICE}/name"), 503, DISCORD_UNAVAILABLE_CODE)
    assert stub.requests == []


async def test_user_stats(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    await _seed_history(db)
    stats = (await client.get(f"/v1/users/{ALICE}/stats")).json()
    assert stats == {
        "account_id": str(ALICE),
        "total_transfers": 4,
        "first_transfer_at": _iso(T0),
        "last_transfer_at": _iso(T0 + timedelta(days=4)),
    }
    empty = (await client.get(f"/v1/users/{NOBODY}/stats")).json()
    assert empty == {"account_id": str(NOBODY), "total_transfers": 0, "first_transfer_at": None, "last_transfer_at": None}
    big = (await client.get(f"/v1/users/{BIG_USER}/stats")).json()
    assert (big["account_id"], big["total_transfers"]) == (str(BIG_USER), 1)
    assert _details(await client.get("/v1/users/x/stats")) == [["path", "account_id"]]


async def test_account_kinds(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    await _seed_history(db)
    async with db.begin() as conn:
        await ensure_stake_custody_account(conn, now=T0)
    created = await create_pool(db, user_id=CAROL, currency_x=CUR_A, currency_y=CUR_B, amount_x=10_000, amount_y=20_000, now=T0)
    issuance = (await client.get(f"/v1/currencies/{CUR_A}")).json()["issuance_account_id"]
    expected = {
        str(ALICE): {"kind": "user", "currency_id": None, "pool_id": None},
        str(BIG_USER): {"kind": "user", "currency_id": None, "pool_id": None},
        issuance: {"kind": "issuance", "currency_id": str(CUR_A), "pool_id": None},
        "-1": {"kind": "stake_custody", "currency_id": None, "pool_id": None},
        str(created.custody_account_id): {"kind": "pool_custody", "currency_id": None, "pool_id": str(created.pool_id)},
    }
    for account_id, fields in expected.items():
        response = await client.get(f"/v1/accounts/{account_id}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert set(body) == {"account_id", "kind", "currency_id", "pool_id", "created_at"}
        assert {key: body[key] for key in ("kind", "currency_id", "pool_id")} == fields, account_id
        assert body["account_id"] == account_id
        assert body["created_at"].endswith("Z")
    assert (await client.get(f"/v1/accounts/{ALICE}")).json()["created_at"] == _iso(T0)
    error = _envelope(await client.get(f"/v1/accounts/{NOBODY}"), 404, "account_not_found")
    assert error["message"] == "The account was not found."
    _envelope(await client.get("/v1/accounts/-999999"), 404, "account_not_found")


@pytest.mark.parametrize("account_id", ["abc", "01", "1.5", "-0", str(2**63), str(-(2**63) - 1)])
async def test_account_kind_rejects_malformed_ids(client: httpx.AsyncClient, account_id: str) -> None:
    assert _details(await client.get(f"/v1/accounts/{account_id}")) == [["path", "account_id"]]


async def test_balances(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    await _seed_history(db)
    await create_currency(db, currency_id=CUR_GONE, symbol="GONE", name="Gone", now=T0, deleted_at=T0)
    listed = (await client.get(f"/v1/accounts/{ALICE}/balances")).json()
    assert listed == {
        "items": [
            {"currency_id": str(CUR_A), "symbol": "AAA", "name": "Alpha", "amount": "0.700"},
            {"currency_id": str(CUR_B), "symbol": "BBB", "name": "Beta", "amount": "1.500"},
        ],
        "page": 1,
        "per_page": paging.BALANCES_PER_PAGE_DEFAULT,
        "total": 2,
        "total_is_lower_bound": False,
    }
    one = (await client.get(f"/v1/accounts/{BIG_USER}/balances/{CUR_A}")).json()
    assert one == {"account_id": str(BIG_USER), "currency_id": str(CUR_A), "symbol": "AAA", "name": "Alpha", "amount": "0.100", "currency_deleted": False}
    zero = (await client.get(f"/v1/accounts/{NOBODY}/balances/{CUR_B}")).json()
    assert zero["amount"] == "0.000"
    gone = (await client.get(f"/v1/accounts/{ALICE}/balances/{CUR_GONE}")).json()
    assert (gone["amount"], gone["currency_deleted"]) == ("0.000", True)
    issuance = (await client.get(f"/v1/currencies/{CUR_A}")).json()["issuance_account_id"]
    assert (await client.get(f"/v1/accounts/{issuance}/balances/{CUR_A}")).json()["amount"] == "-1.000"
    _envelope(await client.get(f"/v1/accounts/{ALICE}/balances/12345"), 404, "currency_not_found")
    assert _details(await client.get(f"/v1/accounts/{ALICE}/balances/0")) == [["path", "currency_id"]]
    assert (await client.get(f"/v1/accounts/{NOBODY}/balances")).json()["items"] == []


async def test_balance_pages(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    for index in range(23):
        currency_id = 100 + index
        await create_currency(db, currency_id=currency_id, symbol="C" + chr(ord("A") + index), now=T0)
        await mint(db, currency_id, ALICE, index + 1, now=T0)
    seen: list[str] = []
    for page in (1, 2, 3):
        body = (await client.get(f"/v1/accounts/{ALICE}/balances", params={"page": page, "per_page": 10})).json()
        assert (body["page"], body["per_page"], body["total"], body["total_is_lower_bound"]) == (page, 10, 23, False)
        seen += [item["currency_id"] for item in body["items"]]
    assert seen == [str(100 + index) for index in range(23)]
    assert (await client.get(f"/v1/accounts/{ALICE}/balances", params={"page": 4, "per_page": 10})).json()["items"] == []
    full = (await client.get(f"/v1/accounts/{ALICE}/balances", params={"per_page": paging.BALANCES_PER_PAGE_MAX})).json()
    assert len(full["items"]) == 23
    assert _details(await client.get(f"/v1/accounts/{ALICE}/balances", params={"per_page": paging.BALANCES_PER_PAGE_MAX + 1})) == [["query", "per_page"]]
    assert _details(await client.get(f"/v1/accounts/{ALICE}/balances", params={"page": 1001, "per_page": 10})) == [["query", "page"]]
    assert (await client.get(f"/v1/accounts/{ALICE}/balances", params={"page": 1000, "per_page": 10})).status_code == 200
    assert _details(await client.get("/v1/history", params={"per_page": paging.HISTORY_PER_PAGE_MAX + 1})) == [["query", "per_page"]]
    assert _details(await client.get("/v1/executions", params={"page": 501, "per_page": 20})) == [["query", "page"]]


async def test_stakes(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR_A, symbol="AAA", name="Alpha", hourly_rate_ppm=10_000, now=T0)
    await create_currency(db, currency_id=CUR_B, symbol="BBB", name="Beta", now=T0)
    started = datetime.now(UTC) - timedelta(hours=2)
    for currency_id in (CUR_A, CUR_B):
        await mint(db, currency_id, BIG_USER, 2_000_000, now=started)
        await staking.deposit(db, account_id=BIG_USER, currency_id=currency_id, amount=1_000_000, now=started)
    listed = (await client.get(f"/v1/accounts/{BIG_USER}/stakes")).json()
    assert (listed["total"], [item["currency_id"] for item in listed["items"]]) == (2, [str(CUR_A), str(CUR_B)])
    stake_a, stake_b = listed["items"]
    assert stake_a["account_id"] == str(BIG_USER)
    assert (stake_a["symbol"], stake_a["name"], stake_a["amount"], stake_a["hourly_rate_ppm"], stake_a["interest_halted"]) == ("AAA", "Alpha", "1000.000", "10000", False)
    assert stake_a["last_accrued_at"].startswith(started.strftime("%Y-%m-%dT%H:%M:%S"))
    # Two whole hours at 1% per hour give 1020.100; the part of an hour since then adds less than one more percent.
    projected = int(stake_a["projected_amount"].replace(".", ""))
    assert 1_020_100 <= projected < 1_030_301
    assert (stake_b["amount"], stake_b["projected_amount"], stake_b["hourly_rate_ppm"]) == ("1000.000", "1000.000", "0")
    one = (await client.get(f"/v1/accounts/{BIG_USER}/stakes/{CUR_A}")).json()
    assert set(one) == set(stake_a)
    assert one["amount"] == "1000.000"
    assert int(one["projected_amount"].replace(".", "")) >= projected
    _envelope(await client.get(f"/v1/accounts/{ALICE}/stakes/{CUR_A}"), 404, "stake_not_found")
    assert (await client.get(f"/v1/accounts/{ALICE}/stakes")).json()["items"] == []


async def test_history_search_filters(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    seed = await _seed_history(db)
    everything = (await client.get("/v1/history")).json()
    assert everything["total"] == 5
    assert [item["execution_id"] for item in everything["items"]] == [str(seed.call), str(seed.keyed_transfer), str(seed.native_mint), str(BIG + 1), str(BIG)]
    legacy = everything["items"][3]
    assert legacy == {
        "transfer_id": str(BIG + 1),
        "execution_id": str(BIG + 1),
        "currency_id": str(CUR_A),
        "from_account": str(ALICE),
        "to_account": str(BIG_USER),
        "amount": "0.100",
        "kind": "principal",
        "created_at": _iso(T0 + timedelta(days=1)),
        "currency_symbol": "AAA",
        "execution_type": "transfer",
        "execution_status": "success",
        "origin": "legacy",
        "label_basis": "row_shape",
        "initiator_id": str(ALICE),
        "parent_execution_id": None,
        "parent_origin": None,
        "parent_label_basis": None,
        "contract_function": None,
    }
    assert everything["items"][0]["origin"] == "native"
    assert everything["items"][0]["label_basis"] is None
    assert everything["items"][0]["contract_function"] == "buy"
    call, keyed, native, legacy_transfer, legacy_mint = (str(seed.call), str(seed.keyed_transfer), str(seed.native_mint), str(BIG + 1), str(BIG))
    assert await _history_ids(client, user_id=str(BIG_USER)) == [legacy_transfer]
    assert await _history_ids(client, user_id=str(BOB)) == [call, keyed, native]
    assert await _history_ids(client, source_id=str(ALICE)) == [call, legacy_transfer]
    assert await _history_ids(client, destination_id=str(ALICE)) == [keyed, legacy_mint]
    assert await _history_ids(client, source_id=str(ALICE), destination_id=str(BOB)) == [call]
    assert await _history_ids(client, currency_id=str(CUR_B)) == [keyed, native]
    assert await _history_ids(client, transfer_id=str(BIG)) == [legacy_mint]
    assert await _history_ids(client, start=_iso(T0 + timedelta(days=1)), end=_iso(T0 + timedelta(days=3))) == [native, legacy_transfer]
    assert await _history_ids(client, start="2026-01-02T09:00:00+09:00", end="2026-01-04T09:00:00+09:00") == [native, legacy_transfer]
    assert await _history_ids(client, start="0001-01-01T00:00:00Z", end="9999-12-31T23:59:59Z") == [call, keyed, native, legacy_transfer, legacy_mint]
    assert await _history_ids(client, min_amount="0.2", max_amount="1.5") == [call, keyed, legacy_mint]
    assert await _history_ids(client, min_amount="1.001") == [keyed, native]
    assert await _history_ids(client, max_amount="0") == []
    assert await _history_ids(client, execution_type="transfer") == [keyed, legacy_transfer]
    assert await _history_ids(client, contract_function="buy") == [call]
    assert await _history_ids(client, contract_function="sell") == []
    assert await _history_ids(client, sort_by="amount", sort_order="asc") == [legacy_transfer, call, legacy_mint, keyed, native]
    assert await _history_ids(client, sort_by="timestamp", sort_order="asc") == [legacy_mint, legacy_transfer, native, keyed, call]
    paged = (await client.get("/v1/history", params={"per_page": 2, "page": 2})).json()
    assert ([item["execution_id"] for item in paged["items"]], paged["total"]) == ([native, legacy_transfer], 5)


@pytest.mark.parametrize(
    ("params", "loc"),
    [
        ({"min_amount": "1.2345"}, ["query", "min_amount"]),
        ({"min_amount": "-1"}, ["query", "min_amount"]),
        ({"max_amount": "1e3"}, ["query", "max_amount"]),
        ({"max_amount": " 1"}, ["query", "max_amount"]),
        ({"max_amount": "01"}, ["query", "max_amount"]),
        ({"min_amount": "1" * 50}, ["query", "min_amount"]),
        ({"start": "2026-01-01T00:00:00"}, ["query", "start"]),
        ({"end": "yesterday"}, ["query", "end"]),
        ({"start": "0001-01-01T00:00:00+01:00"}, ["query", "start"]),
        ({"user_id": "1.0"}, ["query", "user_id"]),
        ({"source_id": "99999999999999999999"}, ["query", "source_id"]),
        ({"transfer_id": "0"}, ["query", "transfer_id"]),
        ({"currency_id": "-3"}, ["query", "currency_id"]),
        ({"sort_by": "random"}, ["query", "sort_by"]),
        ({"sort_order": "ASC"}, ["query", "sort_order"]),
        ({"execution_type": ""}, ["query", "execution_type"]),
        ({"contract_function": "f" * 200}, ["query", "contract_function"]),
    ],
)
async def test_history_rejects_bad_filters(client: httpx.AsyncClient, params: dict[str, str], loc: list[str]) -> None:
    response = await client.get("/v1/history", params=params)
    assert _details(response) == [loc]
    for value in params.values():
        if len(value) > 3:
            assert value not in response.text


async def test_account_history(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    seed = await _seed_history(db)
    assert await _history_ids(client, f"/v1/accounts/{ALICE}/history") == [str(seed.call), str(seed.keyed_transfer), str(BIG + 1), str(BIG)]
    assert await _history_ids(client, f"/v1/accounts/{ALICE}/history", currency_id=str(CUR_A), sort_order="asc") == [str(BIG), str(BIG + 1), str(seed.call)]
    assert await _history_ids(client, f"/v1/accounts/{ALICE}/history", end=_iso(T0 + timedelta(days=1))) == [str(BIG)]
    assert await _history_ids(client, f"/v1/accounts/{ALICE}/history", min_amount="0.5") == [str(seed.keyed_transfer), str(BIG)]
    body = (await client.get(f"/v1/accounts/{ALICE}/history", params={"per_page": 1})).json()
    assert (body["total"], body["per_page"], len(body["items"])) == (4, 1, 1)
    assert await _history_ids(client, f"/v1/accounts/{NOBODY}/history") == []
    assert _details(await client.get(f"/v1/accounts/{ALICE}/history", params={"max_amount": "0.0001"})) == [["query", "max_amount"]]


async def test_execution_detail_children_and_transfers(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR_A, symbol="AAA", now=T0)
    await mint(db, CUR_A, ALICE, 1000, now=T0)
    ids: dict[str, int] = {}

    async def body(tx: LedgerTx, execution_id: int) -> dict[str, Any]:
        await tx.lock_currencies([CUR_A])
        await tx.lock_balances([(ALICE, CUR_A), (BOB, CUR_A)])
        await tx.move(execution_id, CUR_A, ALICE, BOB, 10, TransferKind.PRINCIPAL)

        async def child_body(ctx: LedgerTx, child_id: int) -> dict[str, Any]:
            await ctx.move(child_id, CUR_A, ALICE, BOB, 20, TransferKind.PRINCIPAL)

            async def grandchild_body(gtx: LedgerTx, grandchild_id: int) -> dict[str, Any]:
                await gtx.move(grandchild_id, CUR_A, ALICE, BOB, 30, TransferKind.PRINCIPAL)
                return {}

            ids["grandchild"] = (await run_child(ctx, parent_execution_id=child_id, type=ExecutionType.TRANSFER, initiator_id=CAROL, input={}, body=grandchild_body)).execution_id
            return {"moved": "20"}

        ids["child"] = (await run_child(tx, parent_execution_id=execution_id, type=ExecutionType.TRANSFER, initiator_id=CAROL, input={"x": "1"}, body=child_body, contract_function="pay")).execution_id
        return {"value": "12500", "gas_used": "7"}

    at = T0 + timedelta(hours=1)
    top = await run_operation(db, type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={"amount": "12500"}, body=body, now=at, contract_function="run", user_accounts=[BOB, CAROL])
    detail = (await client.get(f"/v1/executions/{top.execution_id}")).json()
    nested = ("created_at", "transfers", "child_execution_ids", "deliveries", "children")
    assert {key: value for key, value in detail.items() if key not in nested} == {
        "execution_id": str(top.execution_id),
        "type": "contract_call",
        "origin": "native",
        "label_basis": None,
        "initiator_id": str(ALICE),
        "parent_execution_id": None,
        "parent_origin": None,
        "parent_label_basis": None,
        "contract_id": None,
        "status": "success",
        "input": {"amount": "12500"},
        "output": {"value": "12500", "gas_used": "7"},
        "has_idempotency_key": False,
        "contract_function": "run",
    }
    assert detail["created_at"] == _iso(at)
    assert [(item["amount"], item["from_account"], item["to_account"], item["execution_id"]) for item in detail["transfers"]] == [("0.010", str(ALICE), str(BOB), str(top.execution_id))]
    assert detail["child_execution_ids"] == [str(ids["child"])]
    assert detail["deliveries"] == []
    [child] = detail["children"]
    assert (child["execution_id"], child["parent_execution_id"], child["contract_function"], child["output"]) == (str(ids["child"]), str(top.execution_id), "pay", {"moved": "20"})
    assert (child["parent_origin"], child["parent_label_basis"]) == ("native", None)
    assert [item["amount"] for item in child["transfers"]] == ["0.020"]
    assert child["child_execution_ids"] == [str(ids["grandchild"])]
    assert "children" not in child
    grandchild = (await client.get(f"/v1/executions/{ids['grandchild']}")).json()
    assert (grandchild["parent_execution_id"], grandchild["children"], [item["amount"] for item in grandchild["transfers"]]) == (str(ids["child"]), [], ["0.030"])
    _envelope(await client.get("/v1/executions/987654321"), 404, "execution_not_found")
    assert _details(await client.get("/v1/executions/abc")) == [["path", "execution_id"]]


async def test_legacy_executions_and_big_ids(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    await _seed_history(db)
    detail = (await client.get(f"/v1/executions/{BIG + 1}")).json()
    assert (detail["execution_id"], detail["origin"], detail["label_basis"], detail["input"]) == (str(BIG + 1), "legacy", "row_shape", {"amount": "100"})
    assert detail["transfers"][0]["transfer_id"] == str(BIG + 1)
    assert detail["transfers"][0]["to_account"] == str(BIG_USER)
    transfer = (await client.get(f"/v1/transfers/{BIG + 1}")).json()
    assert (transfer["transfer_id"], transfer["to_account"], transfer["origin"]) == (str(BIG + 1), str(BIG_USER), "legacy")
    assert f'"{BIG_USER}"' in (await client.get(f"/v1/transfers/{BIG + 1}")).text


async def test_transfer(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    seed = await _seed_history(db)
    native = (await client.get("/v1/history", params={"transfer_id": str(BIG + 3)})).json()["items"]
    assert [item["execution_id"] for item in native] == [str(seed.keyed_transfer)]
    transfer = (await client.get(f"/v1/transfers/{BIG + 3}")).json()
    assert transfer == native[0]
    assert (transfer["amount"], transfer["currency_symbol"], transfer["execution_type"]) == ("1.500", "BBB", "transfer")
    _envelope(await client.get("/v1/transfers/5"), 404, "transfer_not_found")
    assert _details(await client.get("/v1/transfers/-5")) == [["path", "transfer_id"]]


async def test_list_executions_filters_and_redaction(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    seed = await _seed_history(db)
    issued = await issue_token(db, account_id=CAROL)
    newest = (await client.get("/v1/executions")).json()
    assert newest["total"] == 6
    assert [item["execution_id"] for item in newest["items"]] == [str(BIG + 5), str(seed.call), str(seed.keyed_transfer), str(seed.native_mint), str(BIG + 1), str(BIG)]
    assert newest["items"][0]["execution_id"] == str(issued.execution_id)
    private = newest["items"][0]
    assert (private["type"], private["input"], private["output"]) == ("token_issue", {}, None)
    detail = (await client.get(f"/v1/executions/{issued.execution_id}")).json()
    assert (detail["input"], detail["output"]) == ({}, None)
    assert issued.token not in (await client.get(f"/v1/executions/{issued.execution_id}")).text
    keyed = await client.get(f"/v1/executions/{seed.keyed_transfer}")
    assert keyed.json()["has_idempotency_key"] is True
    assert SECRET_KEY not in keyed.text
    assert "idempotency_key" not in keyed.json()
    assert [item["execution_id"] for item in (await client.get("/v1/executions", params={"initiator_id": str(BOB)})).json()["items"]] == [str(seed.keyed_transfer), str(seed.native_mint)]
    by_type = (await client.get("/v1/executions", params={"execution_type": "mint"})).json()["items"]
    assert [item["execution_id"] for item in by_type] == [str(seed.native_mint), str(BIG)]
    assert (await client.get("/v1/executions", params={"status": "failed"})).json()["total"] == 0
    assert (await client.get("/v1/executions", params={"status": "success", "per_page": 2})).json()["total"] == 6
    assert _details(await client.get("/v1/executions", params={"status": "done"})) == [["query", "status"]]
    assert _details(await client.get("/v1/executions", params={"initiator_id": "x"})) == [["query", "initiator_id"]]
    assert _details(await client.get("/v1/executions", params={"contract_id": "-1"})) == [["query", "contract_id"]]


async def test_list_executions_by_contract(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    async with db.begin() as conn:
        tx = LedgerTx(conn, T0)
        await tx.require_accounts([ALICE, BOB])
        call = await tx.create_execution(type=ExecutionType.CONTRACT_CALL, initiator_id=ALICE, input={"contract_id": BOB, "function": "f"}, contract_function="f")
        paused = await tx.create_execution(type=ExecutionType.CONTRACT_PAUSE, initiator_id=BOB, input={"account_id": BOB})
        await tx.create_execution(type=ExecutionType.TRANSFER, initiator_id=ALICE, input={})
    everything = (await client.get("/v1/executions", params={"contract_id": str(BOB)})).json()
    assert [(item["execution_id"], item["contract_id"]) for item in everything["items"]] == [(str(paused), str(BOB)), (str(call), str(BOB))]
    calls = (await client.get("/v1/executions", params={"contract_id": str(BOB), "execution_type": "contract_call"})).json()
    assert [item["execution_id"] for item in calls["items"]] == [str(call)]


async def test_claims(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR_A, symbol="AAA", now=T0)
    await mint(db, CUR_A, BOB, 10_000, now=T0)
    first = await claims.create_claim(db, claimant_id=ALICE, payer_id=BOB, currency_id=CUR_A, amount=1250, description="lunch", now=T0)
    second = await claims.create_claim(db, claimant_id=BOB, payer_id=ALICE, currency_id=CUR_A, amount=5, now=T0)
    third = await claims.create_claim(db, claimant_id=ALICE, payer_id=CAROL, currency_id=CUR_A, amount=7, now=T0)
    paid = await claims.pay_claim(db, claim_id=first.claim_id, payer_id=BOB, now=T0 + timedelta(hours=1))
    claim = (await client.get(f"/v1/claims/{first.claim_id}")).json()
    assert claim == {
        "claim_id": str(first.claim_id),
        "claimant_id": str(ALICE),
        "payer_id": str(BOB),
        "currency_id": str(CUR_A),
        "amount": "1.250",
        "description": "lunch",
        "status": "paid",
        "created_at": _iso(T0),
        "updated_at": _iso(T0 + timedelta(hours=1)),
        "created_execution_id": str(first.execution_id),
        "created_execution_origin": "native",
        "created_execution_label_basis": None,
        "resolved_execution_id": str(paid.execution_id),
        "resolved_execution_origin": "native",
        "resolved_execution_label_basis": None,
    }
    as_claimant = (await client.get(f"/v1/accounts/{ALICE}/claims", params={"role": "claimant"})).json()
    assert [item["claim_id"] for item in as_claimant["items"]] == [str(third.claim_id), str(first.claim_id)]
    as_payer = (await client.get(f"/v1/accounts/{ALICE}/claims", params={"role": "payer", "status": "pending"})).json()
    assert ([item["claim_id"] for item in as_payer["items"]], as_payer["items"][0]["resolved_execution_origin"]) == ([str(second.claim_id)], None)
    assert _details(await client.get(f"/v1/accounts/{ALICE}/claims", params={"role": "owner"})) == [["query", "role"]]
    listed = (await client.get(f"/v1/accounts/{ALICE}/claims")).json()
    assert (listed["total"], [item["claim_id"] for item in listed["items"]]) == (3, [str(third.claim_id), str(second.claim_id), str(first.claim_id)])
    pending = (await client.get(f"/v1/accounts/{ALICE}/claims", params={"status": "pending"})).json()
    assert [item["claim_id"] for item in pending["items"]] == [str(third.claim_id), str(second.claim_id)]
    assert pending["items"][1]["description"] is None
    assert (await client.get(f"/v1/accounts/{CAROL}/claims")).json()["total"] == 1
    paged = (await client.get(f"/v1/accounts/{ALICE}/claims", params={"per_page": 1, "page": 3})).json()
    assert [item["claim_id"] for item in paged["items"]] == [str(first.claim_id)]
    _envelope(await client.get("/v1/claims/424242"), 404, "claim_not_found")
    assert _details(await client.get("/v1/claims/0")) == [["path", "claim_id"]]
    assert _details(await client.get(f"/v1/accounts/{ALICE}/claims", params={"status": "open"})) == [["query", "status"]]


async def test_allowances(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    await create_currency(db, currency_id=CUR_A, symbol="AAA", now=T0)
    await create_currency(db, currency_id=CUR_B, symbol="BBB", now=T0)
    first = await allowances.approve(db, owner_id=ALICE, spender_id=BIG_USER, currency_id=CUR_A, amount=5000, now=T0)
    second = await allowances.approve(db, owner_id=ALICE, spender_id=BIG_USER, currency_id=CUR_A, amount=1500, now=T0 + timedelta(minutes=1))
    other = await allowances.approve(db, owner_id=ALICE, spender_id=BOB, currency_id=CUR_B, amount=1, now=T0 + timedelta(minutes=2))
    await allowances.approve(db, owner_id=BOB, spender_id=ALICE, currency_id=CUR_B, amount=9, now=T0)
    current = (await client.get(f"/v1/allowances/{ALICE}/{BIG_USER}/{CUR_A}")).json()
    assert current == {"owner_id": str(ALICE), "spender_id": str(BIG_USER), "currency_id": str(CUR_A), "symbol": "AAA", "amount": "1.500"}
    assert (await client.get(f"/v1/allowances/{BIG_USER}/{ALICE}/{CUR_A}")).json()["amount"] == "0.000"
    _envelope(await client.get(f"/v1/allowances/{ALICE}/{BOB}/777"), 404, "currency_not_found")
    assert _details(await client.get(f"/v1/allowances/{ALICE}/x/{CUR_A}")) == [["path", "spender_id"]]
    history = (await client.get(f"/v1/accounts/{ALICE}/allowance-history")).json()
    assert history["total"] == 3
    assert [item["execution_id"] for item in history["items"]] == [str(other.execution_id), str(second.execution_id), str(first.execution_id)]
    change = history["items"][1]
    assert {key: change[key] for key in ("owner_id", "spender_id", "currency_id", "old_amount", "new_amount", "created_at")} == {
        "owner_id": str(ALICE),
        "spender_id": str(BIG_USER),
        "currency_id": str(CUR_A),
        "old_amount": "5.000",
        "new_amount": "1.500",
        "created_at": _iso(T0 + timedelta(minutes=1)),
    }
    assert isinstance(change["allowance_history_id"], str)
    by_spender = (await client.get(f"/v1/accounts/{ALICE}/allowance-history", params={"spender_id": str(BIG_USER)})).json()
    assert [item["execution_id"] for item in by_spender["items"]] == [str(second.execution_id), str(first.execution_id)]
    by_currency = (await client.get(f"/v1/accounts/{ALICE}/allowance-history", params={"currency_id": str(CUR_B)})).json()
    assert [item["execution_id"] for item in by_currency["items"]] == [str(other.execution_id)]
    assert (await client.get(f"/v1/accounts/{BOB}/allowance-history")).json()["total"] == 1
    assert _details(await client.get(f"/v1/accounts/{ALICE}/allowance-history", params={"per_page": 101})) == [["query", "per_page"]]


async def test_currencies(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    requested = T0 + timedelta(days=1)
    now = datetime.now(UTC)
    await create_currency(
        db,
        currency_id=BIG,
        symbol="AAA",
        name="Alpha",
        created_by=ALICE,
        hourly_rate_ppm=1234,
        now=T0,
        pending_rate_ppm=99,
        rate_change_requested_at=requested,
        delete_requested_at=now - timedelta(days=1),
        delegation_enabled=True,
        minting_renounced=True,
    )
    await create_currency(db, currency_id=CUR_B, symbol="BBB", name="Beta", now=T0, delete_requested_at=now - timedelta(days=30))
    await create_currency(db, currency_id=CUR_GONE, symbol="GONE", name="Gone", now=T0, deleted_at=T0 + timedelta(days=2))
    await mint(db, BIG, BOB, 2500, now=T0)
    listed = (await client.get("/v1/currencies")).json()
    assert listed == {
        "items": [
            {"currency_id": str(BIG), "symbol": "AAA", "name": "Alpha", "supply": "2.500", "deleted": False},
            {"currency_id": str(CUR_B), "symbol": "BBB", "name": "Beta", "supply": "0.000", "deleted": False},
        ]
    }
    with_deleted = (await client.get("/v1/currencies", params={"include_deleted": "true"})).json()
    assert [item["symbol"] for item in with_deleted["items"]] == ["AAA", "BBB", "GONE"]
    assert with_deleted["items"][2]["deleted"] is True
    info = (await client.get(f"/v1/currencies/{BIG}")).json()
    delete_at = info["delete_requested_at"]
    assert delete_at is not None
    assert info == {
        "currency_id": str(BIG),
        "name": "Alpha",
        "symbol": "AAA",
        "issuance_account_id": info["issuance_account_id"],
        "created_by": str(ALICE),
        "created_at": _iso(T0),
        "supply": "2.500",
        "hourly_rate_ppm": "1234",
        "pending_rate_ppm": "99",
        "rate_change_requested_at": _iso(requested),
        "rate_change_apply_after": _iso(requested + timedelta(days=7)),
        "minting_renounced": True,
        "delete_requested_at": delete_at,
        "delete_finalize_from": info["delete_finalize_from"],
        "delete_finalize_until": info["delete_finalize_until"],
        "delegation_enabled": True,
        "interest_halted": False,
        "deleted_at": None,
    }
    assert int(info["issuance_account_id"]) < 0
    requested_at = datetime.fromisoformat(delete_at)
    assert datetime.fromisoformat(info["delete_finalize_from"]) - requested_at == timedelta(days=7)
    assert datetime.fromisoformat(info["delete_finalize_until"]) - requested_at == timedelta(days=10)
    lapsed = (await client.get(f"/v1/currencies/{CUR_B}")).json()
    assert (lapsed["delete_requested_at"], lapsed["delete_finalize_from"], lapsed["pending_rate_ppm"], lapsed["rate_change_apply_after"]) == (None, None, None, None)
    assert (await client.get("/v1/currencies/symbol/AAA")).json() == info
    assert (await client.get("/v1/currencies/symbol/aaa")).json()["currency_id"] == str(BIG)
    gone = (await client.get(f"/v1/currencies/{CUR_GONE}")).json()
    assert gone["deleted_at"] == _iso(T0 + timedelta(days=2))
    _envelope(await client.get("/v1/currencies/symbol/GONE"), 404, "currency_not_found")
    _envelope(await client.get("/v1/currencies/symbol/NOPE"), 404, "currency_not_found")
    _envelope(await client.get("/v1/currencies/55"), 404, "currency_not_found")
    assert _details(await client.get("/v1/currencies/abc")) == [["path", "currency_id"]]
    assert _details(await client.get("/v1/currencies/symbol/" + "A" * 50)) == [["path", "symbol"]]


async def test_reads_need_no_token(client: httpx.AsyncClient, db: AsyncEngine) -> None:
    await _seed_history(db)
    for path in ("/v1/history", f"/v1/accounts/{ALICE}", f"/v1/accounts/{ALICE}/balances", "/v1/currencies", f"/v1/executions/{BIG}", f"/v1/accounts/{ALICE}/claims"):
        response = await client.get(path, headers={"Authorization": "Bearer not-a-real-token"})
        assert response.status_code == 200, path


async def test_openapi_describes_the_reads(client: httpx.AsyncClient) -> None:
    document = (await client.get("/openapi.json")).json()
    schemas = document["components"]["schemas"]
    assert "has_idempotency_key" in schemas["ExecutionOut"]["properties"]
    assert schemas["ExecutionOut"]["properties"]["input"]["description"].startswith("The request as stored")
    assert schemas["TransferEntryOut"]["properties"]["amount"]["pattern"] == r"^-?(0|[1-9][0-9]*)\.[0-9]{3}$"
    name = document["paths"]["/v1/users/{account_id}/name"]["get"]
    assert {"200", "404", "422", "503"} <= set(name["responses"])
    for path, item in document["paths"].items():
        if path.startswith(("/v1/accounts/", "/v1/history", "/v1/executions", "/v1/currencies", "/v1/claims/", "/v1/allowances/", "/v1/transfers/", "/v1/users/")) and "get" in item:
            assert "security" not in item["get"], path
