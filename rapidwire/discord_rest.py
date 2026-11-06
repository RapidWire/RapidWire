"""The Discord REST API as the API and the worker use it: one base URL, User-Agent and timeout, and readers for Discord's rate-limit headers, 429 waits and error bodies."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import httpx

__all__ = [
    "DEFAULT_RETRY_AFTER_SECONDS",
    "DISCORD_API_BASE_URL",
    "DISCORD_TIMEOUT_SECONDS",
    "DISCORD_USER_AGENT",
    "MAX_RETRY_AFTER_SECONDS",
    "DiscordError",
    "RateLimit",
    "discord_client",
    "read_error",
    "read_rate_limit",
    "retry_after",
]

DISCORD_API_BASE_URL: Final = "https://discord.com/api/v10"
DISCORD_USER_AGENT: Final = "DiscordBot (https://github.com/RapidWire/RapidWire, 1.0)"
DISCORD_TIMEOUT_SECONDS: Final = 5.0
# The wait after a 429 whose wait cannot be read, and the longest wait a 429 can impose.
DEFAULT_RETRY_AFTER_SECONDS: Final = 5.0
MAX_RETRY_AFTER_SECONDS: Final = 3_600.0
# Discord error messages are recorded up to this many characters.
ERROR_MESSAGE_MAX: Final = 200


def discord_client(token: str | None, *, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """An httpx client for the Discord REST API with the bot User-Agent and, when token is given, the bot Authorization header; transport replaces the network in tests."""
    headers = {"User-Agent": DISCORD_USER_AGENT}
    if token is not None:
        headers["Authorization"] = f"Bot {token}"
    return httpx.AsyncClient(base_url=DISCORD_API_BASE_URL, headers=headers, timeout=DISCORD_TIMEOUT_SECONDS, transport=transport, follow_redirects=False)


def _seconds(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None


def _json_object(response: httpx.Response) -> dict[str, object] | None:
    try:
        payload = response.json()
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def retry_after(response: httpx.Response) -> float:
    """The wait of a 429 in seconds from its JSON retry_after or its Retry-After header, clamped to 0..MAX_RETRY_AFTER_SECONDS; DEFAULT_RETRY_AFTER_SECONDS when neither can be read."""
    payload = _json_object(response)
    candidates = [payload.get("retry_after") if payload is not None else None, response.headers.get("Retry-After")]
    for value in candidates:
        seconds = _seconds(value)
        if seconds is not None:
            return min(max(seconds, 0.0), MAX_RETRY_AFTER_SECONDS)
    return DEFAULT_RETRY_AFTER_SECONDS


@dataclass(frozen=True)
class RateLimit:
    """The X-RateLimit-* headers of a response: the route's bucket, the requests left in it and the seconds until it resets, whether a 429 is the global limit, and the scope (user, global or shared)."""

    bucket: str | None
    remaining: int | None
    reset_after: float | None
    is_global: bool
    scope: str | None


def read_rate_limit(response: httpx.Response) -> RateLimit:
    """The rate-limit headers of response; a 429 is global when its X-RateLimit-Global header or JSON global flag is true or its scope is global."""
    headers = response.headers
    remaining_text = headers.get("X-RateLimit-Remaining")
    remaining = int(remaining_text) if remaining_text is not None and remaining_text.isascii() and remaining_text.isdigit() else None
    reset_after = _seconds(headers.get("X-RateLimit-Reset-After"))
    scope = headers.get("X-RateLimit-Scope")
    is_global = headers.get("X-RateLimit-Global", "").lower() == "true" or scope == "global"
    if response.status_code == 429 and not is_global:
        payload = _json_object(response)
        is_global = payload is not None and payload.get("global") is True
    return RateLimit(
        bucket=headers.get("X-RateLimit-Bucket"),
        remaining=remaining,
        reset_after=None if reset_after is None else min(max(reset_after, 0.0), MAX_RETRY_AFTER_SECONDS),
        is_global=is_global,
        scope=scope,
    )


@dataclass(frozen=True)
class DiscordError:
    """Discord's JSON error code (as a decimal string) and message of a failed response, each None when absent."""

    code: str | None
    message: str | None


def read_error(response: httpx.Response) -> DiscordError:
    """The top-level code and message of a Discord error body, the message cut to ERROR_MESSAGE_MAX characters; field-level details are not read."""
    payload = _json_object(response)
    if payload is None:
        return DiscordError(code=None, message=None)
    code = payload.get("code")
    message = payload.get("message")
    return DiscordError(
        code=str(code) if isinstance(code, int) and not isinstance(code, bool) else None,
        message=message[:ERROR_MESSAGE_MAX] if isinstance(message, str) else None,
    )
