"""Contract endpoints: public reads of contracts, versions, binaries, public functions and storage; registration, lock, pause, resume and delete of the caller's own contract; calls and dry runs of any contract."""

from __future__ import annotations

import base64
import binascii
import hashlib
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Final, Self

from fastapi import APIRouter, Depends, Header, Path, Response
from fastapi.exceptions import RequestValidationError
from pydantic import AfterValidator, AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, StrictInt, model_validator
from pydantic_core import PydanticCustomError

from rapidwire.api.auth import AccountDep, WriterDep
from rapidwire.api.body_limit import body_limit
from rapidwire.api.contract_values import TypedValueIn, TypedValueOut, encode_typed
from rapidwire.api.deps import EngineDep, GuildInfoDep, RuntimeConfigDep
from rapidwire.api.formats import Amount, IntOut, IntString, PositiveAmount, PositiveIdStr
from rapidwire.api.idempotency import IDEMPOTENCY_KEY_HEADER
from rapidwire.api.paging import Page, PageParams, page_params
from rapidwire.contract.limits import DEFAULT_LIMITS
from rapidwire.contract.values import Attachment, Currency, Value
from rapidwire.core import contracts
from rapidwire.core.contract_calls import ContractCallResult, DryRunResult, call_contract, dry_run_contract
from rapidwire.core.contracts import LOCK_HOURS_MAX
from rapidwire.core.errors import ContractNotFound, DomainError, InvalidContract, NotFoundError, PermissionDenied
from rapidwire.core.types import LabelBasis, Origin
from rapidwire.core.validation import BIGINT_MAX
from rapidwire.db.tables import CONTRACT_FUNCTION_MAX
from rapidwire.db.types import AMOUNT_MAX

__all__ = ["BASE64_JSON_ESCAPE_FACTOR", "REGISTER_BODY_SLACK", "SHA256_HEADER", "base64_length", "register_body_limit", "router"]

router = APIRouter(prefix="/v1", tags=["Contracts"])

SHA256_HEADER: Final = "X-Checksum-SHA256"
# Bytes a registration body may carry beyond the base64 text of the largest binary, for the JSON around it.
REGISTER_BODY_SLACK: Final = 1024
# JSON may write the base64 character "/" as the two-character escape "\/", so the text can take up to twice its length in the body.
BASE64_JSON_ESCAPE_FACTOR: Final = 2


def base64_length(size: int) -> int:
    """The length of the padded standard base64 text of size bytes."""
    return 4 * ((size + 2) // 3)


def register_body_limit(max_size: int) -> int:
    """The largest registration body for binaries of at most max_size bytes: twice the length of their base64 text, which covers every "/" written as an escape, plus REGISTER_BODY_SLACK."""
    return BASE64_JSON_ESCAPE_FACTOR * base64_length(max_size) + REGISTER_BODY_SLACK


def _iso_text(value: object) -> object:
    if not isinstance(value, str):
        raise PydanticCustomError("datetime_type", "The time must be an ISO 8601 string with a UTC offset.")
    return value


def _within_lock_horizon(value: datetime) -> datetime:
    try:
        beyond = value > datetime.now(UTC) + timedelta(hours=LOCK_HOURS_MAX)
    except OverflowError:
        beyond = True
    if beyond:
        raise PydanticCustomError("lock_too_long", f"A lock can end at most {LOCK_HOURS_MAX} hours from now.")
    return value


LockEnd = Annotated[AwareDatetime, BeforeValidator(_iso_text), AfterValidator(_within_lock_horizon)]
LockHours = Annotated[StrictInt, Field(ge=1, le=LOCK_HOURS_MAX)]

ContractId = Annotated[int, Path(description="The contract's account ID as a decimal string."), IntString(1, BIGINT_MAX, "A user account ID as a decimal string.")]
VersionNumber = Annotated[int, Path(ge=1, le=2**31 - 1, description="A version number, from 1.")]
StorageKey = Annotated[str, Path(description="The storage key; it may contain slashes.")]
GasLimit = Annotated[int, IntString(1, AMOUNT_MAX, "The gas limit, from 1 to the max_gas of GET /v1/config, as a decimal string; the default_gas applies when omitted.")]


def _require_own(account_id: int, caller_id: int) -> None:
    if account_id != caller_id:
        raise PermissionDenied("You can only manage the contract of your own account.")


class PublicFunctionOut(BaseModel):
    name: str
    params: list[str] = Field(description="The parameter types in order, as the contract language writes them; parameters have no names in a binary.")
    returns: str
    signature: str


class ContractOut(BaseModel):
    account_id: IntOut
    status: str = Field(description="active, paused or deleted.")
    current_version: int = Field(description="The latest registered version; a deleted contract keeps it as history but does not run it.")
    sha256: str = Field(description="The SHA-256 of the current version's binary, as lowercase hex.")
    size: int = Field(description="The current version's binary size in bytes.")
    version_count: int
    locked: bool = Field(description="Whether the update lock is in force now.")
    locked_until: datetime | None
    storage_keys: int
    storage_bytes: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    created_execution_id: IntOut
    created_execution_origin: Origin = Field(description="The origin of the execution that first registered the contract.")
    created_execution_label_basis: LabelBasis | None = Field(description="The label basis of a legacy creating execution, else null.")
    updated_execution_id: IntOut
    updated_execution_origin: Origin = Field(description="The origin of the execution that last changed the contract.")
    updated_execution_label_basis: LabelBasis | None = Field(description="The label basis of a legacy updating execution, else null.")
    functions: list[PublicFunctionOut] = Field(description="The public functions of the current version in name order; empty for a deleted contract.")


class FunctionsOut(BaseModel):
    account_id: IntOut
    status: str
    version: int
    functions: list[PublicFunctionOut]


class VersionSummaryOut(BaseModel):
    version: int
    sha256: str
    size: int
    created_execution_id: IntOut
    created_execution_origin: Origin = Field(description="The origin of the execution that registered the version.")
    created_execution_label_basis: LabelBasis | None = Field(description="The label basis of a legacy registering execution, else null.")
    created_at: datetime


class VersionOut(VersionSummaryOut):
    account_id: IntOut
    binary: str = Field(description="The binary exactly as registered, in standard base64 with padding.")


class StorageEntryOut(BaseModel):
    key: str
    value: TypedValueOut
    size: int = Field(description="Bytes of the encoded value.")
    updated_at: datetime


class StorageValueOut(BaseModel):
    account_id: IntOut
    key: str
    value: TypedValueOut
    size: int = Field(description="Bytes of the encoded value.")
    updated_at: datetime = Field(description="When the value was last written.")


class RegisterIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binary: str = Field(description="The RWC1 binary in standard base64 with padding.")


class RegisteredOut(BaseModel):
    execution_id: IntOut
    account_id: IntOut
    version: int
    sha256: str
    size: int
    status: str
    revived: bool = Field(description="Whether this registration brought a deleted contract back.")


class LockIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    until: LockEnd | None = Field(default=None, description=f"The new end of the lock, an ISO 8601 time string with a UTC offset, at most {LOCK_HOURS_MAX} hours from now.")
    hours: LockHours | None = Field(default=None, description="The lock ends this many hours after the execution's time; a JSON integer.")

    @model_validator(mode="after")
    def _one_end(self) -> Self:
        if (self.until is None) == (self.hours is None):
            raise ValueError("Give exactly one of until and hours.")
        return self


class LockOut(BaseModel):
    execution_id: IntOut
    account_id: IntOut
    locked_until: datetime
    previous_locked_until: datetime | None = Field(description="The end this lock replaced, null when no lock was in force.")


class StatusOut(BaseModel):
    execution_id: IntOut
    account_id: IntOut
    status: str


class DeletedOut(BaseModel):
    execution_id: IntOut
    account_id: IntOut
    version: int
    storage_keys_cleared: int
    storage_bytes_cleared: int


class AttachIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    currency_id: PositiveIdStr
    amount: PositiveAmount


class CallIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    function: str = Field(min_length=1, max_length=CONTRACT_FUNCTION_MAX)
    args: list[TypedValueIn] = Field(default_factory=list, max_length=DEFAULT_LIMITS.max_params, description="The arguments in parameter order, as typed values.")
    attach: AttachIn | None = Field(default=None, description="Currency moved from the caller to the contract when the call starts.")
    gas_limit: GasLimit | None = None

    def attachment(self) -> Attachment | None:
        return None if self.attach is None else Attachment(currency=Currency(self.attach.currency_id), amount=self.attach.amount)


class CallOut(BaseModel):
    execution_id: IntOut
    contract_id: IntOut
    function: str
    value: TypedValueOut
    gas_used: IntOut
    gas_limit: IntOut
    gas_currency_id: IntOut = Field(description="The currency the gas was charged in.")
    gas_price: IntOut = Field(description="The minimal units of the gas currency the call was charged per gas.")
    gas_cost: Amount = Field(description="gas_used times gas_price, burned from the caller in the gas currency.")
    random: list[IntOut] = Field(description="The values the top-level execution drew with random().")


class DryRunErrorOut(BaseModel):
    code: str
    message: str
    kind: str | None
    reason: str | None


class RandomDrawOut(BaseModel):
    order: int
    depth: int
    contract_id: IntOut
    function: str
    values: list[IntOut]


class DryRunTransferOut(BaseModel):
    currency_id: IntOut
    from_account: IntOut
    to_account: IntOut
    amount: Amount
    kind: str


class DryRunOutboxOut(BaseModel):
    kind: str
    payload: dict[str, Any]


class DryRunOut(BaseModel):
    ok: bool
    value: TypedValueOut | None
    error: DryRunErrorOut | None
    gas_used: IntOut
    gas_limit: IntOut
    gas_currency_id: IntOut
    gas_price: IntOut
    gas_cost: Amount
    random: list[IntOut]
    random_tree: list[RandomDrawOut] = Field(description="The draws of every execution of the call in start order.")
    transfers: list[DryRunTransferOut] = Field(description="The transfers the call would make, the gas burn included.")
    outbox: list[DryRunOutboxOut] = Field(description="The Discord operations the call would queue.")


def _functions(found: tuple[contracts.PublicFunction, ...]) -> list[PublicFunctionOut]:
    return [PublicFunctionOut(name=fn.name, params=[str(param) for param in fn.params], returns=str(fn.returns), signature=fn.signature) for fn in found]


async def _details(engine: EngineDep, account_id: int) -> contracts.ContractDetails:
    details = await contracts.get_contract_with_functions(engine, account_id)
    if details is None:
        raise ContractNotFound()
    return details


@router.get("/contracts/{account_id}", response_model=ContractOut, summary="A contract's state and public functions")
async def get_contract(account_id: ContractId, engine: EngineDep) -> ContractOut:
    details = await _details(engine, account_id)
    info = details.info
    return ContractOut(
        account_id=info.account_id,
        status=info.status.value,
        current_version=info.current_version,
        sha256=info.sha256,
        size=info.size,
        version_count=info.version_count,
        locked=info.locked_at(datetime.now(UTC)),
        locked_until=info.locked_until,
        storage_keys=info.storage_keys,
        storage_bytes=info.storage_bytes,
        created_at=info.created_at,
        updated_at=info.updated_at,
        deleted_at=info.deleted_at,
        created_execution_id=info.created_execution_id,
        created_execution_origin=info.created_execution_origin,
        created_execution_label_basis=info.created_execution_label_basis,
        updated_execution_id=info.updated_execution_id,
        updated_execution_origin=info.updated_execution_origin,
        updated_execution_label_basis=info.updated_execution_label_basis,
        functions=_functions(details.functions),
    )


@router.get("/contracts/{account_id}/functions", response_model=FunctionsOut, summary="The public functions of a contract's current version")
async def get_functions(account_id: ContractId, engine: EngineDep) -> FunctionsOut:
    details = await _details(engine, account_id)
    return FunctionsOut(account_id=account_id, status=details.info.status.value, version=details.info.current_version, functions=_functions(details.functions))


@router.get("/contracts/{account_id}/versions", response_model=Page[VersionSummaryOut], summary="The registered versions of a contract, newest first")
async def list_versions(
    account_id: ContractId,
    engine: EngineDep,
    params: Annotated[PageParams, Depends(page_params(contracts.CONTRACT_VERSIONS_PER_PAGE_DEFAULT, contracts.CONTRACT_VERSIONS_PER_PAGE_MAX))],
) -> Page[VersionSummaryOut]:
    found = await contracts.list_contract_versions(engine, account_id, page=params.page, per_page=params.per_page)
    return Page[VersionSummaryOut].build(
        found,
        lambda v: VersionSummaryOut(
            version=v.version,
            sha256=v.sha256,
            size=v.size,
            created_execution_id=v.created_execution_id,
            created_execution_origin=v.created_execution_origin,
            created_execution_label_basis=v.created_execution_label_basis,
            created_at=v.created_at,
        ),
    )


async def _version(engine: EngineDep, account_id: int, version: int) -> contracts.ContractVersion:
    found = await contracts.get_contract_version(engine, account_id, version)
    if found is None:
        raise NotFoundError("This version of the contract was not found.")
    return found


@router.get("/contracts/{account_id}/versions/{version}", response_model=VersionOut, summary="One version with its SHA-256 and its binary in base64")
async def get_version(account_id: ContractId, version: VersionNumber, engine: EngineDep) -> VersionOut:
    found = await _version(engine, account_id, version)
    return VersionOut(
        account_id=found.account_id,
        version=found.version,
        sha256=found.sha256,
        size=found.size,
        created_execution_id=found.created_execution_id,
        created_execution_origin=found.created_execution_origin,
        created_execution_label_basis=found.created_execution_label_basis,
        created_at=found.created_at,
        binary=base64.b64encode(found.binary).decode("ascii"),
    )


@router.get(
    "/contracts/{account_id}/versions/{version}/binary",
    response_class=Response,
    summary="Download one version's binary",
    responses={200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}, "description": f"The binary, with its SHA-256 in {SHA256_HEADER}."}},
)
async def download_version(account_id: ContractId, version: VersionNumber, engine: EngineDep) -> Response:
    found = await _version(engine, account_id, version)
    digest = base64.b64encode(hashlib.sha256(found.binary).digest()).decode("ascii")
    headers = {
        SHA256_HEADER: found.sha256,
        "Repr-Digest": f"sha-256=:{digest}:",
        "Content-Disposition": f'attachment; filename="contract-{found.account_id}-v{found.version}.rwc"',
    }
    return Response(content=found.binary, media_type="application/octet-stream", headers=headers)


@router.get("/contracts/{account_id}/storage", response_model=Page[StorageEntryOut], summary="A contract's storage in key order")
async def list_storage(
    account_id: ContractId,
    engine: EngineDep,
    params: Annotated[PageParams, Depends(page_params(contracts.CONTRACT_STORAGE_PER_PAGE_DEFAULT, contracts.CONTRACT_STORAGE_PER_PAGE_MAX))],
) -> Page[StorageEntryOut]:
    found = await contracts.list_storage(engine, account_id, page=params.page, per_page=params.per_page)
    return Page[StorageEntryOut].build(found, lambda entry: StorageEntryOut(key=entry.key, value=encode_typed(entry.value), size=entry.size, updated_at=entry.updated_at))


@router.get("/contracts/{account_id}/storage/{key:path}", response_model=StorageValueOut, summary="One storage value")
async def get_storage(account_id: ContractId, key: StorageKey, engine: EngineDep) -> StorageValueOut:
    entry = await contracts.get_storage_entry(engine, account_id, key)
    if entry is None:
        if await contracts.get_contract(engine, account_id) is None:
            raise ContractNotFound()
        raise NotFoundError("No value is stored under this key.")
    return StorageValueOut(account_id=account_id, key=entry.key, value=encode_typed(entry.value), size=entry.size, updated_at=entry.updated_at)


def _body_error(loc: tuple[str | int, ...], kind: str, message: str) -> RequestValidationError:
    return RequestValidationError([{"type": kind, "loc": ("body", *loc), "msg": message, "input": None}])


def _decode_binary(text: str, max_size: int) -> bytes:
    """The bytes of the base64 text, refusing text that would decode to more than max_size bytes before decoding it."""
    if len(text) > base64_length(max_size):
        raise InvalidContract(f"The binary is larger than {max_size} bytes.")
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error):
        raise _body_error(("binary",), "base64", "The binary must be standard base64 with padding.") from None


@router.post(
    "/contracts/{account_id}/versions",
    response_model=RegisteredOut,
    summary="Register a new version of your own contract",
    responses={413: {"description": "The body is larger than a registration of the largest binary can be."}},
)
@body_limit(lambda settings: register_body_limit(settings.contract_max_size))
async def register_version(account_id: ContractId, body: RegisterIn, writer: WriterDep, engine: EngineDep, runtime: RuntimeConfigDep) -> RegisteredOut:
    _require_own(account_id, writer.account_id)
    binary = _decode_binary(body.binary, runtime.contract_max_size)
    result = await contracts.register_contract(engine, account_id=writer.account_id, binary=binary, max_size=runtime.contract_max_size, idempotency_key=writer.idempotency_key)
    writer.done(result)
    return RegisteredOut(
        execution_id=result.execution_id, account_id=result.account_id, version=result.version, sha256=result.sha256, size=result.size, status=result.status.value, revived=result.revived
    )


@router.post("/contracts/{account_id}/lock", response_model=LockOut, summary="Lock your own contract against updates until a time")
async def lock(account_id: ContractId, body: LockIn, writer: WriterDep, engine: EngineDep) -> LockOut:
    _require_own(account_id, writer.account_id)
    result = await contracts.lock_contract(engine, account_id=writer.account_id, until=body.until, hours=body.hours, idempotency_key=writer.idempotency_key)
    writer.done(result)
    return LockOut(execution_id=result.execution_id, account_id=result.account_id, locked_until=result.locked_until, previous_locked_until=result.previous_locked_until)


@router.post("/contracts/{account_id}/pause", response_model=StatusOut, summary="Pause your own contract")
async def pause(account_id: ContractId, writer: WriterDep, engine: EngineDep) -> StatusOut:
    _require_own(account_id, writer.account_id)
    result = await contracts.pause_contract(engine, account_id=writer.account_id, idempotency_key=writer.idempotency_key)
    writer.done(result)
    return StatusOut(execution_id=result.execution_id, account_id=result.account_id, status=result.status.value)


@router.post("/contracts/{account_id}/resume", response_model=StatusOut, summary="Resume your own paused contract")
async def resume(account_id: ContractId, writer: WriterDep, engine: EngineDep) -> StatusOut:
    _require_own(account_id, writer.account_id)
    result = await contracts.resume_contract(engine, account_id=writer.account_id, idempotency_key=writer.idempotency_key)
    writer.done(result)
    return StatusOut(execution_id=result.execution_id, account_id=result.account_id, status=result.status.value)


@router.post("/contracts/{account_id}/delete", response_model=DeletedOut, summary="Delete your own contract and clear its storage")
async def delete(account_id: ContractId, writer: WriterDep, engine: EngineDep) -> DeletedOut:
    _require_own(account_id, writer.account_id)
    result = await contracts.delete_contract(engine, account_id=writer.account_id, idempotency_key=writer.idempotency_key)
    writer.done(result)
    return DeletedOut(
        execution_id=result.execution_id,
        account_id=result.account_id,
        version=result.version,
        storage_keys_cleared=result.storage_keys_cleared,
        storage_bytes_cleared=result.storage_bytes_cleared,
    )


def _call_out(result: ContractCallResult) -> CallOut:
    return CallOut(
        execution_id=result.execution_id,
        contract_id=result.contract_id,
        function=result.function,
        value=encode_typed(result.value),
        gas_used=result.gas_used,
        gas_limit=result.gas_limit,
        gas_currency_id=result.gas_currency_id,
        gas_price=result.gas_price,
        gas_cost=result.gas_cost,
        random=list(result.random_values),
    )


@router.post("/contracts/{account_id}/call", response_model=CallOut, summary="Call a public function of a contract")
async def call(account_id: ContractId, body: CallIn, writer: WriterDep, engine: EngineDep, runtime: RuntimeConfigDep, guild_info: GuildInfoDep) -> CallOut:
    args: list[Value] = list(body.args)
    result = await call_contract(
        engine,
        guild_info,
        caller_id=writer.account_id,
        contract_id=account_id,
        function=body.function,
        args=args,
        attach=body.attachment(),
        gas_limit=body.gas_limit,
        config=runtime,
        idempotency_key=writer.idempotency_key,
    )
    return _call_out(writer.done(result))


def _dry_run_out(result: DryRunResult) -> DryRunOut:
    error = result.error
    return DryRunOut(
        ok=result.ok,
        value=None if result.value is None else encode_typed(result.value),
        error=None if error is None else DryRunErrorOut(code=error.code, message=error.message, kind=error.kind, reason=error.reason),
        gas_used=result.gas_used,
        gas_limit=result.gas_limit,
        gas_currency_id=result.gas_currency_id,
        gas_price=result.gas_price,
        gas_cost=result.gas_cost,
        random=list(result.random_values),
        random_tree=[RandomDrawOut(order=d.order, depth=d.depth, contract_id=d.contract_id, function=d.function, values=list(d.values)) for d in result.random_tree],
        transfers=[DryRunTransferOut(currency_id=t.currency_id, from_account=t.from_account, to_account=t.to_account, amount=t.amount, kind=t.kind) for t in result.transfers],
        outbox=[DryRunOutboxOut(kind=o.kind, payload=dict(o.payload)) for o in result.outbox],
    )


@router.post("/contracts/{account_id}/dry-run", response_model=DryRunOut, summary="Run a call as the token's account without changing anything and report its outcome")
async def dry_run(
    account_id: ContractId,
    body: CallIn,
    caller_id: AccountDep,
    engine: EngineDep,
    runtime: RuntimeConfigDep,
    guild_info: GuildInfoDep,
    idempotency_key: Annotated[str | None, Header(alias=IDEMPOTENCY_KEY_HEADER, include_in_schema=False)] = None,
) -> DryRunOut:
    if idempotency_key is not None:
        detail: dict[str, Any] = {"type": "not_allowed", "loc": ("header", IDEMPOTENCY_KEY_HEADER), "msg": "A dry run records nothing, so it takes no idempotency key.", "input": None}
        raise RequestValidationError([detail])
    args: list[Value] = list(body.args)
    try:
        result = await dry_run_contract(
            engine, guild_info, caller_id=caller_id, contract_id=account_id, function=body.function, args=args, attach=body.attachment(), gas_limit=body.gas_limit, config=runtime
        )
    except DomainError as error:
        # Nothing of a dry run is committed, so the error names no execution.
        error.execution_id = None
        raise
    return _dry_run_out(result)
