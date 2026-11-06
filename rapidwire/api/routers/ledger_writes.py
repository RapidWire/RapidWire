"""Authenticated ledger writes: transfers, approvals, claims, staking, token revocation, currency management, notification and Discord permissions, and the private lists that go with them; every write runs as the token's account through the Writer."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Body, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from rapidwire.api.auth import AccountDep, WriterDep
from rapidwire.api.deps import EngineDep, GuildInfoDep
from rapidwire.api.formats import Amount, IntOut, IntString, NonNegativeAmount, PositiveAmount, PositiveIdStr
from rapidwire.api.paging import Page, PageParams, page_params
from rapidwire.core import allowances, claims, currency, discord_permissions, notifications, staking, tokens, transfers
from rapidwire.core import paging as core_paging
from rapidwire.db.tables import CLAIM_DESCRIPTION_MAX, CURRENCY_NAME_MAX, CURRENCY_SYMBOL_MAX, RATE_PPM_MAX

__all__ = ["router"]

router = APIRouter(prefix="/v1", tags=["Ledger writes"])

RatePpm = Annotated[int, IntString(0, RATE_PPM_MAX, f"An hourly rate in parts per million from 0 to {RATE_PPM_MAX}, as a decimal string.")]

TokenPage = Annotated[PageParams, Depends(page_params(core_paging.TOKENS_PER_PAGE_DEFAULT, core_paging.TOKENS_PER_PAGE_MAX))]
NotificationPage = Annotated[PageParams, Depends(page_params(core_paging.NOTIFICATIONS_PER_PAGE_DEFAULT, core_paging.NOTIFICATIONS_PER_PAGE_MAX))]
DiscordPermissionPage = Annotated[PageParams, Depends(page_params(core_paging.DISCORD_PERMISSIONS_PER_PAGE_DEFAULT, core_paging.DISCORD_PERMISSIONS_PER_PAGE_MAX))]


class _Request(BaseModel):
    """A write request body; unknown fields are rejected, so the initiator can never be given in the body."""

    model_config = ConfigDict(extra="forbid")


class TransferIn(_Request):
    recipient_id: PositiveIdStr
    currency_id: PositiveIdStr
    amount: PositiveAmount


class TransferFromIn(_Request):
    source_id: PositiveIdStr = Field(description="The owner whose allowance to the caller is spent.")
    recipient_id: PositiveIdStr
    currency_id: PositiveIdStr
    amount: PositiveAmount


class ApproveIn(_Request):
    spender_id: PositiveIdStr
    currency_id: PositiveIdStr
    amount: NonNegativeAmount = Field(description="The new allowance, replacing the current one; zero removes it.")


class ClaimIn(_Request):
    payer_id: PositiveIdStr
    currency_id: PositiveIdStr
    amount: PositiveAmount
    description: str | None = Field(default=None, max_length=CLAIM_DESCRIPTION_MAX, description="Optional text; empty or whitespace-only text is stored as none.")


class StakeIn(_Request):
    currency_id: PositiveIdStr
    amount: PositiveAmount


class RevokeTokensIn(_Request):
    token_id: PositiveIdStr | None = Field(default=None, description="The token to revoke; without it every active token of the caller is revoked.")


class CurrencyCreateIn(_Request):
    guild_id: PositiveIdStr = Field(description="The Discord server ID, which becomes the currency ID.")
    name: str = Field(max_length=CURRENCY_NAME_MAX)
    symbol: str = Field(max_length=CURRENCY_SYMBOL_MAX, description="The symbol, converted to upper case before it is checked and stored.")
    supply: NonNegativeAmount = Field(description="The initial supply, minted to the caller.")
    hourly_rate_ppm: RatePpm = Field(default=0, description="The hourly staking interest rate in parts per million (1000000 is 100% per hour).")


class AmountIn(_Request):
    amount: PositiveAmount


class RateChangeIn(_Request):
    hourly_rate_ppm: RatePpm = Field(description="The new hourly staking interest rate in parts per million.")


class UserIn(_Request):
    user_id: PositiveIdStr


class AccountIn(_Request):
    account_id: PositiveIdStr


class TransferSentOut(BaseModel):
    execution_id: IntOut
    transfer_id: IntOut
    sender_id: IntOut
    recipient_id: IntOut
    currency_id: IntOut
    amount: Amount

    @classmethod
    def of(cls, result: transfers.TransferResult) -> TransferSentOut:
        return cls(
            execution_id=result.execution_id,
            transfer_id=result.transfer_id,
            sender_id=result.sender_id,
            recipient_id=result.recipient_id,
            currency_id=result.currency_id,
            amount=result.amount,
        )


class TransferFromOut(BaseModel):
    execution_id: IntOut
    transfer_id: IntOut
    spender_id: IntOut
    source_id: IntOut
    recipient_id: IntOut
    currency_id: IntOut
    amount: Amount
    allowance_remaining: Amount

    @classmethod
    def of(cls, result: transfers.TransferFromResult) -> TransferFromOut:
        return cls(
            execution_id=result.execution_id,
            transfer_id=result.transfer_id,
            spender_id=result.spender_id,
            source_id=result.owner_id,
            recipient_id=result.recipient_id,
            currency_id=result.currency_id,
            amount=result.amount,
            allowance_remaining=result.allowance_remaining,
        )


class ApproveOut(BaseModel):
    execution_id: IntOut
    owner_id: IntOut
    spender_id: IntOut
    currency_id: IntOut
    old_amount: Amount
    new_amount: Amount

    @classmethod
    def of(cls, result: allowances.ApproveResult) -> ApproveOut:
        return cls(
            execution_id=result.execution_id,
            owner_id=result.owner_id,
            spender_id=result.spender_id,
            currency_id=result.currency_id,
            old_amount=result.old_amount,
            new_amount=result.new_amount,
        )


class ClaimCreatedOut(BaseModel):
    execution_id: IntOut
    claim_id: IntOut
    claimant_id: IntOut
    payer_id: IntOut
    currency_id: IntOut
    amount: Amount
    description: str | None

    @classmethod
    def of(cls, result: claims.CreateClaimResult) -> ClaimCreatedOut:
        return cls(
            execution_id=result.execution_id,
            claim_id=result.claim_id,
            claimant_id=result.claimant_id,
            payer_id=result.payer_id,
            currency_id=result.currency_id,
            amount=result.amount,
            description=result.description,
        )


class ClaimPaidOut(BaseModel):
    execution_id: IntOut
    claim_id: IntOut
    transfer_id: IntOut
    payer_id: IntOut
    claimant_id: IntOut
    currency_id: IntOut
    amount: Amount

    @classmethod
    def of(cls, result: claims.PayClaimResult) -> ClaimPaidOut:
        return cls(
            execution_id=result.execution_id,
            claim_id=result.claim_id,
            transfer_id=result.transfer_id,
            payer_id=result.payer_id,
            claimant_id=result.claimant_id,
            currency_id=result.currency_id,
            amount=result.amount,
        )


class ClaimCanceledOut(BaseModel):
    execution_id: IntOut
    claim_id: IntOut
    canceled_by: IntOut

    @classmethod
    def of(cls, result: claims.CancelClaimResult) -> ClaimCanceledOut:
        return cls(execution_id=result.execution_id, claim_id=result.claim_id, canceled_by=result.canceled_by)


class StakeChangeOut(BaseModel):
    execution_id: IntOut
    account_id: IntOut
    currency_id: IntOut
    amount: Amount = Field(description="The amount moved into or out of the stake.")
    stake_amount: Amount = Field(description="The stake afterwards.")
    transfer_id: IntOut
    interest: Amount = Field(description="The interest accrued just before the change.")
    interest_execution_id: IntOut | None = Field(description="The interest execution that recorded it, None when no interest accrued.")

    @classmethod
    def of(cls, result: staking.StakeChanged) -> StakeChangeOut:
        return cls(
            execution_id=result.execution_id,
            account_id=result.account_id,
            currency_id=result.currency_id,
            amount=result.amount,
            stake_amount=result.stake_amount,
            transfer_id=result.transfer_id,
            interest=result.interest,
            interest_execution_id=result.interest_execution_id,
        )


class TokenOut(BaseModel):
    token_id: IntOut
    active: bool
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None

    @classmethod
    def of(cls, info: tokens.TokenInfo) -> TokenOut:
        return cls(token_id=info.token_id, active=info.active, created_at=info.created_at, revoked_at=info.revoked_at, last_used_at=info.last_used_at)


class TokensRevokedOut(BaseModel):
    execution_id: IntOut
    revoked_token_ids: list[IntOut]

    @classmethod
    def of(cls, result: tokens.TokenRevocation) -> TokensRevokedOut:
        return cls(execution_id=result.execution_id, revoked_token_ids=list(result.revoked_token_ids))


class CurrencyCreatedOut(BaseModel):
    execution_id: IntOut
    currency_id: IntOut
    name: str
    symbol: str
    hourly_rate_ppm: IntOut
    initial_supply: Amount
    transfer_id: IntOut | None = Field(description="The transfer of the initial supply, None when it was zero.")
    revived: bool = Field(description="Whether a deleted currency of the server was created again.")

    @classmethod
    def of(cls, result: currency.CurrencyCreated) -> CurrencyCreatedOut:
        return cls(
            execution_id=result.execution_id,
            currency_id=result.currency_id,
            name=result.name,
            symbol=result.symbol,
            hourly_rate_ppm=result.hourly_rate_ppm,
            initial_supply=result.initial_supply,
            transfer_id=result.transfer_id,
            revived=result.revived,
        )


class SupplyChangedOut(BaseModel):
    execution_id: IntOut
    currency_id: IntOut
    account_id: IntOut
    amount: Amount
    transfer_id: IntOut

    @classmethod
    def of(cls, result: currency.SupplyChanged) -> SupplyChangedOut:
        return cls(execution_id=result.execution_id, currency_id=result.currency_id, account_id=result.account_id, amount=result.amount, transfer_id=result.transfer_id)


class RenouncedOut(BaseModel):
    execution_id: IntOut
    currency_id: IntOut

    @classmethod
    def of(cls, result: currency.MintingRenounced) -> RenouncedOut:
        return cls(execution_id=result.execution_id, currency_id=result.currency_id)


class RateChangeRequestedOut(BaseModel):
    execution_id: IntOut
    currency_id: IntOut
    new_rate_ppm: IntOut
    requested_at: datetime
    apply_after: datetime

    @classmethod
    def of(cls, result: currency.RateChangeRequested) -> RateChangeRequestedOut:
        return cls(execution_id=result.execution_id, currency_id=result.currency_id, new_rate_ppm=result.new_rate_ppm, requested_at=result.requested_at, apply_after=result.apply_after)


class RateChangeAppliedOut(BaseModel):
    execution_id: IntOut
    currency_id: IntOut
    old_rate_ppm: IntOut
    new_rate_ppm: IntOut
    stakes_accrued: int = Field(description="Stakes accrued at the old rate before the switch.")
    interest_execution_ids: list[IntOut]

    @classmethod
    def of(cls, result: currency.RateChangeApplied) -> RateChangeAppliedOut:
        return cls(
            execution_id=result.execution_id,
            currency_id=result.currency_id,
            old_rate_ppm=result.old_rate_ppm,
            new_rate_ppm=result.new_rate_ppm,
            stakes_accrued=result.stakes_accrued,
            interest_execution_ids=list(result.interest_execution_ids),
        )


class DeleteRequestedOut(BaseModel):
    execution_id: IntOut
    currency_id: IntOut
    requested_at: datetime
    finalize_from: datetime
    finalize_until: datetime

    @classmethod
    def of(cls, result: currency.DeleteRequested) -> DeleteRequestedOut:
        return cls(
            execution_id=result.execution_id,
            currency_id=result.currency_id,
            requested_at=result.requested_at,
            finalize_from=result.finalize_from,
            finalize_until=result.finalize_until,
        )


class CurrencyDeletedOut(BaseModel):
    execution_id: IntOut
    currency_id: IntOut
    burned_amount: Amount
    balances_burned: int
    stakes_burned: int
    claims_canceled: int
    allowances_deleted: int
    pools_dissolved: int

    @classmethod
    def of(cls, result: currency.CurrencyDeleted) -> CurrencyDeletedOut:
        return cls(
            execution_id=result.execution_id,
            currency_id=result.currency_id,
            burned_amount=result.burned_amount,
            balances_burned=result.balances_burned,
            stakes_burned=result.stakes_burned,
            claims_canceled=result.claims_canceled,
            allowances_deleted=result.allowances_deleted,
            pools_dissolved=result.pools_dissolved,
        )


class DelegationOut(BaseModel):
    execution_id: IntOut
    currency_id: IntOut
    enabled: bool
    changed: bool = Field(description="False when delegation already had the requested state.")

    @classmethod
    def of(cls, result: currency.DelegationChanged) -> DelegationOut:
        return cls(execution_id=result.execution_id, currency_id=result.currency_id, enabled=result.enabled, changed=result.changed)


class NotificationOut(BaseModel):
    allowed_user_id: IntOut
    created_at: datetime

    @classmethod
    def of(cls, item: notifications.NotificationPermission) -> NotificationOut:
        return cls(allowed_user_id=item.allowed_user_id, created_at=item.created_at)


class NotificationChangeOut(BaseModel):
    execution_id: IntOut
    account_id: IntOut = Field(description="The caller, who receives the notifications.")
    allowed_user_id: IntOut
    changed: bool = Field(description="False when the permission already had the requested state.")

    @classmethod
    def of(cls, result: notifications.NotificationChange) -> NotificationChangeOut:
        return cls(execution_id=result.execution_id, account_id=result.user_id, allowed_user_id=result.allowed_user_id, changed=result.changed)


class DiscordPermissionOut(BaseModel):
    guild_id: IntOut
    account_id: IntOut
    granted_by: IntOut
    created_at: datetime

    @classmethod
    def of(cls, item: discord_permissions.DiscordPermission) -> DiscordPermissionOut:
        return cls(guild_id=item.guild_id, account_id=item.account_id, granted_by=item.granted_by, created_at=item.created_at)


class DiscordPermissionChangeOut(BaseModel):
    execution_id: IntOut
    guild_id: IntOut
    account_id: IntOut
    changed: bool = Field(description="False when the permission already had the requested state.")

    @classmethod
    def of(cls, result: discord_permissions.DiscordPermissionChange) -> DiscordPermissionChangeOut:
        return cls(execution_id=result.execution_id, guild_id=result.guild_id, account_id=result.account_id, changed=result.changed)


@router.post("/transfers", response_model=TransferSentOut, summary="Transfer to another account")
async def post_transfer(body: TransferIn, writer: WriterDep, engine: EngineDep) -> TransferSentOut:
    result = await transfers.transfer(engine, sender_id=writer.account_id, recipient_id=body.recipient_id, currency_id=body.currency_id, amount=body.amount, idempotency_key=writer.idempotency_key)
    return TransferSentOut.of(writer.done(result))


@router.post("/transfers/from", response_model=TransferFromOut, summary="Transfer from another account against its allowance to the caller")
async def post_transfer_from(body: TransferFromIn, writer: WriterDep, engine: EngineDep) -> TransferFromOut:
    result = await transfers.transfer_from(
        engine,
        spender_id=writer.account_id,
        owner_id=body.source_id,
        recipient_id=body.recipient_id,
        currency_id=body.currency_id,
        amount=body.amount,
        idempotency_key=writer.idempotency_key,
    )
    return TransferFromOut.of(writer.done(result))


@router.post("/allowances", response_model=ApproveOut, summary="Set the allowance of a spender over the caller's funds")
async def post_allowance(body: ApproveIn, writer: WriterDep, engine: EngineDep) -> ApproveOut:
    result = await allowances.approve(engine, owner_id=writer.account_id, spender_id=body.spender_id, currency_id=body.currency_id, amount=body.amount, idempotency_key=writer.idempotency_key)
    return ApproveOut.of(writer.done(result))


@router.post("/claims", response_model=ClaimCreatedOut, summary="Create a claim against a payer")
async def post_claim(body: ClaimIn, writer: WriterDep, engine: EngineDep) -> ClaimCreatedOut:
    result = await claims.create_claim(
        engine,
        claimant_id=writer.account_id,
        payer_id=body.payer_id,
        currency_id=body.currency_id,
        amount=body.amount,
        description=body.description,
        idempotency_key=writer.idempotency_key,
    )
    return ClaimCreatedOut.of(writer.done(result))


@router.post("/claims/{claim_id}/pay", response_model=ClaimPaidOut, summary="Pay a claim the caller owes")
async def post_claim_pay(claim_id: PositiveIdStr, writer: WriterDep, engine: EngineDep) -> ClaimPaidOut:
    result = await claims.pay_claim(engine, claim_id=claim_id, payer_id=writer.account_id, idempotency_key=writer.idempotency_key)
    return ClaimPaidOut.of(writer.done(result))


@router.post("/claims/{claim_id}/cancel", response_model=ClaimCanceledOut, summary="Cancel a pending claim as its payer or claimant")
async def post_claim_cancel(claim_id: PositiveIdStr, writer: WriterDep, engine: EngineDep) -> ClaimCanceledOut:
    result = await claims.cancel_claim(engine, claim_id=claim_id, user_id=writer.account_id, idempotency_key=writer.idempotency_key)
    return ClaimCanceledOut.of(writer.done(result))


@router.post("/stakes/deposit", response_model=StakeChangeOut, summary="Stake part of the caller's balance")
async def post_stake_deposit(body: StakeIn, writer: WriterDep, engine: EngineDep) -> StakeChangeOut:
    result = await staking.deposit(engine, account_id=writer.account_id, currency_id=body.currency_id, amount=body.amount, idempotency_key=writer.idempotency_key)
    return StakeChangeOut.of(writer.done(result))


@router.post("/stakes/withdraw", response_model=StakeChangeOut, summary="Withdraw part of the caller's stake")
async def post_stake_withdraw(body: StakeIn, writer: WriterDep, engine: EngineDep) -> StakeChangeOut:
    result = await staking.withdraw(engine, account_id=writer.account_id, currency_id=body.currency_id, amount=body.amount, idempotency_key=writer.idempotency_key)
    return StakeChangeOut.of(writer.done(result))


@router.get("/tokens", response_model=Page[TokenOut], summary="The caller's API tokens")
async def get_tokens(
    account_id: AccountDep,
    engine: EngineDep,
    params: TokenPage,
    include_revoked: Annotated[bool, Query(description="Whether revoked tokens are listed too.")] = True,
) -> Page[TokenOut]:
    page = await tokens.list_tokens(engine, account_id, include_revoked=include_revoked, page=params.page, per_page=params.per_page)
    return Page[TokenOut].build(page, TokenOut.of)


@router.post("/tokens/revoke", response_model=TokensRevokedOut, summary="Revoke the caller's API tokens")
async def post_tokens_revoke(writer: WriterDep, engine: EngineDep, body: Annotated[RevokeTokensIn | None, Body()] = None) -> TokensRevokedOut:
    token_id = None if body is None else body.token_id
    result = await tokens.revoke_tokens(engine, account_id=writer.account_id, token_id=token_id, idempotency_key=writer.idempotency_key)
    return TokensRevokedOut.of(writer.done(result))


@router.post("/currencies", response_model=CurrencyCreatedOut, summary="Create the currency of a server the caller owns")
async def post_currency(body: CurrencyCreateIn, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> CurrencyCreatedOut:
    result = await currency.create_currency(
        engine,
        guild_info,
        guild_id=body.guild_id,
        user_id=writer.account_id,
        name=body.name,
        symbol=body.symbol.upper(),
        initial_supply=body.supply,
        hourly_rate_ppm=body.hourly_rate_ppm,
        idempotency_key=writer.idempotency_key,
    )
    return CurrencyCreatedOut.of(writer.done(result))


@router.post("/currencies/{currency_id}/mint", response_model=SupplyChangedOut, summary="Mint into the caller's wallet")
async def post_currency_mint(currency_id: PositiveIdStr, body: AmountIn, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> SupplyChangedOut:
    result = await currency.mint(engine, guild_info, currency_id=currency_id, user_id=writer.account_id, amount=body.amount, idempotency_key=writer.idempotency_key)
    return SupplyChangedOut.of(writer.done(result))


@router.post("/currencies/{currency_id}/burn", response_model=SupplyChangedOut, summary="Burn part of the caller's balance")
async def post_currency_burn(currency_id: PositiveIdStr, body: AmountIn, writer: WriterDep, engine: EngineDep) -> SupplyChangedOut:
    result = await currency.burn(engine, account_id=writer.account_id, currency_id=currency_id, amount=body.amount, idempotency_key=writer.idempotency_key)
    return SupplyChangedOut.of(writer.done(result))


@router.post("/currencies/{currency_id}/renounce", response_model=RenouncedOut, summary="Give up minting and rate changes for good")
async def post_currency_renounce(currency_id: PositiveIdStr, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> RenouncedOut:
    result = await currency.renounce(engine, guild_info, currency_id=currency_id, user_id=writer.account_id, idempotency_key=writer.idempotency_key)
    return RenouncedOut.of(writer.done(result))


@router.post("/currencies/{currency_id}/rate-change", response_model=RateChangeRequestedOut, summary="Request a timelocked interest rate change")
async def post_currency_rate_change(currency_id: PositiveIdStr, body: RateChangeIn, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> RateChangeRequestedOut:
    result = await currency.request_rate_change(engine, guild_info, currency_id=currency_id, user_id=writer.account_id, new_rate_ppm=body.hourly_rate_ppm, idempotency_key=writer.idempotency_key)
    return RateChangeRequestedOut.of(writer.done(result))


@router.post("/currencies/{currency_id}/rate-change/apply", response_model=RateChangeAppliedOut, summary="Apply the pending interest rate change")
async def post_currency_rate_change_apply(currency_id: PositiveIdStr, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> RateChangeAppliedOut:
    result = await currency.apply_rate_change(engine, guild_info, currency_id=currency_id, user_id=writer.account_id, idempotency_key=writer.idempotency_key)
    return RateChangeAppliedOut.of(writer.done(result))


@router.post("/currencies/{currency_id}/delete", response_model=DeleteRequestedOut, summary="Request deletion of the currency")
async def post_currency_delete(currency_id: PositiveIdStr, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> DeleteRequestedOut:
    result = await currency.request_delete(engine, guild_info, currency_id=currency_id, user_id=writer.account_id, idempotency_key=writer.idempotency_key)
    return DeleteRequestedOut.of(writer.done(result))


@router.post("/currencies/{currency_id}/delete/finalize", response_model=CurrencyDeletedOut, summary="Finalize a requested deletion")
async def post_currency_delete_finalize(currency_id: PositiveIdStr, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> CurrencyDeletedOut:
    result = await currency.finalize_delete(engine, guild_info, currency_id=currency_id, user_id=writer.account_id, idempotency_key=writer.idempotency_key)
    return CurrencyDeletedOut.of(writer.done(result))


@router.post("/currencies/{currency_id}/delegation/enable", response_model=DelegationOut, summary="Let server administrators manage the currency")
async def post_delegation_enable(currency_id: PositiveIdStr, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> DelegationOut:
    result = await currency.enable_delegation(engine, guild_info, currency_id=currency_id, user_id=writer.account_id, idempotency_key=writer.idempotency_key)
    return DelegationOut.of(writer.done(result))


@router.post("/currencies/{currency_id}/delegation/disable", response_model=DelegationOut, summary="Return currency management to the server owner alone")
async def post_delegation_disable(currency_id: PositiveIdStr, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> DelegationOut:
    result = await currency.disable_delegation(engine, guild_info, currency_id=currency_id, user_id=writer.account_id, idempotency_key=writer.idempotency_key)
    return DelegationOut.of(writer.done(result))


@router.get("/notifications", response_model=Page[NotificationOut], summary="Users whose claims notify the caller")
async def get_notifications(account_id: AccountDep, engine: EngineDep, params: NotificationPage) -> Page[NotificationOut]:
    page = await notifications.list_allowed(engine, account_id, page=params.page, per_page=params.per_page)
    return Page[NotificationOut].build(page, NotificationOut.of)


@router.post("/notifications/allow", response_model=NotificationChangeOut, summary="Receive claim notifications from a user")
async def post_notifications_allow(body: UserIn, writer: WriterDep, engine: EngineDep) -> NotificationChangeOut:
    result = await notifications.allow_notifications(engine, user_id=writer.account_id, allowed_user_id=body.user_id, idempotency_key=writer.idempotency_key)
    return NotificationChangeOut.of(writer.done(result))


@router.post("/notifications/deny", response_model=NotificationChangeOut, summary="Stop claim notifications from a user")
async def post_notifications_deny(body: UserIn, writer: WriterDep, engine: EngineDep) -> NotificationChangeOut:
    result = await notifications.deny_notifications(engine, user_id=writer.account_id, allowed_user_id=body.user_id, idempotency_key=writer.idempotency_key)
    return NotificationChangeOut.of(writer.done(result))


@router.get("/guilds/{guild_id}/discord-permissions", response_model=Page[DiscordPermissionOut], summary="Accounts whose contracts may act in a server")
async def get_discord_permissions(guild_id: PositiveIdStr, account_id: AccountDep, engine: EngineDep, guild_info: GuildInfoDep, params: DiscordPermissionPage) -> Page[DiscordPermissionOut]:
    page = await discord_permissions.list_discord_permissions_as(engine, guild_info, guild_id=guild_id, user_id=account_id, page=params.page, per_page=params.per_page)
    return Page[DiscordPermissionOut].build(page, DiscordPermissionOut.of)


@router.post("/guilds/{guild_id}/discord-permissions/allow", response_model=DiscordPermissionChangeOut, summary="Let an account's contracts act in a server")
async def post_discord_permission_allow(guild_id: PositiveIdStr, body: AccountIn, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> DiscordPermissionChangeOut:
    result = await discord_permissions.allow_discord(engine, guild_info, guild_id=guild_id, account_id=body.account_id, acting_user_id=writer.account_id, idempotency_key=writer.idempotency_key)
    return DiscordPermissionChangeOut.of(writer.done(result))


@router.post("/guilds/{guild_id}/discord-permissions/deny", response_model=DiscordPermissionChangeOut, summary="Withdraw an account's permission to act in a server")
async def post_discord_permission_deny(guild_id: PositiveIdStr, body: AccountIn, writer: WriterDep, engine: EngineDep, guild_info: GuildInfoDep) -> DiscordPermissionChangeOut:
    result = await discord_permissions.deny_discord(engine, guild_info, guild_id=guild_id, account_id=body.account_id, acting_user_id=writer.account_id, idempotency_key=writer.idempotency_key)
    return DiscordPermissionChangeOut.of(writer.done(result))
