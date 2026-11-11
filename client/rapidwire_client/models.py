"""Typed results of the API: frozen dataclasses with int IDs, Decimal amounts, aware datetimes and contract values, a Page for paged lists, and WriteResult as the base of every recorded write."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, dataclass_transform

from rapidwire_client.values import ContractValue

__all__ = [
    "MAX_WINDOW",
    "AccountBalance",
    "AccountInfo",
    "AccountStats",
    "Allowance",
    "AllowanceChange",
    "AmountsConfig",
    "ApproveResult",
    "BackupDownload",
    "BackupInfo",
    "BackupList",
    "Balance",
    "CallResult",
    "Claim",
    "ClaimCanceled",
    "ClaimCreated",
    "ClaimPaid",
    "ContractBinary",
    "ContractConfig",
    "ContractDeleted",
    "ContractFunction",
    "ContractFunctions",
    "ContractInfo",
    "ContractLock",
    "ContractRegistered",
    "ContractStatusChange",
    "ContractVersion",
    "ContractVersionSummary",
    "CurrencyCreated",
    "CurrencyDeleted",
    "CurrencyInfo",
    "CurrencyLimits",
    "CurrencySummary",
    "DelegationChange",
    "DeleteRequested",
    "Delivery",
    "DeliveryError",
    "DiscordPermission",
    "DiscordPermissionChange",
    "DryRunError",
    "DryRunResult",
    "DryRunTransfer",
    "Execution",
    "ExecutionDetail",
    "ExecutionNode",
    "GasConfig",
    "LiquidityChange",
    "MintingRenounced",
    "NotificationChange",
    "NotificationPermission",
    "OutboxItem",
    "Page",
    "PageLimit",
    "PagingConfig",
    "PairPools",
    "Pool",
    "PoolCreated",
    "Position",
    "RandomDraw",
    "RateChangeApplied",
    "RateChangeRequested",
    "ServiceConfig",
    "Stake",
    "StakeChange",
    "StakingConfig",
    "StorageEntry",
    "StorageValue",
    "SupplyChange",
    "SwapConfig",
    "SwapHop",
    "SwapQuote",
    "SwapResult",
    "SwapRoute",
    "TokenInfo",
    "TokensRevoked",
    "Transfer",
    "TransferEntry",
    "TransferFromResult",
    "TransferResult",
    "Username",
    "VersionInfo",
    "WriteResult",
]

# Only the first MAX_WINDOW items of a list can be paged through.
MAX_WINDOW: Final = 10_000


@dataclass_transform(frozen_default=True, kw_only_default=True)
def _model[C](cls: type[C]) -> type[C]:
    return dataclass(cls, frozen=True, kw_only=True)


@_model
class Page[T]:
    """One page of a list; total is exact unless total_is_lower_bound is True, which a database-counted list sets when more than MAX_WINDOW items match and total is MAX_WINDOW."""

    items: tuple[T, ...]
    page: int
    per_page: int
    total: int
    total_is_lower_bound: bool

    @property
    def has_next(self) -> bool:
        """Whether a following page can hold more items and lies within the reachable window."""
        reached = self.page * self.per_page
        more = reached < self.total or self.total_is_lower_bound
        return len(self.items) == self.per_page and more and reached + self.per_page <= MAX_WINDOW


@_model
class WriteResult:
    """A recorded write: execution_id is its execution, and replayed is True when the answer came from an earlier request with the same idempotency key."""

    execution_id: int
    replayed: bool = False


@_model
class VersionInfo:
    name: str
    version: str


@_model
class AmountsConfig:
    decimal_places: int
    minimal_unit: Decimal
    max_amount: Decimal


@_model
class CurrencyLimits:
    name_max_length: int
    symbol_max_length: int
    claim_description_max_length: int


@_model
class StakingConfig:
    rate_ppm_max: int
    rate_percent_per_hour_max: Decimal
    rate_change_timelock_seconds: int
    delete_timelock_seconds: int
    delete_expiry_seconds: int


@_model
class SwapConfig:
    fee_tiers_bps: tuple[int, ...]
    fee_denominator: int
    minimum_liquidity: int
    chunk_count: int
    max_routes: int


@_model
class GasConfig:
    currency_id: int
    price: int
    default_gas: int
    max_gas: int


@_model
class ContractConfig:
    max_size: int
    max_call_depth: int
    storage_max_keys: int
    storage_max_bytes: int
    storage_key_max_length: int
    storage_value_max_bytes: int


@_model
class PageLimit:
    default: int
    max: int


@_model
class PagingConfig:
    max_window: int
    limits: dict[str, PageLimit]


@_model
class ServiceConfig:
    amounts: AmountsConfig
    currency: CurrencyLimits
    staking: StakingConfig
    swap: SwapConfig
    gas: GasConfig
    contract: ContractConfig
    paging: PagingConfig
    idempotency_key_max_length: int
    max_body_bytes: int


@_model
class AccountInfo:
    """What an account is: kind is user, issuance, stake_custody or pool_custody, with the currency of an issuance account and the pool of a pool custody account."""

    account_id: int
    kind: str
    currency_id: int | None
    pool_id: int | None
    created_at: datetime


@_model
class Username:
    account_id: int
    username: str


@_model
class AccountStats:
    """Transfer statistics of an account; total_transfers is exact."""

    account_id: int
    total_transfers: int
    first_transfer_at: datetime | None
    last_transfer_at: datetime | None


@_model
class Balance:
    currency_id: int
    symbol: str
    name: str
    amount: Decimal


@_model
class AccountBalance(Balance):
    account_id: int
    currency_deleted: bool


@_model
class Stake:
    account_id: int
    currency_id: int
    symbol: str
    name: str
    amount: Decimal
    last_accrued_at: datetime
    hourly_rate_ppm: int
    interest_halted: bool
    projected_amount: Decimal
    projected_at: datetime


@_model
class Transfer:
    transfer_id: int
    execution_id: int
    currency_id: int
    from_account: int
    to_account: int
    amount: Decimal
    kind: str
    created_at: datetime


@_model
class TransferEntry(Transfer):
    currency_symbol: str
    execution_type: str
    execution_status: str
    initiator_id: int
    parent_execution_id: int | None
    contract_function: str | None


@_model
class Execution:
    """An execution; input and output are the stored objects, in which every integer, amounts included, is a decimal string in minimal units, and contract_id is the contract a contract_call called or a contract management execution acted on."""

    execution_id: int
    type: str
    initiator_id: int
    parent_execution_id: int | None
    contract_id: int | None
    status: str
    input: dict[str, Any]
    output: dict[str, Any] | None
    has_idempotency_key: bool
    contract_function: str | None
    created_at: datetime


@_model
class DeliveryError:
    reason: str
    http_status: int | None
    code: str | None
    message: str | None


@_model
class Delivery:
    """A Discord message or role change queued by a contract execution and the worker's progress on it; the message text is not included."""

    outbox_id: int
    kind: str
    guild_id: int | None
    channel_id: int | None
    user_id: int | None
    role_id: int | None
    status: str
    attempts: int
    created_at: datetime
    next_attempt_at: datetime | None
    completed_at: datetime | None
    message_id: int | None
    error: DeliveryError | None


@_model
class ExecutionNode(Execution):
    transfers: tuple[Transfer, ...]
    child_execution_ids: tuple[int, ...]
    deliveries: tuple[Delivery, ...]


@_model
class ExecutionDetail(ExecutionNode):
    children: tuple[ExecutionNode, ...]


@_model
class Claim:
    claim_id: int
    claimant_id: int
    payer_id: int
    currency_id: int
    amount: Decimal
    description: str | None
    status: str
    created_at: datetime
    updated_at: datetime
    created_execution_id: int
    resolved_execution_id: int | None


@_model
class Allowance:
    owner_id: int
    spender_id: int
    currency_id: int
    symbol: str
    amount: Decimal


@_model
class AllowanceChange:
    allowance_history_id: int
    execution_id: int
    owner_id: int
    spender_id: int
    currency_id: int
    old_amount: Decimal
    new_amount: Decimal
    created_at: datetime


@_model
class CurrencySummary:
    currency_id: int
    symbol: str
    name: str
    supply: Decimal
    deleted: bool


@_model
class CurrencyInfo:
    currency_id: int
    name: str
    symbol: str
    issuance_account_id: int
    created_by: int
    created_at: datetime
    supply: Decimal
    hourly_rate_ppm: int
    pending_rate_ppm: int | None
    rate_change_requested_at: datetime | None
    rate_change_apply_after: datetime | None
    minting_renounced: bool
    delete_requested_at: datetime | None
    delete_finalize_from: datetime | None
    delete_finalize_until: datetime | None
    delegation_enabled: bool
    interest_halted: bool
    deleted_at: datetime | None


@_model
class _CurrencyList:
    items: tuple[CurrencySummary, ...]


@_model
class TransferResult(WriteResult):
    transfer_id: int
    sender_id: int
    recipient_id: int
    currency_id: int
    amount: Decimal


@_model
class TransferFromResult(WriteResult):
    transfer_id: int
    spender_id: int
    source_id: int
    recipient_id: int
    currency_id: int
    amount: Decimal
    allowance_remaining: Decimal


@_model
class ApproveResult(WriteResult):
    owner_id: int
    spender_id: int
    currency_id: int
    old_amount: Decimal
    new_amount: Decimal


@_model
class ClaimCreated(WriteResult):
    claim_id: int
    claimant_id: int
    payer_id: int
    currency_id: int
    amount: Decimal
    description: str | None


@_model
class ClaimPaid(WriteResult):
    claim_id: int
    transfer_id: int
    payer_id: int
    claimant_id: int
    currency_id: int
    amount: Decimal


@_model
class ClaimCanceled(WriteResult):
    claim_id: int
    canceled_by: int


@_model
class StakeChange(WriteResult):
    account_id: int
    currency_id: int
    amount: Decimal
    stake_amount: Decimal
    transfer_id: int
    interest: Decimal
    interest_execution_id: int | None


@_model
class TokenInfo:
    token_id: int
    active: bool
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None


@_model
class TokensRevoked(WriteResult):
    revoked_token_ids: tuple[int, ...]


@_model
class CurrencyCreated(WriteResult):
    currency_id: int
    name: str
    symbol: str
    hourly_rate_ppm: int
    initial_supply: Decimal
    transfer_id: int | None
    revived: bool


@_model
class SupplyChange(WriteResult):
    currency_id: int
    account_id: int
    amount: Decimal
    transfer_id: int


@_model
class MintingRenounced(WriteResult):
    currency_id: int


@_model
class RateChangeRequested(WriteResult):
    currency_id: int
    new_rate_ppm: int
    requested_at: datetime
    apply_after: datetime


@_model
class RateChangeApplied(WriteResult):
    currency_id: int
    old_rate_ppm: int
    new_rate_ppm: int
    stakes_accrued: int
    interest_execution_ids: tuple[int, ...]


@_model
class DeleteRequested(WriteResult):
    currency_id: int
    requested_at: datetime
    finalize_from: datetime
    finalize_until: datetime


@_model
class CurrencyDeleted(WriteResult):
    currency_id: int
    burned_amount: Decimal
    balances_burned: int
    stakes_burned: int
    claims_canceled: int
    allowances_deleted: int
    pools_dissolved: int


@_model
class DelegationChange(WriteResult):
    currency_id: int
    enabled: bool
    changed: bool


@_model
class NotificationPermission:
    allowed_user_id: int
    created_at: datetime


@_model
class NotificationChange(WriteResult):
    account_id: int
    allowed_user_id: int
    changed: bool


@_model
class DiscordPermission:
    guild_id: int
    account_id: int
    granted_by: int
    created_at: datetime


@_model
class DiscordPermissionChange(WriteResult):
    guild_id: int
    account_id: int
    changed: bool


@_model
class Pool:
    pool_id: int
    currency_a: int
    currency_b: int
    symbol_a: str
    symbol_b: str
    deleted_a: bool
    deleted_b: bool
    fee_bps: int
    active: bool
    reserve_a: Decimal
    reserve_b: Decimal
    total_shares: int
    locked_shares: int
    custody_account_id: int
    created_by: int
    created_at: datetime
    created_execution_id: int
    dissolved_at: datetime | None
    dissolved_execution_id: int | None


@_model
class PairPools:
    currency_a: int
    currency_b: int
    pools: tuple[Pool, ...]


@_model
class Position:
    pool: Pool
    account_id: int
    shares: int
    share_percent: Decimal
    withdrawable_a: Decimal
    withdrawable_b: Decimal


@_model
class SwapHop:
    pool_id: int
    currency_in: int
    currency_out: int
    fee_bps: int
    amount_in: Decimal
    amount_out: Decimal


@_model
class SwapRoute:
    pool_ids: tuple[int, ...]
    currencies: tuple[int, ...]
    fee_bps: tuple[int, ...]
    amount_in: Decimal
    amount_out: Decimal
    hops: tuple[SwapHop, ...]


@_model
class SwapQuote:
    from_currency: int
    to_currency: int
    from_symbol: str
    to_symbol: str
    amount_in: Decimal
    amount_out: Decimal
    routes: tuple[SwapRoute, ...]
    truncated: bool
    symbols: dict[int, str]


@_model
class SwapResult(WriteResult):
    from_currency: int
    to_currency: int
    amount_in: Decimal
    min_out: Decimal
    amount_out: Decimal
    routes: tuple[SwapRoute, ...]
    transfer_ids: tuple[int, ...]


@_model
class LiquidityChange(WriteResult):
    pool_id: int
    currency_a: int
    currency_b: int
    fee_bps: int
    currency_x: int
    currency_y: int
    amount_a: Decimal
    amount_b: Decimal
    amount_x: Decimal
    amount_y: Decimal
    shares: int
    provider_shares: int
    total_shares: int
    reserve_a: Decimal
    reserve_b: Decimal
    transfer_ids: tuple[int, ...]


@_model
class PoolCreated(LiquidityChange):
    locked_shares: int
    custody_account_id: int


@_model
class ContractFunction:
    name: str
    params: tuple[str, ...]
    returns: str
    signature: str


@_model
class ContractInfo:
    account_id: int
    status: str
    current_version: int
    sha256: str
    size: int
    version_count: int
    locked: bool
    locked_until: datetime | None
    storage_keys: int
    storage_bytes: int
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None
    created_execution_id: int
    updated_execution_id: int
    functions: tuple[ContractFunction, ...]


@_model
class ContractFunctions:
    account_id: int
    status: str
    version: int
    functions: tuple[ContractFunction, ...]


@_model
class ContractVersionSummary:
    version: int
    sha256: str
    size: int
    created_execution_id: int
    created_at: datetime


@_model
class ContractVersion(ContractVersionSummary):
    """One version with its binary decoded from base64."""

    account_id: int
    binary: bytes


@_model
class ContractBinary:
    """A downloaded binary whose SHA-256 was checked against the X-Checksum-SHA256 header."""

    account_id: int
    version: int
    data: bytes
    sha256: str
    filename: str | None


@_model
class StorageEntry:
    key: str
    value: ContractValue
    size: int
    updated_at: datetime


@_model
class StorageValue:
    account_id: int
    key: str
    value: ContractValue
    size: int
    updated_at: datetime


@_model
class ContractRegistered(WriteResult):
    account_id: int
    version: int
    sha256: str
    size: int
    status: str
    revived: bool


@_model
class ContractLock(WriteResult):
    account_id: int
    locked_until: datetime
    previous_locked_until: datetime | None


@_model
class ContractStatusChange(WriteResult):
    account_id: int
    status: str


@_model
class ContractDeleted(WriteResult):
    account_id: int
    version: int
    storage_keys_cleared: int
    storage_bytes_cleared: int


@_model
class CallResult(WriteResult):
    contract_id: int
    function: str
    value: ContractValue
    gas_used: int
    gas_limit: int
    gas_currency_id: int
    gas_price: int
    gas_cost: Decimal
    random: tuple[int, ...]


@_model
class DryRunError:
    code: str
    message: str
    kind: str | None
    reason: str | None


@_model
class RandomDraw:
    order: int
    depth: int
    contract_id: int
    function: str
    values: tuple[int, ...]


@_model
class DryRunTransfer:
    currency_id: int
    from_account: int
    to_account: int
    amount: Decimal
    kind: str


@_model
class OutboxItem:
    kind: str
    payload: dict[str, Any]


@_model
class DryRunResult:
    ok: bool
    value: ContractValue | None
    error: DryRunError | None
    gas_used: int
    gas_limit: int
    gas_currency_id: int
    gas_price: int
    gas_cost: Decimal
    random: tuple[int, ...]
    random_tree: tuple[RandomDraw, ...]
    transfers: tuple[DryRunTransfer, ...]
    outbox: tuple[OutboxItem, ...]


@_model
class BackupInfo:
    name: str
    created_at: datetime
    size: int
    sha256: str


@_model
class BackupList(Page[BackupInfo]):
    """One page of the backups, newest first, and the backup_list execution that recorded the listing."""

    execution_id: int


@_model
class BackupDownload:
    """A completed backup download whose size and SHA-256 matched the response headers; path is the written file when one was given."""

    name: str
    size: int
    sha256: str
    execution_id: int | None
    content_type: str | None
    path: Path | None = None
