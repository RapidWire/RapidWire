from __future__ import annotations

from urllib.parse import unquote

import anyio
import httpx
from fastapi import FastAPI
from starlette.types import Message, Scope

from rapidwire.api.app import WEB_DIR, WEB_MEDIA_TYPES, WEB_RESPONSE_HEADERS

NOT_FOUND_PAGE = WEB_DIR / "404.html"
WEB_FILES = sorted(path for path in WEB_DIR.rglob("*") if path.is_file() and path != NOT_FOUND_PAGE)


def _assert_web_headers(response: httpx.Response) -> None:
    for name, value in WEB_RESPONSE_HEADERS.items():
        assert response.headers.get(name) == value, name


async def test_every_web_file_is_served_with_its_media_type(client: httpx.AsyncClient) -> None:
    assert WEB_FILES
    for path in WEB_FILES:
        name = path.relative_to(WEB_DIR).as_posix()
        response = await client.get(f"/{name}")
        assert response.status_code == 200, name
        assert response.headers["content-type"] == WEB_MEDIA_TYPES[path.suffix], name
        assert response.content == path.read_bytes(), name
        _assert_web_headers(response)


async def test_web_files_are_revalidated_on_every_load(client: httpx.AsyncClient) -> None:
    response = await client.get("/assets/app.js")
    assert response.headers["cache-control"] == "no-cache"
    etag = response.headers["etag"]
    revalidated = await client.get("/assets/app.js", headers={"If-None-Match": etag})
    assert revalidated.status_code == 304
    _assert_web_headers(revalidated)


async def test_root_serves_the_landing_page(client: httpx.AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"] == "text/html; charset=utf-8"
    assert response.content == (WEB_DIR / "index.html").read_bytes()
    _assert_web_headers(response)


async def test_pages_are_reached_by_their_file_names(client: httpx.AsyncClient) -> None:
    response = await client.get("/explorer.html")
    assert response.status_code == 200
    assert response.content == (WEB_DIR / "explorer.html").read_bytes()
    # StaticFiles maps a path to a file name as it is, so the name without its suffix is a missing page.
    assert (await client.get("/explorer")).status_code == 404


async def test_a_missing_page_is_the_not_found_page(client: httpx.AsyncClient) -> None:
    for path in ("/nope.html", "/assets/nope.js", "/assets", "/favicon.ico", "/docs/oauth2-redirect"):
        response = await client.get(path)
        assert response.status_code == 404, path
        assert response.headers["content-type"] == "text/html; charset=utf-8", path
        assert response.content == NOT_FOUND_PAGE.read_bytes(), path
        _assert_web_headers(response)


async def test_the_not_found_page_is_not_found_at_its_own_path(client: httpx.AsyncClient) -> None:
    assert NOT_FOUND_PAGE.is_file()
    for method in ("GET", "HEAD"):
        missing = await client.request(method, "/nope.html")
        direct = await client.request(method, "/404.html")
        assert direct.status_code == missing.status_code == 404, method
        assert direct.content == missing.content, method
        assert {name: value for name, value in direct.headers.items() if name != "date"} == {name: value for name, value in missing.headers.items() if name != "date"}, method
        assert direct.headers["content-type"] == "text/html; charset=utf-8", method
        _assert_web_headers(direct)
    assert (await client.get("/404.html")).content == NOT_FOUND_PAGE.read_bytes()


NON_CANONICAL_PATHS = (
    "//evil.example/..%2fexplorer.html",
    "//explorer.html",
    "/explorer.html/",
    "/assets/../index.html",
    "/./explorer.html",
    "/%2e%2e/index.html",
    "/%2E/explorer.html",
    "/assets%2fapp.js",
    "/assets%2Fapp.js",
    "/assets//app.js",
    "/%5cevil.example/explorer.html",
    "/\\evil.example/explorer.html",
    "/explorer%2ehtml",
    "/.hidden",
    "/index.html/",
)


async def _send_raw(app: FastAPI, method: str, raw_path: str, *, root_path: str = "") -> httpx.Response:
    """Call the app with the scope a server builds for a request target sent as written: path is the percent-decoded target and raw_path its bytes, with no dot segment or slash normalised away as httpx would; root_path is the ASGI root_path, which the target then starts with."""
    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": unquote(raw_path),
        "raw_path": raw_path.encode("latin-1"),
        "query_string": b"",
        "root_path": root_path,
        "headers": [(b"host", b"test")],
        "client": ("127.0.0.1", 50000),
        "server": ("test", 80),
    }
    status = 0
    headers: list[tuple[bytes, bytes]] = []
    body = bytearray()
    requested = False
    finished = anyio.Event()

    async def receive() -> Message:
        nonlocal requested
        if not requested:
            requested = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await finished.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        nonlocal status, headers
        if message["type"] == "http.response.start":
            status = message["status"]
            headers = list(message.get("headers", []))
        elif message["type"] == "http.response.body":
            body.extend(message.get("body", b""))
            if not message.get("more_body", False):
                finished.set()

    await app(scope, receive, send)
    return httpx.Response(status, headers=headers, content=bytes(body))


async def test_non_canonical_paths_are_the_not_found_page(api_app: FastAPI) -> None:
    for path in NON_CANONICAL_PATHS:
        for method in ("GET", "HEAD"):
            response = await _send_raw(api_app, method, path)
            assert response.status_code == 404, (method, path)
            assert response.headers["content-type"] == "text/html; charset=utf-8", (method, path)
            if method == "GET":
                assert response.content == NOT_FOUND_PAGE.read_bytes(), path
            _assert_web_headers(response)


async def test_canonical_paths_are_still_served(api_app: FastAPI) -> None:
    for path in ("/", "/explorer.html", "/assets/app.js", "/assets/app.css", "/assets/404.js"):
        response = await _send_raw(api_app, "GET", path)
        assert response.status_code == 200, path
        assert response.content == (WEB_DIR / (path.lstrip("/") or "index.html")).read_bytes(), path
        _assert_web_headers(response)


async def test_non_canonical_api_paths_keep_the_api_routes(api_app: FastAPI) -> None:
    response = await _send_raw(api_app, "GET", "/v1/version")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    response = await _send_raw(api_app, "GET", "/v1/../v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_api_paths_below_a_root_path_keep_the_api_routes(api_app: FastAPI) -> None:
    response = await _send_raw(api_app, "GET", "/prefix/v1/version", root_path="/prefix")
    assert response.status_code == 200
    assert "content-security-policy" not in response.headers
    for path in ("/prefix/v1/nope", "/prefix/v1"):
        response = await _send_raw(api_app, "GET", path, root_path="/prefix")
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "not_found", path
        assert "content-security-policy" not in response.headers, path
    response = await _send_raw(api_app, "GET", "/prefix/explorer.html", root_path="/prefix")
    assert response.status_code == 200
    assert response.content == (WEB_DIR / "explorer.html").read_bytes()
    _assert_web_headers(response)
    response = await _send_raw(api_app, "GET", "/prefix/404.html", root_path="/prefix")
    assert response.status_code == 404
    assert response.content == NOT_FOUND_PAGE.read_bytes()
    _assert_web_headers(response)


async def test_api_responses_carry_no_web_headers(client: httpx.AsyncClient) -> None:
    for path in ("/v1/version", "/docs", "/openapi.json"):
        response = await client.get(path)
        assert response.status_code == 200, path
        for name in WEB_RESPONSE_HEADERS:
            assert name not in response.headers, (path, name)


async def test_the_docs_page_has_no_oauth_redirect(client: httpx.AsyncClient) -> None:
    response = await client.get("/docs")
    assert response.status_code == 200
    assert "oauth2-redirect" not in response.text


async def test_unknown_api_paths_keep_the_json_envelope(client: httpx.AsyncClient) -> None:
    response = await client.get("/v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    assert "content-security-policy" not in response.headers
    response = await client.get("/v1")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_web_files_refuse_writes(client: httpx.AsyncClient) -> None:
    for method in ("POST", "PUT", "DELETE", "OPTIONS"):
        for path in ("/", "/index.html", "/nope.html"):
            response = await client.request(method, path)
            assert response.status_code == 405, (method, path)
            assert response.headers["allow"] == "GET, HEAD", (method, path)
            assert response.json()["error"]["code"] == "method_not_allowed", (method, path)
            _assert_web_headers(response)
