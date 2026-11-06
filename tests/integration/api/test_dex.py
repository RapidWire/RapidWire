from __future__ import annotations

import itertools
import threading
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api.formats import format_amount
from rapidwire.config import Settings
from rapidwire.core import currency as cur
from rapidwire.core import pools, swap
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.types import ExecutionStatus, ExecutionType
from rapidwire.db.tables import MINIMUM_LIQUIDITY, execution
from rapidwire.db.tables import transfer as transfer_table
from tests.integration.api.conftest import AuthHeaders
from tests.integration.factories import add_liquidity, balance_of, create_currency, create_pool, mint

OWNER, LP, ALICE = 8100, 8111, 8222
A, B, C, D = 31, 32, 33, 34
SYMBOLS = {A: "AAA", B: "BBB", C: "CCC", D: "DDD"}


@pytest.fixture
async def seeded(db: AsyncEngine) -> dict[str, pools.PoolCreated]:
    """Currencies A-D and pools A/B at fees 100 and 10, A/C and C/B, so A to B has three routes and D has no pool."""
    for currency_id, symbol in SYMBOLS.items():
        await create_currency(db, currency_id=currency_id, symbol=symbol)
    return {
        "ab100": await create_pool(db, user_id=LP, currency_x=A, currency_y=B, amount_x=1_000_000, amount_y=1_000_000, fee_bps=100),
        "ab10": await create_pool(db, user_id=LP, currency_x=B, currency_y=A, amount_x=400_000, amount_y=400_000, fee_bps=10),
        "ac": await create_pool(db, user_id=LP, currency_x=A, currency_y=C, amount_x=800_000, amount_y=800_000, fee_bps=50),
        "cb": await create_pool(db, user_id=LP, currency_x=C, currency_y=B, amount_x=800_000, amount_y=800_000, fee_bps=50),
    }


async def _count(engine: AsyncEngine, type: str, status: str | None = None) -> int:
    stmt = select(func.count()).select_from(execution).where(execution.c.type == type)
    if status is not None:
        stmt = stmt.where(execution.c.status == status)
    async with engine.connect() as conn:
        return int((await conn.execute(stmt)).scalar_one())


async def _transfers_of(engine: AsyncEngine, execution_id: int) -> list[int]:
    async with engine.connect() as conn:
        return list((await conn.execute(select(transfer_table.c.transfer_id).where(transfer_table.c.execution_id == execution_id).order_by(transfer_table.c.transfer_id))).scalars())


async def _balances(engine: AsyncEngine, *accounts: int) -> dict[tuple[int, int], int]:
    return {(account_id, currency_id): await balance_of(engine, account_id, currency_id) for account_id in accounts for currency_id in SYMBOLS}


def _error(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    error: dict[str, Any] = body["error"]
    return error


def _units(text: str) -> int:
    whole, _, fraction = text.partition(".")
    assert len(fraction) == 3
    return int(whole) * 1000 + int(fraction)


def _assert_allocation(body: dict[str, Any]) -> None:
    """The routes spend amount_in and pay amount_out in total, and each route's hops chain from its input to its output."""
    routes = body["routes"]
    assert routes
    assert sum(_units(route["amount_in"]) for route in routes) == _units(body["amount_in"])
    assert sum(_units(route["amount_out"]) for route in routes) == _units(body["amount_out"])
    for route in routes:
        hops = route["hops"]
        assert route["pool_ids"] == [hop["pool_id"] for hop in hops]
        assert route["fee_bps"] == [hop["fee_bps"] for hop in hops]
        assert route["currencies"] == [hops[0]["currency_in"], *(hop["currency_out"] for hop in hops)]
        assert (route["currencies"][0], route["currencies"][-1]) == (body["from_currency"], body["to_currency"])
        assert (hops[0]["amount_in"], hops[-1]["amount_out"]) == (route["amount_in"], route["amount_out"])
        for before, after in itertools.pairwise(hops):
            assert before["amount_out"] == after["amount_in"]


def _swap_body(amount_in: str, min_out: str, from_currency: int = A, to_currency: int = B) -> dict[str, str]:
    return {"from_currency": str(from_currency), "to_currency": str(to_currency), "amount_in": amount_in, "min_out": min_out}


async def test_list_pools_pages(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated]) -> None:
    ids = sorted(created.pool_id for created in seeded.values())
    first = await client.get("/v1/pools", params={"per_page": 3})
    assert first.status_code == 200, first.text
    body = first.json()
    assert (body["page"], body["per_page"], body["total"], body["total_is_lower_bound"]) == (1, 3, 4, False)
    assert [int(item["pool_id"]) for item in body["items"]] == ids[:3]
    second = (await client.get("/v1/pools", params={"page": 2, "per_page": 3})).json()
    assert [int(item["pool_id"]) for item in second["items"]] == ids[3:]
    assert (await client.get("/v1/pools")).json()["per_page"] == 15
    for params in ({"per_page": 101}, {"page": 0}, {"page": 101, "per_page": 100}):
        _error(await client.get("/v1/pools", params=params), 422, "validation_error")


async def test_get_pool(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated]) -> None:
    created = seeded["ab10"]
    response = await client.get(f"/v1/pools/{created.pool_id}")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["pool_id"] == str(created.pool_id)
    assert (body["currency_a"], body["currency_b"], body["symbol_a"], body["symbol_b"]) == (str(A), str(B), "AAA", "BBB")
    assert (body["fee_bps"], body["active"], body["deleted_a"], body["deleted_b"]) == (10, True, False, False)
    assert (body["reserve_a"], body["reserve_b"]) == ("400.000", "400.000")
    assert (body["total_shares"], body["locked_shares"]) == ("400000", str(MINIMUM_LIQUIDITY))
    assert body["custody_account_id"] == str(created.custody_account_id)
    assert (body["created_by"], body["created_execution_id"]) == (str(LP), str(created.execution_id))
    assert body["dissolved_at"] is None and body["dissolved_execution_id"] is None
    error = _error(await client.get("/v1/pools/999999"), 404, "pool_not_found")
    assert "execution_id" not in error
    for bad in ("0", "-1", "abc", "01"):
        _error(await client.get(f"/v1/pools/{bad}"), 422, "validation_error")


async def test_dissolved_pool_is_still_readable(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine) -> None:
    guilds = InMemoryGuildInfo()
    guilds.add_guild(C, OWNER)
    start = datetime.now(UTC)
    await cur.request_delete(db, guilds, currency_id=C, user_id=OWNER, now=start)
    deleted = await cur.finalize_delete(db, guilds, currency_id=C, user_id=OWNER, now=start + cur.DELETE_TIMELOCK + timedelta(seconds=1))
    body = (await client.get(f"/v1/pools/{seeded['ac'].pool_id}")).json()
    assert (body["active"], body["reserve_a"], body["total_shares"], body["locked_shares"]) == (False, "0.000", "0", "0")
    assert body["deleted_b"] is True
    assert body["dissolved_execution_id"] == str(deleted.execution_id)
    assert body["dissolved_at"] is not None
    listed = (await client.get("/v1/pools")).json()
    assert {int(item["pool_id"]) for item in listed["items"]} == {seeded["ab100"].pool_id, seeded["ab10"].pool_id}


async def test_pair_pools(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated]) -> None:
    for x, y in ((A, B), (B, A)):
        response = await client.get(f"/v1/pools/pair/{x}/{y}")
        assert response.status_code == 200, response.text
        body = response.json()
        assert (body["currency_a"], body["currency_b"]) == (str(A), str(B))
        assert [(item["pool_id"], item["fee_bps"]) for item in body["pools"]] == [(str(seeded["ab10"].pool_id), 10), (str(seeded["ab100"].pool_id), 100)]
    assert (await client.get(f"/v1/pools/pair/{D}/{A}")).json() == {"currency_a": str(A), "currency_b": str(D), "pools": []}
    _error(await client.get(f"/v1/pools/pair/{A}/{A}"), 422, "invalid_argument")
    _error(await client.get(f"/v1/pools/pair/{A}/x"), 422, "validation_error")


async def test_provider_positions(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine) -> None:
    await add_liquidity(db, user_id=ALICE, currency_x=A, currency_y=B, amount_x=500_000, amount_y=500_000, fee_bps=100)
    body = (await client.get(f"/v1/accounts/{ALICE}/pools")).json()
    assert (body["total"], body["per_page"]) == (1, 15)
    [item] = body["items"]
    assert item["account_id"] == str(ALICE)
    assert item["pool"]["pool_id"] == str(seeded["ab100"].pool_id)
    assert (item["shares"], item["share_percent"]) == ("500000", "33.33")
    assert (item["withdrawable_a"], item["withdrawable_b"]) == ("500.000", "500.000")
    lp = (await client.get(f"/v1/accounts/{LP}/pools", params={"per_page": 3, "page": 2})).json()
    assert (lp["total"], len(lp["items"])) == (4, 1)
    assert (await client.get("/v1/accounts/-1/pools")).json()["items"] == []
    _error(await client.get(f"/v1/accounts/{LP}/pools", params={"per_page": 101}), 422, "validation_error")
    _error(await client.get("/v1/accounts/abc/pools"), 422, "validation_error")


async def test_quote_breakdown(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, api_settings: Settings) -> None:
    response = await client.post("/v1/swap/quote", json={"from_currency": str(A), "to_currency": str(B), "amount_in": "300"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["from_currency"], body["to_currency"], body["from_symbol"], body["to_symbol"]) == (str(A), str(B), "AAA", "BBB")
    assert (body["amount_in"], body["truncated"]) == ("300.000", False)
    assert len(body["routes"]) >= 2
    _assert_allocation(body)
    involved = sorted({int(currency_id) for route in body["routes"] for currency_id in route["currencies"]})
    assert body["symbols"] == {str(currency_id): SYMBOLS[currency_id] for currency_id in involved}
    expected = await swap.quote_swap(db, from_currency=A, to_currency=B, amount_in=300_000, config=swap.SwapConfig.from_settings(api_settings))
    assert _units(body["amount_out"]) == expected.amount_out
    assert [[int(pool_id) for pool_id in route["pool_ids"]] for route in body["routes"]] == [list(route.pool_ids) for route in expected.routes]
    assert await _count(db, ExecutionType.SWAP) == 0


async def test_quote_runs_the_search_off_the_event_loop(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], monkeypatch: pytest.MonkeyPatch) -> None:
    threads: list[threading.Thread] = []
    original = swap._search_and_allocate

    def recording(*args: Any, **kwargs: Any) -> Any:
        threads.append(threading.current_thread())
        return original(*args, **kwargs)

    monkeypatch.setattr(swap, "_search_and_allocate", recording)
    response = await client.post("/v1/swap/quote", json={"from_currency": str(A), "to_currency": str(B), "amount_in": "1"})
    assert response.status_code == 200, response.text
    assert len(threads) == 1 and threads[0] is not threading.current_thread()


async def test_quote_errors(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated]) -> None:
    error = _error(await client.post("/v1/swap/quote", json={"from_currency": str(A), "to_currency": str(D), "amount_in": "1"}), 409, "no_route")
    assert "execution_id" not in error
    _error(await client.post("/v1/swap/quote", json={"from_currency": str(A), "to_currency": "999", "amount_in": "1"}), 404, "currency_not_found")
    _error(await client.post("/v1/swap/quote", json={"from_currency": str(A), "to_currency": str(A), "amount_in": "1"}), 422, "invalid_argument")
    _error(await client.post("/v1/swap/quote", json={"from_currency": str(A), "to_currency": str(B), "amount_in": "0"}), 422, "validation_error")


async def test_swap_executes_the_quote(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(db, A, ALICE, 300_000)
    quote = (await client.post("/v1/swap/quote", json={"from_currency": str(A), "to_currency": str(B), "amount_in": "300"})).json()
    response = await client.post("/v1/swap", json=_swap_body("300", quote["amount_out"]), headers=await auth_headers(ALICE))
    assert response.status_code == 200, response.text
    body = response.json()
    assert "idempotency-replayed" not in response.headers
    assert (body["amount_in"], body["min_out"], body["amount_out"]) == ("300.000", quote["amount_out"], quote["amount_out"])
    assert body["routes"] == quote["routes"]
    _assert_allocation(body)
    assert [int(transfer_id) for transfer_id in body["transfer_ids"]] == await _transfers_of(db, int(body["execution_id"]))
    assert await balance_of(db, ALICE, A) == 0
    assert await balance_of(db, ALICE, B) == _units(quote["amount_out"])


async def test_swap_below_min_out_records_a_failure_and_moves_nothing(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(db, A, ALICE, 300_000)
    quote = (await client.post("/v1/swap/quote", json={"from_currency": str(A), "to_currency": str(B), "amount_in": "300"})).json()
    before = await _balances(db, ALICE, *(created.custody_account_id for created in seeded.values()))
    too_much = format_amount(_units(quote["amount_out"]) + 1)
    error = _error(await client.post("/v1/swap", json=_swap_body("300", too_much), headers=await auth_headers(ALICE)), 409, "slippage_exceeded")
    execution_id = int(error["execution_id"])
    assert await _transfers_of(db, execution_id) == []
    assert await _count(db, ExecutionType.SWAP, ExecutionStatus.FAILED) == 1
    assert await _balances(db, ALICE, *(created.custody_account_id for created in seeded.values())) == before


async def test_swap_errors(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    headers = await auth_headers(ALICE)
    await mint(db, A, ALICE, 1000)
    no_route = _error(await client.post("/v1/swap", json=_swap_body("1", "0", to_currency=D), headers=headers), 409, "no_route")
    assert isinstance(no_route["execution_id"], str)
    broke = _error(await client.post("/v1/swap", json=_swap_body("2", "0"), headers=headers), 409, "insufficient_balance")
    assert isinstance(broke["execution_id"], str)
    unknown = _error(await client.post("/v1/swap", json=_swap_body("1", "0", to_currency=999), headers=headers), 404, "currency_not_found")
    assert isinstance(unknown["execution_id"], str)
    _error(await client.post("/v1/swap", json=_swap_body("1", "0", to_currency=A), headers=headers), 422, "invalid_argument")
    missing = _error(await client.post("/v1/swap", json={"from_currency": str(A), "to_currency": str(B), "amount_in": "1"}, headers=headers), 422, "validation_error")
    assert [detail["loc"] for detail in missing["details"]] == [["body", "min_out"]]
    extra = _error(await client.post("/v1/swap", json={**_swap_body("1", "0"), "slippage": "1"}, headers=headers), 422, "validation_error")
    assert [(detail["loc"], detail["type"]) for detail in extra["details"]] == [(["body", "<extra>"], "extra_forbidden")]
    unauthenticated = await client.post("/v1/swap", json=_swap_body("1", "0"))
    _error(unauthenticated, 401, "unauthorized")
    assert unauthenticated.headers["www-authenticate"] == "Bearer"
    assert await _count(db, ExecutionType.SWAP) == 3
    assert await balance_of(db, ALICE, A) == 1000


@pytest.mark.parametrize("field", ["amount_in", "min_out"])
@pytest.mark.parametrize("value", [1, 1.5, "1.0001", "-1", "1e3", " 1", "01", None])
async def test_swap_amounts_are_strict(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders, field: str, value: object) -> None:
    payload: dict[str, object] = {**_swap_body("1", "0"), field: value}
    error = _error(await client.post("/v1/swap", json=payload, headers=await auth_headers(ALICE)), 422, "validation_error")
    assert [detail["loc"] for detail in error["details"]] == [["body", field]]
    assert await _count(db, ExecutionType.SWAP) == 0


async def test_swap_min_out_zero_is_accepted(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(db, A, ALICE, 1500)
    response = await client.post("/v1/swap", json=_swap_body("1.5", "0"), headers=await auth_headers(ALICE))
    assert response.status_code == 200, response.text
    assert response.json()["min_out"] == "0.000"


async def test_swap_replays_with_the_same_key(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(db, A, ALICE, 10_000)
    headers = {**await auth_headers(ALICE), "Idempotency-Key": "swap-1"}
    first = await client.post("/v1/swap", json=_swap_body("5", "0"), headers=headers)
    assert first.status_code == 200, first.text
    again = await client.post("/v1/swap", json=_swap_body("5", "0"), headers=headers)
    assert again.status_code == 200
    assert again.headers["idempotency-replayed"] == "true"
    assert again.json() == first.json()
    assert await _count(db, ExecutionType.SWAP) == 1
    assert await balance_of(db, ALICE, A) == 5000
    _error(await client.post("/v1/swap", json=_swap_body("6", "0"), headers=headers), 422, "idempotency_conflict")


async def test_create_pool(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(db, D, ALICE, 4000)
    await mint(db, A, ALICE, 1000)
    headers = await auth_headers(ALICE)
    payload = {"currency_x": str(D), "currency_y": str(A), "fee_bps": 50, "amount_x": "4", "amount_y": "1"}
    response = await client.post("/v1/pools", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["currency_a"], body["currency_b"], body["currency_x"], body["currency_y"]) == (str(A), str(D), str(D), str(A))
    assert (body["amount_a"], body["amount_b"], body["amount_x"], body["amount_y"]) == ("1.000", "4.000", "4.000", "1.000")
    assert (body["total_shares"], body["locked_shares"], body["shares"], body["provider_shares"]) == ("2000", str(MINIMUM_LIQUIDITY), "1000", "1000")
    assert (body["reserve_a"], body["reserve_b"], body["fee_bps"]) == ("1.000", "4.000", 50)
    assert [int(transfer_id) for transfer_id in body["transfer_ids"]] == await _transfers_of(db, int(body["execution_id"]))
    pool = (await client.get(f"/v1/pools/{body['pool_id']}")).json()
    assert pool["custody_account_id"] == body["custody_account_id"]
    duplicate = _error(await client.post("/v1/pools", json=payload, headers=headers), 409, "pool_already_exists")
    assert isinstance(duplicate["execution_id"], str)
    _error(await client.post("/v1/pools", json={**payload, "fee_bps": 100, "amount_x": "1", "amount_y": "1"}, headers=headers), 409, "insufficient_liquidity")
    _error(await client.post("/v1/pools", json={**payload, "currency_y": str(D)}, headers=headers), 422, "invalid_argument")
    _error(await client.post("/v1/pools", json={**payload, "currency_y": "999"}, headers=headers), 404, "currency_not_found")
    _error(await client.post("/v1/pools", json=payload), 401, "unauthorized")
    assert await _count(db, ExecutionType.POOL_CREATE, ExecutionStatus.FAILED) == 3


@pytest.mark.parametrize("fee", [25, 0, "100", 100.0, 100.5, True, None])
async def test_invalid_fee_tier_is_a_validation_error(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders, fee: object) -> None:
    headers = await auth_headers(ALICE)
    requests: list[tuple[str, dict[str, object]]] = [
        ("/v1/pools", {"currency_x": str(A), "currency_y": str(D), "amount_x": "1", "amount_y": "1"}),
        ("/v1/pools/liquidity/add", {"currency_x": str(A), "currency_y": str(B), "amount_x": "1", "amount_y": "1"}),
        ("/v1/pools/liquidity/remove", {"currency_x": str(A), "currency_y": str(B), "shares": "1"}),
    ]
    for path, payload in requests:
        error = _error(await client.post(path, json={**payload, "fee_bps": fee}, headers=headers), 422, "validation_error")
        assert [detail["loc"] for detail in error["details"]] == [["body", "fee_bps"]], path
    for type in (ExecutionType.POOL_CREATE, ExecutionType.LIQUIDITY_ADD, ExecutionType.LIQUIDITY_REMOVE):
        assert await _count(db, type) == len(seeded) * (type == ExecutionType.POOL_CREATE)


async def test_liquidity_round_trip(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(db, A, ALICE, 200_000)
    await mint(db, B, ALICE, 50_000)
    headers = await auth_headers(ALICE)
    pool_id = seeded["ab10"].pool_id
    add = await client.post("/v1/pools/liquidity/add", json={"currency_x": str(B), "currency_y": str(A), "fee_bps": 10, "amount_x": "50", "amount_y": "200"}, headers=headers)
    assert add.status_code == 200, add.text
    added = add.json()
    assert (added["pool_id"], added["amount_x"], added["amount_y"], added["shares"], added["provider_shares"]) == (str(pool_id), "50.000", "50.000", "50000", "50000")
    assert (added["total_shares"], added["reserve_a"], added["reserve_b"]) == ("450000", "450.000", "450.000")
    assert len(added["transfer_ids"]) == 2 and "execution_id" in added
    assert await balance_of(db, ALICE, A) == 150_000
    strict = await client.post("/v1/pools/liquidity/add", json={"currency_x": str(A), "currency_y": str(B), "fee_bps": 10, "amount_x": "1", "amount_y": "1", "min_shares": "1001"}, headers=headers)
    _error(strict, 409, "slippage_exceeded")
    remove_body = {"currency_x": str(A), "currency_y": str(B), "fee_bps": 10, "shares": "50000"}
    _error(await client.post("/v1/pools/liquidity/remove", json={**remove_body, "amount_y_min": "50.001"}, headers=headers), 409, "slippage_exceeded")
    _error(await client.post("/v1/pools/liquidity/remove", json={**remove_body, "shares": "50001"}, headers=headers), 409, "insufficient_shares")
    remove = await client.post("/v1/pools/liquidity/remove", json={**remove_body, "amount_x_min": "50", "amount_y_min": "50"}, headers=headers)
    assert remove.status_code == 200, remove.text
    removed = remove.json()
    assert (removed["amount_x"], removed["amount_y"], removed["shares"], removed["provider_shares"], removed["total_shares"]) == ("50.000", "50.000", "50000", "0", "400000")
    assert (await balance_of(db, ALICE, A), await balance_of(db, ALICE, B)) == (200_000, 50_000)
    assert (await client.get(f"/v1/accounts/{ALICE}/pools")).json()["items"] == []
    no_pool = {"currency_x": str(A), "currency_y": str(D), "fee_bps": 10, "amount_x": "1", "amount_y": "1"}
    missing = _error(await client.post("/v1/pools/liquidity/add", json=no_pool, headers=headers), 404, "pool_not_found")
    assert isinstance(missing["execution_id"], str)
    _error(await client.post("/v1/pools/liquidity/remove", json={**remove_body, "fee_bps": 50}, headers=headers), 404, "pool_not_found")
    for bad in ({**remove_body, "shares": 5}, {**remove_body, "shares": "0"}, {**remove_body, "amount_x_min": "-1"}):
        _error(await client.post("/v1/pools/liquidity/remove", json=bad, headers=headers), 422, "validation_error")
    _error(await client.post("/v1/pools/liquidity/add", json={"currency_x": str(A), "currency_y": str(B), "fee_bps": 10, "amount_x": "1", "amount_y": "1"}), 401, "unauthorized")


async def test_liquidity_add_replays(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(db, A, ALICE, 1000)
    await mint(db, B, ALICE, 1000)
    headers = {**await auth_headers(ALICE), "Idempotency-Key": "lp-1"}
    payload = {"currency_x": str(A), "currency_y": str(B), "fee_bps": 100, "amount_x": "1", "amount_y": "1"}
    first = await client.post("/v1/pools/liquidity/add", json=payload, headers=headers)
    again = await client.post("/v1/pools/liquidity/add", json=payload, headers=headers)
    assert first.status_code == again.status_code == 200
    assert again.headers["idempotency-replayed"] == "true"
    assert again.json() == first.json()
    assert await _count(db, ExecutionType.LIQUIDITY_ADD) == 1


async def test_create_pool_replays(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(db, D, ALICE, 4000)
    await mint(db, A, ALICE, 1000)
    headers = {**await auth_headers(ALICE), "Idempotency-Key": "pool-1"}
    payload = {"currency_x": str(D), "currency_y": str(A), "fee_bps": 50, "amount_x": "4", "amount_y": "1"}
    first = await client.post("/v1/pools", json=payload, headers=headers)
    assert first.status_code == 200, first.text
    again = await client.post("/v1/pools", json=payload, headers=headers)
    assert again.status_code == 200, again.text
    assert "idempotency-replayed" not in first.headers
    assert again.headers["idempotency-replayed"] == "true"
    assert again.json() == first.json()
    assert await _count(db, ExecutionType.POOL_CREATE) == len(seeded) + 1
    assert (await balance_of(db, ALICE, D), await balance_of(db, ALICE, A)) == (0, 0)
    _error(await client.post("/v1/pools", json={**payload, "fee_bps": 100}, headers=headers), 422, "idempotency_conflict")
    # A recorded failure replays as the same envelope.
    failed_headers = {**headers, "Idempotency-Key": "pool-2"}
    failed = _error(await client.post("/v1/pools", json=payload, headers=failed_headers), 409, "pool_already_exists")
    replayed = await client.post("/v1/pools", json=payload, headers=failed_headers)
    assert replayed.headers["idempotency-replayed"] == "true"
    assert replayed.json() == {"error": failed}
    assert await _count(db, ExecutionType.POOL_CREATE) == len(seeded) + 2


async def test_liquidity_remove_replays(client: httpx.AsyncClient, seeded: dict[str, pools.PoolCreated], db: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(db, A, ALICE, 1000)
    await mint(db, B, ALICE, 1000)
    headers = await auth_headers(ALICE)
    added = await client.post("/v1/pools/liquidity/add", json={"currency_x": str(A), "currency_y": str(B), "fee_bps": 100, "amount_x": "1", "amount_y": "1"}, headers=headers)
    assert added.status_code == 200, added.text
    keyed = {**headers, "Idempotency-Key": "lp-remove-1"}
    payload = {"currency_x": str(B), "currency_y": str(A), "fee_bps": 100, "shares": "400"}
    first = await client.post("/v1/pools/liquidity/remove", json=payload, headers=keyed)
    assert first.status_code == 200, first.text
    again = await client.post("/v1/pools/liquidity/remove", json=payload, headers=keyed)
    assert again.status_code == 200, again.text
    assert again.headers["idempotency-replayed"] == "true"
    assert again.json() == first.json()
    assert await _count(db, ExecutionType.LIQUIDITY_REMOVE) == 1
    assert first.json()["provider_shares"] == str(int(added.json()["provider_shares"]) - 400)
    positions = (await client.get(f"/v1/accounts/{ALICE}/pools")).json()["items"]
    assert [item["shares"] for item in positions] == [first.json()["provider_shares"]]
    _error(await client.post("/v1/pools/liquidity/remove", json={**payload, "shares": "401"}, headers=keyed), 422, "idempotency_conflict")
