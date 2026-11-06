"""Injectable clock: services take now=None and resolve it here, so tests can move time with FixedClock instead of sleeping."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """The current time as a timezone-aware UTC datetime."""
        ...


class SystemClock:
    """The wall clock."""

    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """A clock that returns a set time until it is moved with set() or advance()."""

    def __init__(self, start: datetime) -> None:
        self._now = ensure_utc(start)

    def now(self) -> datetime:
        return self._now

    def set(self, value: datetime) -> None:
        self._now = ensure_utc(value)

    def advance(self, delta: timedelta) -> datetime:
        self._now = self._now + delta
        return self._now


_clock: Clock = SystemClock()


def get_clock() -> Clock:
    return _clock


def set_clock(clock: Clock) -> Clock:
    """Install clock as the process-wide default and return the previous one."""
    global _clock
    previous, _clock = _clock, clock
    return previous


def ensure_utc(value: datetime) -> datetime:
    """Return value converted to UTC; a naive datetime raises ValueError."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("naive datetime is not allowed; pass a timezone-aware value")
    return value.astimezone(UTC)


def resolve_now(now: datetime | None = None) -> datetime:
    """now converted to UTC, or the installed clock's time when now is None."""
    return ensure_utc(now if now is not None else _clock.now())
