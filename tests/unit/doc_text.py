"""Text helpers shared by the documentation tests: the anchors GitHub gives Markdown headings and the character rule of the Japanese docs."""

from __future__ import annotations

import re
import unicodedata
from collections import Counter
from pathlib import Path

CODE_SPAN = re.compile(r"`+[^`]*`+")
HEADING = re.compile(r"(#{1,6})\s+(.*?)\s*#*\s*$")
# Hiragana, the hiragana iteration marks, katakana up to U+30FA, the long vowel mark and katakana iteration marks from U+30FC (the middle dot U+30FB is excluded), CJK extension A, CJK unified ideographs, the ideographic iteration mark and the Japanese comma and full stop.
JAPANESE_RANGES = ((0x3041, 0x3096), (0x309D, 0x309F), (0x30A1, 0x30FA), (0x30FC, 0x30FF), (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0x3005, 0x3005), (0x3001, 0x3002))


def github_slug(heading: str) -> str:
    """The anchor GitHub gives a heading: lower case, Unicode letters, marks, numbers, "-" and "_" kept, other characters dropped, each space as "-"."""
    text = heading.strip().lower()
    return "".join(char for char in text if unicodedata.category(char)[0] in "LMN" or char in " -_").replace(" ", "-")


def prose_lines(path: Path) -> list[str]:
    """The lines of a Markdown file outside fenced code blocks, with inline code spans removed."""
    lines: list[str] = []
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        if not fenced:
            lines.append(CODE_SPAN.sub("", line))
    return lines


def heading_anchors(path: Path) -> set[str]:
    """The anchors of every heading of a Markdown file outside fenced code blocks, a repeated slug numbered "-1", "-2" ... as GitHub does."""
    anchors: set[str] = set()
    seen: Counter[str] = Counter()
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("```"):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.fullmatch(line)
        if match is None:
            continue
        slug = github_slug(match.group(2))
        anchors.add(slug if seen[slug] == 0 else f"{slug}-{seen[slug]}")
        seen[slug] += 1
    return anchors


def is_japanese(char: str) -> bool:
    """Whether char is one of the Japanese characters the documentation allows."""
    return any(low <= ord(char) <= high for low, high in JAPANESE_RANGES)


def disallowed_characters(text: str) -> set[str]:
    """The characters of text that are neither ASCII nor allowed Japanese; an empty set means text obeys the documentation rule."""
    return {char for char in text if not char.isascii() and not is_japanese(char)}


def character_violations(path: Path) -> list[str]:
    """Every character of a Markdown file that breaks the documentation rule, as "line N: U+XXXX" entries; an empty list means the file obeys it."""
    violations: list[str] = []
    fence: str | None = None
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if line.lstrip().startswith("```"):
            fence = line.lstrip()[3:].strip() if fence is None else None
            continue
        # Prose, inline code and Mermaid labels may hold Japanese text; every other fenced block is ASCII only.
        bad = disallowed_characters(line) if fence in (None, "mermaid") else {char for char in line if not char.isascii()}
        violations.extend(f"line {number}: U+{ord(char):04X}" for char in sorted(bad))
    return violations
