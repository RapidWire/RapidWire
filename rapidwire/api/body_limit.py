"""The request body size cap: BodyLimitMiddleware answers 413 payload_too_large for a body over its route's limit, from Content-Length before the application runs or as soon as the streamed body passes it, so no oversized body is buffered, parsed or authenticated.

Every route accepts DEFAULT_BODY_LIMIT bytes, the largest contract call body; an endpoint declares another limit with @body_limit(...) under its route decorator.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Final

from fastapi.routing import APIRoute, iter_route_contexts
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse
from starlette.routing import BaseRoute, compile_path
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rapidwire.config import Settings
from rapidwire.contract.limits import DEFAULT_LIMITS, Limits
from rapidwire.db.tables import CONTRACT_FUNCTION_MAX

__all__ = [
    "CALL_BODY_OVERHEAD",
    "DEFAULT_BODY_LIMIT",
    "JSON_CHAR_BYTES_MAX",
    "PAYLOAD_TOO_LARGE_CODE",
    "TYPED_VALUE_OVERHEAD",
    "BodyLimitMiddleware",
    "PayloadTooLarge",
    "RouteBodyLimit",
    "body_limit",
    "contract_call_body_limit",
    "payload_too_large_body",
    "route_body_limits",
]

PAYLOAD_TOO_LARGE_CODE: Final = "payload_too_large"
# The longest standard JSON spelling of one character: a code point above U+FFFF written as two \uXXXX escapes.
JSON_CHAR_BYTES_MAX: Final = 12
# Bytes of one {"type": ..., "value": ...} argument besides its value, every character escaped, with spacing.
TYPED_VALUE_OVERHEAD: Final = 128
# Bytes of a call body besides its arguments and function name: the field names, attach, gas_limit and spacing.
CALL_BODY_OVERHEAD: Final = 1024

type BodyLimit = int | Callable[[Settings], int]

_LIMIT_ATTRIBUTE: Final = "__rapidwire_body_limit__"


def contract_call_body_limit(limits: Limits = DEFAULT_LIMITS) -> int:
    """The largest body of a valid contract call or dry run: limits.max_params arguments, each the longest scalar (a str of limits.str_max_length characters, every one escaped), and a function name of CONTRACT_FUNCTION_MAX escaped characters."""
    longest_scalar = max(limits.str_max_length * JSON_CHAR_BYTES_MAX, len(str(limits.int_bound)) + 1)
    return limits.max_params * (longest_scalar + TYPED_VALUE_OVERHEAD) + CONTRACT_FUNCTION_MAX * JSON_CHAR_BYTES_MAX + CALL_BODY_OVERHEAD


DEFAULT_BODY_LIMIT: Final = contract_call_body_limit()


def body_limit[F: Callable[..., Any]](limit: BodyLimit) -> Callable[[F], F]:
    """Declare the body limit of an endpoint in bytes, or as a function of the application's Settings; apply it below the route decorator."""
    if isinstance(limit, int) and (isinstance(limit, bool) or limit < 1):
        raise ValueError("a body limit must be a positive number of bytes")

    def mark(endpoint: F) -> F:
        setattr(endpoint, _LIMIT_ATTRIBUTE, limit)
        return endpoint

    return mark


@dataclass(frozen=True)
class RouteBodyLimit:
    """A route whose endpoint declared its own body limit, resolved against the settings: path is the route's full path as served, including every include_router() prefix, and methods its HTTP methods."""

    path: str
    methods: frozenset[str]
    limit: int
    path_regex: re.Pattern[str] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        regex, _, _ = compile_path(self.path)
        object.__setattr__(self, "path_regex", regex)

    def matches(self, scope: Scope) -> bool:
        return scope.get("method") in self.methods and self.path_regex.match(_route_path(scope)) is not None


def _route_path(scope: Scope) -> str:
    """The request path below the ASGI root_path, the part routes are matched against."""
    path = str(scope.get("path", ""))
    root = str(scope.get("root_path", ""))
    if root and path.startswith(root) and (len(path) == len(root) or path[len(root)] == "/"):
        return path[len(root) :]
    return path


def route_body_limits(routes: Sequence[BaseRoute], settings: Settings) -> tuple[RouteBodyLimit, ...]:
    """The routes among routes (included routers too, at their prefixed paths) whose endpoints declared a body limit with body_limit()."""
    found: list[RouteBodyLimit] = []
    for context in iter_route_contexts(routes):
        route = context.original_route
        if not isinstance(route, APIRoute):
            continue
        declared = getattr(route.endpoint, _LIMIT_ATTRIBUTE, None)
        if declared is None:
            continue
        limit = declared if isinstance(declared, int) else declared(settings)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError(f"the body limit of {context.path} must be a positive number of bytes")
        if context.path is None:
            raise ValueError(f"the route of {route.path} has no served path")
        found.append(RouteBodyLimit(path=context.path, methods=frozenset(context.methods or route.methods or ()), limit=limit))
    return tuple(found)


def payload_too_large_body(limit: int) -> dict[str, Any]:
    return {"error": {"code": PAYLOAD_TOO_LARGE_CODE, "message": f"The request body is larger than the {limit} bytes this endpoint accepts."}}


class PayloadTooLarge(StarletteHTTPException):
    """Raised from the body stream of a request whose body passed limit bytes; the error handlers answer it with the payload_too_large envelope."""

    def __init__(self, limit: int) -> None:
        super().__init__(status_code=413, detail=payload_too_large_body(limit)["error"]["message"], headers={"Connection": "close"})
        self.limit = limit


def _declared_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length" and value.isdigit():
            return int(value)
    return None


class BodyLimitMiddleware:
    """Cap request bodies at default bytes, or at the limit of the first route in limits that matches the request path and method.

    A Content-Length above the limit is answered with 413 before the application runs; a body sent without one (or longer than it says) makes the application's next read raise PayloadTooLarge as soon as the bytes received pass the limit, so no route ever sees more than the limit.
    """

    def __init__(self, app: ASGIApp, *, default: int = DEFAULT_BODY_LIMIT, limits: Iterable[RouteBodyLimit] = ()) -> None:
        if default < 1:
            raise ValueError("the default body limit must be positive")
        self.app = app
        self.default = default
        self.limits = tuple(limits)

    def limit_for(self, scope: Scope) -> int:
        for entry in self.limits:
            if entry.matches(scope):
                return entry.limit
        return self.default

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.limit_for(scope)
        declared = _declared_length(scope)
        if declared is not None and declared > limit:
            response = JSONResponse(payload_too_large_body(limit), status_code=413, headers={"Connection": "close"})
            await response(scope, receive, send)
            return
        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > limit:
                    raise PayloadTooLarge(limit)
            return message

        await self.app(scope, limited_receive, send)
