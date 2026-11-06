"""Client exceptions: RapidWireAPIError and its subclasses by HTTP status for error envelopes, ResponseFormatError for answers that do not follow the API format, and ChecksumMismatchError for downloads whose bytes do not match their digest header."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import httpx

__all__ = [
    "REPLAYED_HEADER",
    "AuthenticationError",
    "ChecksumMismatchError",
    "ConflictError",
    "ErrorDetail",
    "NotFoundError",
    "PermissionDeniedError",
    "RapidWireAPIError",
    "RapidWireError",
    "ResponseFormatError",
    "ServerError",
    "ValidationError",
    "error_from_response",
]

REPLAYED_HEADER: Final = "Idempotency-Replayed"
_ID_RE: Final = re.compile(r"[1-9][0-9]*")


class RapidWireError(Exception):
    """The base of every exception the client raises itself."""


class ResponseFormatError(RapidWireError):
    """The server answered with a body or header that does not follow the API format."""


class ChecksumMismatchError(RapidWireError):
    """Downloaded bytes whose SHA-256 or size differs from what the response headers announced."""

    def __init__(self, message: str, *, expected: str, actual: str) -> None:
        super().__init__(message)
        self.expected = expected
        self.actual = actual


@dataclass(frozen=True, slots=True)
class ErrorDetail:
    """One field of a request validation error, such as loc ("body", "amount")."""

    loc: tuple[str | int, ...]
    message: str
    type: str


class RapidWireAPIError(RapidWireError):
    """An error envelope answered by the API; execution_id names the recorded failed execution, kind and reason describe a failed contract execution, and replayed is True when the answer came from an earlier request with the same idempotency key."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        execution_id: int | None = None,
        kind: str | None = None,
        reason: str | None = None,
        child: Mapping[str, Any] | None = None,
        details: tuple[ErrorDetail, ...] | None = None,
        replayed: bool = False,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(f"[{status_code} {code}] {message}")
        self.status_code = status_code
        self.code = code
        self.message = message
        self.execution_id = execution_id
        self.kind = kind
        self.reason = reason
        self.child = None if child is None else dict(child)
        self.details = details
        self.replayed = replayed
        self.retry_after = retry_after


class AuthenticationError(RapidWireAPIError):
    """401: the token is missing, malformed, unknown or revoked."""


class PermissionDeniedError(RapidWireAPIError):
    """403: the caller may not do the operation."""


class NotFoundError(RapidWireAPIError):
    """404: a named item does not exist."""


class ConflictError(RapidWireAPIError):
    """409: the request is well formed but the current state refuses it."""


class ValidationError(RapidWireAPIError):
    """422: the request is invalid, or the operation it ran failed on its own terms."""


class ServerError(RapidWireAPIError):
    """5xx: the server failed or a service it needs is unavailable."""


_BY_STATUS: Final[Mapping[int, type[RapidWireAPIError]]] = {401: AuthenticationError, 403: PermissionDeniedError, 404: NotFoundError, 409: ConflictError, 422: ValidationError}


def _class_for(status: int) -> type[RapidWireAPIError]:
    if status >= 500:
        return ServerError
    return _BY_STATUS.get(status, RapidWireAPIError)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _details(raw: object) -> tuple[ErrorDetail, ...] | None:
    if not isinstance(raw, list):
        return None
    details = []
    for item in raw:
        if isinstance(item, dict):
            loc = tuple(part for part in item.get("loc", ()) if isinstance(part, str | int) and not isinstance(part, bool))
            details.append(ErrorDetail(loc=loc, message=str(item.get("message", "")), type=str(item.get("type", ""))))
    return tuple(details)


def error_from_response(response: httpx.Response) -> RapidWireAPIError:
    """The exception for an error response; a body without the error envelope gives code http_error and the status phrase as message."""
    status = response.status_code
    cls = _class_for(status)
    replayed = response.headers.get(REPLAYED_HEADER) == "true"
    retry = response.headers.get("Retry-After")
    retry_after = int(retry) if retry is not None and retry.isdigit() else None
    try:
        body = response.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    if not isinstance(error, dict) or not isinstance(error.get("code"), str):
        return cls(status, "http_error", response.reason_phrase or "HTTP error", replayed=replayed, retry_after=retry_after)
    execution_text = _text(error.get("execution_id"))
    child = error.get("child")
    return cls(
        status,
        error["code"],
        _text(error.get("message")) or "",
        execution_id=int(execution_text) if execution_text is not None and _ID_RE.fullmatch(execution_text) else None,
        kind=_text(error.get("kind")),
        reason=_text(error.get("reason")),
        child=child if isinstance(child, dict) else None,
        details=_details(error.get("details")),
        replayed=replayed,
        retry_after=retry_after,
    )
