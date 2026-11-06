"""Static checks of the contract page: it reads the contract, its versions list and its storage, and no web script requests a contract binary."""

from __future__ import annotations

import re

from rapidwire.api.app import WEB_DIR

SCRIPT = WEB_DIR / "assets" / "contract.js"
SCRIPTS = sorted(path for path in WEB_DIR.rglob("*.js") if path.is_file())
# A single version or its binary download; the versions list ends at "/versions" and is allowed.
BINARY_ROUTES = re.compile(r"/versions/|/binary\b")


def test_no_web_script_requests_a_contract_binary() -> None:
    found = {path.relative_to(WEB_DIR).as_posix(): BINARY_ROUTES.findall(path.read_text(encoding="utf-8")) for path in SCRIPTS}
    assert {name: hits for name, hits in found.items() if hits} == {}


def test_the_contract_page_reads_its_lists() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert "`/v1/contracts/${id}`" in text
    assert "`/v1/contracts/${id}/versions`" in text
    assert "`/v1/contracts/${id}/storage`" in text


def test_storage_values_are_rendered_without_html() -> None:
    text = SCRIPT.read_text(encoding="utf-8")
    assert re.search(r"\binnerHTML\b|\binsertAdjacentHTML\b|\bouterHTML\b", text) is None
    assert 'case "str":' in text
    assert "JSON.stringify(String(raw))" in text
