"""Discord usernames for the API: an LRU cache, created once per application lifespan, over the Discord REST API GET /users/{id}, with a TTL, a short negative cache for unknown users, stale fallback while Discord cannot answer, one shared upstream fetch per uncached user, a bound on concurrent upstream requests, and a pause after Discord answers 429, 401 or 403."""

from __future__ import annotations

import asyncio
import enum
import logging
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

import httpx

from rapidwire.core.errors import NotFoundError
from rapidwire.core.validation import USER_ID_MAX
from rapidwire.discord_rest import DEFAULT_RETRY_AFTER_SECONDS, MAX_RETRY_AFTER_SECONDS, retry_after

__all__ = [
    "AUTH_FAILURE_PAUSE_SECONDS",
    "CACHE_CAPACITY",
    "CACHE_TTL_SECONDS",
    "DEFAULT_RETRY_AFTER_SECONDS",
    "MAX_CONCURRENT_LOOKUPS",
    "MAX_RETRY_AFTER_SECONDS",
    "NOT_FOUND_TTL_SECONDS",
    "DiscordUnavailable",
    "DiscordUserNotFound",
    "DiscordUsers",
]

CACHE_CAPACITY: Final = 100
CACHE_TTL_SECONDS: Final = 86_400.0
# How long an answer that Discord does not know a user is kept before Discord is asked again.
NOT_FOUND_TTL_SECONDS: Final = 300.0
MAX_CONCURRENT_LOOKUPS: Final = 4
# The pause after Discord refuses the bot token (401 or 403), which only an operator can fix.
AUTH_FAILURE_PAUSE_SECONDS: Final = 600.0

log = logging.getLogger("rapidwire.api.discord_users")


class DiscordUserNotFound(NotFoundError):
    code = "user_not_found"
    default_message = "The user was not found on Discord."


class DiscordUnavailable(Exception):
    """Discord could not answer and no cached name exists; retry_after is the remaining pause after a 429, 401 or 403 in seconds, None when unknown."""

    def __init__(self, retry_after: float | None = None) -> None:
        super().__init__("Discord is unavailable")
        self.retry_after = retry_after


class _Missing(enum.Enum):
    NOT_FOUND = "not_found"
    UNAVAILABLE = "unavailable"


@dataclass
class _Entry:
    username: str
    fetched_at: float


class DiscordUsers:
    """Usernames by Discord user ID; an entry is fresh for ttl_seconds after it was fetched, an unknown user is remembered for not_found_ttl_seconds, the least recently used entries are dropped beyond capacity, at most max_concurrent upstream requests run at once, and clock is a monotonic time in seconds."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        configured: bool,
        capacity: int = CACHE_CAPACITY,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        not_found_ttl_seconds: float = NOT_FOUND_TTL_SECONDS,
        max_concurrent: int = MAX_CONCURRENT_LOOKUPS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if capacity < 1 or max_concurrent < 1 or ttl_seconds < 0 or not_found_ttl_seconds < 0:
            raise ValueError("capacity and max_concurrent must be positive and the TTLs non-negative")
        self.client = client
        self.configured = configured
        self.capacity = capacity
        self.ttl_seconds = ttl_seconds
        self.not_found_ttl_seconds = not_found_ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[int, _Entry] = OrderedDict()
        self._not_found: OrderedDict[int, float] = OrderedDict()
        self._inflight: dict[int, asyncio.Task[str | _Missing]] = {}
        self._slots = asyncio.Semaphore(max_concurrent)
        self._paused_until: float | None = None
        self._auth_failure_logged = False
        self._closed = False

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, user_id: object) -> bool:
        return user_id in self._entries

    async def username(self, user_id: int) -> str:
        """The username of the user: a fresh cached name, else Discord's answer, else a stale cached name; raises DiscordUserNotFound when Discord does not know the user and nothing is cached, and DiscordUnavailable when Discord cannot answer and nothing is cached."""
        if isinstance(user_id, bool) or not isinstance(user_id, int) or not 1 <= user_id <= USER_ID_MAX:
            raise ValueError("user_id must be a positive 64-bit integer")
        entry = self._entries.get(user_id)
        if entry is not None and self._clock() - entry.fetched_at <= self.ttl_seconds:
            self._entries.move_to_end(user_id)
            return entry.username
        outcome = _Missing.NOT_FOUND if self._known_missing(user_id) else await self._shared_fetch(user_id)
        if isinstance(outcome, str):
            return outcome
        stale = self._entries.get(user_id)
        if stale is not None:
            self._entries.move_to_end(user_id)
            return stale.username
        if outcome is _Missing.NOT_FOUND:
            raise DiscordUserNotFound()
        raise DiscordUnavailable(self._pause_remaining())

    async def aclose(self) -> None:
        """Stop asking Discord and cancel the lookups in flight; their waiters get the stale name or DiscordUnavailable."""
        self._closed = True
        tasks = list(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def _known_missing(self, user_id: int) -> bool:
        missed_at = self._not_found.get(user_id)
        if missed_at is None:
            return False
        if self._clock() - missed_at <= self.not_found_ttl_seconds:
            return True
        del self._not_found[user_id]
        return False

    async def _shared_fetch(self, user_id: int) -> str | _Missing:
        # Waiters are shielded, so a request that goes away does not cancel the fetch the other waiters share; the fetch stores its result itself.
        task = self._inflight.get(user_id)
        if task is None:
            task = asyncio.ensure_future(self._fetch(user_id))
            self._inflight[user_id] = task

            def forget(done: asyncio.Task[str | _Missing]) -> None:
                if self._inflight.get(user_id) is done:
                    del self._inflight[user_id]
                if not done.cancelled() and (error := done.exception()) is not None:
                    log.error("Discord user lookup crashed", exc_info=error)

            task.add_done_callback(forget)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if task.cancelled() and (current is None or current.cancelling() == 0):
                return _Missing.UNAVAILABLE
            raise

    def _pause_remaining(self) -> float | None:
        if self._paused_until is None:
            return None
        remaining = self._paused_until - self._clock()
        if remaining <= 0:
            self._paused_until = None
            return None
        return remaining

    def _unavailable(self) -> bool:
        return self._closed or not self.configured or self._pause_remaining() is not None

    async def _fetch(self, user_id: int) -> str | _Missing:
        """Ask Discord once, without retrying; a 404 is remembered for not_found_ttl_seconds, a 429 pauses every upstream request for the wait Discord asks for, a 401 or 403 pauses them for AUTH_FAILURE_PAUSE_SECONDS, and any other failure is UNAVAILABLE."""
        if self._unavailable():
            return _Missing.UNAVAILABLE
        async with self._slots:
            # The pause may have started while this lookup waited for a slot.
            if self._unavailable():
                return _Missing.UNAVAILABLE
            try:
                response = await self.client.get(f"users/{user_id}")
            except httpx.HTTPError as error:
                log.warning("Discord user lookup failed: %s", type(error).__name__)
                return _Missing.UNAVAILABLE
            except RuntimeError:
                # httpx raises RuntimeError once the client is closed, which happens when the application shuts down.
                return _Missing.UNAVAILABLE
        return self._answer(user_id, response)

    def _answer(self, user_id: int, response: httpx.Response) -> str | _Missing:
        if response.status_code == 404:
            self._remember_missing(user_id)
            return _Missing.NOT_FOUND
        if response.status_code == 429:
            wait = retry_after(response)
            self._paused_until = self._clock() + wait
            log.warning("Discord rate limited user lookups for %.1f seconds", wait)
            return _Missing.UNAVAILABLE
        if response.status_code in (401, 403):
            self._paused_until = self._clock() + AUTH_FAILURE_PAUSE_SECONDS
            if not self._auth_failure_logged:
                self._auth_failure_logged = True
                log.error("Discord refused the bot token with status %d; user lookups pause for %.0f seconds at a time", response.status_code, AUTH_FAILURE_PAUSE_SECONDS)
            return _Missing.UNAVAILABLE
        if response.status_code != 200:
            log.warning("Discord user lookup answered status %d", response.status_code)
            return _Missing.UNAVAILABLE
        try:
            payload = response.json()
        except ValueError:
            payload = None
        username = payload.get("username") if isinstance(payload, dict) else None
        if not isinstance(username, str) or not username:
            log.warning("Discord user lookup answered without a username")
            return _Missing.UNAVAILABLE
        self._auth_failure_logged = False
        self._store(user_id, username)
        return username

    def _remember_missing(self, user_id: int) -> None:
        self._not_found[user_id] = self._clock()
        self._not_found.move_to_end(user_id)
        while len(self._not_found) > self.capacity:
            self._not_found.popitem(last=False)

    def _store(self, user_id: int, username: str) -> None:
        self._not_found.pop(user_id, None)
        self._entries[user_id] = _Entry(username=username, fetched_at=self._clock())
        self._entries.move_to_end(user_id)
        while len(self._entries) > self.capacity:
            self._entries.popitem(last=False)
