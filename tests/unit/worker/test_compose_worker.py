"""compose.yaml gives the worker enough stop grace for its bounded shutdown."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml  # type: ignore[import-untyped]

from rapidwire.worker.scheduler import SHUTDOWN_CANCEL_SECONDS, SHUTDOWN_DRAIN_SECONDS

COMPOSE = Path(__file__).resolve().parents[3] / "compose.yaml"
# Time allowed after the scheduler stops for closing the Discord client and disposing the engine.
DISPOSE_MARGIN_SECONDS = 5.0


def _seconds(duration: str) -> float:
    units = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0}
    for suffix in sorted(units, key=len, reverse=True):
        if duration.endswith(suffix):
            return float(duration.removesuffix(suffix)) * units[suffix]
    raise AssertionError(f"unsupported duration {duration!r}")


def test_worker_stop_grace_covers_the_shutdown_bound() -> None:
    compose = cast(dict[str, Any], yaml.safe_load(COMPOSE.read_text(encoding="utf-8")))
    grace = _seconds(str(compose["services"]["worker"]["stop_grace_period"]))
    assert grace > SHUTDOWN_DRAIN_SECONDS + SHUTDOWN_CANCEL_SECONDS + DISPOSE_MARGIN_SECONDS
