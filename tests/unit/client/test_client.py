"""The client against stub transports: request shapes, error mapping, replay flags, paging, checksum checks and the route registry."""

from __future__ import annotations

import hashlib
import io
import json
import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.routing import APIRoute, iter_route_contexts

from rapidwire.api.app import API_PREFIX, create_app
from rapidwire.config import Settings
from rapidwire_client import (
    AsyncRapidWireClient,
    AuthenticationError,
    ChecksumMismatchError,
    ConflictError,
    NotFoundError,
    Page,
    PermissionDeniedError,
    RapidWireAPIError,
    RapidWireClient,
    ResponseFormatError,
    ServerError,
    ValidationError,
    client_routes,
    new_idempotency_key,
)
from rapidwire_client.client import IDEMPOTENCY_KEY_HEADER, _Client
from rapidwire_client.models import MAX_WINDOW

TOKEN = "unit-test-token-0123456789"
Handler = Callable[[httpx.Request], httpx.Response]


def _app_routes() -> set[tuple[str, str]]:
    app = create_app(Settings(_env_file=None, db_password="password-1234"))
    return {
        (method, str(context.path))
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute) and str(context.path).startswith(API_PREFIX)
        for method in context.methods or ()
    }


@pytest.mark.parametrize("cls", [AsyncRapidWireClient, RapidWireClient])
def test_every_route_has_a_client_method(cls: type) -> None:
    routes = _app_routes()
    assert ("GET", "/v1/version") in routes
    assert sorted(routes - client_routes(cls)) == []
    assert sorted(client_routes(cls) - routes) == []


def _sync(handler: Handler, token: str | None = TOKEN) -> RapidWireClient:
    return RapidWireClient("http://test", token, transport=httpx.MockTransport(handler))


def _envelope(status: int, code: str, headers: dict[str, str] | None = None, **extra: Any) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"error": {"code": code, "message": f"{code} message", **extra}}, headers=headers)

    return handler


@pytest.mark.parametrize(
    ("status", "cls"),
    [(401, AuthenticationError), (403, PermissionDeniedError), (404, NotFoundError), (409, ConflictError), (422, ValidationError), (500, ServerError), (503, ServerError), (405, RapidWireAPIError)],
)
def test_errors_map_to_classes_by_status(status: int, cls: type[RapidWireAPIError]) -> None:
    with _sync(_envelope(status, "some_code", execution_id="42", kind="REVERTED", reason="NO_FUNCTION")) as client, pytest.raises(cls) as caught:
        client.get_version()
    error = caught.value
    assert type(error) is cls
    assert (error.status_code, error.code, error.message, error.execution_id, error.kind, error.reason) == (status, "some_code", "some_code message", 42, "REVERTED", "NO_FUNCTION")
    assert error.replayed is False


def test_validation_details_child_replay_and_retry_after() -> None:
    details = [{"loc": ["body", "amount"], "message": "bad", "type": "amount_format"}]
    child = {"type": "contract_call", "contract_function": "f", "code": "contract_reverted", "message": "m"}
    with _sync(_envelope(422, "validation_error", {"Idempotency-Replayed": "true", "Retry-After": "7"}, details=details, child=child)) as client, pytest.raises(ValidationError) as caught:
        client.transfer(1, 2, "1")
    error = caught.value
    assert error.details is not None
    assert [(detail.loc, detail.message, detail.type) for detail in error.details] == [(("body", "amount"), "bad", "amount_format")]
    assert error.child == child
    assert error.replayed is True
    assert error.retry_after == 7
    assert error.execution_id is None


def test_non_envelope_error_body() -> None:
    with _sync(lambda request: httpx.Response(502, text="<html>bad gateway</html>")) as client, pytest.raises(ServerError) as caught:
        client.get_version()
    assert (caught.value.status_code, caught.value.code, caught.value.message) == (502, "http_error", "Bad Gateway")


def test_malformed_success_body_is_a_format_error() -> None:
    with _sync(lambda request: httpx.Response(200, json={"name": "x"})) as client, pytest.raises(ResponseFormatError, match="version"):
        client.get_version()
    with _sync(lambda request: httpx.Response(200, text="not json")) as client, pytest.raises(ResponseFormatError):
        client.get_version()


def test_token_is_sent_only_where_used_and_never_shown(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/v1/version":
            return httpx.Response(200, json={"name": "RapidWire API", "version": "2.0.0"})
        return httpx.Response(200, json={"items": [], "page": 1, "per_page": 15, "total": 0, "total_is_lower_bound": False})

    with caplog.at_level(logging.DEBUG), _sync(handler) as client:
        client.get_version()
        client.list_tokens()
        assert TOKEN not in repr(client)
        assert TOKEN not in str(client)
        assert repr(client) == "RapidWireClient(base_url='http://test', authenticated=True)"
    assert "authorization" not in seen[0].headers
    assert seen[1].headers["authorization"] == f"Bearer {TOKEN}"
    assert "include_revoked" not in seen[1].url.params
    assert TOKEN not in caplog.text
    assert TOKEN not in repr(seen[1].headers)


def test_write_request_shape_and_replay_flag() -> None:
    seen: list[httpx.Request] = []
    key = new_idempotency_key()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        body = {"execution_id": "9", "transfer_id": "10", "sender_id": "1", "recipient_id": "2", "currency_id": "3", "amount": "1.500"}
        return httpx.Response(200, json=body, headers={"Idempotency-Replayed": "true"} if len(seen) > 1 else {})

    with _sync(handler) as client:
        first = client.transfer(2, 3, "1.5", idempotency_key=key)
        again = client.transfer(2, 3, "1.5", idempotency_key=key)
    assert json.loads(seen[0].content) == {"recipient_id": "2", "currency_id": "3", "amount": "1.500"}
    assert seen[0].headers["idempotency-key"] == key
    assert (first.replayed, again.replayed) == (False, True)
    assert first.execution_id == again.execution_id == 9
    assert str(first.amount) == "1.500"


@pytest.mark.parametrize("key", ["", " lead", "trail ", "x" * 256, "tab\tkey", "\xe9"])
def test_invalid_idempotency_keys_are_refused_before_sending(key: str) -> None:
    with _sync(lambda request: pytest.fail("sent")) as client, pytest.raises(ValueError):
        client.mint(1, "1", idempotency_key=key)


def test_path_and_query_encoding() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "m"}})

    with _sync(handler) as client:
        for call in (
            lambda: client.get_storage(5, "a/b c?d"),
            lambda: client.get_currency_by_symbol("A-B"),
            lambda: client.search_history(user_id=-3, min_amount="1.5", start=datetime.fromisoformat("2026-01-01T00:00:00+09:00"), sort_order="asc"),
        ):
            with pytest.raises(NotFoundError):
                call()
    assert seen[0].url.raw_path == b"/v1/contracts/5/storage/a/b%20c%3Fd"
    assert seen[1].url.raw_path == b"/v1/currencies/symbol/A-B"
    assert dict(seen[2].url.params) == {"user_id": "-3", "min_amount": "1.500", "start": "2026-01-01T00:00:00+09:00", "sort_order": "asc"}


def test_account_kind_parses_nullable_owner_ids() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"account_id": "-7", "kind": "pool_custody", "currency_id": None, "pool_id": "3", "created_at": "2026-10-31T00:00:00Z"})

    with _sync(handler, token=None) as client:
        info = client.get_account(-7)
    assert seen[0].url.raw_path == b"/v1/accounts/-7"
    assert (info.account_id, info.kind, info.currency_id, info.pool_id, info.created_at.isoformat()) == (-7, "pool_custody", None, 3, "2026-10-31T00:00:00+00:00")


def test_arguments_are_checked_before_sending() -> None:
    with _sync(lambda request: pytest.fail("sent")) as client:
        with pytest.raises(TypeError):
            client.transfer(1, 2, 1.5)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            client.get_balance(True, 1)
        with pytest.raises(ValueError):
            client.search_history(start=datetime(2026, 1, 1))
        with pytest.raises(ValueError):
            client.lock_contract(1)


def test_responses_with_added_fields_still_parse() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/version":
            return httpx.Response(200, json={"name": "RapidWire API", "version": "2.0.0", "added": {"nested": [1]}})
        item = {"key": "k", "value": {"type": "int", "value": "1"}, "size": 2, "updated_at": "2026-10-31T00:00:00Z", "added": True}
        return httpx.Response(200, json={"items": [item], "page": 1, "per_page": 15, "total": 1, "total_is_lower_bound": False, "added": "x"})

    with _sync(handler) as client:
        assert client.get_version().version == "2.0.0"
        page = client.list_storage(5)
    assert [(entry.key, entry.value, entry.size) for entry in page.items] == [("k", 1, 2)]


def test_contract_request_shapes() -> None:
    seen: list[httpx.Request] = []
    dry_run = {
        "ok": True,
        "value": {"type": "int", "value": "1"},
        "error": None,
        "gas_used": "3",
        "gas_limit": "128",
        "gas_currency_id": "9",
        "gas_price": "2",
        "gas_cost": "0.006",
        "random": [],
        "random_tree": [],
        "transfers": [],
        "outbox": [],
    }
    lock = {"execution_id": "1", "account_id": "5", "locked_until": "2026-11-01T00:00:00Z", "previous_locked_until": None}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=dry_run if request.url.path.endswith("/dry-run") else lock)

    with _sync(handler) as client:
        result = client.dry_run_contract(5, "f", [1])
        client.lock_contract(5, hours=2, idempotency_key="lock-1")
        with pytest.raises(TypeError):
            client.lock_contract(5, hours=True)
    assert (result.gas_currency_id, result.gas_price, str(result.gas_cost)) == (9, 2, "0.006")
    assert json.loads(seen[0].content) == {"function": "f", "args": [{"type": "int", "value": "1"}]}
    assert seen[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert IDEMPOTENCY_KEY_HEADER.lower() not in seen[0].headers
    assert json.loads(seen[1].content) == {"hours": 2}
    assert seen[1].headers["idempotency-key"] == "lock-1"
    assert len(seen) == 2


def test_backup_names_are_fully_quoted() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(404, json={"error": {"code": "not_found", "message": "m"}})

    with _sync(handler) as client, pytest.raises(NotFoundError):
        client.download_backup("a/b c", io.BytesIO())
    assert seen[0].url.raw_path == b"/v1/admin/backups/a%2Fb%20c"


def _page(page: int, per_page: int, total: int, lower: bool = False) -> Page[int]:
    count = max(0, min(per_page, total - (page - 1) * per_page)) if not lower else per_page
    return Page(items=tuple(range(count)), page=page, per_page=per_page, total=total, total_is_lower_bound=lower)


def test_page_has_next() -> None:
    assert _page(1, 2, 5).has_next
    assert not _page(3, 2, 5).has_next
    assert not _page(2, 2, 4).has_next
    assert _page(1, 100, MAX_WINDOW, lower=True).has_next
    assert not _page(MAX_WINDOW // 100, 100, MAX_WINDOW, lower=True).has_next


def test_sync_paginate_walks_every_page() -> None:
    pages: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page, per_page = int(request.url.params["page"]), int(request.url.params["per_page"])
        pages.append(page)
        items = [{"currency_id": str(n), "symbol": f"S{n}", "name": "N", "amount": "1.000"} for n in range((page - 1) * per_page, min(page * per_page, 5))]
        return httpx.Response(200, json={"items": items, "page": page, "per_page": per_page, "total": 5, "total_is_lower_bound": False})

    with _sync(handler) as client:
        ids = [balance.currency_id for balance in client.paginate(client.list_balances, 1, per_page=2)]
    assert ids == [0, 1, 2, 3, 4]
    assert pages == [1, 2, 3]


def test_backup_list_is_a_page_with_its_execution() -> None:
    names = ["c.gz", "b.gz", "a.gz"]

    def handler(request: httpx.Request) -> httpx.Response:
        page, per_page = int(request.url.params["page"]), int(request.url.params["per_page"])
        items = [{"name": name, "created_at": "2026-01-01T00:00:00Z", "size": 1, "sha256": "0" * 64} for name in names[(page - 1) * per_page : page * per_page]]
        return httpx.Response(200, json={"items": items, "page": page, "per_page": per_page, "total": 3, "total_is_lower_bound": False, "execution_id": str(40 + page)})

    with _sync(handler) as client:
        first = client.list_backups(page=1, per_page=2)
        assert isinstance(first, Page)
        assert ([item.name for item in first.items], first.execution_id, first.has_next) == (["c.gz", "b.gz"], 41, True)
        assert [item.name for item in client.paginate(client.list_backups, per_page=2)] == names


def test_the_shared_client_base_is_abstract() -> None:
    assert "_send" in _Client.__abstractmethods__
    with pytest.raises(TypeError):
        _Client("http://example.invalid", None)  # type: ignore[abstract]


def _backup_handler(data: bytes, digest: str) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=data, headers={"X-Checksum-SHA256": digest, "X-Execution-Id": "77", "Content-Type": "application/gzip"})

    return handler


def test_backup_checksum_mismatch_keeps_no_file(tmp_path: Path) -> None:
    data = b"backup bytes"
    target = tmp_path / "b.gz"
    with _sync(_backup_handler(data, hashlib.sha256(b"other").hexdigest())) as client:
        with pytest.raises(ChecksumMismatchError) as caught:
            client.download_backup("b.gz", target)
        assert caught.value.actual == hashlib.sha256(data).hexdigest()
        assert list(tmp_path.iterdir()) == []
        with client.stream_backup("b.gz") as stream, pytest.raises(ChecksumMismatchError):
            b"".join(stream)
    with _sync(_backup_handler(data, "not-hex")) as client, pytest.raises(ResponseFormatError):
        client.download_backup("b.gz", io.BytesIO())


def test_backup_download_to_file_object() -> None:
    data = b"backup bytes"
    sink = io.BytesIO()
    with _sync(_backup_handler(data, hashlib.sha256(data).hexdigest())) as client:
        result = client.download_backup("b.gz", sink)
    assert sink.getvalue() == data
    assert (result.name, result.size, result.sha256, result.execution_id, result.content_type, result.path) == ("b.gz", len(data), hashlib.sha256(data).hexdigest(), 77, "application/gzip", None)


async def test_async_contract_binary_checksum() -> None:
    data = b"RWC1 bytes"

    def handler(request: httpx.Request) -> httpx.Response:
        digest = hashlib.sha256(data if request.url.path.endswith("/1/binary") else b"x").hexdigest()
        return httpx.Response(200, content=data, headers={"X-Checksum-SHA256": digest, "Content-Disposition": 'attachment; filename="contract-5-v1.rwc"'})

    async with AsyncRapidWireClient("http://test", transport=httpx.MockTransport(handler)) as client:
        binary = await client.download_contract_binary(5, 1)
        assert (binary.data, binary.filename, binary.version) == (data, "contract-5-v1.rwc", 1)
        with pytest.raises(ChecksumMismatchError):
            await client.download_contract_binary(5, 2)
