"""Public reads of accounts and history: account kinds, Discord usernames, account statistics, balances, stakes, transfer history, executions, transfers, currencies, allowances and claims; none of them needs a token."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Any, Final, Literal

from fastapi import APIRouter, Depends, Path, Query
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, AwareDatetime, BaseModel, Field

from rapidwire.api.deps import DiscordUsersDep, EngineDep
from rapidwire.api.discord_users import DiscordUnavailable
from rapidwire.api.errors import ErrorResponse, envelope
from rapidwire.api.formats import Amount, IdStr, IntOut, NonNegativeAmount, PositiveIdStr
from rapidwire.api.paging import Page, PageParams, page_params
from rapidwire.core import allowances, claims, queries, staking
from rapidwire.core import paging as core_paging
from rapidwire.core.clock import resolve_now
from rapidwire.core.currency import CurrencyInfo, get_currency, get_currency_by_symbol
from rapidwire.core.db import consistent_snapshot
from rapidwire.core.errors import AccountNotFound, CurrencyNotFound, NotFoundError
from rapidwire.core.queries import currency_supplies
from rapidwire.core.types import AccountKind, ClaimStatus, ExecutionStatus, LabelBasis, Origin, OutboxStatus
from rapidwire.db.tables import CONTRACT_FUNCTION_MAX, CURRENCY_SYMBOL_MAX, ENUM_LENGTH

__all__ = ["DISCORD_UNAVAILABLE_CODE", "DISCORD_UNAVAILABLE_MESSAGE", "router"]

DISCORD_UNAVAILABLE_CODE: Final = "discord_unavailable"
DISCORD_UNAVAILABLE_MESSAGE: Final = "Discord is unavailable, so the username cannot be looked up now; try again later."

router = APIRouter(prefix="/v1", tags=["Accounts and history"])

_NOT_FOUND: Final[dict[int | str, dict[str, Any]]] = {404: {"model": ErrorResponse, "description": "The named item does not exist."}}


class ExecutionNotFound(NotFoundError):
    code = "execution_not_found"
    default_message = "The execution was not found."


class TransferNotFound(NotFoundError):
    code = "transfer_not_found"
    default_message = "The transfer was not found."


class StakeNotFound(NotFoundError):
    code = "stake_not_found"
    default_message = "The account has no stake in this currency."


def _to_utc(value: datetime) -> datetime:
    try:
        return value.astimezone(UTC)
    except OverflowError:
        raise ValueError("The time is outside the supported range.") from None


UtcTime = Annotated[AwareDatetime, AfterValidator(_to_utc)]
AccountId = Annotated[IdStr, Path(description="The account ID as a decimal string; system accounts have negative IDs.")]
CurrencyId = Annotated[PositiveIdStr, Path(description="The currency ID (the Discord server ID) as a decimal string.")]


class AccountOut(BaseModel):
    account_id: IntOut
    kind: AccountKind = Field(description="user, issuance (a currency's supply account), stake_custody (holds every stake) or pool_custody (holds a DEX pool's reserves).")
    currency_id: IntOut | None = Field(description="The currency of an issuance account, else null.")
    pool_id: IntOut | None = Field(description="The pool of a pool custody account, else null.")
    created_at: datetime


class UsernameOut(BaseModel):
    account_id: IntOut
    username: str = Field(description="The Discord username (not the display name).")


class AccountStatsOut(BaseModel):
    account_id: IntOut
    total_transfers: int = Field(description="The exact number of transfers with the account on either side.")
    first_transfer_at: datetime | None = Field(description="The time of the account's first transfer, null when it has none.")
    last_transfer_at: datetime | None = Field(description="The time of the account's last transfer, null when it has none.")


class BalanceOut(BaseModel):
    currency_id: IntOut
    symbol: str
    name: str
    amount: Amount


class AccountBalanceOut(BalanceOut):
    account_id: IntOut
    currency_deleted: bool = Field(description="Whether the currency has been deleted.")


class StakeInfoOut(BaseModel):
    account_id: IntOut
    currency_id: IntOut
    symbol: str
    name: str
    amount: Amount = Field(description="The staked amount as of the last accrual.")
    last_accrued_at: datetime = Field(description="When interest was last added to the stored amount.")
    hourly_rate_ppm: IntOut = Field(description="The currency's hourly interest rate in parts per million (1000000 is 100% per hour).")
    interest_halted: bool = Field(description="Whether interest has stopped because the currency's supply reached the largest amount.")
    projected_amount: Amount = Field(description="The amount an accrual at projected_at would produce, computed without writing anything.")
    projected_at: datetime


class TransferRecordOut(BaseModel):
    transfer_id: IntOut
    execution_id: IntOut = Field(description="The execution that made the transfer.")
    currency_id: IntOut
    from_account: IntOut
    to_account: IntOut
    amount: Amount
    kind: str = Field(description="principal, interest or gas.")
    created_at: datetime


class TransferEntryOut(TransferRecordOut):
    currency_symbol: str = Field(description="The currency's current symbol.")
    execution_type: str = Field(description="The type of the execution that made the transfer.")
    execution_status: ExecutionStatus
    origin: Origin = Field(description="native for executions of this version, legacy for executions imported from the old version.")
    label_basis: LabelBasis | None = Field(description="How the type of a legacy execution was decided; null for native executions.")
    initiator_id: IntOut
    parent_execution_id: IntOut | None = Field(description="The calling execution of a nested contract execution, else null.")
    parent_origin: Origin | None = Field(description="The origin of the parent execution, null without a parent.")
    parent_label_basis: LabelBasis | None = Field(description="The label basis of a legacy parent execution, else null.")
    contract_function: str | None = Field(description="The called function of a contract call, else null.")


class ExecutionOut(BaseModel):
    execution_id: IntOut
    type: str = Field(description="The operation, such as transfer, swap or contract_call; legacy imports may carry other values.")
    origin: Origin = Field(description="native for executions of this version, legacy for executions imported from the old version.")
    label_basis: LabelBasis | None = Field(description="How the type of a legacy execution was decided; null for native executions.")
    initiator_id: IntOut
    parent_execution_id: IntOut | None = Field(description="The calling execution of a nested contract execution, else null.")
    parent_origin: Origin | None = Field(description="The origin of the parent execution, null without a parent.")
    parent_label_basis: LabelBasis | None = Field(description="The label basis of a legacy parent execution, else null.")
    contract_id: IntOut | None = Field(description="The contract a contract_call called or a contract management execution acted on, else null.")
    status: ExecutionStatus
    input: dict[str, Any] = Field(description="The request as stored; every integer, amounts included, is a decimal string in minimal units. Empty for private types.")
    output: dict[str, Any] | None = Field(description="The result or error as stored, in the same format as input; null while unset and for private types.")
    has_idempotency_key: bool = Field(description="Whether the request carried an idempotency key; the key itself is never returned.")
    contract_function: str | None
    created_at: datetime


class DeliveryErrorOut(BaseModel):
    reason: str = Field(
        description="Why the operation failed: permission_revoked, channel_not_in_guild, channel_unavailable, message_rejected, role_change_rejected, retries_exhausted, malformed_payload or internal_error."
    )
    http_status: int | None = Field(description="The HTTP status of Discord's answer, null when no answer decided the failure.")
    code: str | None = Field(description="Discord's JSON error code as a decimal string, null when Discord gave none.")
    message: str | None = Field(description="Discord's error message, null when Discord gave none.")


class DeliveryOut(BaseModel):
    outbox_id: IntOut
    kind: str = Field(description="discord_send, discord_add_role or discord_remove_role.")
    guild_id: IntOut | None
    channel_id: IntOut | None = Field(description="The channel of discord_send, else null.")
    user_id: IntOut | None = Field(description="The member of a role change, else null.")
    role_id: IntOut | None = Field(description="The role of a role change, else null.")
    status: Literal["pending", "sent", "failed"] = Field(description="pending until the worker finishes, then sent or failed.")
    attempts: int = Field(description="Delivery attempts that sent a request to Discord.")
    created_at: datetime
    next_attempt_at: datetime | None = Field(description="When a pending operation is tried next, else null.")
    completed_at: datetime | None = Field(description="When the operation was sent or failed, null while pending.")
    message_id: IntOut | None = Field(description="The ID of the message discord_send posted, else null.")
    error: DeliveryErrorOut | None = Field(description="Why a failed operation failed, else null.")


class ExecutionNodeOut(ExecutionOut):
    transfers: list[TransferRecordOut] = Field(description="The execution's own transfers in ascending transfer_id order.")
    child_execution_ids: list[IntOut] = Field(description="The direct nested executions in ascending order.")
    deliveries: list[DeliveryOut] = Field(description="The Discord messages and role changes the execution queued, in queue order, with the worker's progress; the message text is not included.")


class ExecutionDetailOut(ExecutionNodeOut):
    children: list[ExecutionNodeOut] = Field(description="The direct nested executions with their transfers; deeper levels are read by their own IDs.")


class ClaimOut(BaseModel):
    claim_id: IntOut
    claimant_id: IntOut = Field(description="The account that asks to be paid.")
    payer_id: IntOut = Field(description="The account asked to pay.")
    currency_id: IntOut
    amount: Amount
    description: str | None
    status: ClaimStatus
    created_at: datetime
    updated_at: datetime
    created_execution_id: IntOut
    created_execution_origin: Origin = Field(description="The origin of the execution that created the claim.")
    created_execution_label_basis: LabelBasis | None = Field(description="The label basis of a legacy creating execution, else null.")
    resolved_execution_id: IntOut | None = Field(description="The execution that paid or canceled the claim, null while pending.")
    resolved_execution_origin: Origin | None = Field(description="The origin of the resolving execution, null while pending.")
    resolved_execution_label_basis: LabelBasis | None = Field(description="The label basis of a legacy resolving execution, else null.")


class AllowanceOut(BaseModel):
    owner_id: IntOut
    spender_id: IntOut
    currency_id: IntOut
    symbol: str
    amount: Amount = Field(description="What the spender may still move from the owner's balance; zero when no allowance was given.")


class AllowanceChangeOut(BaseModel):
    allowance_history_id: IntOut
    execution_id: IntOut = Field(description="The execution that changed the allowance (an approve, a transfer_from or a currency deletion).")
    owner_id: IntOut
    spender_id: IntOut
    currency_id: IntOut
    old_amount: Amount
    new_amount: Amount
    created_at: datetime


class CurrencySummaryOut(BaseModel):
    currency_id: IntOut
    symbol: str
    name: str
    supply: Amount
    deleted: bool


class CurrencyListOut(BaseModel):
    items: list[CurrencySummaryOut] = Field(description="Every currency, ordered by symbol then currency_id.")


class CurrencyOut(BaseModel):
    currency_id: IntOut = Field(description="The Discord server ID the currency belongs to.")
    name: str
    symbol: str
    issuance_account_id: IntOut = Field(description="The system account whose negative balance is the supply.")
    created_by: IntOut
    created_at: datetime
    supply: Amount
    hourly_rate_ppm: IntOut = Field(description="The hourly staking interest rate in parts per million (1000000 is 100% per hour).")
    pending_rate_ppm: IntOut | None = Field(description="A requested rate that is not applied yet, else null.")
    rate_change_requested_at: datetime | None
    rate_change_apply_after: datetime | None = Field(description="The pending rate can be applied from this time on.")
    minting_renounced: bool
    delete_requested_at: datetime | None = Field(description="The time of a deletion request that has not lapsed, else null.")
    delete_finalize_from: datetime | None = Field(description="The requested deletion can be finalized from this time on.")
    delete_finalize_until: datetime | None = Field(description="The deletion request lapses after this time.")
    delegation_enabled: bool = Field(description="Whether server Administrators may manage the currency besides the server owner.")
    interest_halted: bool = Field(description="Whether staking interest has stopped because the supply reached the largest amount.")
    deleted_at: datetime | None


@dataclass(frozen=True)
class TransferFilters:
    """The transfer history filters shared by the account history and the history search."""

    currency_id: int | None
    start: datetime | None
    end: datetime | None
    min_amount: int | None
    max_amount: int | None
    execution_type: str | None
    contract_function: str | None
    sort_by: str
    sort_order: str

    def arguments(self) -> dict[str, Any]:
        return {
            "currency_id": self.currency_id,
            "start": self.start,
            "end": self.end,
            "min_amount": self.min_amount,
            "max_amount": self.max_amount,
            "execution_type": self.execution_type,
            "contract_function": self.contract_function,
            "sort_by": self.sort_by,
            "sort_order": self.sort_order,
        }


async def _transfer_filters(
    currency_id: Annotated[PositiveIdStr | None, Query(description="Only transfers of this currency.")] = None,
    start: Annotated[UtcTime | None, Query(description="Only transfers at or after this ISO 8601 time with a UTC offset.")] = None,
    end: Annotated[UtcTime | None, Query(description="Only transfers before this ISO 8601 time with a UTC offset (exclusive).")] = None,
    min_amount: Annotated[NonNegativeAmount | None, Query(description="Only transfers of at least this amount string.")] = None,
    max_amount: Annotated[NonNegativeAmount | None, Query(description="Only transfers of at most this amount string.")] = None,
    execution_type: Annotated[str | None, Query(min_length=1, max_length=ENUM_LENGTH, description="Only transfers made by executions of this type.")] = None,
    contract_function: Annotated[str | None, Query(min_length=1, max_length=CONTRACT_FUNCTION_MAX, description="Only transfers made by calls of this contract function.")] = None,
    sort_by: Annotated[Literal["transfer_id", "timestamp", "amount"], Query(description="The sort key; ties are ordered by transfer_id.")] = "transfer_id",
    sort_order: Annotated[Literal["asc", "desc"], Query(description="The sort direction.")] = "desc",
) -> TransferFilters:
    return TransferFilters(
        currency_id=currency_id,
        start=start,
        end=end,
        min_amount=min_amount,
        max_amount=max_amount,
        execution_type=execution_type,
        contract_function=contract_function,
        sort_by=sort_by,
        sort_order=sort_order,
    )


TransferFiltersDep = Annotated[TransferFilters, Depends(_transfer_filters)]
BalancePage = Annotated[PageParams, Depends(page_params(core_paging.BALANCES_PER_PAGE_DEFAULT, core_paging.BALANCES_PER_PAGE_MAX))]
StakePage = Annotated[PageParams, Depends(page_params(core_paging.STAKES_PER_PAGE_DEFAULT, core_paging.STAKES_PER_PAGE_MAX))]
HistoryPage = Annotated[PageParams, Depends(page_params(core_paging.HISTORY_PER_PAGE_DEFAULT, core_paging.HISTORY_PER_PAGE_MAX))]
ExecutionPage = Annotated[PageParams, Depends(page_params(core_paging.EXECUTIONS_PER_PAGE_DEFAULT, core_paging.EXECUTIONS_PER_PAGE_MAX))]
ClaimPage = Annotated[PageParams, Depends(page_params(core_paging.CLAIMS_PER_PAGE_DEFAULT, core_paging.CLAIMS_PER_PAGE_MAX))]
AllowanceHistoryPage = Annotated[PageParams, Depends(page_params(core_paging.ALLOWANCE_HISTORY_PER_PAGE_DEFAULT, core_paging.ALLOWANCE_HISTORY_PER_PAGE_MAX))]


def _transfer_fields(item: queries.Transfer) -> dict[str, Any]:
    return {
        "transfer_id": item.transfer_id,
        "execution_id": item.execution_id,
        "currency_id": item.currency_id,
        "from_account": item.from_account,
        "to_account": item.to_account,
        "amount": item.amount,
        "kind": item.kind,
        "created_at": item.created_at,
    }


def _transfer(item: queries.Transfer) -> TransferRecordOut:
    return TransferRecordOut(**_transfer_fields(item))


def _transfer_entry(item: queries.TransferEntry) -> TransferEntryOut:
    return TransferEntryOut(
        **_transfer_fields(item),
        currency_symbol=item.currency_symbol,
        execution_type=item.execution_type,
        execution_status=item.execution_status,
        origin=item.origin,
        label_basis=item.label_basis,
        initiator_id=item.initiator_id,
        parent_execution_id=item.parent_execution_id,
        parent_origin=item.parent_origin,
        parent_label_basis=item.parent_label_basis,
        contract_function=item.contract_function,
    )


def _execution_fields(item: queries.Execution) -> dict[str, Any]:
    return {
        "execution_id": item.execution_id,
        "type": item.type,
        "origin": item.origin,
        "label_basis": item.label_basis,
        "initiator_id": item.initiator_id,
        "parent_execution_id": item.parent_execution_id,
        "parent_origin": item.parent_origin,
        "parent_label_basis": item.parent_label_basis,
        "contract_id": item.contract_id,
        "status": item.status,
        "input": dict(item.input),
        "output": None if item.output is None else dict(item.output),
        "has_idempotency_key": item.has_idempotency_key,
        "contract_function": item.contract_function,
        "created_at": item.created_at,
    }


def _execution(item: queries.Execution) -> ExecutionOut:
    return ExecutionOut(**_execution_fields(item))


# The statuses a contract delivery can have; the dispatcher never skips contract operations.
_DELIVERY_STATUSES: Final[dict[OutboxStatus, Literal["pending", "sent", "failed"]]] = {OutboxStatus.PENDING: "pending", OutboxStatus.SENT: "sent", OutboxStatus.FAILED: "failed"}


def _delivery(item: queries.Delivery) -> DeliveryOut:
    error = None
    if item.error is not None:
        error = DeliveryErrorOut(reason=item.error.reason, http_status=item.error.http_status, code=item.error.code, message=item.error.message)
    return DeliveryOut(
        outbox_id=item.outbox_id,
        kind=item.kind,
        guild_id=item.guild_id,
        channel_id=item.channel_id,
        user_id=item.user_id,
        role_id=item.role_id,
        status=_DELIVERY_STATUSES[item.status],
        attempts=item.attempts,
        created_at=item.created_at,
        next_attempt_at=item.next_attempt_at,
        completed_at=item.completed_at,
        message_id=item.message_id,
        error=error,
    )


def _node_fields(node: queries.ExecutionNode) -> dict[str, Any]:
    return {
        **_execution_fields(node.execution),
        "transfers": [_transfer(item) for item in node.transfers],
        "child_execution_ids": list(node.child_execution_ids),
        "deliveries": [_delivery(item) for item in node.deliveries],
    }


def _claim(item: claims.Claim) -> ClaimOut:
    return ClaimOut(
        claim_id=item.claim_id,
        claimant_id=item.claimant_id,
        payer_id=item.payer_id,
        currency_id=item.currency_id,
        amount=item.amount,
        description=item.description,
        status=item.status,
        created_at=item.created_at,
        updated_at=item.updated_at,
        created_execution_id=item.created_execution_id,
        created_execution_origin=item.created_execution_origin,
        created_execution_label_basis=item.created_execution_label_basis,
        resolved_execution_id=item.resolved_execution_id,
        resolved_execution_origin=item.resolved_execution_origin,
        resolved_execution_label_basis=item.resolved_execution_label_basis,
    )


def _allowance_change(item: allowances.AllowanceChange) -> AllowanceChangeOut:
    return AllowanceChangeOut(
        allowance_history_id=item.allowance_history_id,
        execution_id=item.execution_id,
        owner_id=item.owner_id,
        spender_id=item.spender_id,
        currency_id=item.currency_id,
        old_amount=item.old_amount,
        new_amount=item.new_amount,
        created_at=item.created_at,
    )


def _stake(item: staking.StakeInfo, now: datetime, supply: int | None) -> StakeInfoOut:
    return StakeInfoOut(
        account_id=item.account_id,
        currency_id=item.currency_id,
        symbol=item.symbol,
        name=item.name,
        amount=item.amount,
        last_accrued_at=item.last_accrued_at,
        hourly_rate_ppm=item.hourly_rate_ppm,
        interest_halted=item.interest_halted,
        projected_amount=staking.projected_amount(item, now, supply=supply),
        projected_at=now,
    )


def _currency(info: CurrencyInfo) -> CurrencyOut:
    return CurrencyOut(
        currency_id=info.currency_id,
        name=info.name,
        symbol=info.symbol,
        issuance_account_id=info.issuance_account_id,
        created_by=info.created_by,
        created_at=info.created_at,
        supply=info.supply,
        hourly_rate_ppm=info.hourly_rate_ppm,
        pending_rate_ppm=info.pending_rate_ppm,
        rate_change_requested_at=info.rate_change_requested_at,
        rate_change_apply_after=info.rate_change_apply_after,
        minting_renounced=info.minting_renounced,
        delete_requested_at=info.delete_requested_at,
        delete_finalize_from=info.delete_finalize_from,
        delete_finalize_until=info.delete_finalize_until,
        delegation_enabled=info.delegation_enabled,
        interest_halted=info.interest_halted,
        deleted_at=info.deleted_at,
    )


@router.get("/accounts/{account_id}", response_model=AccountOut, summary="Kind of an account and what a system account belongs to", responses=_NOT_FOUND)
async def get_account(account_id: AccountId, engine: EngineDep) -> AccountOut:
    record = await queries.get_account(engine, account_id)
    if record is None:
        raise AccountNotFound()
    return AccountOut(account_id=record.account_id, kind=record.kind, currency_id=record.currency_id, pool_id=record.pool_id, created_at=record.created_at)


@router.get(
    "/users/{account_id}/name",
    response_model=UsernameOut,
    summary="Discord username of a user",
    responses={**_NOT_FOUND, 503: {"model": ErrorResponse, "description": "Discord cannot answer and no cached name exists."}},
)
async def get_user_name(account_id: Annotated[PositiveIdStr, Path(description="The Discord user ID as a decimal string.")], users: DiscordUsersDep) -> UsernameOut | JSONResponse:
    try:
        username = await users.username(account_id)
    except DiscordUnavailable as error:
        headers = {} if error.retry_after is None else {"Retry-After": str(max(1, math.ceil(error.retry_after)))}
        return JSONResponse(envelope(DISCORD_UNAVAILABLE_CODE, DISCORD_UNAVAILABLE_MESSAGE), status_code=503, headers=headers)
    return UsernameOut(account_id=account_id, username=username)


@router.get("/users/{account_id}/stats", response_model=AccountStatsOut, summary="Transfer statistics of an account")
async def get_user_stats(account_id: AccountId, engine: EngineDep) -> AccountStatsOut:
    stats = await queries.user_stats(engine, account_id)
    return AccountStatsOut(
        account_id=account_id,
        total_transfers=stats.total_transfers,
        first_transfer_at=stats.first_transfer_at,
        last_transfer_at=stats.last_transfer_at,
    )


@router.get("/accounts/{account_id}/balances", response_model=Page[BalanceOut], summary="Non-zero balances of an account")
async def list_account_balances(account_id: AccountId, params: BalancePage, engine: EngineDep) -> Page[BalanceOut]:
    page = await queries.list_balances(engine, account_id, page=params.page, per_page=params.per_page)
    return Page[BalanceOut].build(page, lambda item: BalanceOut(currency_id=item.currency_id, symbol=item.symbol, name=item.name, amount=item.amount))


@router.get("/accounts/{account_id}/balances/{currency_id}", response_model=AccountBalanceOut, summary="Balance of an account in one currency", responses=_NOT_FOUND)
async def get_account_balance(account_id: AccountId, currency_id: CurrencyId, engine: EngineDep) -> AccountBalanceOut:
    async with consistent_snapshot(engine) as conn:
        info = await get_currency(conn, currency_id, include_deleted=True)
        if info is None:
            raise CurrencyNotFound()
        amount = await queries.get_balance(conn, account_id, currency_id)
    return AccountBalanceOut(account_id=account_id, currency_id=currency_id, symbol=info.symbol, name=info.name, amount=amount, currency_deleted=info.deleted_at is not None)


@router.get("/accounts/{account_id}/stakes", response_model=Page[StakeInfoOut], summary="Stakes of an account")
async def list_account_stakes(account_id: AccountId, params: StakePage, engine: EngineDep) -> Page[StakeInfoOut]:
    now = resolve_now()
    async with consistent_snapshot(engine) as conn:
        page = await staking.list_stakes(conn, account_id, page=params.page, per_page=params.per_page)
        supplies = await currency_supplies(conn, (item.currency_id for item in page.items))
    return Page[StakeInfoOut].build(page, lambda item: _stake(item, now, supplies.get(item.currency_id)))


@router.get("/accounts/{account_id}/stakes/{currency_id}", response_model=StakeInfoOut, summary="Stake of an account in one currency", responses=_NOT_FOUND)
async def get_account_stake(account_id: AccountId, currency_id: CurrencyId, engine: EngineDep) -> StakeInfoOut:
    now = resolve_now()
    async with consistent_snapshot(engine) as conn:
        info = await staking.get_stake(conn, account_id, currency_id)
        if info is None:
            raise StakeNotFound()
        supplies = await currency_supplies(conn, [currency_id])
    return _stake(info, now, supplies.get(currency_id))


@router.get("/accounts/{account_id}/history", response_model=Page[TransferEntryOut], summary="Transfers of an account")
async def get_account_history(account_id: AccountId, filters: TransferFiltersDep, params: HistoryPage, engine: EngineDep) -> Page[TransferEntryOut]:
    page = await queries.search_history(engine, user_id=account_id, page=params.page, per_page=params.per_page, **filters.arguments())
    return Page[TransferEntryOut].build(page, _transfer_entry)


@router.get("/accounts/{account_id}/claims", response_model=Page[ClaimOut], summary="Claims of an account as claimant or payer")
async def list_account_claims(
    account_id: AccountId,
    params: ClaimPage,
    engine: EngineDep,
    status: Annotated[ClaimStatus | None, Query(description="Only claims with this status.")] = None,
    role: Annotated[claims.ClaimRole | None, Query(description="Only claims where the account is the claimant or only those where it is the payer; both sides when omitted.")] = None,
) -> Page[ClaimOut]:
    page = await claims.list_claims_for_user(engine, account_id, page=params.page, per_page=params.per_page, status=status, role=role)
    return Page[ClaimOut].build(page, _claim)


@router.get("/accounts/{account_id}/allowance-history", response_model=Page[AllowanceChangeOut], summary="Allowance changes of an owner")
async def list_account_allowance_history(
    account_id: AccountId,
    params: AllowanceHistoryPage,
    engine: EngineDep,
    spender_id: Annotated[IdStr | None, Query(description="Only changes of allowances to this spender.")] = None,
    currency_id: Annotated[PositiveIdStr | None, Query(description="Only changes in this currency.")] = None,
) -> Page[AllowanceChangeOut]:
    page = await allowances.list_allowance_history(engine, owner_id=account_id, spender_id=spender_id, currency_id=currency_id, page=params.page, per_page=params.per_page)
    return Page[AllowanceChangeOut].build(page, _allowance_change)


@router.get("/claims/{claim_id}", response_model=ClaimOut, summary="One claim", responses=_NOT_FOUND)
async def get_claim(claim_id: Annotated[PositiveIdStr, Path(description="The claim ID as a decimal string.")], engine: EngineDep) -> ClaimOut:
    return _claim(await claims.get_claim(engine, claim_id))


@router.get("/allowances/{owner_id}/{spender_id}/{currency_id}", response_model=AllowanceOut, summary="Allowance of a spender over an owner's balance", responses=_NOT_FOUND)
async def get_allowance(
    owner_id: Annotated[IdStr, Path(description="The account whose balance the allowance draws on.")],
    spender_id: Annotated[IdStr, Path(description="The account allowed to move it.")],
    currency_id: CurrencyId,
    engine: EngineDep,
) -> AllowanceOut:
    async with consistent_snapshot(engine) as conn:
        info = await get_currency(conn, currency_id, include_deleted=True)
        if info is None:
            raise CurrencyNotFound()
        amount = await allowances.get_allowance(conn, owner_id, spender_id, currency_id)
    return AllowanceOut(owner_id=owner_id, spender_id=spender_id, currency_id=currency_id, symbol=info.symbol, amount=amount)


@router.get("/currencies", response_model=CurrencyListOut, summary="Every currency with its supply")
async def list_currencies(engine: EngineDep, include_deleted: Annotated[bool, Query(description="Also list deleted currencies.")] = False) -> CurrencyListOut:
    items = await queries.list_currencies(engine, include_deleted=include_deleted)
    return CurrencyListOut(items=[CurrencySummaryOut(currency_id=item.currency_id, symbol=item.symbol, name=item.name, supply=item.supply, deleted=item.deleted) for item in items])


@router.get("/currencies/symbol/{symbol}", response_model=CurrencyOut, summary="A live currency by symbol", responses=_NOT_FOUND)
async def get_currency_by_symbol_route(
    symbol: Annotated[str, Path(min_length=1, max_length=CURRENCY_SYMBOL_MAX, description="The symbol, matched after converting it to upper case.")], engine: EngineDep
) -> CurrencyOut:
    info = await get_currency_by_symbol(engine, symbol.upper())
    if info is None:
        raise CurrencyNotFound()
    return _currency(info)


@router.get("/currencies/{currency_id}", response_model=CurrencyOut, summary="A currency by ID, deleted ones included", responses=_NOT_FOUND)
async def get_currency_route(currency_id: CurrencyId, engine: EngineDep) -> CurrencyOut:
    info = await get_currency(engine, currency_id, include_deleted=True)
    if info is None:
        raise CurrencyNotFound()
    return _currency(info)


@router.get("/history", response_model=Page[TransferEntryOut], summary="Search the transfer history")
async def search_history(
    filters: TransferFiltersDep,
    params: HistoryPage,
    engine: EngineDep,
    transfer_id: Annotated[PositiveIdStr | None, Query(description="Only this transfer.")] = None,
    user_id: Annotated[IdStr | None, Query(description="Only transfers with this account on either side.")] = None,
    source_id: Annotated[IdStr | None, Query(description="Only transfers from this account.")] = None,
    destination_id: Annotated[IdStr | None, Query(description="Only transfers to this account.")] = None,
) -> Page[TransferEntryOut]:
    page = await queries.search_history(
        engine, transfer_id=transfer_id, user_id=user_id, source_id=source_id, destination_id=destination_id, page=params.page, per_page=params.per_page, **filters.arguments()
    )
    return Page[TransferEntryOut].build(page, _transfer_entry)


@router.get("/executions", response_model=Page[ExecutionOut], summary="List executions newest first")
async def list_executions(
    params: ExecutionPage,
    engine: EngineDep,
    initiator_id: Annotated[IdStr | None, Query(description="Only executions started by this account.")] = None,
    execution_type: Annotated[str | None, Query(min_length=1, max_length=ENUM_LENGTH, description="Only executions of this type.")] = None,
    status: Annotated[ExecutionStatus | None, Query(description="Only executions with this status.")] = None,
    contract_id: Annotated[PositiveIdStr | None, Query(description="Only calls of this contract (nested calls included) and its management executions.")] = None,
) -> Page[ExecutionOut]:
    page = await queries.list_executions(engine, initiator_id=initiator_id, execution_type=execution_type, status=status, contract_id=contract_id, page=params.page, per_page=params.per_page)
    return Page[ExecutionOut].build(page, _execution)


@router.get("/executions/{execution_id}", response_model=ExecutionDetailOut, summary="One execution with its transfers and nested executions", responses=_NOT_FOUND)
async def get_execution(execution_id: Annotated[PositiveIdStr, Path(description="The execution ID as a decimal string.")], engine: EngineDep) -> ExecutionDetailOut:
    detail = await queries.get_execution(engine, execution_id)
    if detail is None:
        raise ExecutionNotFound()
    return ExecutionDetailOut(**_node_fields(detail), children=[ExecutionNodeOut(**_node_fields(child)) for child in detail.children])


@router.get("/transfers/{transfer_id}", response_model=TransferEntryOut, summary="One transfer with its execution summary", responses=_NOT_FOUND)
async def get_transfer(transfer_id: Annotated[PositiveIdStr, Path(description="The transfer ID as a decimal string.")], engine: EngineDep) -> TransferEntryOut:
    entry = await queries.get_transfer(engine, transfer_id)
    if entry is None:
        raise TransferNotFound()
    return _transfer_entry(entry)
