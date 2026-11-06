"""Service information: the API version and the configuration clients need to format amounts, page lists and build contract calls."""

from __future__ import annotations

from typing import Final

from fastapi import APIRouter
from pydantic import BaseModel, Field

from rapidwire import __version__
from rapidwire.api.body_limit import DEFAULT_BODY_LIMIT
from rapidwire.api.deps import StateDep
from rapidwire.api.formats import DECIMALS, MINIMAL_UNIT, Amount, IntOut
from rapidwire.core import paging
from rapidwire.core.currency import DELETE_EXPIRY, DELETE_TIMELOCK, RATE_CHANGE_TIMELOCK
from rapidwire.db.tables import (
    CLAIM_DESCRIPTION_MAX,
    CONTRACT_STORAGE_KEY_MAX,
    CONTRACT_STORAGE_MAX_BYTES,
    CONTRACT_STORAGE_MAX_KEYS,
    CONTRACT_STORAGE_VALUE_MAX,
    CURRENCY_NAME_MAX,
    CURRENCY_SYMBOL_MAX,
    FEE_DENOMINATOR,
    FEE_TIERS_BPS,
    IDEMPOTENCY_KEY_MAX,
    MINIMUM_LIQUIDITY,
    RATE_PPM_MAX,
)
from rapidwire.db.types import AMOUNT_MAX

__all__ = ["API_NAME", "router"]

API_NAME: Final = "RapidWire API"
# The hourly rate unit: 1,000,000 ppm is 100% per hour, so a percentage has 4 decimal places.
_PPM_PER_PERCENT: Final = 10_000

router = APIRouter(prefix="/v1", tags=["Meta"])


class VersionResponse(BaseModel):
    name: str
    version: str


class AmountsConfig(BaseModel):
    decimal_places: int = Field(description="Amounts have this many decimal places; JSON amounts are strings in currency units.")
    minimal_unit: str = Field(description="The smallest amount, in currency units.")
    max_amount: Amount = Field(description="The largest amount any balance or supply can hold.")


class CurrencyConfig(BaseModel):
    name_max_length: int
    symbol_max_length: int
    claim_description_max_length: int


class StakingConfig(BaseModel):
    rate_ppm_max: IntOut = Field(description="The largest hourly interest rate in parts per million (1000000 is 100% per hour).")
    rate_percent_per_hour_max: str = Field(description="The same limit as a percentage per hour with 4 decimal places.")
    rate_change_timelock_seconds: int = Field(description="A requested rate change can be applied this many seconds after the request.")
    delete_timelock_seconds: int = Field(description="A requested currency deletion can be finalized from this many seconds after the request.")
    delete_expiry_seconds: int = Field(description="A requested currency deletion expires this many seconds after the request.")


class SwapConfigResponse(BaseModel):
    fee_tiers_bps: list[int] = Field(description="The fee tiers a pool can have, in basis points of FEE_DENOMINATOR.")
    fee_denominator: int
    minimum_liquidity: IntOut = Field(description="Shares locked forever when a pool is created.")
    chunk_count: int = Field(description="Number of chunks a swap is split into when allocating it across routes.")
    max_routes: int = Field(description="Number of candidate routes considered for one swap.")


class GasConfig(BaseModel):
    currency_id: IntOut
    price: IntOut = Field(description="Minimal units of the gas currency per gas unit.")
    default_gas: IntOut = Field(description="The gas limit of a call that gives none.")
    max_gas: IntOut = Field(description="The largest gas limit a call may give.")


class ContractConfig(BaseModel):
    max_size: int = Field(description="The largest contract binary in bytes.")
    max_call_depth: int = Field(description="Contract executions on the call stack, counting the top-level call.")
    storage_max_keys: int
    storage_max_bytes: int = Field(description="Total storage per contract: key UTF-8 bytes plus encoded value bytes over all entries.")
    storage_key_max_length: int = Field(description="Characters of one storage key.")
    storage_value_max_bytes: int = Field(description="Encoded bytes of one storage value.")


class PageLimit(BaseModel):
    default: int
    max: int


class PagingConfig(BaseModel):
    max_window: int = Field(description="Only the first this many items of a list can be paged through.")
    limits: dict[str, PageLimit] = Field(description="Default and largest per_page of each paged list.")


class ConfigResponse(BaseModel):
    amounts: AmountsConfig
    currency: CurrencyConfig
    staking: StakingConfig
    swap: SwapConfigResponse
    gas: GasConfig
    contract: ContractConfig
    paging: PagingConfig
    idempotency_key_max_length: int
    max_body_bytes: int = Field(description="The largest request body an endpoint accepts unless it states its own limit.")


_PAGE_LISTS: Final = (
    "balances",
    "stakes",
    "claims",
    "allowance_history",
    "history",
    "executions",
    "pools",
    "positions",
    "tokens",
    "notifications",
    "discord_permissions",
    "contract_versions",
    "contract_storage",
    "backups",
)


def _page_limits() -> dict[str, PageLimit]:
    return {name: PageLimit(default=getattr(paging, f"{name.upper()}_PER_PAGE_DEFAULT"), max=getattr(paging, f"{name.upper()}_PER_PAGE_MAX")) for name in _PAGE_LISTS}


def _percent(ppm: int) -> str:
    whole, fraction = divmod(ppm, _PPM_PER_PERCENT)
    return f"{whole}.{fraction:04d}"


@router.get("/version", response_model=VersionResponse, summary="API name and version")
async def get_version() -> VersionResponse:
    return VersionResponse(name=API_NAME, version=__version__)


@router.get("/config", response_model=ConfigResponse, summary="Service limits and settings")
async def get_config(state: StateDep) -> ConfigResponse:
    runtime = state.runtime_config
    return ConfigResponse(
        amounts=AmountsConfig(decimal_places=DECIMALS, minimal_unit=MINIMAL_UNIT, max_amount=AMOUNT_MAX),
        currency=CurrencyConfig(name_max_length=CURRENCY_NAME_MAX, symbol_max_length=CURRENCY_SYMBOL_MAX, claim_description_max_length=CLAIM_DESCRIPTION_MAX),
        staking=StakingConfig(
            rate_ppm_max=RATE_PPM_MAX,
            rate_percent_per_hour_max=_percent(RATE_PPM_MAX),
            rate_change_timelock_seconds=int(RATE_CHANGE_TIMELOCK.total_seconds()),
            delete_timelock_seconds=int(DELETE_TIMELOCK.total_seconds()),
            delete_expiry_seconds=int(DELETE_EXPIRY.total_seconds()),
        ),
        swap=SwapConfigResponse(
            fee_tiers_bps=list(FEE_TIERS_BPS),
            fee_denominator=FEE_DENOMINATOR,
            minimum_liquidity=MINIMUM_LIQUIDITY,
            chunk_count=state.swap_config.chunk_count,
            max_routes=state.swap_config.max_routes,
        ),
        gas=GasConfig(currency_id=runtime.gas_currency_id, price=runtime.gas_price, default_gas=runtime.default_gas, max_gas=runtime.max_gas),
        contract=ContractConfig(
            max_size=runtime.contract_max_size,
            max_call_depth=runtime.limits.max_contract_depth,
            storage_max_keys=CONTRACT_STORAGE_MAX_KEYS,
            storage_max_bytes=CONTRACT_STORAGE_MAX_BYTES,
            storage_key_max_length=CONTRACT_STORAGE_KEY_MAX,
            storage_value_max_bytes=CONTRACT_STORAGE_VALUE_MAX,
        ),
        paging=PagingConfig(max_window=paging.MAX_WINDOW, limits=_page_limits()),
        idempotency_key_max_length=IDEMPOTENCY_KEY_MAX,
        max_body_bytes=DEFAULT_BODY_LIMIT,
    )
