"""Bearer-token authentication: require_principal and require_account for endpoints that need a token, and Writer, which hands a write endpoint the token's account and idempotency key while a write guard re-checks the token inside every write transaction of the request."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from types import TracebackType
from typing import Annotated, Final, Self

from fastapi import Depends, Request, Response, Security
from fastapi.security import HTTPBearer
from sqlalchemy.ext.asyncio import AsyncConnection

from rapidwire.api.deps import StateDep
from rapidwire.api.idempotency import IdempotencyKeyHeader, Replayable, mark_replayed
from rapidwire.core.errors import Unauthorized
from rapidwire.core.ledger import write_guard
from rapidwire.core.tokens import SecretToken, authenticate_token, token_account_in_tx

__all__ = [
    "AccountDep",
    "Principal",
    "PrincipalDep",
    "Writer",
    "WriterDep",
    "bearer_scheme",
    "get_writer",
    "require_account",
    "require_principal",
]

_SCHEME: Final = "bearer"

# Declared for the OpenAPI security scheme; the header itself is parsed by _bearer_token() so that a malformed header is distinguishable from a missing one.
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerToken", description="An API token issued by the Discord bot, sent as Authorization: Bearer <token>.")


@dataclass(frozen=True)
class Principal:
    """The authenticated caller: the token's account and the token itself (redacted in repr), kept so writes can re-check it in their transaction."""

    account_id: int
    token: SecretToken = field(repr=False)


def _bearer_token(request: Request) -> str | None:
    """The token of the Authorization header, None when the header is absent; Unauthorized when it is present but not "Bearer <token>"."""
    header = request.headers.get("Authorization")
    if header is None:
        return None
    scheme, _, token = header.strip().partition(" ")
    token = token.strip()
    if scheme.lower() != _SCHEME or not token or " " in token:
        raise Unauthorized()
    return token


async def _authenticate(request: Request, state: StateDep) -> Principal | None:
    token = _bearer_token(request)
    if token is None:
        return None
    account_id = await authenticate_token(state.engine, token)
    if account_id is None:
        raise Unauthorized()
    return Principal(account_id=account_id, token=SecretToken(token))


async def require_principal(request: Request, state: StateDep, _: Annotated[object, Security(bearer_scheme)] = None) -> Principal:
    """The caller of an endpoint that needs a token; a missing, malformed, unknown or revoked token raises Unauthorized (401 with WWW-Authenticate: Bearer)."""
    principal = await _authenticate(request, state)
    if principal is None:
        raise Unauthorized()
    return principal


async def require_account(principal: Annotated[Principal, Depends(require_principal)]) -> int:
    """The account ID of the caller of an endpoint that needs a token."""
    return principal.account_id


PrincipalDep = Annotated[Principal, Depends(require_principal)]
AccountDep = Annotated[int, Depends(require_account)]


class Writer:
    """A write made by the authenticated caller: account_id is the initiator to pass to the service and idempotency_key its idempotency_key argument.

    While the writer is active, every run_operation() re-checks the token with FOR SHARE in its own transaction before anything is recorded and raises Unauthorized when it was revoked meanwhile or belongs to another account; get_writer() keeps it active for the whole request, so write endpoints call their services directly, and `async with writer:` activates it for a block, which only a Writer built outside get_writer() needs (the admin endpoints build one themselves).
    """

    def __init__(self, principal: Principal, idempotency_key: str | None, response: Response) -> None:
        self.account_id = principal.account_id
        self.idempotency_key = idempotency_key
        self.response = response
        self._token = principal.token
        self._guard_contexts: list[AbstractContextManager[None]] = []

    async def guard(self, conn: AsyncConnection, initiator_id: int) -> None:
        """The write guard: raise Unauthorized unless the token is active, read under FOR SHARE in conn's transaction, and belongs to initiator_id."""
        account_id = await token_account_in_tx(conn, self._token.reveal())
        if account_id is None or account_id != initiator_id:
            raise Unauthorized()

    def activate(self) -> AbstractContextManager[None]:
        """A context in which every run_operation() awaits guard()."""
        return write_guard(self.guard)

    async def __aenter__(self) -> Self:
        context = self.activate()
        context.__enter__()
        self._guard_contexts.append(context)
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self._guard_contexts.pop().__exit__(exc_type, exc, tb)

    def done[T: Replayable](self, result: T) -> T:
        """Set Idempotency-Replayed: true on the response when result came from an earlier execution under the same key, and return result."""
        return mark_replayed(self.response, result)


async def get_writer(principal: PrincipalDep, response: Response, idempotency_key: IdempotencyKeyHeader = None) -> AsyncIterator[Writer]:
    """The Writer of a write endpoint, active from before the endpoint runs until it returns, so every run_operation() of the request is guarded whether or not the endpoint enters it."""
    writer = Writer(principal, idempotency_key, response)
    with writer.activate():
        yield writer


WriterDep = Annotated[Writer, Depends(get_writer)]
