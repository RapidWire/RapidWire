"""The outbox dispatcher: sends committed outbox rows to Discord over REST and records each row's outcome on the row; it never writes executions, balances or transfers.

Each due pending row is locked with SELECT ... FOR UPDATE SKIP LOCKED for the whole delivery, and its outcome is written in that same transaction, so concurrent dispatchers never deliver one row at the same time.
Delivery is at least once: when Discord accepted a request but the outcome was not committed (a crash, a lost connection or a cancelled shutdown), or Discord returned a timeout or server error after accepting it, the row is still pending and is delivered again.
A message carries a nonce derived from the outbox ID with enforce_nonce, so Discord drops a resend that arrives within its nonce window (a few minutes) and a resend after that window posts a second message; role changes are idempotent.
"""

from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final

import httpx
from sqlalchemy import RowMapping, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.bot.views.claim import claim_notification_message, parse_claim_payload
from rapidwire.contract.runtime import DISCORD_MESSAGE_MAX_LENGTH
from rapidwire.core.amounts import NumberFormatError, parse_int
from rapidwire.core.clock import Clock, get_clock
from rapidwire.core.discord_permissions import is_discord_allowed
from rapidwire.core.notifications import is_notification_allowed
from rapidwire.core.types import ClaimStatus, OutboxKind, OutboxStatus, to_json_object
from rapidwire.core.validation import BIGINT_MAX
from rapidwire.db.tables import claim, outbox
from rapidwire.discord_rest import DiscordError, read_error, read_rate_limit, retry_after

__all__ = [
    "AUTH_FAILURE_PAUSE_SECONDS",
    "BATCH_SIZE",
    "INLINE_RATE_LIMIT_RETRIES",
    "INLINE_WAIT_MAX_SECONDS",
    "MAX_DELIVERY_ATTEMPTS",
    "RETRY_BASE_SECONDS",
    "RETRY_MAX_SECONDS",
    "DispatchStats",
    "Dispatcher",
    "message_nonce",
    "retry_delay",
]

# A row whose delivery failed with a retryable error this many times becomes failed.
MAX_DELIVERY_ATTEMPTS: Final = 8
# Retry n waits a random time between half and all of min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * 2 ** (n - 1)).
RETRY_BASE_SECONDS: Final = 5.0
RETRY_MAX_SECONDS: Final = 900.0
# Rate-limit waits up to this long are slept in place, at most INLINE_RATE_LIMIT_RETRIES times per request; longer waits reschedule the row without using an attempt.
INLINE_WAIT_MAX_SECONDS: Final = 2.0
INLINE_RATE_LIMIT_RETRIES: Final = 3
# The dispatcher pause after Discord answers 401, which only a new bot token fixes.
AUTH_FAILURE_PAUSE_SECONDS: Final = 60.0
# Due rows read per query; each is then locked and delivered on its own.
BATCH_SIZE: Final = 50

# Users may be mentioned (and notified) by contract messages; @everyone, @here and role mentions are not.
ALLOWED_MENTIONS: Final = {"parse": ["users"]}

_SEND_KEYS: Final = frozenset({"contract_id", "guild_id", "channel_id", "message"})
_ROLE_KEYS: Final = frozenset({"contract_id", "guild_id", "user_id", "role_id"})
_MAJOR_SEGMENTS: Final = frozenset({"channels", "guilds", "webhooks"})
_SNOWFLAKE: Final = re.compile(r"[1-9][0-9]{0,18}")

log = logging.getLogger("rapidwire.worker.dispatcher")


def message_nonce(outbox_id: int) -> str:
    """The Discord nonce of the message an outbox row sends: rw followed by the outbox ID, at most 21 characters."""
    return f"rw{outbox_id}"


def retry_delay(attempt: int, *, rand: Callable[[], float] = random.random) -> float:
    """The wait in seconds before retry number attempt (1-based): exponential from RETRY_BASE_SECONDS, capped at RETRY_MAX_SECONDS, with jitter over the upper half."""
    ceiling: float = min(RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * float(2 ** max(attempt - 1, 0)))
    return ceiling / 2 + ceiling / 2 * rand()


@dataclass
class DispatchStats:
    """Rows handled by one run_once(): delivered, terminally failed, skipped, rescheduled after a retryable failure, and rescheduled for a rate limit."""

    sent: int = 0
    failed: int = 0
    skipped: int = 0
    retried: int = 0
    deferred: int = 0


class _Malformed(Exception):
    pass


class _Terminal(Exception):
    def __init__(self, reason: str, *, http_status: int | None = None, error: DiscordError | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status
        self.error = error


class _Retryable(Exception):
    def __init__(self, description: str, *, http_status: int | None = None, error: DiscordError | None = None) -> None:
        super().__init__(description)
        self.description = description
        self.http_status = http_status
        self.error = error


class _Deferred(Exception):
    def __init__(self, seconds: float) -> None:
        super().__init__(f"rate limited for {seconds:.3f} seconds")
        self.seconds = seconds


class _Paused(Exception):
    pass


@dataclass(frozen=True)
class _Outcome:
    label: str
    values: Mapping[str, Any]


@dataclass
class _Buckets:
    """Discord's bucket per route template (a fixed set of paths) and, per bucket and major parameter, when an exhausted bucket resets; expired resets are pruned whenever one is recorded."""

    routes: dict[str, str] = field(default_factory=dict)
    resets: dict[str, float] = field(default_factory=dict)


def _route_key(method: str, path: str) -> tuple[str, str]:
    """Discord's rate-limit route as (template, major): the template is the method and path with every ID replaced, and major is the ID after channels, guilds or webhooks that Discord limits separately (empty when the path has none)."""
    segments = path.split("/")
    major = next((segment for index, segment in enumerate(segments) if segment.isdigit() and index > 0 and segments[index - 1] in _MAJOR_SEGMENTS), "")
    template = "/".join(":id" if segment.isdigit() else segment for segment in segments)
    return f"{method} {template}", major


def _id_field(payload: Mapping[str, Any], key: str) -> int:
    try:
        return parse_int(payload[key], 1, BIGINT_MAX)
    except NumberFormatError:
        raise _Malformed(f"payload field {key} is not an ID") from None


def _checked_keys(payload: object, keys: frozenset[str]) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping) or set(payload) != keys:
        raise _Malformed("payload keys do not match its kind")
    return payload


def _snowflake(value: object) -> int | None:
    return int(value) if isinstance(value, str) and _SNOWFLAKE.fullmatch(value) is not None and int(value) <= BIGINT_MAX else None


def _response_id(response: httpx.Response, key: str) -> int:
    try:
        payload = response.json()
    except ValueError:
        payload = None
    value = _snowflake(payload.get(key)) if isinstance(payload, dict) else None
    if value is None:
        raise _Retryable(f"Discord answered without a valid {key}", http_status=response.status_code)
    return value


class Dispatcher:
    """Delivers due outbox rows through client, an httpx client for the Discord REST API carrying the bot token; clock gives the time that due rows and completion times are compared with and written as."""

    def __init__(self, engine: AsyncEngine, client: httpx.AsyncClient, *, clock: Clock | None = None, batch_size: int = BATCH_SIZE, rand: Callable[[], float] = random.random) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._engine = engine
        self._client = client
        self._clock = clock
        self._batch_size = batch_size
        self._rand = rand
        self._paused_until = 0.0
        self._buckets = _Buckets()
        # Requests sent for the row being delivered; run_once() handles one row at a time.
        self._requests_made = 0
        self._handlers: dict[str, Callable[[AsyncConnection, RowMapping], Awaitable[dict[str, Any]]]] = {
            OutboxKind.CLAIM_CREATED: self._claim_created,
            OutboxKind.DISCORD_SEND: self._discord_send,
            OutboxKind.DISCORD_ADD_ROLE: self._role_change,
            OutboxKind.DISCORD_REMOVE_ROLE: self._role_change,
        }

    def _now(self) -> datetime:
        return (self._clock or get_clock()).now()

    def paused_for(self) -> float:
        """Seconds until the dispatcher sends again after a global rate limit or a refused bot token; 0 when it is not paused."""
        return max(0.0, self._paused_until - time.monotonic())

    def _pause(self, seconds: float) -> None:
        self._paused_until = max(self._paused_until, time.monotonic() + seconds)

    async def run_once(self, *, should_stop: Callable[[], bool] = lambda: False) -> DispatchStats:
        """Deliver every row due now in outbox_id order, one transaction per row, until none is left, the dispatcher pauses or should_stop() is true; rows locked by another dispatcher are left to it."""
        stats = DispatchStats()
        after = 0
        while not should_stop() and not self.paused_for():
            now = self._now()
            candidates = (
                select(outbox.c.outbox_id)
                .where(outbox.c.status == OutboxStatus.PENDING.value, outbox.c.available_at <= now, outbox.c.outbox_id > after)
                .order_by(outbox.c.outbox_id)
                .limit(self._batch_size)
            )
            async with self._engine.connect() as conn:
                ids = list((await conn.execute(candidates)).scalars())
            if not ids:
                break
            for outbox_id in ids:
                if should_stop() or self.paused_for():
                    break
                label = await self._dispatch(outbox_id)
                if label is not None:
                    setattr(stats, label, getattr(stats, label) + 1)
            after = ids[-1]
        return stats

    async def _dispatch(self, outbox_id: int) -> str | None:
        try:
            async with self._engine.begin() as conn:
                stmt = select(outbox).where(outbox.c.outbox_id == outbox_id, outbox.c.status == OutboxStatus.PENDING.value, outbox.c.available_at <= self._now()).with_for_update(skip_locked=True)
                row = (await conn.execute(stmt)).mappings().one_or_none()
                if row is None:
                    return None
                outcome = await self._attempt(conn, row)
                await conn.execute(update(outbox).where(outbox.c.outbox_id == outbox_id).values(**outcome.values))
        except _Paused:
            return None
        return outcome.label

    async def _attempt(self, conn: AsyncConnection, row: RowMapping) -> _Outcome:
        outbox_id, kind = row["outbox_id"], row["kind"]
        extra: dict[str, Any] = {"outbox_id": outbox_id, "kind": kind}
        self._requests_made = 0
        try:
            handler = self._handlers.get(kind)
            if handler is None:
                raise _Terminal("unknown_kind")
            try:
                result = await handler(conn, row)
            except ValueError as error:
                raise _Malformed(str(error)) from None
        except _Malformed as error:
            log.error("outbox row has a malformed payload", extra={**extra, "error": str(error)})
            return self._terminal(row, OutboxStatus.FAILED, {"status": OutboxStatus.FAILED.value, "reason": "malformed_payload"})
        except _Terminal as error:
            result = {"status": OutboxStatus.FAILED.value, "reason": error.reason}
            if error.http_status is not None:
                result["http_status"] = error.http_status
            if error.error is not None and error.error.code is not None:
                result["code"] = error.error.code
            if error.error is not None and error.error.message is not None:
                result["message"] = error.error.message
            log.warning("outbox delivery failed", extra={**extra, "reason": error.reason, "http_status": error.http_status, "code": result.get("code")})
            return self._terminal(row, OutboxStatus.FAILED, result)
        except _Retryable as error:
            return self._retry(row, error)
        except _Deferred as error:
            log.info("outbox delivery rate limited", extra={**extra, "wait_seconds": round(error.seconds, 3)})
            return _Outcome("deferred", {"available_at": self._now() + timedelta(seconds=error.seconds), "last_error": str(error)})
        except (_Paused, DBAPIError):
            raise
        except Exception as error:
            log.exception("outbox delivery crashed", extra=extra)
            description = f"internal error: {type(error).__name__}"
            if self._requests_made:
                return self._retry(row, _Retryable(description))
            # A crash before any request repeats on every retry, so the row fails at once.
            outcome = self._terminal(row, OutboxStatus.FAILED, {"status": OutboxStatus.FAILED.value, "reason": "internal_error"})
            return _Outcome(outcome.label, {**outcome.values, "last_error": description})
        status = OutboxStatus(result["status"])
        log.info("outbox delivery %s", status.value, extra={**extra, "reason": result.get("reason")})
        return self._terminal(row, status, result)

    def _terminal(self, row: RowMapping, status: OutboxStatus, result: dict[str, Any]) -> _Outcome:
        return _Outcome(
            status.value,
            {
                "status": status.value,
                "attempts": row["attempts"] + (1 if self._requests_made else 0),
                "completed_at": self._now(),
                "result": to_json_object(result),
                "last_error": None,
            },
        )

    def _retry(self, row: RowMapping, error: _Retryable) -> _Outcome:
        """Reschedule after a retryable failure of an attempt that sent at least one request; the last allowed attempt fails the row with Discord's last answer in the result and RapidWire's description only in last_error."""
        attempts = row["attempts"] + 1
        extra = {"outbox_id": row["outbox_id"], "kind": row["kind"], "attempts": attempts, "http_status": error.http_status, "error": error.description}
        if attempts >= MAX_DELIVERY_ATTEMPTS:
            log.warning("outbox delivery failed after its last attempt", extra=extra)
            result: dict[str, Any] = {"status": OutboxStatus.FAILED.value, "reason": "retries_exhausted"}
            if error.http_status is not None:
                result["http_status"] = error.http_status
            if error.error is not None and error.error.code is not None:
                result["code"] = error.error.code
            if error.error is not None and error.error.message is not None:
                result["message"] = error.error.message
            return _Outcome("failed", {"status": OutboxStatus.FAILED.value, "attempts": attempts, "completed_at": self._now(), "result": to_json_object(result), "last_error": error.description})
        delay = retry_delay(attempts, rand=self._rand)
        log.warning("outbox delivery will be retried", extra={**extra, "retry_in_seconds": round(delay, 3)})
        return _Outcome("retried", {"attempts": attempts, "available_at": self._now() + timedelta(seconds=delay), "last_error": error.description})

    async def _wait_for_bucket(self, route: tuple[str, str]) -> None:
        if self.paused_for():
            raise _Paused
        template, major = route
        bucket = self._buckets.routes.get(template)
        if bucket is None:
            return
        key = f"{bucket}:{major}"
        reset = self._buckets.resets.get(key)
        if reset is None:
            return
        remaining = reset - time.monotonic()
        if remaining <= 0:
            self._buckets.resets.pop(key, None)
        elif remaining <= INLINE_WAIT_MAX_SECONDS:
            await asyncio.sleep(remaining)
        else:
            raise _Deferred(remaining)

    def _note_rate_limit(self, route: tuple[str, str], response: httpx.Response, wait: float | None) -> None:
        limit = read_rate_limit(response)
        if limit.bucket is None:
            return
        template, major = route
        self._buckets.routes[template] = limit.bucket
        key = f"{limit.bucket}:{major}"
        now = time.monotonic()
        reset_after = wait if wait is not None else limit.reset_after if limit.remaining == 0 else None
        if reset_after is not None:
            for expired in [name for name, reset in self._buckets.resets.items() if reset <= now]:
                del self._buckets.resets[expired]
            self._buckets.resets[key] = now + reset_after
        else:
            self._buckets.resets.pop(key, None)

    def rate_limit_state(self) -> tuple[int, int]:
        """The number of known route templates and of recorded bucket resets."""
        return len(self._buckets.routes), len(self._buckets.resets)

    async def _request(self, method: str, path: str, *, reason: str, json: Mapping[str, Any] | None = None, headers: Mapping[str, str] | None = None) -> httpx.Response:
        """Send one request, sleeping in place through short rate limits; a 4xx raises _Terminal with reason, a network error, timeout or 5xx _Retryable, a long route rate limit _Deferred, and a global rate limit or a 401 pauses the dispatcher and raises _Paused."""
        route = _route_key(method, path)
        for _ in range(INLINE_RATE_LIMIT_RETRIES + 1):
            await self._wait_for_bucket(route)
            self._requests_made += 1
            try:
                response = await self._client.request(method, path, json=json, headers=headers)
            except httpx.HTTPError as error:
                raise _Retryable(f"request failed: {type(error).__name__}") from None
            if response.status_code == 429:
                wait = retry_after(response)
                if read_rate_limit(response).is_global:
                    self._pause(wait)
                    log.warning("Discord applied its global rate limit; the dispatcher pauses", extra={"wait_seconds": round(wait, 3)})
                    raise _Paused
                self._note_rate_limit(route, response, wait)
                if wait > INLINE_WAIT_MAX_SECONDS:
                    raise _Deferred(wait)
                await asyncio.sleep(wait)
                continue
            self._note_rate_limit(route, response, None)
            if response.status_code == 401:
                self._pause(AUTH_FAILURE_PAUSE_SECONDS)
                log.error("Discord refused the bot token with status 401; the dispatcher pauses", extra={"pause_seconds": AUTH_FAILURE_PAUSE_SECONDS})
                raise _Paused
            if response.status_code >= 500:
                raise _Retryable(f"Discord answered status {response.status_code}", http_status=response.status_code, error=read_error(response))
            if response.status_code >= 400:
                raise _Terminal(reason, http_status=response.status_code, error=read_error(response))
            return response
        raise _Deferred(INLINE_WAIT_MAX_SECONDS)

    async def _send_message(self, outbox_id: int, channel_id: int, body: Mapping[str, Any], *, reason: str) -> dict[str, Any]:
        response = await self._request("POST", f"channels/{channel_id}/messages", json={**body, "nonce": message_nonce(outbox_id), "enforce_nonce": True}, reason=reason)
        return {"status": OutboxStatus.SENT.value, "channel_id": channel_id, "message_id": _response_id(response, "id")}

    async def _claim_created(self, conn: AsyncConnection, row: RowMapping) -> dict[str, Any]:
        """Send the claim notification to the payer's DM channel, or skip it when the claim is no longer pending or the payer no longer allows notifications from the claimant."""
        payload = row["payload"]
        notice = parse_claim_payload(payload)
        status = (await conn.execute(select(claim.c.status).where(claim.c.claim_id == notice.claim_id))).scalar_one_or_none()
        if status is None:
            return {"status": OutboxStatus.SKIPPED.value, "reason": "claim_not_found"}
        if status != ClaimStatus.PENDING.value:
            return {"status": OutboxStatus.SKIPPED.value, "reason": f"claim_{status}"}
        if not await is_notification_allowed(conn, notice.payer_id, notice.claimant_id):
            return {"status": OutboxStatus.SKIPPED.value, "reason": "notification_revoked"}
        body = claim_notification_message(payload)
        dm = await self._request("POST", "users/@me/channels", json={"recipient_id": str(notice.payer_id)}, reason="dm_unavailable")
        return await self._send_message(row["outbox_id"], _response_id(dm, "id"), body, reason="dm_refused")

    async def _require_permission(self, conn: AsyncConnection, guild_id: int, contract_id: int) -> None:
        if not await is_discord_allowed(conn, guild_id, contract_id):
            raise _Terminal("permission_revoked")

    async def _discord_send(self, conn: AsyncConnection, row: RowMapping) -> dict[str, Any]:
        """Send a contract's message after checking that the contract may still act in the guild and that the channel belongs to that guild."""
        payload = _checked_keys(row["payload"], _SEND_KEYS)
        contract_id, guild_id, channel_id = _id_field(payload, "contract_id"), _id_field(payload, "guild_id"), _id_field(payload, "channel_id")
        message = payload["message"]
        if not isinstance(message, str) or not 1 <= len(message) <= DISCORD_MESSAGE_MAX_LENGTH:
            raise _Malformed("payload field message is invalid")
        await self._require_permission(conn, guild_id, contract_id)
        channel = await self._request("GET", f"channels/{channel_id}", reason="channel_unavailable")
        try:
            channel_payload = channel.json()
        except ValueError:
            channel_payload = None
        channel_guild = _snowflake(channel_payload.get("guild_id")) if isinstance(channel_payload, dict) else None
        if channel_guild != guild_id:
            raise _Terminal("channel_not_in_guild")
        return await self._send_message(row["outbox_id"], channel_id, {"content": message, "allowed_mentions": ALLOWED_MENTIONS}, reason="message_rejected")

    async def _role_change(self, conn: AsyncConnection, row: RowMapping) -> dict[str, Any]:
        """Add or remove a member's role after checking that the contract may still act in the guild; the audit log reason names the contract account and the execution."""
        payload = _checked_keys(row["payload"], _ROLE_KEYS)
        contract_id, guild_id = _id_field(payload, "contract_id"), _id_field(payload, "guild_id")
        user_id, role_id = _id_field(payload, "user_id"), _id_field(payload, "role_id")
        await self._require_permission(conn, guild_id, contract_id)
        method = "PUT" if row["kind"] == OutboxKind.DISCORD_ADD_ROLE else "DELETE"
        reason = f"RapidWire contract {contract_id} execution {row['execution_id']}"
        await self._request(method, f"guilds/{guild_id}/members/{user_id}/roles/{role_id}", headers={"X-Audit-Log-Reason": reason}, reason="role_change_rejected")
        return {"status": OutboxStatus.SENT.value}
