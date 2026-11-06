"""Static checks of the operator page: the API token lives only in a module variable of operator.js, never reaches storage, a URL, history or the DOM, and only operator.js sends an Authorization header."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from rapidwire.api.app import WEB_DIR
from rapidwire.core.tokens import TOKEN_PREFIX

PAGE = WEB_DIR / "operator.html"
SCRIPT = WEB_DIR / "assets" / "operator.js"
SCRIPTS = sorted(path for path in WEB_DIR.rglob("*.js") if path.is_file())

STORAGE = re.compile(
    r"\blocalStorage\b|\bsessionStorage\b|document\s*\.\s*cookie|\bcookieStore\b|\bindexedDB\b|\bcaches\s*\.|\bCacheStorage\b"
    r"|navigator\s*\.\s*(?:serviceWorker|storage|credentials)\b|window\s*\.\s*name\b"
)
URL_WRITES = re.compile(
    r"\bhistory\s*\.\s*(?:pushState|replaceState)\b|\blocation\s*\.\s*(?:assign|replace|href|search|hash)\b|\blocation\s*=|\bURLSearchParams\b|\bwithParams\b|\bwindow\s*\.\s*open\b"
)
DOM_WRITES = re.compile(r"setAttribute\([^)]*\btoken\b|\bdataset\b[^\n]*\btoken\b|\.value\s*=\s*token\b")
AUTHORIZATION = re.compile(r"\bAuthorization\b")
TOKEN_HEADER = re.compile(r"Authorization:\s*`Bearer \$\{token\}`")
TOKEN_ASSIGNMENTS = re.compile(r"\btoken\s*=(?!=)\s*([^;\n]+);")
SUBMIT_HANDLER = re.compile(r'form\.addEventListener\("submit",\s*\(event\)\s*=>\s*\{\s*event\.preventDefault\(\);')


class _Tags(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))


def _tags() -> list[tuple[str, dict[str, str | None]]]:
    parser = _Tags()
    parser.feed(PAGE.read_text(encoding="utf-8"))
    parser.close()
    return parser.tags


def _by_id(element_id: str) -> tuple[str, dict[str, str | None]]:
    found = [(tag, attrs) for tag, attrs in _tags() if attrs.get("id") == element_id]
    assert len(found) == 1, element_id
    return found[0]


def _script() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_the_token_is_one_module_variable() -> None:
    text = _script()
    assert re.findall(r"^let token = null;$", text, re.MULTILINE) == ["let token = null;"]
    assert re.findall(r"\b(?:const|let|var)\s+token\b", text) == ["let token"]
    assert sorted(set(TOKEN_ASSIGNMENTS.findall(text))) == ["null", "value"]


def test_the_token_never_reaches_storage_urls_or_the_dom() -> None:
    text = _script()
    assert STORAGE.findall(text) == []
    assert URL_WRITES.findall(text) == []
    assert DOM_WRITES.findall(text) == []


def test_the_form_never_navigates_and_clears_its_input() -> None:
    text = _script()
    assert SUBMIT_HANDLER.search(text)
    assert 'input.value = "";' in text
    assert re.search(r'addEventListener\("pagehide",\s*\(\)\s*=>\s*forget\(\)\)', text)


def test_the_token_input_is_a_labelled_unsaved_password_field() -> None:
    tag, attrs = _by_id("token-input")
    assert tag == "input"
    assert attrs.get("type") == "password"
    assert attrs.get("autocomplete") == "off"
    assert attrs.get("spellcheck") == "false"
    assert "name" not in attrs
    assert "value" not in attrs
    assert "data-1p-ignore" in attrs
    assert attrs.get("data-lpignore") == "true"
    assert "data-bwignore" in attrs
    assert [label for name, label in _tags() if name == "label" and label.get("for") == "token-input"]
    form_tag, form = _by_id("token-form")
    assert form_tag == "form"
    assert "action" not in form
    assert "method" not in form


def test_only_operator_js_sends_an_authorization_header() -> None:
    users = sorted(path.relative_to(WEB_DIR).as_posix() for path in SCRIPTS if AUTHORIZATION.search(path.read_text(encoding="utf-8")))
    assert users == ["assets/operator.js"]
    text = _script()
    assert len(AUTHORIZATION.findall(text)) == len(TOKEN_HEADER.findall(text)) == 1


def test_the_page_carries_no_secret() -> None:
    page = PAGE.read_text(encoding="utf-8")
    assert "Bearer" not in page
    for text in (page, _script()):
        assert re.findall(re.escape(TOKEN_PREFIX) + r"[A-Za-z0-9_-]{8,}", text) == []


def test_a_download_of_an_earlier_session_is_never_saved() -> None:
    text = _script()
    assert re.findall(r"^let session = 0;$", text, re.MULTILINE) == ["let session = 0;"]
    assert text.count("session += 1;") == 2
    download = text[text.index("async function download(") : text.index("function backupColumns(")]
    assert "const started = session;" in download
    assert download.index("if (session !== started) {") < download.index("saveBlob(blob, item.name);")


def test_downloads_are_verified_before_they_are_saved() -> None:
    text = _script()
    assert 'crypto.subtle.digest("SHA-256"' in text
    assert "X-Checksum-SHA256" in text
    assert "X-Execution-Id" in text
    assert "encodeURIComponent(item.name)" in text
    assert text.index("computedOk === false") < text.index("saveBlob(blob, item.name);")
    assert "URL.revokeObjectURL(url)" in text
