"""Static checks of every file of the web explorer: no browser storage, no HTML injection sinks, CSP-compatible pages with working links, API calls only to existing GET routes, and no embedded secrets."""

from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from fastapi.routing import APIRoute, iter_route_contexts
from starlette.routing import compile_path

from rapidwire.api.app import API_PREFIX, WEB_DIR, WEB_MEDIA_TYPES, create_app
from rapidwire.config import Settings
from rapidwire.core.types import ExecutionType

FILES = sorted(path for path in WEB_DIR.rglob("*") if path.is_file())
PAGES = [path for path in FILES if path.suffix == ".html"]
SCRIPTS = [path for path in FILES if path.suffix == ".js"]
APP_JS = WEB_DIR / "assets" / "app.js"
EXTERNAL_LINKS = frozenset({"https://discord.gg/tBtMQYEYTb", "https://github.com/rapidwire/"})
# Paths on the web origin that the API process serves itself rather than the web directory.
SERVER_PATHS = frozenset({"/docs", "/openapi.json"})
URL_ATTRIBUTES = frozenset({"href", "src", "action", "poster", "srcset", "formaction", "data", "xlink:href"})
# The empty icon every page declares so that browsers do not request /favicon.ico.
EMPTY_ICON = "data:,"

STORAGE_PATTERNS = (
    r"\blocalStorage\b",
    r"\bsessionStorage\b",
    r"document\s*\.\s*cookie",
    r"\bcookieStore\b",
    r"\bindexedDB\b",
    r"\bcaches\s*\.",
    r"\bCacheStorage\b",
    r"navigator\s*\.\s*serviceWorker",
    r"navigator\s*\.\s*storage\b",
    r"navigator\s*\.\s*credentials\b",
    r"window\s*\.\s*name\b",
)
STORAGE_APIS = [re.compile(pattern) for pattern in STORAGE_PATTERNS]
SINK_PATTERNS = (r"\binnerHTML\b", r"\bouterHTML\b", r"\binsertAdjacentHTML\b", r"document\s*\.\s*write", r"\beval\s*\(", r"\bnew\s+Function\b", r"\bset(?:Timeout|Interval)\s*\(\s*[\"'`]")
HTML_SINKS = [re.compile(pattern) for pattern in SINK_PATTERNS]
API_LITERALS = (re.compile(r'"(/v1/[^"\n]*)"'), re.compile(r"'(/v1/[^'\n]*)'"), re.compile(r"`(/v1/[^`]*)`"))
TEMPLATE_SUBSTITUTION = re.compile(r"\$\{[^}]*\}")
AUTHORIZATION = re.compile(r"\bAuthorization\b")
AUTHORIZATION_FROM_VARIABLE = re.compile(r"Authorization[\"']?\s*:\s*`Bearer \$\{[A-Za-z_$][\w$]*\}`")
LITERAL_BEARER = re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}")
LONG_HEX = re.compile(r"[0-9A-Fa-f]{32,}")
LONG_BASE64 = re.compile(r"[A-Za-z0-9+/_-]{40,}={0,2}")
# A Discord bot token: three dot-separated base64url parts.
DISCORD_BOT_TOKEN = re.compile(r"[A-Za-z0-9_-]{23,28}\.[A-Za-z0-9_-]{6,7}\.[A-Za-z0-9_-]{27,}")
# The generic API wrappers of app.js, whose bodies pass their callers' paths on; every other call names its API path as a literal.
API_WRAPPERS = ("apiFetch", "api", "apiOrNull", "loadPagedTable")
API_CALL = re.compile(r"(?<![\w$.])(?:apiFetch|apiOrNull|api)\(\s*")
PAGED_PATH = re.compile(r"(?<![\w$])path:\s*")
API_PATH_LITERAL = re.compile(r"""(["'`])/v1/""")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _name(path: Path) -> str:
    return path.relative_to(WEB_DIR).as_posix()


class _Page(HTMLParser):
    """Collects what the page checks need: tags with their attributes, script and style contents, the doctype and the title."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.doctype: str | None = None
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.script_text: list[str] = []
        self.title = ""
        self._open: list[str] = []

    def handle_decl(self, decl: str) -> None:
        self.doctype = decl

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))
        self._open.append(tag)
        if tag == "script":
            self.script_text.append("")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag: str) -> None:
        if tag in self._open:
            while self._open.pop() != tag:
                pass

    def handle_data(self, data: str) -> None:
        if not self._open:
            return
        if self._open[-1] == "script":
            self.script_text[-1] += data
        elif self._open[-1] == "title":
            self.title += data

    def find(self, tag: str) -> list[dict[str, str | None]]:
        return [attrs for name, attrs in self.tags if name == tag]


def _parse(path: Path) -> _Page:
    page = _Page()
    page.feed(_text(path))
    page.close()
    return page


def test_the_web_directory_has_the_shared_assets_and_known_file_types() -> None:
    names = {_name(path) for path in FILES}
    assert {"index.html", "explorer.html", "assets/app.css", "assets/app.js"} <= names
    assert sorted(_name(path) for path in FILES if path.suffix not in WEB_MEDIA_TYPES) == []


@pytest.mark.parametrize("path", FILES, ids=_name)
def test_no_browser_storage(path: Path) -> None:
    text = _text(path)
    assert [pattern.pattern for pattern in STORAGE_APIS if pattern.search(text)] == []


@pytest.mark.parametrize("path", SCRIPTS, ids=_name)
def test_scripts_use_no_html_sinks(path: Path) -> None:
    text = _text(path)
    assert [pattern.pattern for pattern in HTML_SINKS if pattern.search(text)] == []


@pytest.mark.parametrize("path", PAGES, ids=_name)
def test_page_head(path: Path) -> None:
    page = _parse(path)
    assert page.doctype is not None
    assert page.doctype.lower() == "doctype html"
    assert _text(path).lstrip().lower().startswith("<!doctype html>")
    html = page.find("html")
    assert len(html) == 1
    assert html[0].get("lang")
    assert any((meta.get("charset") or "").lower() == "utf-8" for meta in page.find("meta"))
    assert any(meta.get("name") == "viewport" and "width=device-width" in (meta.get("content") or "") for meta in page.find("meta"))
    assert any(meta.get("name") == "color-scheme" and meta.get("content") == "light dark" for meta in page.find("meta"))
    assert page.title.strip() != ""
    assert any(link.get("rel") == "stylesheet" and link.get("href") == "/assets/app.css" for link in page.find("link"))
    assert [link.get("href") for link in page.find("link") if link.get("rel") == "icon"] == [EMPTY_ICON]


@pytest.mark.parametrize("path", PAGES, ids=_name)
def test_page_is_compatible_with_the_content_security_policy(path: Path) -> None:
    page = _parse(path)
    assert page.find("style") == []
    assert [text for text in page.script_text if text.strip()] == []
    for script in page.find("script"):
        assert script.get("type") == "module"
        assert re.fullmatch(r"/assets/[a-z0-9-]+\.js", script.get("src") or "")
    sources = [script.get("src") for script in page.find("script")]
    assert f"/assets/{path.stem}.js" in sources
    for tag, attrs in page.tags:
        assert [name for name in attrs if name.startswith("on")] == [], tag
        assert "style" not in attrs, tag


@pytest.mark.parametrize("path", PAGES, ids=_name)
def test_page_links_resolve(path: Path) -> None:
    page = _parse(path)
    ids = {attrs["id"] for _, attrs in page.tags if attrs.get("id")}
    broken: list[str] = []
    for tag, attrs in page.tags:
        for name, value in attrs.items():
            if name not in URL_ATTRIBUTES or value is None:
                continue
            parts = urlsplit(value)
            if tag == "link" and attrs.get("rel") == "icon" and value == EMPTY_ICON:
                continue
            if parts.scheme or value.startswith("//"):
                if not (tag == "a" and name == "href" and value in EXTERNAL_LINKS):
                    broken.append(f"<{tag} {name}={value}>: external URL not allowed")
                continue
            if value.startswith("#"):
                if value[1:] not in ids:
                    broken.append(f"<{tag} {name}={value}>: no element with that id")
                continue
            if not parts.path.startswith("/"):
                broken.append(f"<{tag} {name}={value}>: local URLs are absolute paths")
                continue
            if parts.path in SERVER_PATHS:
                continue
            target = WEB_DIR / ("index.html" if parts.path == "/" else parts.path.lstrip("/"))
            if not target.is_file():
                broken.append(f"<{tag} {name}={value}>: no such file in the web directory")
    assert broken == []


def _get_route_patterns() -> list[re.Pattern[str]]:
    app = create_app(Settings(_env_file=None, db_password="password-1234"))
    patterns: list[re.Pattern[str]] = []
    for context in iter_route_contexts(app.routes):
        path = str(context.path)
        if isinstance(context.original_route, APIRoute) and path.startswith(API_PREFIX) and "GET" in (context.methods or ()):
            patterns.append(compile_path(path)[0])
    assert len(patterns) > 20
    return patterns


def _api_paths() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for path in SCRIPTS:
        text = _text(path)
        for pattern in API_LITERALS:
            for match in pattern.finditer(text):
                literal = TEMPLATE_SUBSTITUTION.sub("1", match.group(1))
                found.setdefault(literal.split("?", 1)[0].split("#", 1)[0], set()).add(_name(path))
    return found


def test_scripts_call_only_existing_get_routes() -> None:
    paths = _api_paths()
    assert paths, "no API path literal found in the scripts"
    patterns = _get_route_patterns()
    unknown = {path: sorted(files) for path, files in paths.items() if not any(pattern.fullmatch(path) for pattern in patterns)}
    assert unknown == {}


def test_the_path_scan_sees_template_literals() -> None:
    assert "/v1/executions/1" in _api_paths()


def _without_wrappers(text: str) -> str:
    """app.js without the bodies of the generic API wrappers."""
    for name in API_WRAPPERS:
        match = re.search(rf"^export (?:async )?function {name}\(.*?^\}}$", text, re.MULTILINE | re.DOTALL)
        assert match is not None, name
        text = text[: match.start()] + text[match.end() :]
    return text


def _api_arguments(text: str) -> list[str]:
    """The text that follows each API call's opening parenthesis and each paged table's path key."""
    return [text[match.end() : match.end() + 40] for pattern in (API_CALL, PAGED_PATH) for match in pattern.finditer(text)]


def test_the_argument_scan_flags_paths_built_elsewhere() -> None:
    flagged = [argument for argument in _api_arguments("api(path); apiOrNull(`/v1/x/${id}`); loadPagedTable({ path: base }); x.api(y)") if not API_PATH_LITERAL.match(argument)]
    assert [argument[:4] for argument in flagged] == ["path", "base"]


def test_every_api_call_names_its_path_as_a_literal() -> None:
    arguments: dict[str, list[str]] = {}
    for path in SCRIPTS:
        text = _text(path)
        arguments[_name(path)] = _api_arguments(_without_wrappers(text) if path == APP_JS else text)
    assert sum(len(found) for found in arguments.values()) > 20
    built = {name: [argument for argument in found if not API_PATH_LITERAL.match(argument)] for name, found in arguments.items()}
    assert {name: found for name, found in built.items() if found} == {}


def test_execution_types_match_the_ledger() -> None:
    match = re.search(r"export const EXECUTION_TYPES = Object\.freeze\(\[(.*?)\]\);", _text(APP_JS), re.DOTALL)
    assert match is not None
    assert re.findall(r'"([a-z_]+)"', match.group(1)) == [member.value for member in ExecutionType]


@pytest.mark.parametrize("path", FILES, ids=_name)
def test_no_embedded_secrets(path: Path) -> None:
    text = _text(path)
    assert "RAPIDWIRE_" not in text
    assert LITERAL_BEARER.findall(text) == []
    assert LONG_HEX.findall(text) == []
    assert LONG_BASE64.findall(text) == []
    assert DISCORD_BOT_TOKEN.findall(text) == []


def test_the_secret_scan_recognises_a_discord_bot_token() -> None:
    sample = ".".join(("M" + "A" * 25, "G" + "a" * 5, "x" * 38))
    assert DISCORD_BOT_TOKEN.findall(f'const value = "{sample}";') == [sample]


@pytest.mark.parametrize("path", SCRIPTS, ids=_name)
def test_authorization_is_only_sent_from_a_variable(path: Path) -> None:
    text = _text(path)
    uses = len(AUTHORIZATION.findall(text))
    assert len(AUTHORIZATION_FROM_VARIABLE.findall(text)) == uses


LOCATION_URL = re.compile(r"\blocation\s*\.\s*(?:pathname|href)\b|\bdocument\s*\.\s*(?:URL|baseURI)\b")
# The only reads of the page's own path or URL: withParams() uses the path behind the same-origin guard, and the 404 page shows it as text.
ALLOWED_LOCATION_LINES = frozenset(
    {
        ("assets/app.js", 'const path = SAME_ORIGIN_PATH_PATTERN.test(window.location.pathname) ? window.location.pathname : "";'),
        ("assets/404.js", 'mount("missing-path", "404 ", h("code", null, window.location.pathname));'),
    }
)


def test_the_page_url_is_read_only_behind_the_same_origin_guard() -> None:
    uses = {(_name(path), line.strip()) for path in SCRIPTS for line in _text(path).splitlines() if LOCATION_URL.search(line)}
    assert uses == ALLOWED_LOCATION_LINES


def test_the_same_origin_guards_refuse_a_second_slash_or_backslash() -> None:
    text = _text(APP_JS)
    for name in ("SAFE_URL_PATTERN", "SAME_ORIGIN_PATH_PATTERN"):
        match = re.search(rf"^const {name} = /(.*)/;$", text, re.MULTILINE)
        assert match is not None, name
        pattern = re.compile(match.group(1))
        for value in ("//evil.example", "/\\evil.example", "/\\/evil.example"):
            assert pattern.match(value) is None, (name, value)
        assert pattern.match("/explorer.html") is not None, name


def test_the_placeholder_page_is_gone() -> None:
    assert [_name(path) for path in FILES if "The web explorer is not available yet" in _text(path)] == []
