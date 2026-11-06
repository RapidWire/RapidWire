"""The API error envelope {"error": {"code", "message", ...}} for every failure, the DomainError status table, and the handlers and middleware that produce them."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from http import HTTPStatus
from typing import Any, Final

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rapidwire.api.body_limit import PAYLOAD_TOO_LARGE_CODE, PayloadTooLarge
from rapidwire.api.idempotency import REPLAYED_HEADER
from rapidwire.core import errors as core_errors
from rapidwire.core.errors import DomainError
from rapidwire.core.ledger import failure_output, is_replayed_error

__all__ = [
    "EXTRA_FIELD_LOC",
    "INTERNAL_ERROR_CODE",
    "KEY_LOC",
    "STATUS_BY_ERROR",
    "VALIDATION_ERROR_CODE",
    "ErrorBody",
    "ErrorDetail",
    "ErrorResponse",
    "UnhandledErrorMiddleware",
    "domain_error_body",
    "envelope",
    "install_error_handlers",
    "status_for",
]

log = logging.getLogger("rapidwire.api")

INTERNAL_ERROR_CODE: Final = "internal_error"
VALIDATION_ERROR_CODE: Final = "validation_error"
# The loc segments that stand in for names the client chose: an unknown field, and a mapping key.
EXTRA_FIELD_LOC: Final = "<extra>"
KEY_LOC: Final = "<key>"
# The loc segment pydantic appends after a mapping key that failed validation.
_PYDANTIC_KEY_MARKER: Final = "[key]"

# 404 a named item does not exist; 403 the caller may not do this; 401 no valid token; 409 the request is well formed but the current state refuses it; 422 the request itself is invalid, or the operation it ran failed on its own terms (a contract execution).
STATUS_BY_ERROR: Final[Mapping[type[DomainError], int]] = {
    DomainError: 422,
    core_errors.NotFoundError: 404,
    core_errors.Unauthorized: 401,
    core_errors.PermissionDenied: 403,
    core_errors.GuildUnavailable: 409,
    core_errors.InsufficientBalance: 409,
    core_errors.InsufficientAllowance: 409,
    core_errors.InsufficientStake: 409,
    core_errors.InsufficientShares: 409,
    core_errors.InsufficientLiquidity: 409,
    core_errors.SlippageExceeded: 409,
    core_errors.NoRoute: 409,
    core_errors.TimelockActive: 409,
    core_errors.RequestExpired: 409,
    core_errors.NoPendingRequest: 409,
    core_errors.InvalidState: 409,
    core_errors.CurrencyRenounced: 409,
    core_errors.ContractLocked: 409,
    core_errors.ContractPaused: 409,
    core_errors.ContractStorageLimit: 409,
    core_errors.AlreadyExists: 409,
    core_errors.IdempotencyConflict: 422,
    core_errors.InvalidAmount: 422,
    core_errors.InvalidArgument: 422,
    core_errors.InvalidContract: 422,
    core_errors.AmountOverflow: 422,
    core_errors.RevertError: 422,
    core_errors.ContractExecutionError: 422,
}

_HTTP_CODES: Final[Mapping[int, tuple[str, str]]] = {
    401: ("unauthorized", core_errors.Unauthorized.default_message),
    404: ("not_found", "The requested resource was not found."),
    405: ("method_not_allowed", "The method is not allowed for this resource."),
    413: (PAYLOAD_TOO_LARGE_CODE, "The request body is too large."),
}


class ErrorDetail(BaseModel):
    """One request validation failure: where it is (for example ["body", "amount"]), a short message and the error type; the offending value is never included, and a field name or key the client chose is replaced by "<extra>" or "<key>"."""

    loc: list[str | int]
    message: str
    type: str


class ErrorBody(BaseModel):
    code: str = Field(description="A stable snake_case error code.")
    message: str = Field(description="An English message safe to show to users.")
    execution_id: str | None = Field(default=None, description="The recorded failed execution, when the failure was recorded.")
    kind: str | None = Field(
        default=None, description="The failure kind of a contract execution (REVERTED, RUNTIME, OUT_OF_GAS, CALL_FAILED, MEMORY_LIMIT, WALL_CLOCK_LIMIT, HOST_TIME_LIMIT or SETTLEMENT)."
    )
    reason: str | None = Field(default=None, description="The CALL_FAILED reason of a contract execution.")
    child: dict[str, str | None] | None = Field(default=None, description="The failed nested execution that caused this failure: type, contract_function, code and message.")
    details: list[ErrorDetail] | None = Field(default=None, description="The individual failures of a request validation error.")


class ErrorResponse(BaseModel):
    """The body of every error response."""

    error: ErrorBody


def status_for(error: DomainError) -> int:
    """The HTTP status of a DomainError: the STATUS_BY_ERROR entry of its nearest class in method resolution order."""
    for cls in type(error).__mro__:
        status = STATUS_BY_ERROR.get(cls)
        if status is not None:
            return status
    return STATUS_BY_ERROR[DomainError]


def envelope(code: str, message: str, **extra: Any) -> dict[str, Any]:
    """An error body with the optional fields that are not None."""
    return {"error": {"code": code, "message": message, **{key: value for key, value in extra.items() if value is not None}}}


def domain_error_body(error: DomainError) -> dict[str, Any]:
    """The envelope of a DomainError, with the recorded execution, the contract failure kind and reason, and the failed child when present; a replayed error gives the same body as the original."""
    fields = failure_output(error)
    kind = fields.get("kind")
    reason = fields.get("reason")
    return envelope(
        error.code,
        error.message,
        execution_id=None if error.execution_id is None else str(error.execution_id),
        kind=kind if isinstance(kind, str) else None,
        reason=reason if isinstance(reason, str) else None,
        child=None if error.child is None else dict(error.child),
    )


def _safe_loc(raw: Sequence[Any], error_type: str) -> list[str | int]:
    """The loc of a validation error without client-chosen names: the last segment of an extra_forbidden error is the unknown field's name, and the segment before pydantic's "[key]" marker is a mapping key."""
    loc: list[str | int] = [part if isinstance(part, int) else str(part) for part in raw]
    for index, part in enumerate(loc):
        if part == _PYDANTIC_KEY_MARKER and index > 0:
            loc[index - 1] = KEY_LOC
    if error_type == "extra_forbidden" and loc:
        loc[-1] = EXTRA_FIELD_LOC
    return loc


def _validation_details(errors: Sequence[Any]) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for item in errors:
        error_type = str(item.get("type", "value_error"))
        details.append({"loc": _safe_loc(item.get("loc", ()), error_type), "message": str(item.get("msg", "Invalid value.")), "type": error_type})
    return details


async def _domain_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, DomainError):
        raise exc
    status = status_for(exc)
    headers: dict[str, str] = {}
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    if is_replayed_error(exc):
        headers[REPLAYED_HEADER] = "true"
    return JSONResponse(domain_error_body(exc), status_code=status, headers=headers)


async def _validation_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, RequestValidationError):
        raise exc
    body = envelope(VALIDATION_ERROR_CODE, "The request is invalid.", details=_validation_details(exc.errors()))
    return JSONResponse(body, status_code=422)


async def _http_error_handler(request: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, StarletteHTTPException):
        raise exc
    status = exc.status_code
    known = _HTTP_CODES.get(status)
    if isinstance(exc, PayloadTooLarge):
        code, message = PAYLOAD_TOO_LARGE_CODE, str(exc.detail)
    elif known is not None:
        code, message = known
    else:
        try:
            phrase = HTTPStatus(status).phrase
        except ValueError:
            phrase = "HTTP error"
        code, message = phrase.lower().replace(" ", "_").replace("-", "_"), f"{phrase}."
    headers = dict(exc.headers or {})
    if status == 401:
        headers["WWW-Authenticate"] = "Bearer"
    return JSONResponse(envelope(code, message), status_code=status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    """Register the envelope handlers for DomainError, request validation and HTTP errors (including 404 and 405 from routing and static files), and the middleware for every other exception."""
    app.add_exception_handler(DomainError, _domain_error_handler)
    app.add_exception_handler(RequestValidationError, _validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, _http_error_handler)
    app.add_middleware(UnhandledErrorMiddleware)


class UnhandledErrorMiddleware:
    """Turn any exception escaping the application into a logged traceback and a 500 internal_error envelope that carries no exception text; when the response has already started the exception propagates."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = False

        async def tracking_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception:
            log.exception("unhandled error in API request", extra={"method": scope.get("method"), "path": scope.get("path")})
            if started:
                raise
            response = JSONResponse(envelope(INTERNAL_ERROR_CODE, "Internal server error."), status_code=500)
            await response(scope, receive, send)
