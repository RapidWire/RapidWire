from __future__ import annotations

import re
from pathlib import Path

from fastapi.routing import APIRoute, iter_route_contexts

from rapidwire.api.app import API_PREFIX, create_app
from rapidwire.api.body_limit import PAYLOAD_TOO_LARGE_CODE
from rapidwire.api.errors import INTERNAL_ERROR_CODE, STATUS_BY_ERROR, VALIDATION_ERROR_CODE, status_for
from rapidwire.api.routers.accounts import DISCORD_UNAVAILABLE_CODE
from rapidwire.config import Settings
from rapidwire.core.errors import DomainError
from tests.unit.doc_text import character_violations

API_DOC = Path(__file__).resolve().parents[3] / "document" / "api.md"
ROUTE_MENTION = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/v1[^`\s]*)`")
BACKTICKED = re.compile(r"`([a-z_]+)`")
ROUTER_SECTIONS = ("## 勘定と履歴", "## 台帳への書き込み", "## DEX", "## コントラクト", "## 運営者用バックアップ")
DOMAIN_ERROR_TABLE = "| エラークラス | ステータス | コード |"
OTHER_ERROR_TABLE = "| ステータス | コード | 発生条件 |"


def _app_routes() -> set[tuple[str, str]]:
    app = create_app(Settings(_env_file=None, db_password="password-1234"))
    # app.routes holds included routers as nested entries; iter_route_contexts() yields each route with its prefixed path.
    routes = {
        (method, str(context.path))
        for context in iter_route_contexts(app.routes)
        if isinstance(context.original_route, APIRoute) and str(context.path).startswith(API_PREFIX)
        for method in context.methods or ()
    }
    assert ("GET", "/v1/version") in routes
    return routes


def _documented_routes() -> set[tuple[str, str]]:
    return {(match.group(1), match.group(2)) for match in ROUTE_MENTION.finditer(API_DOC.read_text(encoding="utf-8"))}


def test_every_route_is_documented() -> None:
    assert sorted(_app_routes() - _documented_routes()) == []


def test_every_documented_route_exists() -> None:
    assert sorted(_documented_routes() - _app_routes()) == []


def test_router_sections_exist() -> None:
    lines = API_DOC.read_text(encoding="utf-8").splitlines()
    for heading in ROUTER_SECTIONS:
        assert heading in lines


def test_status_table_lists_every_mapped_error() -> None:
    text = API_DOC.read_text(encoding="utf-8")
    for cls, status in STATUS_BY_ERROR.items():
        assert f"| `{cls.__name__}` | {status} |" in text, cls.__name__


def _table_rows(header: str) -> list[list[str]]:
    lines = API_DOC.read_text(encoding="utf-8").splitlines()
    start = lines.index(header) + 2
    rows: list[list[str]] = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append([cell.strip() for cell in line.strip("|").split("|")])
    return rows


def _domain_error_classes() -> set[type[DomainError]]:
    # Building the app imports every module that defines an error class.
    _app_routes()
    found: set[type[DomainError]] = set()
    pending: list[type[DomainError]] = [DomainError]
    while pending:
        for sub in pending.pop().__subclasses__():
            if sub not in found:
                found.add(sub)
                pending.append(sub)
    return found


def test_every_domain_error_code_is_in_the_row_of_its_status() -> None:
    documented: dict[str, int] = {}
    for _, status, codes in _table_rows(DOMAIN_ERROR_TABLE):
        for code in BACKTICKED.findall(codes):
            documented[code] = int(status)
    classes = [cls for cls in _domain_error_classes() if "code" in cls.__dict__]
    assert len(classes) > 35
    for cls in classes:
        assert documented.get(cls.code) == status_for(cls()), cls.__qualname__


def test_other_error_codes_are_documented() -> None:
    documented = {BACKTICKED.findall(code)[0]: int(status) for status, code, _ in _table_rows(OTHER_ERROR_TABLE)}
    assert documented == {
        "bad_request": 400,
        "not_found": 404,
        "method_not_allowed": 405,
        PAYLOAD_TOO_LARGE_CODE: 413,
        VALIDATION_ERROR_CODE: 422,
        INTERNAL_ERROR_CODE: 500,
        DISCORD_UNAVAILABLE_CODE: 503,
    }


def test_openapi_schema_names_are_plain_model_names() -> None:
    # FastAPI qualifies a schema name with its module path when two models share a class name.
    schemas = create_app(Settings(_env_file=None, db_password="password-1234")).openapi()["components"]["schemas"]
    assert "ErrorResponse" in schemas
    assert sorted(name for name in schemas if "__" in name) == []


def test_doc_is_ascii_outside_japanese_text() -> None:
    assert character_violations(API_DOC) == []
