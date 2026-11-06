"""stop_signals(): SIGINT and SIGTERM call the callback inside the block, and the handlers installed before it are back afterwards, also for the bot and worker serve loops."""

from __future__ import annotations

import asyncio
import os
import signal
from collections.abc import Iterator
from types import FrameType
from typing import Any

import pytest

from rapidwire.config import Settings
from rapidwire.signals import STOP_SIGNALS, stop_signals
from rapidwire.worker import app as worker_app


def _sentinel(signum: int, frame: FrameType | None) -> None:
    raise AssertionError(f"the sentinel handler ran for signal {signum}")


@pytest.fixture
def sentinel_handlers() -> Iterator[None]:
    """Install _sentinel for SIGINT and SIGTERM, and put the original handlers back after the test."""
    original = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}
    for sig in STOP_SIGNALS:
        signal.signal(sig, _sentinel)
    yield
    for sig, handler in original.items():
        signal.signal(sig, handler)


@pytest.mark.parametrize("sig", STOP_SIGNALS)
async def test_a_signal_calls_the_callback_and_the_previous_handlers_return(sentinel_handlers: None, sig: signal.Signals) -> None:
    received = asyncio.Event()
    with stop_signals(received.set):
        assert all(signal.getsignal(other) is not _sentinel for other in STOP_SIGNALS)
        os.kill(os.getpid(), sig)
        await asyncio.wait_for(received.wait(), 5)
    assert all(signal.getsignal(other) is _sentinel for other in STOP_SIGNALS)


async def test_the_previous_handlers_return_when_the_block_raises(sentinel_handlers: None) -> None:
    with pytest.raises(RuntimeError, match="boom"), stop_signals(lambda: None):
        raise RuntimeError("boom")
    assert all(signal.getsignal(sig) is _sentinel for sig in STOP_SIGNALS)


async def test_the_worker_serve_loop_restores_the_previous_handlers(sentinel_handlers: None, monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_serve(settings: Settings, token: str, stop: asyncio.Event, **kwargs: Any) -> None:
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(stop.wait(), 5)

    monkeypatch.setattr(worker_app, "serve", fake_serve)
    await worker_app._serve_until_signal(Settings(_env_file=None, db_password="password-1234"), "unit-test-token")
    assert all(signal.getsignal(sig) is _sentinel for sig in STOP_SIGNALS)
