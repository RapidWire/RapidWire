"""document/web.md pinned to the code: its pages table names every page of the web directory, its response header table equals WEB_RESPONSE_HEADERS, its page parameter table names every parameter the page modules read, its links resolve, and it uses ASCII outside Japanese text."""

from __future__ import annotations

import re
from pathlib import Path

from rapidwire.api.app import WEB_DIR, WEB_MEDIA_TYPES, WEB_RESPONSE_HEADERS
from tests.unit.doc_text import character_violations, heading_anchors

DOCS = Path(__file__).resolve().parents[3] / "document"
DOC = DOCS / "web.md"
PAGES_HEADING = "## ページ"
RESPONSE_HEADERS_HEADING = "## レスポンスヘッダー"
PAGE_PARAMETERS_HEADING = "### ページのパラメータ"
OPERATOR_PAGE_ANCHOR = "運営者用ページ"


def _text() -> str:
    return DOC.read_text(encoding="utf-8")


def _section(heading: str) -> str:
    text = _text()
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + len(heading) + 2)
    return text[start : end if end != -1 else len(text)]


def _rows(text: str) -> list[list[str]]:
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in text.splitlines() if line.startswith("| ")]
    return [row for row in rows if not all(set(cell) <= {"-"} for cell in row)][1:]


def test_the_document_is_ascii_outside_japanese_text() -> None:
    assert character_violations(DOC) == []


def test_the_pages_table_names_every_page() -> None:
    documented: set[str] = set()
    for row in _rows(_section(PAGES_HEADING).split("\n### ")[0]):
        match = re.match(r"`/([a-z0-9-]*\.html)?", row[1])
        if match is not None:
            documented.add(match[1] or "index.html")
    pages = {path.name for path in WEB_DIR.glob("*.html")} - {"404.html"}
    assert documented == pages


def test_the_response_header_table_equals_the_app() -> None:
    rows = {row[0].strip("`").lower(): row[1].strip("`") for row in _rows(_section(RESPONSE_HEADERS_HEADING))}
    assert rows == dict(WEB_RESPONSE_HEADERS)
    for media_type in set(WEB_MEDIA_TYPES.values()):
        assert f"`{media_type}`" in _text()


# URL parameters a page module reads: getParam(), pageParam(), a paged table's param and URLSearchParams lookups of the page URL.
PARAM_READS = (
    re.compile(r"\bgetParam\(\"([a-z_]+)\""),
    re.compile(r"\bpageParam\(\"([a-z_]+)\""),
    re.compile(r"\bparam: \"([a-z_]+)\""),
    re.compile(r"(?:\bparams|window\.location\.search\))\.get\(\"([a-z_]+)\"\)"),
)
# Parameters read by app.js helpers on behalf of the page that calls them.
HELPER_PARAMS = {"readHistorySort": {"sort_by", "sort_order"}}


def _params_read(script: Path) -> set[str]:
    text = script.read_text(encoding="utf-8")
    found = {name for pattern in PARAM_READS for name in pattern.findall(text)}
    for helper, names in HELPER_PARAMS.items():
        if re.search(rf"\b{helper}\(", text):
            found |= names
    return found


def test_the_page_parameter_table_names_every_parameter_read() -> None:
    documented = {}
    for row in _rows(_section(PAGE_PARAMETERS_HEADING).split("\n### ")[1]):
        documented[row[0].strip("`")] = set(re.findall(r"`([a-z_]+)`", row[1]))
    read = {}
    for page in sorted(WEB_DIR.glob("*.html")):
        params = _params_read(WEB_DIR / "assets" / f"{page.stem}.js")
        if params:
            read[f"/{page.name}"] = params
    assert documented == read


def test_links_reach_existing_files_and_headings() -> None:
    for name in ("web.md", "api.md", "operations.md", "backups.md"):
        path = DOCS / name
        for link in re.findall(r"\]\(([^)\s]+)\)", path.read_text(encoding="utf-8")):
            target, _, anchor = link.partition("#")
            if target.startswith("http"):
                continue
            target_path = (path.parent / target).resolve() if target else path
            assert target_path.is_file(), (name, link)
            if anchor:
                assert anchor in heading_anchors(target_path), (name, link)


def test_the_operator_documents_link_to_the_web_document() -> None:
    for name in ("api.md", "operations.md", "backups.md"):
        assert f"(web.md#{OPERATOR_PAGE_ANCHOR})" in (DOCS / name).read_text(encoding="utf-8"), name
