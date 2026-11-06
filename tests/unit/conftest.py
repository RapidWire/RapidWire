from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from rapidwire.config import FOREIGN_SECRET_ENV_VARS, reset_settings_cache
from rapidwire.logging import clear_secrets


@pytest.fixture(autouse=True)
def isolated_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Drop RAPIDWIRE_* variables, run from an empty directory so no .env is read, and restore global logging state afterwards."""
    for key in list(os.environ):
        if key.startswith("RAPIDWIRE_") or key in FOREIGN_SECRET_ENV_VARS:
            monkeypatch.delenv(key)
    monkeypatch.chdir(tmp_path)
    reset_settings_cache()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    logging.getLogger("alembic.runtime.plugins").setLevel(logging.NOTSET)
    logging.captureWarnings(False)
    clear_secrets()
    reset_settings_cache()
