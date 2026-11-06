"""The request body cap: Content-Length and streamed bodies over the limit get 413 payload_too_large before any route sees them, normal bodies pass, and a route can declare its own limit."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator

import httpx
import pytest
from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel

from rapidwire.api.app import create_app
from rapidwire.api.body_limit import DEFAULT_BODY_LIMIT, BodyLimitMiddleware, body_limit, contract_call_body_limit, route_body_limits
from rapidwire.api.errors import install_error_handlers
from rapidwire.config import Settings
from rapidwire.contract.limits import DEFAULT_LIMITS
from rapidwire.db.tables import CONTRACT_FUNCTION_MAX

SMALL = 200
LARGE = 2000
SETTINGS = Settings(_env_file=None, db_password="password-1234")


class Note(BaseModel):
    text: str


def _app() -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    install_error_handlers(app)
    seen: list[str] = []

    @app.post("/note")
    async def note(body: Note) -> dict[str, int]:
        seen.append("note")
        return {"length": len(body.text)}

    @app.post("/upload")
    @body_limit(lambda settings: LARGE)
    async def upload(request: Request) -> dict[str, int]:
        body = await request.body()
        seen.append("upload")
        return {"length": len(body)}

    @app.post("/fixed")
    @body_limit(LARGE // 2)
    async def fixed(body: Note) -> dict[str, int]:
        return {"length": len(body.text)}

    app.add_middleware(BodyLimitMiddleware, default=SMALL, limits=route_body_limits(app.routes, SETTINGS))
    return app, seen


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _note(size: int) -> bytes:
    overhead = len(json.dumps({"text": ""}))
    return json.dumps({"text": "x" * (size - overhead)}).encode()


async def _chunks(data: bytes, size: int = 64) -> AsyncIterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _assert_too_large(response: httpx.Response, limit: int) -> None:
    assert response.status_code == 413, response.text
    assert response.json() == {"error": {"code": "payload_too_large", "message": f"The request body is larger than the {limit} bytes this endpoint accepts."}}


async def test_bodies_within_the_limit_pass() -> None:
    app, seen = _app()
    async with _client(app) as client:
        exact = await client.post("/note", content=_note(SMALL), headers={"Content-Type": "application/json"})
        assert exact.status_code == 200, exact.text
        streamed = await client.post("/note", content=_chunks(_note(SMALL)), headers={"Content-Type": "application/json"})
        assert streamed.status_code == 200, streamed.text
    assert seen == ["note", "note"]


async def test_content_length_over_the_limit_is_refused_before_the_app() -> None:
    app, seen = _app()
    async with _client(app) as client:
        response = await client.post("/note", content=_note(SMALL + 1), headers={"Content-Type": "application/json"})
    _assert_too_large(response, SMALL)
    assert response.headers["connection"] == "close"
    assert seen == []


async def test_streamed_body_over_the_limit_is_refused_while_reading() -> None:
    app, seen = _app()
    async with _client(app) as client:
        response = await client.post("/note", content=_chunks(_note(SMALL * 10)), headers={"Content-Type": "application/json"})
        assert "content-length" not in response.request.headers
        _assert_too_large(response, SMALL)
        raw = await client.post("/upload", content=_chunks(b"y" * (LARGE + 1)))
        _assert_too_large(raw, LARGE)
    assert seen == []


async def test_a_route_can_declare_a_larger_limit() -> None:
    app, seen = _app()
    async with _client(app) as client:
        allowed = await client.post("/upload", content=b"y" * LARGE)
        assert allowed.status_code == 200
        assert allowed.json() == {"length": LARGE}
        streamed = await client.post("/upload", content=_chunks(b"y" * LARGE))
        assert streamed.json() == {"length": LARGE}
        _assert_too_large(await client.post("/upload", content=b"y" * (LARGE + 1)), LARGE)
        fixed = await client.post("/fixed", content=_note(LARGE // 2), headers={"Content-Type": "application/json"})
        assert fixed.status_code == 200
        _assert_too_large(await client.post("/fixed", content=_note(LARGE // 2 + 1), headers={"Content-Type": "application/json"}), LARGE // 2)
        _assert_too_large(await client.post("/note", content=_note(SMALL + 1), headers={"Content-Type": "application/json"}), SMALL)
    assert seen == ["upload", "upload"]


async def test_deeply_nested_json_is_a_client_error() -> None:
    app, _ = _app()
    depth = SMALL // 2 - 1
    async with _client(app) as client:
        response = await client.post("/note", content=b"[" * depth + b"]" * depth, headers={"Content-Type": "application/json"})
    assert 400 <= response.status_code < 500
    assert set(response.json()) == {"error"}


async def test_a_route_limit_follows_the_include_prefix() -> None:
    router = APIRouter(prefix="/files")

    @router.post("/{name}")
    @body_limit(LARGE)
    async def store(name: str, request: Request) -> dict[str, int]:
        return {"length": len(await request.body())}

    @router.get("/{name}")
    async def read(name: str) -> dict[str, str]:
        return {"name": name}

    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router, prefix="/outer")
    limits = route_body_limits(app.routes, SETTINGS)
    assert [(entry.path, entry.methods, entry.limit) for entry in limits] == [("/outer/files/{name}", frozenset({"POST"}), LARGE)]
    app.add_middleware(BodyLimitMiddleware, default=SMALL, limits=limits)
    async with _client(app) as client:
        allowed = await client.post("/outer/files/a", content=b"y" * LARGE)
        assert allowed.status_code == 200, allowed.text
        assert allowed.json() == {"length": LARGE}
        _assert_too_large(await client.post("/outer/files/a", content=b"y" * (LARGE + 1)), LARGE)
        # The unprefixed path and other methods keep the default.
        _assert_too_large(await client.post("/files/a", content=b"y" * (SMALL + 1)), SMALL)
        _assert_too_large(await client.request("GET", "/outer/files/a", content=b"y" * (SMALL + 1)), SMALL)


def test_declared_limits_must_be_positive() -> None:
    with pytest.raises(ValueError, match="positive"):
        body_limit(0)

    app = FastAPI()

    @app.post("/bad")
    @body_limit(lambda settings: 0)
    async def bad() -> None:
        return None

    with pytest.raises(ValueError, match="/bad"):
        route_body_limits(app.routes, SETTINGS)


def test_the_default_fits_the_largest_contract_call() -> None:
    # Every argument is the longest str, each character a code point above U+FFFF that json.dumps writes as two escapes.
    longest = chr(0x1F600) * DEFAULT_LIMITS.str_max_length
    argument = {"type": "str", "value": longest}
    body = {
        "function": chr(0x3042) * CONTRACT_FUNCTION_MAX,
        "args": [argument] * DEFAULT_LIMITS.max_params,
        "attach": {"currency_id": str(2**63 - 1), "amount": "9" * 35 + ".999"},
        "gas_limit": str(500_000),
    }
    encoded = json.dumps(body, indent=2).encode()
    assert len(encoded) <= DEFAULT_BODY_LIMIT == contract_call_body_limit()
    # One more argument of that size does not fit, so the default is not far above the largest call.
    assert len(encoded) + len(json.dumps(argument)) > DEFAULT_BODY_LIMIT


async def test_the_api_refuses_oversized_bodies_without_reading_them() -> None:
    app = create_app(SETTINGS)
    async with _client(app) as client:
        declared = await client.post("/v1/transfers", content=b"{" + b" " * DEFAULT_BODY_LIMIT + b"}", headers={"Content-Type": "application/json"})
        _assert_too_large(declared, DEFAULT_BODY_LIMIT)
        streamed = await client.post("/v1/contracts/5/call", content=_chunks(b" " * (DEFAULT_BODY_LIMIT + 1), size=65536), headers={"Content-Type": "application/json"})
        _assert_too_large(streamed, DEFAULT_BODY_LIMIT)
