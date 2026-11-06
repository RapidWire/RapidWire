"""Every RAPIDWIRE_ variable that CI, deployment files, docs or tests set or read is a setting or carries the test-only prefix, because Settings rejects any other RAPIDWIRE_ name at startup."""

from __future__ import annotations

import re
from pathlib import Path

from rapidwire.config import TEST_ENV_PREFIX, Settings

ROOT = Path(__file__).resolve().parents[2]
NAME = re.compile(r"\bRAPIDWIRE_[A-Z0-9_]*[A-Z0-9]\b")
# Tests read their own switches with os.environ.get or os.getenv; names inside other strings may be deliberate negative cases.
TEST_READ = re.compile(r"(?:os\.environ\.get|os\.getenv|os\.environ\[)\(?\s*[\"'](RAPIDWIRE_[A-Z0-9_]+)[\"']")


def environment_files() -> list[Path]:
    """Files whose RAPIDWIRE_ names are all real variables: CI, compose, the image, env examples and the documentation."""
    files = [ROOT / ".github/workflows/ci.yml", ROOT / "compose.yaml", ROOT / "Dockerfile", ROOT / "README.md"]
    files += sorted(ROOT.glob(".env*.example"))
    files += sorted((ROOT / "document").rglob("*.md"))
    return files


def is_allowed(name: str) -> bool:
    return name in Settings.known_variables() or name.startswith(TEST_ENV_PREFIX)


def test_environment_files_only_use_settings_or_test_variables() -> None:
    unknown = {f"{path.relative_to(ROOT)}: {name}" for path in environment_files() for name in NAME.findall(path.read_text(encoding="utf-8")) if not is_allowed(name)}
    assert sorted(unknown) == []


def test_tests_only_read_settings_or_test_variables() -> None:
    sources = [path for path in sorted((ROOT / "tests").rglob("*.py")) if path != Path(__file__).resolve()]
    unknown = {f"{path.relative_to(ROOT)}: {name}" for path in sources for name in TEST_READ.findall(path.read_text(encoding="utf-8")) if not is_allowed(name)}
    assert sorted(unknown) == []


def test_the_test_read_pattern_finds_environment_reads() -> None:
    assert TEST_READ.findall('X = int(os.environ.get("RAPIDWIRE_PROPERTY_EXAMPLES", "25"))') == ["RAPIDWIRE_PROPERTY_EXAMPLES"]
    assert TEST_READ.findall("os.getenv('RAPIDWIRE_TEST_DB_URL')") == ["RAPIDWIRE_TEST_DB_URL"]
