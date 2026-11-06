"""SIGINT and SIGTERM handling for the long-running services: the running event loop calls a callback for either signal, and the previous handlers are back in place afterwards."""

from __future__ import annotations

import asyncio
import signal
from collections.abc import Callable, Iterator
from contextlib import contextmanager

__all__ = ["STOP_SIGNALS", "stop_signals"]

STOP_SIGNALS = (signal.SIGINT, signal.SIGTERM)


@contextmanager
def stop_signals(callback: Callable[[], object]) -> Iterator[None]:
    """Call callback from the running loop on SIGINT or SIGTERM inside the block, then restore the handlers that were installed before it."""
    loop = asyncio.get_running_loop()
    previous = {sig: signal.getsignal(sig) for sig in STOP_SIGNALS}
    installed: list[signal.Signals] = []
    try:
        for sig in STOP_SIGNALS:
            loop.add_signal_handler(sig, callback)
            installed.append(sig)
        yield
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
            # remove_signal_handler() leaves the default disposition; a handler not installed from Python reads back as None and stays default.
            handler = previous[sig]
            if handler is not None:
                signal.signal(sig, handler)
