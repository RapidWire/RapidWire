"""Every relative link in the documentation resolves to an existing file and heading, document/index.md lists every document, and every document keeps to ASCII outside Japanese text."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.doc_text import character_violations, github_slug, heading_anchors, prose_lines

ROOT = Path(__file__).resolve().parents[2]
DOCUMENT = ROOT / "document"
INDEX = DOCUMENT / "index.md"
DOCS = sorted([ROOT / "README.md", ROOT / "client" / "README.md", *DOCUMENT.rglob("*.md")])
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
SCHEME = re.compile(r"[a-z][a-z0-9+.-]*:", re.IGNORECASE)


def _links(path: Path) -> list[str]:
    return [target for line in prose_lines(path) for target in LINK.findall(line) if SCHEME.match(target) is None]


def test_the_doc_set_is_found() -> None:
    assert INDEX in DOCS
    assert len(DOCS) > 10


@pytest.mark.parametrize("doc", DOCS, ids=lambda doc: str(doc.relative_to(ROOT)))
def test_relative_links_resolve(doc: Path) -> None:
    broken: list[str] = []
    for target in _links(doc):
        file_part, _, anchor = target.partition("#")
        resolved = (doc.parent / file_part).resolve() if file_part else doc
        if not resolved.is_relative_to(ROOT) or not resolved.exists():
            broken.append(f"{target}: no such file")
        elif anchor and resolved.suffix == ".md" and anchor not in heading_anchors(resolved):
            broken.append(f"{target}: no such heading")
    assert broken == []


@pytest.mark.parametrize("doc", DOCS, ids=lambda doc: str(doc.relative_to(ROOT)))
def test_characters_are_ascii_outside_japanese_text(doc: Path) -> None:
    """Prose, inline code and Mermaid labels may hold Japanese text; every other fenced block is ASCII."""
    assert character_violations(doc) == []


def test_index_lists_every_document_and_sample() -> None:
    linked = {(INDEX.parent / target.partition("#")[0]).resolve() for target in _links(INDEX)}
    expected = {doc.resolve() for doc in DOCS if doc != INDEX}
    expected |= {sample.resolve() for sample in (DOCUMENT / "contracts" / "samples").iterdir() if sample.is_file()}
    assert sorted(str(path.relative_to(ROOT)) for path in expected - linked) == []


def test_slugs_follow_github() -> None:
    assert github_slug("/approve set") == "approve-set"
    assert github_slug("Lock, pause, resume and delete") == "lock-pause-resume-and-delete"
    assert github_slug("`rwc` tool") == "rwc-tool"
    assert github_slug("outbox の dispatcher") == "outbox-の-dispatcher"
    assert github_slug("API トークンのメンション") == "api-トークンのメンション"
    assert github_slug("`rwc` のコマンド") == "rwc-のコマンド"
    assert github_slug("ロック、停止、再開、削除") == "ロック停止再開削除"
    assert github_slug("ゲートウェイ intent") == "ゲートウェイ-intent"
