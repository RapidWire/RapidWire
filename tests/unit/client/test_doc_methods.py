"""The method table of client/README.md against the client's endpoint definitions, both ways."""

from __future__ import annotations

import re
from pathlib import Path

import httpx

from rapidwire_client import AsyncRapidWireClient, RapidWireClient
from rapidwire_client.client import _ROUTE_ATTR, _Endpoint
from tests.unit.doc_text import character_violations

CLIENT_README = Path(__file__).resolve().parents[3] / "client" / "README.md"
TABLE_HEADER = "| メソッド | エンドポイント | トークン |"
TOKEN_SENT = "あり"
TOKEN_NOT_SENT = "なし"
METHOD_NAME = re.compile(r"`([a-z_]+)`")
ENDPOINT = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/v1[^`\s]*)`")
TOKEN = "doc-test-token-0123456789"

type Row = tuple[str, str, str, bool]


def _hand_written_sends_token() -> bool:
    # The hand-written methods are the backup downloads, which build their request through _backup_request().
    with RapidWireClient("http://test", TOKEN, transport=httpx.MockTransport(lambda request: httpx.Response(500))) as client:
        request = client._backup_request(client._http, "b.gz")
    sent: str | None = request.headers.get("authorization")
    return sent == f"Bearer {TOKEN}"


def _client_rows(cls: type) -> set[Row]:
    rows: set[Row] = set()
    hand_written_auth = _hand_written_sends_token()
    for klass in cls.__mro__:
        for name, value in vars(klass).items():
            if isinstance(value, _Endpoint):
                rows.add((name, value.method, value.path, value.auth))
            elif (marked := getattr(value, _ROUTE_ATTR, None)) is not None:
                method, path = marked
                rows.add((name, method, path, hand_written_auth))
    return rows


def _documented_rows() -> set[Row]:
    lines = CLIENT_README.read_text(encoding="utf-8").splitlines()
    start = lines.index(TABLE_HEADER) + 2
    rows: set[Row] = set()
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        names, endpoint, token = (cell.strip() for cell in line.strip("|").split("|"))
        match = ENDPOINT.fullmatch(endpoint)
        assert match is not None, line
        assert token in (TOKEN_SENT, TOKEN_NOT_SENT), line
        found = METHOD_NAME.findall(names)
        assert found, line
        for name in found:
            rows.add((name, match.group(1), match.group(2), token == TOKEN_SENT))
    return rows


def test_every_client_method_is_documented() -> None:
    assert sorted(_client_rows(RapidWireClient) - _documented_rows()) == []


def test_every_documented_method_exists() -> None:
    assert sorted(_documented_rows() - _client_rows(RapidWireClient)) == []


def test_both_clients_define_the_same_methods() -> None:
    assert _client_rows(AsyncRapidWireClient) == _client_rows(RapidWireClient)


def test_doc_is_ascii_outside_japanese_text() -> None:
    assert character_violations(CLIENT_README) == []
