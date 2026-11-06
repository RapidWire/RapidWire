from __future__ import annotations

import base64
import logging
from typing import Annotated, Any

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire import __version__
from rapidwire.api import auth
from rapidwire.api.app import create_app
from rapidwire.api.auth import AccountDep, WriterDep, get_writer
from rapidwire.api.body_limit import DEFAULT_BODY_LIMIT
from rapidwire.api.deps import EngineDep, StateDep
from rapidwire.api.formats import Amount, IdStr, IntOut, PositiveAmount, PositiveIdStr
from rapidwire.api.paging import Page, PageParams, page_params
from rapidwire.config import Settings
from rapidwire.core import paging as core_paging
from rapidwire.core import tokens, transfers
from rapidwire.core.currency import DELETE_EXPIRY, DELETE_TIMELOCK, RATE_CHANGE_TIMELOCK
from rapidwire.core.errors import CurrencyNotFound
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.tokens import generate_token, issue_token, revoke_tokens
from rapidwire.core.types import ExecutionType
from rapidwire.db.tables import FEE_DENOMINATOR, FEE_TIERS_BPS, IDEMPOTENCY_KEY_MAX, MINIMUM_LIQUIDITY, RATE_PPM_MAX, account, execution
from rapidwire.db.types import AMOUNT_MAX
from tests.integration.api.conftest import AuthHeaders, bearer
from tests.integration.factories import balance_of, contract_binary, create_currency, mint

ALICE = 7001
BOB = 7002
CUR = 4242
LEAKY = "leaky-detail-31415"

probe_router = APIRouter(prefix="/v1/_test")


class Probe(BaseModel):
    amount: PositiveAmount
    account_id: IdStr


class TransferIn(BaseModel):
    recipient_id: PositiveIdStr
    currency_id: PositiveIdStr
    amount: PositiveAmount


class TransferOut(BaseModel):
    execution_id: IntOut
    transfer_id: IntOut
    recipient_id: IntOut
    amount: Amount


class Item(BaseModel):
    item_id: IntOut
    amount: Amount


class Who(BaseModel):
    account_id: IntOut | None


@probe_router.post("/probe")
async def probe(body: Probe) -> Probe:
    return body


@probe_router.get("/missing-currency")
async def missing_currency() -> None:
    raise CurrencyNotFound()


@probe_router.get("/crash")
async def crash() -> None:
    raise RuntimeError(LEAKY)


@probe_router.get("/whoami", response_model=Who)
async def whoami(account_id: AccountDep) -> Who:
    return Who(account_id=account_id)


def _transfer_out(result: transfers.TransferResult) -> TransferOut:
    return TransferOut(execution_id=result.execution_id, transfer_id=result.transfer_id, recipient_id=result.recipient_id, amount=result.amount)


@probe_router.post("/transfer", response_model=TransferOut)
async def do_transfer(body: TransferIn, writer: WriterDep, engine: EngineDep) -> TransferOut:
    result = await transfers.transfer(engine, sender_id=writer.account_id, recipient_id=body.recipient_id, currency_id=body.currency_id, amount=body.amount, idempotency_key=writer.idempotency_key)
    return _transfer_out(writer.done(result))


@probe_router.post("/transfer-after-revoke", response_model=TransferOut)
async def transfer_after_revoke(body: TransferIn, writer: WriterDep, engine: EngineDep) -> TransferOut:
    # The token passed authentication; a revoke commits before the write starts, as a concurrent request would.
    await revoke_tokens(engine, account_id=writer.account_id)
    result = await transfers.transfer(engine, sender_id=writer.account_id, recipient_id=body.recipient_id, currency_id=body.currency_id, amount=body.amount)
    return _transfer_out(writer.done(result))


@probe_router.post("/transfer-as-recipient", response_model=TransferOut)
async def transfer_as_other(body: TransferIn, writer: WriterDep, engine: EngineDep) -> TransferOut:
    result = await transfers.transfer(engine, sender_id=body.recipient_id, recipient_id=writer.account_id, currency_id=body.currency_id, amount=body.amount)
    return _transfer_out(writer.done(result))


@probe_router.get("/page", response_model=Page[Item])
async def paged(params: Annotated[PageParams, Depends(page_params(15, 100))], state: StateDep) -> Page[Item]:
    core = core_paging.Page(items=(1, 2), total=2, page=params.page, per_page=params.per_page)
    return Page[Item].build(core, lambda n: Item(item_id=n, amount=n * 1500))


@pytest.fixture
def app(api_app: FastAPI) -> FastAPI:
    api_app.include_router(probe_router)
    return api_app


@pytest.fixture
async def funded(db: AsyncEngine) -> AsyncEngine:
    await create_currency(db, currency_id=CUR, symbol="APIT")
    await mint(db, CUR, ALICE, 10_000)
    return db


async def _executions(engine: AsyncEngine, type: str) -> int:
    async with engine.connect() as conn:
        return int((await conn.execute(select(func.count()).select_from(execution).where(execution.c.type == type))).scalar_one())


def _assert_envelope(response: httpx.Response, status: int, code: str) -> dict[str, object]:
    assert response.status_code == status, response.text
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == {"error"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str)
    error: dict[str, object] = body["error"]
    return error


async def test_unknown_api_path_is_a_json_404(client: httpx.AsyncClient) -> None:
    for path in ("/v1/unknown", "/v1", "/v1/", "/v1/version/extra"):
        error = _assert_envelope(await client.get(path), 404, "not_found")
        assert error["message"] == "The requested resource was not found."


async def test_wrong_method_is_a_json_405(client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/version")
    _assert_envelope(response, 405, "method_not_allowed")
    assert "GET" in response.headers["allow"]


async def test_validation_errors_list_fields_without_values(app: FastAPI, client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/_test/probe", json={"amount": 12.5, "account_id": LEAKY})
    error = _assert_envelope(response, 422, "validation_error")
    assert error["details"] == [
        {"loc": ["body", "amount"], "message": 'Amounts must be JSON strings such as "12.5", with at most 3 decimal places.', "type": "amount_type"},
        {"loc": ["body", "account_id"], "message": "The value must be a decimal integer string, without plus sign, spaces or leading zeros.", "type": "int_string_format"},
    ]
    assert LEAKY not in response.text
    ok = await client.post("/v1/_test/probe", json={"amount": "12.5", "account_id": "-1"})
    assert ok.status_code == 200
    assert ok.json() == {"amount": "12.500", "account_id": "-1"}


async def test_malformed_json_is_a_validation_error(app: FastAPI, client: httpx.AsyncClient) -> None:
    response = await client.post("/v1/_test/probe", content=b'{"amount": "' + LEAKY.encode() + b'"', headers={"Content-Type": "application/json"})
    _assert_envelope(response, 422, "validation_error")
    assert LEAKY not in response.text


async def test_domain_error_maps_to_its_status(app: FastAPI, client: httpx.AsyncClient) -> None:
    error = _assert_envelope(await client.get("/v1/_test/missing-currency"), 404, "currency_not_found")
    assert error == {"code": "currency_not_found", "message": CurrencyNotFound.default_message}


async def test_unhandled_exception_is_logged_and_hidden(app: FastAPI, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="rapidwire.api"):
        response = await client.get("/v1/_test/crash")
    error = _assert_envelope(response, 500, "internal_error")
    assert error == {"code": "internal_error", "message": "Internal server error."}
    assert LEAKY not in response.text
    [record] = [record for record in caplog.records if record.name == "rapidwire.api"]
    assert record.exc_info is not None
    assert record.__dict__["path"] == "/v1/_test/crash"


async def test_static_site_is_served_outside_the_api(client: httpx.AsyncClient) -> None:
    for path in ("/", "/index.html"):
        response = await client.get(path)
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        assert "<title>RapidWire</title>" in response.text
    missing = await client.get("/no-such-page.html")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("text/html")


async def test_openapi_lists_the_routes(client: httpx.AsyncClient) -> None:
    response = await client.get("/openapi.json")
    assert response.status_code == 200
    document = response.json()
    assert document["info"] == {"title": "RapidWire", "version": __version__, "description": document["info"]["description"]}
    assert {"/v1/version", "/v1/config"} <= set(document["paths"])
    assert "ErrorResponse" in document["components"]["schemas"]
    assert (await client.get("/docs")).status_code == 200


async def test_missing_or_bad_tokens_are_401(app: FastAPI, client: httpx.AsyncClient, db: AsyncEngine) -> None:
    old = await issue_token(db, account_id=ALICE)
    current = await issue_token(db, account_id=ALICE)
    cases: list[dict[str, str]] = [
        {},
        {"Authorization": "Basic abc"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer a b"},
        {"Authorization": "Bearer not-a-token"},
        bearer(generate_token()),
        bearer(old.token),
    ]
    for headers in cases:
        response = await client.get("/v1/_test/whoami", headers=headers)
        _assert_envelope(response, 401, "unauthorized")
        assert response.headers["www-authenticate"] == "Bearer"
    ok = await client.get("/v1/_test/whoami", headers=bearer(current.token))
    assert ok.status_code == 200
    assert ok.json() == {"account_id": str(ALICE)}
    lower = await client.get("/v1/_test/whoami", headers={"Authorization": f"bearer {current.token}"})
    assert lower.status_code == 200


async def test_write_records_and_replays(app: FastAPI, client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    headers = await auth_headers(ALICE)
    payload = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1.25"}
    first = await client.post("/v1/_test/transfer", json=payload, headers={**headers, "Idempotency-Key": "pay-1"})
    assert first.status_code == 200
    assert "idempotency-replayed" not in first.headers
    assert first.json()["amount"] == "1.250"
    again = await client.post("/v1/_test/transfer", json=payload, headers={**headers, "Idempotency-Key": "pay-1"})
    assert again.status_code == 200
    assert again.headers["idempotency-replayed"] == "true"
    assert again.json() == first.json()
    assert await balance_of(funded, BOB, CUR) == 1250
    unkeyed = await client.post("/v1/_test/transfer", json=payload, headers=headers)
    assert "idempotency-replayed" not in unkeyed.headers
    assert await balance_of(funded, BOB, CUR) == 2500
    conflict = await client.post("/v1/_test/transfer", json={**payload, "amount": "2"}, headers={**headers, "Idempotency-Key": "pay-1"})
    _assert_envelope(conflict, 422, "idempotency_conflict")
    assert await _executions(funded, ExecutionType.TRANSFER) == 2


async def test_failed_write_replays_the_same_envelope(app: FastAPI, client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    headers = {**await auth_headers(ALICE), "Idempotency-Key": "too-much"}
    payload = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1000"}
    first = await client.post("/v1/_test/transfer", json=payload, headers=headers)
    error = _assert_envelope(first, 409, "insufficient_balance")
    assert isinstance(error["execution_id"], str)
    assert "idempotency-replayed" not in first.headers
    again = await client.post("/v1/_test/transfer", json=payload, headers=headers)
    assert again.status_code == 409
    assert again.json() == first.json()
    assert again.headers["idempotency-replayed"] == "true"
    assert await _executions(funded, ExecutionType.TRANSFER) == 1


@pytest.mark.parametrize("key", [b"", b"x" * (IDEMPOTENCY_KEY_MAX + 1), b"caf\xe9", b"tab\there"])
async def test_invalid_idempotency_keys_are_rejected(app: FastAPI, client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders, key: bytes) -> None:
    payload = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1"}
    headers = [(name.encode(), value.encode()) for name, value in (await auth_headers(ALICE)).items()] + [(b"Idempotency-Key", key)]
    response = await client.post("/v1/_test/transfer", json=payload, headers=headers)
    error = _assert_envelope(response, 422, "validation_error")
    details = error["details"]
    assert isinstance(details, list)
    assert [detail["loc"] for detail in details] == [["header", "Idempotency-Key"]]
    assert await _executions(funded, ExecutionType.TRANSFER) == 0


async def test_longest_idempotency_key_is_accepted(app: FastAPI, client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    payload = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1"}
    key = "k" * (IDEMPOTENCY_KEY_MAX - 2) + " ~"
    response = await client.post("/v1/_test/transfer", json=payload, headers={**await auth_headers(ALICE), "Idempotency-Key": key})
    assert response.status_code == 200, response.text


async def test_revoke_between_authentication_and_write_records_nothing(app: FastAPI, client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    payload = {"recipient_id": "7999", "currency_id": str(CUR), "amount": "1"}
    response = await client.post("/v1/_test/transfer-after-revoke", json=payload, headers=await auth_headers(ALICE))
    _assert_envelope(response, 401, "unauthorized")
    assert response.headers["www-authenticate"] == "Bearer"
    assert await _executions(funded, ExecutionType.TRANSFER) == 0
    assert await _executions(funded, ExecutionType.TOKEN_REVOKE) == 1
    assert await balance_of(funded, ALICE, CUR) == 10_000
    async with funded.connect() as conn:
        created = (await conn.execute(select(func.count()).select_from(execution).where(execution.c.initiator_id == 7999))).scalar_one()
    assert created == 0


CUR2 = 4243
GUILD_ID = 7_700_001
# Every endpoint that depends on the Writer, with a request that passes the checks made before its write transaction starts.
WRITE_REQUESTS: dict[str, tuple[str, dict[str, Any] | None]] = {
    "/v1/transfers": ("/v1/transfers", {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "1"}),
    "/v1/transfers/from": ("/v1/transfers/from", {"source_id": str(BOB), "recipient_id": str(ALICE + 50), "currency_id": str(CUR), "amount": "1"}),
    "/v1/allowances": ("/v1/allowances", {"spender_id": str(BOB), "currency_id": str(CUR), "amount": "1"}),
    "/v1/claims": ("/v1/claims", {"payer_id": str(BOB), "currency_id": str(CUR), "amount": "1"}),
    "/v1/claims/{claim_id}/pay": ("/v1/claims/1/pay", None),
    "/v1/claims/{claim_id}/cancel": ("/v1/claims/1/cancel", None),
    "/v1/stakes/deposit": ("/v1/stakes/deposit", {"currency_id": str(CUR), "amount": "1"}),
    "/v1/stakes/withdraw": ("/v1/stakes/withdraw", {"currency_id": str(CUR), "amount": "1"}),
    "/v1/tokens/revoke": ("/v1/tokens/revoke", None),
    "/v1/currencies": ("/v1/currencies", {"guild_id": str(GUILD_ID), "name": "Guarded", "symbol": "GRD", "supply": "1"}),
    "/v1/currencies/{currency_id}/mint": (f"/v1/currencies/{CUR}/mint", {"amount": "1"}),
    "/v1/currencies/{currency_id}/burn": (f"/v1/currencies/{CUR}/burn", {"amount": "1"}),
    "/v1/currencies/{currency_id}/renounce": (f"/v1/currencies/{CUR}/renounce", None),
    "/v1/currencies/{currency_id}/rate-change": (f"/v1/currencies/{CUR}/rate-change", {"hourly_rate_ppm": "5"}),
    "/v1/currencies/{currency_id}/rate-change/apply": (f"/v1/currencies/{CUR}/rate-change/apply", None),
    "/v1/currencies/{currency_id}/delete": (f"/v1/currencies/{CUR}/delete", None),
    "/v1/currencies/{currency_id}/delete/finalize": (f"/v1/currencies/{CUR}/delete/finalize", None),
    "/v1/currencies/{currency_id}/delegation/enable": (f"/v1/currencies/{CUR}/delegation/enable", None),
    "/v1/currencies/{currency_id}/delegation/disable": (f"/v1/currencies/{CUR}/delegation/disable", None),
    "/v1/notifications/allow": ("/v1/notifications/allow", {"user_id": str(BOB)}),
    "/v1/notifications/deny": ("/v1/notifications/deny", {"user_id": str(BOB)}),
    "/v1/guilds/{guild_id}/discord-permissions/allow": (f"/v1/guilds/{GUILD_ID}/discord-permissions/allow", {"account_id": str(BOB)}),
    "/v1/guilds/{guild_id}/discord-permissions/deny": (f"/v1/guilds/{GUILD_ID}/discord-permissions/deny", {"account_id": str(BOB)}),
    "/v1/swap": ("/v1/swap", {"from_currency": str(CUR), "to_currency": str(CUR2), "amount_in": "1", "min_out": "0"}),
    "/v1/pools": ("/v1/pools", {"currency_x": str(CUR), "currency_y": str(CUR2), "fee_bps": 100, "amount_x": "5", "amount_y": "5"}),
    "/v1/pools/liquidity/add": ("/v1/pools/liquidity/add", {"currency_x": str(CUR), "currency_y": str(CUR2), "fee_bps": 100, "amount_x": "5", "amount_y": "5"}),
    "/v1/pools/liquidity/remove": ("/v1/pools/liquidity/remove", {"currency_x": str(CUR), "currency_y": str(CUR2), "fee_bps": 100, "shares": "1"}),
    "/v1/contracts/{account_id}/versions": (f"/v1/contracts/{ALICE}/versions", {"binary": base64.b64encode(contract_binary()).decode("ascii")}),
    "/v1/contracts/{account_id}/lock": (f"/v1/contracts/{ALICE}/lock", {"hours": 1}),
    "/v1/contracts/{account_id}/pause": (f"/v1/contracts/{ALICE}/pause", None),
    "/v1/contracts/{account_id}/resume": (f"/v1/contracts/{ALICE}/resume", None),
    "/v1/contracts/{account_id}/delete": (f"/v1/contracts/{ALICE}/delete", None),
    "/v1/contracts/{account_id}/call": (f"/v1/contracts/{BOB}/call", {"function": "ping"}),
}


def _depends_on(dependant: Dependant, call: object) -> bool:
    return dependant.call is call or any(_depends_on(sub, call) for sub in dependant.dependencies)


def test_every_writer_route_is_covered(api_settings: Settings) -> None:
    app = create_app(api_settings)
    writer_paths = {str(context.path) for context in iter_route_contexts(app.routes) if isinstance(context.original_route, APIRoute) and _depends_on(context.original_route.dependant, get_writer)}
    assert writer_paths == set(WRITE_REQUESTS)


@pytest.mark.parametrize("template", sorted(WRITE_REQUESTS))
async def test_revoke_before_the_write_refuses_every_writer_route(
    api_app: FastAPI, client: httpx.AsyncClient, funded: AsyncEngine, monkeypatch: pytest.MonkeyPatch, guild_info: InMemoryGuildInfo, template: str
) -> None:
    await create_currency(funded, currency_id=CUR2, symbol="APIU")
    guild_info.add_guild(GUILD_ID, ALICE)
    guild_info.add_guild(CUR, ALICE)
    issued = await issue_token(funded, account_id=ALICE)
    authenticate = tokens.authenticate_token

    async def authenticate_then_revoke(engine: AsyncEngine, token: object) -> int | None:
        # The token passes authentication, then a revoke commits before the endpoint writes, as a concurrent request would.
        account_id = await authenticate(engine, token)
        await revoke_tokens(engine, account_id=ALICE)
        return account_id

    monkeypatch.setattr(auth, "authenticate_token", authenticate_then_revoke)
    async with funded.connect() as conn:
        before = (await conn.execute(select(func.max(execution.c.execution_id)))).scalar_one()
        accounts = (await conn.execute(select(func.count()).select_from(account))).scalar_one()
    path, payload = WRITE_REQUESTS[template]
    headers = {**bearer(issued.token), "Idempotency-Key": f"guarded-{template}"}
    response = await client.post(path, headers=headers) if payload is None else await client.post(path, json=payload, headers=headers)
    _assert_envelope(response, 401, "unauthorized")
    async with funded.connect() as conn:
        recorded = (await conn.execute(select(execution.c.type).where(execution.c.execution_id > before))).scalars().all()
        assert (await conn.execute(select(func.count()).select_from(account))).scalar_one() == accounts
    assert recorded == [ExecutionType.TOKEN_REVOKE]
    assert await balance_of(funded, ALICE, CUR) == 10_000


async def test_write_as_another_account_is_refused(app: FastAPI, client: httpx.AsyncClient, funded: AsyncEngine, auth_headers: AuthHeaders) -> None:
    await mint(funded, CUR, BOB, 100)
    payload = {"recipient_id": str(BOB), "currency_id": str(CUR), "amount": "0.05"}
    response = await client.post("/v1/_test/transfer-as-recipient", json=payload, headers=await auth_headers(ALICE))
    _assert_envelope(response, 401, "unauthorized")
    assert await _executions(funded, ExecutionType.TRANSFER) == 0
    assert await balance_of(funded, BOB, CUR) == 100


async def test_paging_parameters(app: FastAPI, client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/_test/page")
    assert response.status_code == 200
    assert response.json() == {"items": [{"item_id": "1", "amount": "1.500"}, {"item_id": "2", "amount": "3.000"}], "page": 1, "per_page": 15, "total": 2, "total_is_lower_bound": False}
    assert (await client.get("/v1/_test/page", params={"page": 2, "per_page": 100})).json()["per_page"] == 100
    assert (await client.get("/v1/_test/page", params={"page": 666, "per_page": 15})).status_code == 200
    for params in ({"page": 0}, {"per_page": 0}, {"per_page": 101}, {"page": "x"}, {"page": 667, "per_page": 15}, {"page": 101, "per_page": 100}, {"page": 10**30}):
        error = _assert_envelope(await client.get("/v1/_test/page", params=params), 422, "validation_error")
        assert error["details"], params


async def test_version(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/version")
    assert response.status_code == 200
    assert response.json() == {"name": "RapidWire API", "version": __version__}


async def test_config_reports_settings_and_constants(client: httpx.AsyncClient, api_settings: Settings) -> None:
    response = await client.get("/v1/config")
    assert response.status_code == 200
    config = response.json()
    assert config["amounts"] == {"decimal_places": 3, "minimal_unit": "0.001", "max_amount": "9" * 35 + ".999"}
    assert int(config["amounts"]["max_amount"].replace(".", "")) == AMOUNT_MAX
    assert config["staking"] == {
        "rate_ppm_max": str(RATE_PPM_MAX),
        "rate_percent_per_hour_max": "100.0000",
        "rate_change_timelock_seconds": int(RATE_CHANGE_TIMELOCK.total_seconds()),
        "delete_timelock_seconds": int(DELETE_TIMELOCK.total_seconds()),
        "delete_expiry_seconds": int(DELETE_EXPIRY.total_seconds()),
    }
    assert config["swap"] == {
        "fee_tiers_bps": list(FEE_TIERS_BPS),
        "fee_denominator": FEE_DENOMINATOR,
        "minimum_liquidity": str(MINIMUM_LIQUIDITY),
        "chunk_count": api_settings.swap_chunk_count,
        "max_routes": api_settings.swap_max_routes,
    }
    assert config["gas"] == {
        "currency_id": str(api_settings.gas_currency_id),
        "price": str(api_settings.gas_price),
        "default_gas": str(api_settings.default_gas),
        "max_gas": str(api_settings.max_gas),
    }
    assert config["contract"]["max_size"] == api_settings.contract_max_size
    assert config["paging"]["max_window"] == core_paging.MAX_WINDOW
    assert config["paging"]["limits"]["history"] == {"default": core_paging.HISTORY_PER_PAGE_DEFAULT, "max": core_paging.HISTORY_PER_PAGE_MAX}
    assert config["paging"]["limits"]["balances"] == {"default": core_paging.BALANCES_PER_PAGE_DEFAULT, "max": core_paging.BALANCES_PER_PAGE_MAX}
    assert config["paging"]["limits"]["contract_versions"] == {"default": core_paging.CONTRACT_VERSIONS_PER_PAGE_DEFAULT, "max": core_paging.CONTRACT_VERSIONS_PER_PAGE_MAX}
    assert config["paging"]["limits"]["contract_storage"] == {"default": core_paging.CONTRACT_STORAGE_PER_PAGE_DEFAULT, "max": core_paging.CONTRACT_STORAGE_PER_PAGE_MAX}
    assert config["idempotency_key_max_length"] == IDEMPOTENCY_KEY_MAX
    assert config["max_body_bytes"] == DEFAULT_BODY_LIMIT


async def test_settings_change_the_config(db: AsyncEngine, backup_dir: object) -> None:
    from rapidwire.api.app import create_app
    from rapidwire.core.permissions import InMemoryGuildInfo

    settings = Settings(_env_file=None, db_password="api-test-db-password", gas_price=3, max_gas=1000, default_gas=10, swap_chunk_count=9, contract_max_size=4000)
    app = create_app(settings, engine=db, guild_info=InMemoryGuildInfo())
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        config = (await http.get("/v1/config")).json()
    assert (config["gas"]["price"], config["gas"]["max_gas"], config["gas"]["default_gas"]) == ("3", "1000", "10")
    assert (config["swap"]["chunk_count"], config["contract"]["max_size"]) == (9, 4000)
    async with db.connect() as conn:
        assert (await conn.execute(select(1))).scalar_one() == 1


async def test_state_is_per_app_and_released(db: AsyncEngine, api_settings: Settings) -> None:
    from rapidwire.api.app import create_app
    from rapidwire.core.permissions import InMemoryGuildInfo

    app = create_app(api_settings, engine=db, guild_info=InMemoryGuildInfo())
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        _assert_envelope(await http.get("/v1/config"), 500, "internal_error")
        async with app.router.lifespan_context(app):
            assert (await http.get("/v1/config")).status_code == 200
        _assert_envelope(await http.get("/v1/config"), 500, "internal_error")
