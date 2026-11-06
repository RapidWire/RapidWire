"""The RapidWire API clients: every endpoint is defined once as a request builder with its route, and AsyncRapidWireClient and RapidWireClient differ only in how they send the request it builds."""

from __future__ import annotations

import abc
import asyncio
import base64
import dataclasses
import functools
import hashlib
import os
import re
import secrets
from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Concatenate, Final, Literal, Self, cast, final, overload
from urllib.parse import quote

import httpx

from rapidwire_client._parse import parse_as
from rapidwire_client._version import __version__
from rapidwire_client.amounts import AmountInput, amount_string
from rapidwire_client.errors import REPLAYED_HEADER, ChecksumMismatchError, ResponseFormatError, error_from_response
from rapidwire_client.models import (
    AccountBalance,
    AccountInfo,
    AccountStats,
    Allowance,
    AllowanceChange,
    ApproveResult,
    BackupDownload,
    BackupList,
    Balance,
    CallResult,
    Claim,
    ClaimCanceled,
    ClaimCreated,
    ClaimPaid,
    ContractBinary,
    ContractDeleted,
    ContractFunctions,
    ContractInfo,
    ContractLock,
    ContractRegistered,
    ContractStatusChange,
    ContractVersion,
    ContractVersionSummary,
    CurrencyCreated,
    CurrencyDeleted,
    CurrencyInfo,
    CurrencySummary,
    DelegationChange,
    DeleteRequested,
    DiscordPermission,
    DiscordPermissionChange,
    DryRunResult,
    Execution,
    ExecutionDetail,
    LiquidityChange,
    MintingRenounced,
    NotificationChange,
    NotificationPermission,
    Page,
    PairPools,
    Pool,
    PoolCreated,
    Position,
    RateChangeApplied,
    RateChangeRequested,
    ServiceConfig,
    Stake,
    StakeChange,
    StorageEntry,
    StorageValue,
    SupplyChange,
    SwapQuote,
    SwapResult,
    TokenInfo,
    TokensRevoked,
    TransferEntry,
    TransferFromResult,
    TransferResult,
    Username,
    VersionInfo,
    WriteResult,
    _CurrencyList,
)
from rapidwire_client.values import ContractValue, encode_args

__all__ = [
    "CHECKSUM_HEADER",
    "DEFAULT_TIMEOUT",
    "IDEMPOTENCY_KEY_HEADER",
    "IDEMPOTENCY_KEY_MAX",
    "AsyncBackupStream",
    "AsyncRapidWireClient",
    "Attachment",
    "BackupStream",
    "RapidWireClient",
    "client_routes",
    "new_idempotency_key",
]

DEFAULT_TIMEOUT: Final = 30.0
IDEMPOTENCY_KEY_HEADER: Final = "Idempotency-Key"
IDEMPOTENCY_KEY_MAX: Final = 255
CHECKSUM_HEADER: Final = "X-Checksum-SHA256"
USER_AGENT: Final = f"rapidwire-client/{__version__}"

_KEY_RE: Final = re.compile(r"[\x21-\x7e]([\x20-\x7e]*[\x21-\x7e])?")
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_PARAM_RE: Final = re.compile(r"\{(\w+)(:path)?\}")
_FILENAME_RE: Final = re.compile(r'filename="([^"]*)"')
_ROUTE_ATTR: Final = "__rapidwire_route__"
_BACKUP_PATH: Final = "/v1/admin/backups/{name:path}"
# The download URL quotes every reserved character of the name, a slash included, unlike a {name:path} parameter.
_BACKUP_URL: Final = "/v1/admin/backups/{name}"

type SortBy = Literal["transfer_id", "timestamp", "amount"]
type SortOrder = Literal["asc", "desc"]
type Destination = str | os.PathLike[str] | IO[bytes]


def new_idempotency_key() -> str:
    """A random idempotency key; send the same key again to repeat a write without running it twice."""
    return secrets.token_urlsafe(24)


def _check_key(key: str | None) -> str | None:
    if key is not None and (not isinstance(key, str) or len(key) > IDEMPOTENCY_KEY_MAX or _KEY_RE.fullmatch(key) is None):
        raise ValueError(f"an idempotency key has 1 to {IDEMPOTENCY_KEY_MAX} printable ASCII characters without leading or trailing spaces")
    return key


def _id(value: int) -> str:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"IDs and integers are ints, not {type(value).__name__}")
    return str(value)


def _number(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"expected an int, not {type(value).__name__}")
    return value


def _time(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("times must be aware datetimes with a UTC offset")
    return value.isoformat()


def _query(values: Mapping[str, str | int | bool | None]) -> dict[str, str]:
    query: dict[str, str] = {}
    for name, value in values.items():
        if value is None:
            continue
        if isinstance(value, bool):
            query[name] = "true" if value else "false"
        elif isinstance(value, int):
            query[name] = str(value)
        else:
            query[name] = value
    return query


def _paging(page: int | None, per_page: int | None) -> dict[str, int | None]:
    return {"page": None if page is None else _number(page), "per_page": None if per_page is None else _number(per_page)}


def _format_path(template: str, values: Mapping[str, str]) -> str:
    def substitute(match: re.Match[str]) -> str:
        return quote(values[match.group(1)], safe="/" if match.group(2) else "")

    return _PARAM_RE.sub(substitute, template)


class _Token:
    """The API token, redacted in repr and str."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        if not isinstance(value, str) or not value or any(char.isspace() for char in value):
            raise ValueError("the token must be a non-empty string without spaces")
        self._value = value

    def __repr__(self) -> str:
        return "<redacted token>"

    __str__ = __repr__

    def reveal(self) -> str:
        return self._value


@dataclass(frozen=True, kw_only=True)
class Attachment:
    """Currency moved from the caller to a called contract when the call starts."""

    currency_id: int
    amount: AmountInput

    def to_json(self) -> dict[str, str]:
        return {"currency_id": _id(self.currency_id), "amount": amount_string(self.amount)}


@dataclass(frozen=True, kw_only=True)
class _Spec[T]:
    """One request of an endpoint: the path parameters (already text), query, JSON body (None for no body), idempotency key and the parser of the success response."""

    parse: Callable[[httpx.Response], T]
    path: Mapping[str, str] = field(default_factory=dict)
    query: Mapping[str, str] = field(default_factory=dict)
    body: Any = None
    idempotency_key: str | None = None


def _parse_json[T](hint: type[T], response: httpx.Response) -> T:
    try:
        data = response.json()
    except ValueError:
        raise ResponseFormatError("The response body is not JSON.") from None
    result = parse_as(hint, data)
    if isinstance(result, WriteResult) and response.headers.get(REPLAYED_HEADER) == "true":
        return cast(T, dataclasses.replace(result, replayed=True))
    return result


def _json[T](hint: type[T], *, path: Mapping[str, str] | None = None, query: Mapping[str, str] | None = None, body: Any = None, idempotency_key: str | None = None) -> _Spec[T]:
    return _Spec(parse=functools.partial(_parse_json, hint), path=path or {}, query=query or {}, body=body, idempotency_key=_check_key(idempotency_key))


def _parse_currencies(response: httpx.Response) -> tuple[CurrencySummary, ...]:
    return _parse_json(_CurrencyList, response).items


def _parse_binary(account_id: int, version: int, response: httpx.Response) -> ContractBinary:
    expected = response.headers.get(CHECKSUM_HEADER, "")
    if _SHA256_RE.fullmatch(expected) is None:
        raise ResponseFormatError(f"The binary download has no valid {CHECKSUM_HEADER} header.")
    actual = hashlib.sha256(response.content).hexdigest()
    if actual != expected:
        raise ChecksumMismatchError(f"The downloaded binary does not match its {CHECKSUM_HEADER} header.", expected=expected, actual=actual)
    match = _FILENAME_RE.search(response.headers.get("Content-Disposition", ""))
    return ContractBinary(account_id=account_id, version=version, data=response.content, sha256=actual, filename=None if match is None else match.group(1))


def _finish[T](spec: _Spec[T], response: httpx.Response) -> T:
    if not response.is_success:
        raise error_from_response(response)
    return spec.parse(response)


class _Endpoint[**P, T]:
    """An endpoint: its method and route template, whether it sends the token, and the builder of its request; reading it from a client gives the bound method, a coroutine function on AsyncRapidWireClient and a plain function on RapidWireClient."""

    def __init__(self, method: str, path: str, auth: bool, build: Callable[Concatenate[Any, P], _Spec[T]]) -> None:
        self.method = method
        self.path = path
        self.auth = auth
        self.build = build
        self.name = build.__name__
        self.__doc__ = build.__doc__

    @overload
    def __get__(self, obj: None, owner: type[Any] | None = None) -> Self: ...

    @overload
    def __get__(self, obj: AsyncRapidWireClient, owner: type[Any] | None = None) -> Callable[P, Coroutine[Any, Any, T]]: ...

    @overload
    def __get__(self, obj: RapidWireClient, owner: type[Any] | None = None) -> Callable[P, T]: ...

    def __get__(self, obj: object, owner: type[Any] | None = None) -> Any:
        if obj is None:
            return self
        if not isinstance(obj, _Client):
            raise TypeError("endpoints are read from a RapidWire client")
        client = obj

        def bound(*args: P.args, **kwargs: P.kwargs) -> Any:
            return client._send(self, self.build(client, *args, **kwargs))

        bound.__name__ = self.name
        bound.__qualname__ = f"{type(client).__name__}.{self.name}"
        bound.__doc__ = self.__doc__
        return bound


def _endpoint[**P, T](method: Literal["GET", "POST"], path: str, *, auth: bool = False) -> Callable[[Callable[Concatenate[Any, P], _Spec[T]]], _Endpoint[P, T]]:
    def wrap(build: Callable[Concatenate[Any, P], _Spec[T]]) -> _Endpoint[P, T]:
        return _Endpoint(method, path, auth, build)

    return wrap


def _route[F: Callable[..., Any]](method: str, path: str) -> Callable[[F], F]:
    """Mark a hand-written method as the client of a route, for client_routes()."""

    def mark(function: F) -> F:
        setattr(function, _ROUTE_ATTR, (method, path))
        return function

    return mark


def client_routes(cls: type) -> frozenset[tuple[str, str]]:
    """The (method, route template) pairs a client class has a method for."""
    routes: set[tuple[str, str]] = set()
    for klass in cls.__mro__:
        for value in vars(klass).values():
            if isinstance(value, _Endpoint):
                routes.add((value.method, value.path))
            elif (marked := getattr(value, _ROUTE_ATTR, None)) is not None:
                routes.add(marked)
    return frozenset(routes)


class _DigestCheck:
    """The SHA-256, size and execution announced by a backup download's headers, checked against the bytes read."""

    def __init__(self, name: str, response: httpx.Response) -> None:
        headers = response.headers
        expected = headers.get(CHECKSUM_HEADER, "")
        if _SHA256_RE.fullmatch(expected) is None:
            raise ResponseFormatError(f"The backup download has no valid {CHECKSUM_HEADER} header.")
        length = headers.get("Content-Length")
        execution = headers.get("X-Execution-Id")
        self.name = name
        self.sha256 = expected
        # A Content-Encoding added on the way makes Content-Length count the encoded bytes, so only the digest is compared then.
        self.size = int(length) if length is not None and length.isdigit() and "Content-Encoding" not in headers else None
        self.execution_id = int(execution) if execution is not None and execution.isdigit() else None
        self.content_type = headers.get("Content-Type")
        self.result: BackupDownload | None = None
        self._hash = hashlib.sha256()
        self._read = 0

    def update(self, chunk: bytes) -> None:
        self._hash.update(chunk)
        self._read += len(chunk)

    def finish(self) -> BackupDownload:
        actual = self._hash.hexdigest()
        if actual != self.sha256:
            raise ChecksumMismatchError(f"The downloaded backup does not match its {CHECKSUM_HEADER} header.", expected=self.sha256, actual=actual)
        if self.size is not None and self._read != self.size:
            raise ChecksumMismatchError("The downloaded backup is shorter or longer than its Content-Length.", expected=str(self.size), actual=str(self._read))
        self.result = BackupDownload(name=self.name, size=self._read, sha256=actual, execution_id=self.execution_id, content_type=self.content_type)
        return self.result

    def completed(self) -> BackupDownload:
        if self.result is None:
            raise RuntimeError("the backup has not been read to the end")
        return self.result


class _StreamInfo:
    def __init__(self, check: _DigestCheck) -> None:
        self._check = check
        self.name = check.name
        self.sha256 = check.sha256
        self.size = check.size
        self.execution_id = check.execution_id
        self.content_type = check.content_type

    @property
    def result(self) -> BackupDownload:
        """The verified download, available once the bytes were read to the end without a mismatch."""
        return self._check.completed()


class AsyncBackupStream(_StreamInfo):
    """An open backup download; iterating it yields the bytes and raises ChecksumMismatchError at the end when they differ from the announced SHA-256 or size."""

    def __init__(self, response: httpx.Response, check: _DigestCheck) -> None:
        super().__init__(check)
        self._response = response

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self._response.aiter_bytes():
            self._check.update(chunk)
            yield chunk
        self._check.finish()


class BackupStream(_StreamInfo):
    """An open backup download; iterating it yields the bytes and raises ChecksumMismatchError at the end when they differ from the announced SHA-256 or size."""

    def __init__(self, response: httpx.Response, check: _DigestCheck) -> None:
        super().__init__(check)
        self._response = response

    def __iter__(self) -> Iterator[bytes]:
        for chunk in self._response.iter_bytes():
            self._check.update(chunk)
            yield chunk
        self._check.finish()


def _partial_path(target: Path) -> Path:
    return target.with_name(target.name + ".partial")


def _history_query(
    *,
    currency_id: int | None,
    start: datetime | None,
    end: datetime | None,
    min_amount: AmountInput | None,
    max_amount: AmountInput | None,
    execution_type: str | None,
    contract_function: str | None,
    sort_by: SortBy | None,
    sort_order: SortOrder | None,
    page: int | None,
    per_page: int | None,
) -> dict[str, str | int | bool | None]:
    return {
        "currency_id": None if currency_id is None else _id(currency_id),
        "start": None if start is None else _time(start),
        "end": None if end is None else _time(end),
        "min_amount": None if min_amount is None else amount_string(min_amount),
        "max_amount": None if max_amount is None else amount_string(max_amount),
        "execution_type": execution_type,
        "contract_function": contract_function,
        "sort_by": sort_by,
        "sort_order": sort_order,
        **_paging(page, per_page),
    }


def _call_body(function: str, args: Sequence[ContractValue], attach: Attachment | None, gas_limit: int | None) -> dict[str, Any]:
    body: dict[str, Any] = {"function": function, "args": encode_args(args)}
    if attach is not None:
        body["attach"] = attach.to_json()
    if gas_limit is not None:
        body["gas_limit"] = _id(gas_limit)
    return body


class _Client(abc.ABC):
    """The endpoints shared by both clients, each building its request; base_url is the server root, before /v1, and each subclass sends the requests in its own way through _send()."""

    def __init__(self, base_url: str, token: str | None) -> None:
        self._base_url = str(base_url)
        self._token = None if token is None else _Token(token)

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self._base_url!r}, authenticated={self._token is not None})"

    @abc.abstractmethod
    def _send(self, endpoint: _Endpoint[Any, Any], spec: _Spec[Any]) -> Any:
        """Send the request spec of endpoint and return its parsed result, or an awaitable of it for the asynchronous client."""

    def _build(self, http: httpx.Client | httpx.AsyncClient, method: str, template: str, auth: bool, spec: _Spec[Any]) -> httpx.Request:
        headers: dict[str, str] = {}
        if auth and self._token is not None:
            headers["Authorization"] = f"Bearer {self._token.reveal()}"
        if spec.idempotency_key is not None:
            headers[IDEMPOTENCY_KEY_HEADER] = spec.idempotency_key
        return http.build_request(method, _format_path(template, spec.path), params=spec.query or None, headers=headers, json=spec.body)

    def _backup_request(self, http: httpx.Client | httpx.AsyncClient, name: str) -> httpx.Request:
        if not isinstance(name, str) or not name:
            raise ValueError("the backup name must be a non-empty string")
        return self._build(http, "GET", _BACKUP_URL, True, _Spec(parse=lambda response: None, path={"name": name}))

    # Service information.

    @_endpoint("GET", "/v1/version")
    def get_version(self) -> _Spec[VersionInfo]:
        """The API name and server version."""
        return _json(VersionInfo)

    @_endpoint("GET", "/v1/config")
    def get_config(self) -> _Spec[ServiceConfig]:
        """The server's limits and settings."""
        return _json(ServiceConfig)

    # Accounts and history.

    @_endpoint("GET", "/v1/accounts/{account_id}")
    def get_account(self, account_id: int) -> _Spec[AccountInfo]:
        """The kind of an account and the currency or pool a system account belongs to."""
        return _json(AccountInfo, path={"account_id": _id(account_id)})

    @_endpoint("GET", "/v1/users/{account_id}/name")
    def get_username(self, account_id: int) -> _Spec[Username]:
        """The Discord username of a user."""
        return _json(Username, path={"account_id": _id(account_id)})

    @_endpoint("GET", "/v1/users/{account_id}/stats")
    def get_user_stats(self, account_id: int) -> _Spec[AccountStats]:
        """The transfer statistics of an account."""
        return _json(AccountStats, path={"account_id": _id(account_id)})

    @_endpoint("GET", "/v1/accounts/{account_id}/balances")
    def list_balances(self, account_id: int, *, page: int | None = None, per_page: int | None = None) -> _Spec[Page[Balance]]:
        """A page of the account's non-zero balances."""
        return _json(Page[Balance], path={"account_id": _id(account_id)}, query=_query(_paging(page, per_page)))

    @_endpoint("GET", "/v1/accounts/{account_id}/balances/{currency_id}")
    def get_balance(self, account_id: int, currency_id: int) -> _Spec[AccountBalance]:
        """The account's balance in one currency, zero when it holds none."""
        return _json(AccountBalance, path={"account_id": _id(account_id), "currency_id": _id(currency_id)})

    @_endpoint("GET", "/v1/accounts/{account_id}/stakes")
    def list_stakes(self, account_id: int, *, page: int | None = None, per_page: int | None = None) -> _Spec[Page[Stake]]:
        """A page of the account's stakes."""
        return _json(Page[Stake], path={"account_id": _id(account_id)}, query=_query(_paging(page, per_page)))

    @_endpoint("GET", "/v1/accounts/{account_id}/stakes/{currency_id}")
    def get_stake(self, account_id: int, currency_id: int) -> _Spec[Stake]:
        """The account's stake in one currency."""
        return _json(Stake, path={"account_id": _id(account_id), "currency_id": _id(currency_id)})

    @_endpoint("GET", "/v1/accounts/{account_id}/history")
    def get_account_history(
        self,
        account_id: int,
        *,
        currency_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        min_amount: AmountInput | None = None,
        max_amount: AmountInput | None = None,
        execution_type: str | None = None,
        contract_function: str | None = None,
        sort_by: SortBy | None = None,
        sort_order: SortOrder | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> _Spec[Page[TransferEntry]]:
        """A page of the transfers with the account on either side; start is inclusive and end exclusive."""
        query = _history_query(
            currency_id=currency_id,
            start=start,
            end=end,
            min_amount=min_amount,
            max_amount=max_amount,
            execution_type=execution_type,
            contract_function=contract_function,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            per_page=per_page,
        )
        return _json(Page[TransferEntry], path={"account_id": _id(account_id)}, query=_query(query))

    @_endpoint("GET", "/v1/accounts/{account_id}/claims")
    def list_claims(
        self,
        account_id: int,
        *,
        status: Literal["pending", "paid", "canceled"] | None = None,
        role: Literal["claimant", "payer"] | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> _Spec[Page[Claim]]:
        """A page of the claims where the account is the claimant (role "claimant"), the payer (role "payer") or either (no role), newest first."""
        return _json(Page[Claim], path={"account_id": _id(account_id)}, query=_query({"status": status, "role": role, **_paging(page, per_page)}))

    @_endpoint("GET", "/v1/accounts/{account_id}/allowance-history")
    def list_allowance_history(
        self, account_id: int, *, spender_id: int | None = None, currency_id: int | None = None, page: int | None = None, per_page: int | None = None
    ) -> _Spec[Page[AllowanceChange]]:
        """A page of the changes to allowances the account gave, newest first."""
        query = {"spender_id": None if spender_id is None else _id(spender_id), "currency_id": None if currency_id is None else _id(currency_id), **_paging(page, per_page)}
        return _json(Page[AllowanceChange], path={"account_id": _id(account_id)}, query=_query(query))

    @_endpoint("GET", "/v1/claims/{claim_id}")
    def get_claim(self, claim_id: int) -> _Spec[Claim]:
        """One claim."""
        return _json(Claim, path={"claim_id": _id(claim_id)})

    @_endpoint("GET", "/v1/allowances/{owner_id}/{spender_id}/{currency_id}")
    def get_allowance(self, owner_id: int, spender_id: int, currency_id: int) -> _Spec[Allowance]:
        """What the spender may still move from the owner's balance."""
        return _json(Allowance, path={"owner_id": _id(owner_id), "spender_id": _id(spender_id), "currency_id": _id(currency_id)})

    @_endpoint("GET", "/v1/currencies")
    def list_currencies(self, *, include_deleted: bool = False) -> _Spec[tuple[CurrencySummary, ...]]:
        """Every live currency ordered by symbol, and the deleted ones too with include_deleted."""
        return _Spec(parse=_parse_currencies, query=_query({"include_deleted": True if include_deleted else None}))

    @_endpoint("GET", "/v1/currencies/symbol/{symbol}")
    def get_currency_by_symbol(self, symbol: str) -> _Spec[CurrencyInfo]:
        """The live currency with a symbol, matched case-insensitively."""
        if not isinstance(symbol, str) or not symbol:
            raise ValueError("the symbol must be a non-empty string")
        return _json(CurrencyInfo, path={"symbol": symbol})

    @_endpoint("GET", "/v1/currencies/{currency_id}")
    def get_currency(self, currency_id: int) -> _Spec[CurrencyInfo]:
        """One currency, deleted ones included."""
        return _json(CurrencyInfo, path={"currency_id": _id(currency_id)})

    @_endpoint("GET", "/v1/history")
    def search_history(
        self,
        *,
        transfer_id: int | None = None,
        user_id: int | None = None,
        source_id: int | None = None,
        destination_id: int | None = None,
        currency_id: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        min_amount: AmountInput | None = None,
        max_amount: AmountInput | None = None,
        execution_type: str | None = None,
        contract_function: str | None = None,
        sort_by: SortBy | None = None,
        sort_order: SortOrder | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> _Spec[Page[TransferEntry]]:
        """A page of the transfers matching every given filter."""
        query = _history_query(
            currency_id=currency_id,
            start=start,
            end=end,
            min_amount=min_amount,
            max_amount=max_amount,
            execution_type=execution_type,
            contract_function=contract_function,
            sort_by=sort_by,
            sort_order=sort_order,
            page=page,
            per_page=per_page,
        )
        ids = {"transfer_id": transfer_id, "user_id": user_id, "source_id": source_id, "destination_id": destination_id}
        return _json(Page[TransferEntry], query=_query({**{name: None if value is None else _id(value) for name, value in ids.items()}, **query}))

    @_endpoint("GET", "/v1/executions")
    def list_executions(
        self,
        *,
        initiator_id: int | None = None,
        execution_type: str | None = None,
        status: Literal["success", "failed", "reverted"] | None = None,
        contract_id: int | None = None,
        page: int | None = None,
        per_page: int | None = None,
    ) -> _Spec[Page[Execution]]:
        """A page of executions, newest first; contract_id selects the calls of that contract (nested ones included) and its management executions."""
        query = {
            "initiator_id": None if initiator_id is None else _id(initiator_id),
            "execution_type": execution_type,
            "status": status,
            "contract_id": None if contract_id is None else _id(contract_id),
            **_paging(page, per_page),
        }
        return _json(Page[Execution], query=_query(query))

    @_endpoint("GET", "/v1/executions/{execution_id}")
    def get_execution(self, execution_id: int) -> _Spec[ExecutionDetail]:
        """One execution with its transfers and direct nested executions."""
        return _json(ExecutionDetail, path={"execution_id": _id(execution_id)})

    @_endpoint("GET", "/v1/transfers/{transfer_id}")
    def get_transfer(self, transfer_id: int) -> _Spec[TransferEntry]:
        """One transfer with its execution summary."""
        return _json(TransferEntry, path={"transfer_id": _id(transfer_id)})

    # Ledger writes.

    @_endpoint("POST", "/v1/transfers", auth=True)
    def transfer(self, recipient_id: int, currency_id: int, amount: AmountInput, *, idempotency_key: str | None = None) -> _Spec[TransferResult]:
        """Move the caller's funds to another user."""
        body = {"recipient_id": _id(recipient_id), "currency_id": _id(currency_id), "amount": amount_string(amount)}
        return _json(TransferResult, body=body, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/transfers/from", auth=True)
    def transfer_from(self, source_id: int, recipient_id: int, currency_id: int, amount: AmountInput, *, idempotency_key: str | None = None) -> _Spec[TransferFromResult]:
        """Move funds from source_id to recipient_id against the allowance source_id gave the caller."""
        body = {"source_id": _id(source_id), "recipient_id": _id(recipient_id), "currency_id": _id(currency_id), "amount": amount_string(amount)}
        return _json(TransferFromResult, body=body, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/allowances", auth=True)
    def approve(self, spender_id: int, currency_id: int, amount: AmountInput, *, idempotency_key: str | None = None) -> _Spec[ApproveResult]:
        """Set what spender_id may move from the caller's balance, replacing the current allowance; zero removes it."""
        body = {"spender_id": _id(spender_id), "currency_id": _id(currency_id), "amount": amount_string(amount)}
        return _json(ApproveResult, body=body, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/claims", auth=True)
    def create_claim(self, payer_id: int, currency_id: int, amount: AmountInput, *, description: str | None = None, idempotency_key: str | None = None) -> _Spec[ClaimCreated]:
        """Ask payer_id to pay the caller."""
        body: dict[str, Any] = {"payer_id": _id(payer_id), "currency_id": _id(currency_id), "amount": amount_string(amount)}
        if description is not None:
            body["description"] = description
        return _json(ClaimCreated, body=body, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/claims/{claim_id}/pay", auth=True)
    def pay_claim(self, claim_id: int, *, idempotency_key: str | None = None) -> _Spec[ClaimPaid]:
        """Pay a pending claim the caller owes."""
        return _json(ClaimPaid, path={"claim_id": _id(claim_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/claims/{claim_id}/cancel", auth=True)
    def cancel_claim(self, claim_id: int, *, idempotency_key: str | None = None) -> _Spec[ClaimCanceled]:
        """Cancel a pending claim as its payer or claimant."""
        return _json(ClaimCanceled, path={"claim_id": _id(claim_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/stakes/deposit", auth=True)
    def deposit_stake(self, currency_id: int, amount: AmountInput, *, idempotency_key: str | None = None) -> _Spec[StakeChange]:
        """Move part of the caller's balance into the caller's stake."""
        return _json(StakeChange, body={"currency_id": _id(currency_id), "amount": amount_string(amount)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/stakes/withdraw", auth=True)
    def withdraw_stake(self, currency_id: int, amount: AmountInput, *, idempotency_key: str | None = None) -> _Spec[StakeChange]:
        """Move part of the caller's stake back into the caller's balance."""
        return _json(StakeChange, body={"currency_id": _id(currency_id), "amount": amount_string(amount)}, idempotency_key=idempotency_key)

    @_endpoint("GET", "/v1/tokens", auth=True)
    def list_tokens(self, *, include_revoked: bool = True, page: int | None = None, per_page: int | None = None) -> _Spec[Page[TokenInfo]]:
        """A page of the caller's API tokens in issue order."""
        return _json(Page[TokenInfo], query=_query({"include_revoked": None if include_revoked else False, **_paging(page, per_page)}))

    @_endpoint("POST", "/v1/tokens/revoke", auth=True)
    def revoke_tokens(self, *, token_id: int | None = None, idempotency_key: str | None = None) -> _Spec[TokensRevoked]:
        """Revoke one of the caller's tokens, or every active one without token_id."""
        return _json(TokensRevoked, body=None if token_id is None else {"token_id": _id(token_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/currencies", auth=True)
    def create_currency(self, guild_id: int, name: str, symbol: str, supply: AmountInput, *, hourly_rate_ppm: int | None = None, idempotency_key: str | None = None) -> _Spec[CurrencyCreated]:
        """Create the currency of a server the caller owns, minting supply to the caller."""
        body = {"guild_id": _id(guild_id), "name": name, "symbol": symbol, "supply": amount_string(supply)}
        if hourly_rate_ppm is not None:
            body["hourly_rate_ppm"] = _id(hourly_rate_ppm)
        return _json(CurrencyCreated, body=body, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/currencies/{currency_id}/mint", auth=True)
    def mint(self, currency_id: int, amount: AmountInput, *, idempotency_key: str | None = None) -> _Spec[SupplyChange]:
        """Mint into the caller's wallet."""
        return _json(SupplyChange, path={"currency_id": _id(currency_id)}, body={"amount": amount_string(amount)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/currencies/{currency_id}/burn", auth=True)
    def burn(self, currency_id: int, amount: AmountInput, *, idempotency_key: str | None = None) -> _Spec[SupplyChange]:
        """Burn part of the caller's balance."""
        return _json(SupplyChange, path={"currency_id": _id(currency_id)}, body={"amount": amount_string(amount)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/currencies/{currency_id}/renounce", auth=True)
    def renounce_minting(self, currency_id: int, *, idempotency_key: str | None = None) -> _Spec[MintingRenounced]:
        """Give up minting and rate changes of the currency for good."""
        return _json(MintingRenounced, path={"currency_id": _id(currency_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/currencies/{currency_id}/rate-change", auth=True)
    def request_rate_change(self, currency_id: int, hourly_rate_ppm: int, *, idempotency_key: str | None = None) -> _Spec[RateChangeRequested]:
        """Request a timelocked change of the hourly staking rate."""
        return _json(RateChangeRequested, path={"currency_id": _id(currency_id)}, body={"hourly_rate_ppm": _id(hourly_rate_ppm)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/currencies/{currency_id}/rate-change/apply", auth=True)
    def apply_rate_change(self, currency_id: int, *, idempotency_key: str | None = None) -> _Spec[RateChangeApplied]:
        """Apply the pending rate change once its timelock passed."""
        return _json(RateChangeApplied, path={"currency_id": _id(currency_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/currencies/{currency_id}/delete", auth=True)
    def request_currency_deletion(self, currency_id: int, *, idempotency_key: str | None = None) -> _Spec[DeleteRequested]:
        """Request deletion of the currency."""
        return _json(DeleteRequested, path={"currency_id": _id(currency_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/currencies/{currency_id}/delete/finalize", auth=True)
    def finalize_currency_deletion(self, currency_id: int, *, idempotency_key: str | None = None) -> _Spec[CurrencyDeleted]:
        """Finalize a requested deletion within its window."""
        return _json(CurrencyDeleted, path={"currency_id": _id(currency_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/currencies/{currency_id}/delegation/enable", auth=True)
    def enable_delegation(self, currency_id: int, *, idempotency_key: str | None = None) -> _Spec[DelegationChange]:
        """Let the server's administrators manage the currency."""
        return _json(DelegationChange, path={"currency_id": _id(currency_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/currencies/{currency_id}/delegation/disable", auth=True)
    def disable_delegation(self, currency_id: int, *, idempotency_key: str | None = None) -> _Spec[DelegationChange]:
        """Return currency management to the server owner alone."""
        return _json(DelegationChange, path={"currency_id": _id(currency_id)}, idempotency_key=idempotency_key)

    @_endpoint("GET", "/v1/notifications", auth=True)
    def list_notifications(self, *, page: int | None = None, per_page: int | None = None) -> _Spec[Page[NotificationPermission]]:
        """A page of the users whose claims notify the caller."""
        return _json(Page[NotificationPermission], query=_query(_paging(page, per_page)))

    @_endpoint("POST", "/v1/notifications/allow", auth=True)
    def allow_notifications(self, user_id: int, *, idempotency_key: str | None = None) -> _Spec[NotificationChange]:
        """Receive claim notifications from a user."""
        return _json(NotificationChange, body={"user_id": _id(user_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/notifications/deny", auth=True)
    def deny_notifications(self, user_id: int, *, idempotency_key: str | None = None) -> _Spec[NotificationChange]:
        """Stop claim notifications from a user."""
        return _json(NotificationChange, body={"user_id": _id(user_id)}, idempotency_key=idempotency_key)

    @_endpoint("GET", "/v1/guilds/{guild_id}/discord-permissions", auth=True)
    def list_discord_permissions(self, guild_id: int, *, page: int | None = None, per_page: int | None = None) -> _Spec[Page[DiscordPermission]]:
        """A page of the accounts whose contracts may act in a server the caller administers."""
        return _json(Page[DiscordPermission], path={"guild_id": _id(guild_id)}, query=_query(_paging(page, per_page)))

    @_endpoint("POST", "/v1/guilds/{guild_id}/discord-permissions/allow", auth=True)
    def allow_discord(self, guild_id: int, account_id: int, *, idempotency_key: str | None = None) -> _Spec[DiscordPermissionChange]:
        """Let an account's contracts send messages and change roles in a server."""
        return _json(DiscordPermissionChange, path={"guild_id": _id(guild_id)}, body={"account_id": _id(account_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/guilds/{guild_id}/discord-permissions/deny", auth=True)
    def deny_discord(self, guild_id: int, account_id: int, *, idempotency_key: str | None = None) -> _Spec[DiscordPermissionChange]:
        """Withdraw an account's permission to act in a server."""
        return _json(DiscordPermissionChange, path={"guild_id": _id(guild_id)}, body={"account_id": _id(account_id)}, idempotency_key=idempotency_key)

    # DEX.

    @_endpoint("GET", "/v1/pools")
    def list_pools(self, *, page: int | None = None, per_page: int | None = None) -> _Spec[Page[Pool]]:
        """A page of the active pools."""
        return _json(Page[Pool], query=_query(_paging(page, per_page)))

    @_endpoint("GET", "/v1/pools/pair/{currency_x}/{currency_y}")
    def get_pair_pools(self, currency_x: int, currency_y: int) -> _Spec[PairPools]:
        """The active pools of a currency pair, one per fee tier."""
        return _json(PairPools, path={"currency_x": _id(currency_x), "currency_y": _id(currency_y)})

    @_endpoint("GET", "/v1/pools/{pool_id}")
    def get_pool(self, pool_id: int) -> _Spec[Pool]:
        """One pool, active or dissolved."""
        return _json(Pool, path={"pool_id": _id(pool_id)})

    @_endpoint("GET", "/v1/accounts/{account_id}/pools")
    def list_positions(self, account_id: int, *, page: int | None = None, per_page: int | None = None) -> _Spec[Page[Position]]:
        """A page of the account's liquidity positions."""
        return _json(Page[Position], path={"account_id": _id(account_id)}, query=_query(_paging(page, per_page)))

    @_endpoint("POST", "/v1/swap/quote")
    def quote_swap(self, from_currency: int, to_currency: int, amount_in: AmountInput) -> _Spec[SwapQuote]:
        """Price a swap without writing anything."""
        return _json(SwapQuote, body={"from_currency": _id(from_currency), "to_currency": _id(to_currency), "amount_in": amount_string(amount_in)})

    @_endpoint("POST", "/v1/swap", auth=True)
    def swap(self, from_currency: int, to_currency: int, amount_in: AmountInput, min_out: AmountInput, *, idempotency_key: str | None = None) -> _Spec[SwapResult]:
        """Swap the caller's currency, failing without moving anything when the result would be below min_out."""
        body = {"from_currency": _id(from_currency), "to_currency": _id(to_currency), "amount_in": amount_string(amount_in), "min_out": amount_string(min_out)}
        return _json(SwapResult, body=body, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/pools", auth=True)
    def create_pool(self, currency_x: int, currency_y: int, fee_bps: int, amount_x: AmountInput, amount_y: AmountInput, *, idempotency_key: str | None = None) -> _Spec[PoolCreated]:
        """Create a pool with the caller's initial deposit."""
        body = {"currency_x": _id(currency_x), "currency_y": _id(currency_y), "fee_bps": _number(fee_bps), "amount_x": amount_string(amount_x), "amount_y": amount_string(amount_y)}
        return _json(PoolCreated, body=body, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/pools/liquidity/add", auth=True)
    def add_liquidity(
        self,
        currency_x: int,
        currency_y: int,
        fee_bps: int,
        amount_x: AmountInput,
        amount_y: AmountInput,
        *,
        amount_x_min: AmountInput | None = None,
        amount_y_min: AmountInput | None = None,
        min_shares: int | None = None,
        idempotency_key: str | None = None,
    ) -> _Spec[LiquidityChange]:
        """Deposit at most amount_x and amount_y into the pool of the pair and tier."""
        body: dict[str, Any] = {"currency_x": _id(currency_x), "currency_y": _id(currency_y), "fee_bps": _number(fee_bps), "amount_x": amount_string(amount_x), "amount_y": amount_string(amount_y)}
        if amount_x_min is not None:
            body["amount_x_min"] = amount_string(amount_x_min)
        if amount_y_min is not None:
            body["amount_y_min"] = amount_string(amount_y_min)
        if min_shares is not None:
            body["min_shares"] = _id(min_shares)
        return _json(LiquidityChange, body=body, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/pools/liquidity/remove", auth=True)
    def remove_liquidity(
        self,
        currency_x: int,
        currency_y: int,
        fee_bps: int,
        shares: int,
        *,
        amount_x_min: AmountInput | None = None,
        amount_y_min: AmountInput | None = None,
        idempotency_key: str | None = None,
    ) -> _Spec[LiquidityChange]:
        """Burn the caller's shares of the pool of the pair and tier."""
        body: dict[str, Any] = {"currency_x": _id(currency_x), "currency_y": _id(currency_y), "fee_bps": _number(fee_bps), "shares": _id(shares)}
        if amount_x_min is not None:
            body["amount_x_min"] = amount_string(amount_x_min)
        if amount_y_min is not None:
            body["amount_y_min"] = amount_string(amount_y_min)
        return _json(LiquidityChange, body=body, idempotency_key=idempotency_key)

    # Contracts.

    @_endpoint("GET", "/v1/contracts/{account_id}")
    def get_contract(self, account_id: int) -> _Spec[ContractInfo]:
        """A contract's state and public functions."""
        return _json(ContractInfo, path={"account_id": _id(account_id)})

    @_endpoint("GET", "/v1/contracts/{account_id}/functions")
    def get_contract_functions(self, account_id: int) -> _Spec[ContractFunctions]:
        """The public functions of a contract's current version."""
        return _json(ContractFunctions, path={"account_id": _id(account_id)})

    @_endpoint("GET", "/v1/contracts/{account_id}/versions")
    def list_contract_versions(self, account_id: int, *, page: int | None = None, per_page: int | None = None) -> _Spec[Page[ContractVersionSummary]]:
        """A page of a contract's registered versions, newest first."""
        return _json(Page[ContractVersionSummary], path={"account_id": _id(account_id)}, query=_query(_paging(page, per_page)))

    @_endpoint("GET", "/v1/contracts/{account_id}/versions/{version}")
    def get_contract_version(self, account_id: int, version: int) -> _Spec[ContractVersion]:
        """One version with its binary."""
        return _json(ContractVersion, path={"account_id": _id(account_id), "version": _id(version)})

    @_endpoint("GET", "/v1/contracts/{account_id}/versions/{version}/binary")
    def download_contract_binary(self, account_id: int, version: int) -> _Spec[ContractBinary]:
        """One version's binary, checked against its X-Checksum-SHA256 header; a mismatch raises ChecksumMismatchError."""
        return _Spec(parse=functools.partial(_parse_binary, account_id, version), path={"account_id": _id(account_id), "version": _id(version)})

    @_endpoint("GET", "/v1/contracts/{account_id}/storage")
    def list_storage(self, account_id: int, *, page: int | None = None, per_page: int | None = None) -> _Spec[Page[StorageEntry]]:
        """A page of a contract's storage in key order."""
        return _json(Page[StorageEntry], path={"account_id": _id(account_id)}, query=_query(_paging(page, per_page)))

    @_endpoint("GET", "/v1/contracts/{account_id}/storage/{key:path}")
    def get_storage(self, account_id: int, key: str) -> _Spec[StorageValue]:
        """One storage value; the key may contain slashes."""
        if not isinstance(key, str) or not key:
            raise ValueError("the storage key must be a non-empty string")
        return _json(StorageValue, path={"account_id": _id(account_id), "key": key})

    @_endpoint("POST", "/v1/contracts/{account_id}/versions", auth=True)
    def register_contract(self, account_id: int, binary: bytes, *, idempotency_key: str | None = None) -> _Spec[ContractRegistered]:
        """Register a compiled RWC1 binary as the next version of the caller's own contract."""
        if not isinstance(binary, bytes | bytearray | memoryview):
            raise TypeError("the binary must be bytes")
        body = {"binary": base64.b64encode(bytes(binary)).decode("ascii")}
        return _json(ContractRegistered, path={"account_id": _id(account_id)}, body=body, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/contracts/{account_id}/lock", auth=True)
    def lock_contract(self, account_id: int, *, until: datetime | None = None, hours: int | None = None, idempotency_key: str | None = None) -> _Spec[ContractLock]:
        """Forbid updates of the caller's contract until an aware time or for a number of hours; give exactly one of them."""
        if (until is None) == (hours is None):
            raise ValueError("give exactly one of until and hours")
        body = {"until": _time(until)} if until is not None else {"hours": _number(cast(int, hours))}
        return _json(ContractLock, path={"account_id": _id(account_id)}, body=body, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/contracts/{account_id}/pause", auth=True)
    def pause_contract(self, account_id: int, *, idempotency_key: str | None = None) -> _Spec[ContractStatusChange]:
        """Pause the caller's contract."""
        return _json(ContractStatusChange, path={"account_id": _id(account_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/contracts/{account_id}/resume", auth=True)
    def resume_contract(self, account_id: int, *, idempotency_key: str | None = None) -> _Spec[ContractStatusChange]:
        """Resume the caller's paused contract."""
        return _json(ContractStatusChange, path={"account_id": _id(account_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/contracts/{account_id}/delete", auth=True)
    def delete_contract(self, account_id: int, *, idempotency_key: str | None = None) -> _Spec[ContractDeleted]:
        """Delete the caller's contract and clear its storage."""
        return _json(ContractDeleted, path={"account_id": _id(account_id)}, idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/contracts/{account_id}/call", auth=True)
    def call_contract(
        self,
        account_id: int,
        function: str,
        args: Sequence[ContractValue] = (),
        *,
        attach: Attachment | None = None,
        gas_limit: int | None = None,
        idempotency_key: str | None = None,
    ) -> _Spec[CallResult]:
        """Call a public function of any contract as the caller; args are contract values such as 5, "text", True, Account(1) or Currency(2)."""
        return _json(CallResult, path={"account_id": _id(account_id)}, body=_call_body(function, args, attach, gas_limit), idempotency_key=idempotency_key)

    @_endpoint("POST", "/v1/contracts/{account_id}/dry-run", auth=True)
    def dry_run_contract(self, account_id: int, function: str, args: Sequence[ContractValue] = (), *, attach: Attachment | None = None, gas_limit: int | None = None) -> _Spec[DryRunResult]:
        """Run a call as the token's account and roll it back, reporting what it would do; it needs a token."""
        return _json(DryRunResult, path={"account_id": _id(account_id)}, body=_call_body(function, args, attach, gas_limit))

    # Admin backups.

    @_endpoint("GET", "/v1/admin/backups", auth=True)
    def list_backups(self, *, page: int | None = None, per_page: int | None = None) -> _Spec[BackupList]:
        """A page of the backups, newest first, with the execution that recorded the listing; operators only."""
        return _json(BackupList, query=_query(_paging(page, per_page)))


@final
class AsyncRapidWireClient(_Client):
    """The asynchronous client; use it as an async context manager, or call aclose(), to close its connections."""

    def __init__(self, base_url: str, token: str | None = None, *, transport: httpx.AsyncBaseTransport | None = None, timeout: float | httpx.Timeout = DEFAULT_TIMEOUT) -> None:
        super().__init__(base_url, token)
        self._http = httpx.AsyncClient(base_url=self._base_url, transport=transport, timeout=timeout, headers={"User-Agent": USER_AGENT})

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _send[T](self, endpoint: _Endpoint[Any, T], spec: _Spec[T]) -> T:
        response = await self._http.send(self._build(self._http, endpoint.method, endpoint.path, endpoint.auth, spec))
        return _finish(spec, response)

    async def paginate[T](self, method: Callable[..., Awaitable[Page[T]]], /, *args: Any, per_page: int | None = None, **kwargs: Any) -> AsyncIterator[T]:
        """Yield every item of a paged list, such as paginate(client.list_balances, account_id), requesting one page after another within the reachable window."""
        page = 1
        while True:
            result = await method(*args, page=page, per_page=per_page, **kwargs)
            for item in result.items:
                yield item
            if not result.has_next:
                return
            page += 1

    @_route("GET", _BACKUP_PATH)
    @asynccontextmanager
    async def stream_backup(self, name: str) -> AsyncIterator[AsyncBackupStream]:
        """Open a backup download for `async with`; iterate the stream for its bytes, which are checked against the announced SHA-256 at the end."""
        response = await self._http.send(self._backup_request(self._http, name), stream=True)
        try:
            if not response.is_success:
                await response.aread()
                raise error_from_response(response)
            yield AsyncBackupStream(response, _DigestCheck(name, response))
        finally:
            await response.aclose()

    @_route("GET", _BACKUP_PATH)
    async def download_backup(self, name: str, destination: Destination) -> BackupDownload:
        """Download a backup into a file object, or into a path through a .partial file renamed into place only after the SHA-256 matched."""
        async with self.stream_backup(name) as stream:
            if not isinstance(destination, str | os.PathLike):
                async for chunk in stream:
                    destination.write(chunk)
                return stream.result
            target = Path(destination)
            partial = _partial_path(target)
            file = await asyncio.to_thread(partial.open, "wb")
            try:
                async for chunk in stream:
                    await asyncio.to_thread(file.write, chunk)
                await asyncio.to_thread(file.close)
                await asyncio.to_thread(os.replace, partial, target)
            except BaseException:
                file.close()
                partial.unlink(missing_ok=True)
                raise
            return dataclasses.replace(stream.result, path=target)


@final
class RapidWireClient(_Client):
    """The synchronous client; use it as a context manager, or call close(), to close its connections."""

    def __init__(self, base_url: str, token: str | None = None, *, transport: httpx.BaseTransport | None = None, timeout: float | httpx.Timeout = DEFAULT_TIMEOUT) -> None:
        super().__init__(base_url, token)
        self._http = httpx.Client(base_url=self._base_url, transport=transport, timeout=timeout, headers={"User-Agent": USER_AGENT})

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: TracebackType | None) -> None:
        self.close()

    def close(self) -> None:
        self._http.close()

    def _send[T](self, endpoint: _Endpoint[Any, T], spec: _Spec[T]) -> T:
        response = self._http.send(self._build(self._http, endpoint.method, endpoint.path, endpoint.auth, spec))
        return _finish(spec, response)

    def paginate[T](self, method: Callable[..., Page[T]], /, *args: Any, per_page: int | None = None, **kwargs: Any) -> Iterator[T]:
        """Yield every item of a paged list, such as paginate(client.list_balances, account_id), requesting one page after another within the reachable window."""
        page = 1
        while True:
            result = method(*args, page=page, per_page=per_page, **kwargs)
            yield from result.items
            if not result.has_next:
                return
            page += 1

    @_route("GET", _BACKUP_PATH)
    @contextmanager
    def stream_backup(self, name: str) -> Iterator[BackupStream]:
        """Open a backup download for `with`; iterate the stream for its bytes, which are checked against the announced SHA-256 at the end."""
        response = self._http.send(self._backup_request(self._http, name), stream=True)
        try:
            if not response.is_success:
                response.read()
                raise error_from_response(response)
            yield BackupStream(response, _DigestCheck(name, response))
        finally:
            response.close()

    @_route("GET", _BACKUP_PATH)
    def download_backup(self, name: str, destination: Destination) -> BackupDownload:
        """Download a backup into a file object, or into a path through a .partial file renamed into place only after the SHA-256 matched."""
        with self.stream_backup(name) as stream:
            if not isinstance(destination, str | os.PathLike):
                for chunk in stream:
                    destination.write(chunk)
                return stream.result
            target = Path(destination)
            partial = _partial_path(target)
            try:
                with partial.open("wb") as file:
                    for chunk in stream:
                        file.write(chunk)
                os.replace(partial, target)
            except BaseException:
                partial.unlink(missing_ok=True)
                raise
            return dataclasses.replace(stream.result, path=target)
