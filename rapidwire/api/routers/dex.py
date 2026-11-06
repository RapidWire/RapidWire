"""DEX endpoints: pools, quotes, swaps and liquidity."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, Path
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, StrictInt, WithJsonSchema
from pydantic_core import PydanticCustomError

from rapidwire.api.auth import WriterDep
from rapidwire.api.deps import EngineDep, SwapConfigDep
from rapidwire.api.formats import Amount, IdStr, IntOut, IntString, NonNegativeAmount, PositiveAmount, PositiveIdStr
from rapidwire.api.paging import Page, PageParams, page_params
from rapidwire.core import pools, swap
from rapidwire.core.errors import PoolNotFound
from rapidwire.core.paging import POOLS_PER_PAGE_DEFAULT, POOLS_PER_PAGE_MAX, POSITIONS_PER_PAGE_DEFAULT, POSITIONS_PER_PAGE_MAX
from rapidwire.core.types import LabelBasis, Origin
from rapidwire.db.tables import FEE_TIERS_BPS
from rapidwire.db.types import AMOUNT_MAX

__all__ = ["router"]

router = APIRouter(prefix="/v1", tags=["DEX"])

_TIERS_TEXT: Final = ", ".join(str(tier) for tier in FEE_TIERS_BPS)


def _check_fee_tier(value: int) -> int:
    if value not in FEE_TIERS_BPS:
        raise PydanticCustomError("fee_tier", f"The fee tier must be one of {_TIERS_TEXT} basis points.")
    return value


# A JSON number that must be one of the fee tiers; strings, floats and booleans are rejected.
FeeTier = Annotated[
    StrictInt,
    AfterValidator(_check_fee_tier),
    WithJsonSchema({"type": "integer", "enum": list(FEE_TIERS_BPS), "description": f"The pool's fee tier in basis points: one of {_TIERS_TEXT}."}),
]
PositiveIntStr = Annotated[int, IntString(1, AMOUNT_MAX, "A positive integer as a decimal string.")]


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreatePoolRequest(_Request):
    currency_x: PositiveIdStr
    currency_y: PositiveIdStr
    fee_bps: FeeTier
    amount_x: PositiveAmount = Field(description="The deposit of currency_x, which sets the pool's initial price together with amount_y.")
    amount_y: PositiveAmount


class AddLiquidityRequest(_Request):
    currency_x: PositiveIdStr
    currency_y: PositiveIdStr
    fee_bps: FeeTier
    amount_x: PositiveAmount = Field(description="The most of currency_x to deposit.")
    amount_y: PositiveAmount = Field(description="The most of currency_y to deposit.")
    amount_x_min: NonNegativeAmount = Field(default=0, description="Fail when the deposit at the pool's price would use less of currency_x; 0 by default.")
    amount_y_min: NonNegativeAmount = Field(default=0, description="Fail when the deposit at the pool's price would use less of currency_y; 0 by default.")
    min_shares: PositiveIntStr = Field(default=1, description="Fail when the deposit would mint fewer shares; 1 by default.")


class RemoveLiquidityRequest(_Request):
    currency_x: PositiveIdStr
    currency_y: PositiveIdStr
    fee_bps: FeeTier
    shares: PositiveIntStr = Field(description="The shares to burn.")
    amount_x_min: NonNegativeAmount = Field(default=0, description="Fail when the payout of currency_x would be smaller; 0 by default.")
    amount_y_min: NonNegativeAmount = Field(default=0, description="Fail when the payout of currency_y would be smaller; 0 by default.")


class QuoteRequest(_Request):
    from_currency: PositiveIdStr
    to_currency: PositiveIdStr
    amount_in: PositiveAmount


class SwapRequest(QuoteRequest):
    min_out: NonNegativeAmount = Field(description="Fail without moving anything when the swap would pay out less; required, 0 accepts any amount.")


class PoolOut(BaseModel):
    pool_id: IntOut
    currency_a: IntOut = Field(description="The lower currency ID of the pair.")
    currency_b: IntOut = Field(description="The higher currency ID of the pair.")
    symbol_a: str = Field(description="The current symbol of currency_a.")
    symbol_b: str
    deleted_a: bool = Field(description="Whether currency_a is deleted now.")
    deleted_b: bool
    fee_bps: int
    active: bool = Field(description="False once the pool was dissolved by a currency deletion.")
    reserve_a: Amount
    reserve_b: Amount
    total_shares: IntOut
    locked_shares: IntOut = Field(description="Shares locked forever at creation; 0 for a dissolved pool.")
    custody_account_id: IntOut = Field(description="The system account holding the reserves.")
    created_by: IntOut
    created_at: datetime
    created_execution_id: IntOut
    created_execution_origin: Origin = Field(description="The origin of the execution that created the pool.")
    created_execution_label_basis: LabelBasis | None = Field(description="The label basis of a legacy creating execution, else null.")
    dissolved_at: datetime | None
    dissolved_execution_id: IntOut | None
    dissolved_execution_origin: Origin | None = Field(description="The origin of the execution that dissolved the pool, null while active.")
    dissolved_execution_label_basis: LabelBasis | None = Field(description="The label basis of a legacy dissolving execution, else null.")


class PoolPairResponse(BaseModel):
    currency_a: IntOut
    currency_b: IntOut
    pools: list[PoolOut] = Field(description="The active pools of the pair, one per fee tier in ascending fee_bps; empty when there is none.")


class PositionOut(BaseModel):
    pool: PoolOut
    account_id: IntOut
    shares: IntOut
    share_percent: str = Field(description="shares / pool.total_shares as a percentage with 2 decimals, rounded half to even.")
    withdrawable_a: Amount = Field(description="What burning all the shares would pay out of currency_a now.")
    withdrawable_b: Amount


class HopOut(BaseModel):
    pool_id: IntOut
    currency_in: IntOut
    currency_out: IntOut
    fee_bps: int
    amount_in: Amount
    amount_out: Amount


class RouteOut(BaseModel):
    pool_ids: list[IntOut]
    currencies: list[IntOut] = Field(description="The currencies along the path, from the source to the target.")
    fee_bps: list[int] = Field(description="The fee tier of each pool of the path.")
    amount_in: Amount
    amount_out: Amount
    hops: list[HopOut]


class QuoteResponse(BaseModel):
    from_currency: IntOut
    to_currency: IntOut
    from_symbol: str
    to_symbol: str
    amount_in: Amount
    amount_out: Amount = Field(description="The total received over all routes.")
    routes: list[RouteOut] = Field(description="The allocation: the share of amount_in sent along each route.")
    truncated: bool = Field(description="True when the path search hit its limit, so a better route may exist.")
    symbols: dict[str, str] = Field(description="The symbol of every currency in the quote, keyed by currency ID.")


class SwapResponse(BaseModel):
    execution_id: IntOut
    from_currency: IntOut
    to_currency: IntOut
    amount_in: Amount
    min_out: Amount
    amount_out: Amount
    routes: list[RouteOut] = Field(description="The executed allocation, computed on the reserves locked by the swap.")
    transfer_ids: list[IntOut]


class LiquidityResponse(BaseModel):
    execution_id: IntOut
    pool_id: IntOut
    currency_a: IntOut
    currency_b: IntOut
    fee_bps: int
    currency_x: IntOut
    currency_y: IntOut
    amount_a: Amount
    amount_b: Amount
    amount_x: Amount
    amount_y: Amount
    shares: IntOut = Field(description="The shares minted, or burned by a withdrawal.")
    provider_shares: IntOut = Field(description="The caller's shares of the pool afterwards.")
    total_shares: IntOut
    reserve_a: Amount
    reserve_b: Amount
    transfer_ids: list[IntOut]


class PoolCreatedResponse(LiquidityResponse):
    locked_shares: IntOut
    custody_account_id: IntOut


PoolIdPath = Annotated[PositiveIdStr, Path(description="The pool ID.")]
CurrencyPath = Annotated[PositiveIdStr, Path(description="A currency ID; the pair may be given in either order.")]
AccountPath = Annotated[IdStr, Path(description="The provider's account ID.")]
PoolPage = Annotated[PageParams, Depends(page_params(POOLS_PER_PAGE_DEFAULT, POOLS_PER_PAGE_MAX))]
PositionPage = Annotated[PageParams, Depends(page_params(POSITIONS_PER_PAGE_DEFAULT, POSITIONS_PER_PAGE_MAX))]


def _pool(info: pools.PoolInfo) -> PoolOut:
    return PoolOut(
        pool_id=info.pool_id,
        currency_a=info.currency_a,
        currency_b=info.currency_b,
        symbol_a=info.symbol_a,
        symbol_b=info.symbol_b,
        deleted_a=info.deleted_a,
        deleted_b=info.deleted_b,
        fee_bps=info.fee_bps,
        active=info.active,
        reserve_a=info.reserve_a,
        reserve_b=info.reserve_b,
        total_shares=info.total_shares,
        locked_shares=info.locked_shares,
        custody_account_id=info.custody_account_id,
        created_by=info.created_by,
        created_at=info.created_at,
        created_execution_id=info.created_execution_id,
        created_execution_origin=info.created_execution_origin,
        created_execution_label_basis=info.created_execution_label_basis,
        dissolved_at=info.dissolved_at,
        dissolved_execution_id=info.dissolved_execution_id,
        dissolved_execution_origin=info.dissolved_execution_origin,
        dissolved_execution_label_basis=info.dissolved_execution_label_basis,
    )


def _position(position: pools.ProviderPosition) -> PositionOut:
    return PositionOut(
        pool=_pool(position.pool),
        account_id=position.account_id,
        shares=position.shares,
        share_percent=position.share_percent,
        withdrawable_a=position.withdrawable_a,
        withdrawable_b=position.withdrawable_b,
    )


def _route(route: swap.SwapRoute) -> RouteOut:
    hops = [HopOut(pool_id=hop.pool_id, currency_in=hop.currency_in, currency_out=hop.currency_out, fee_bps=hop.fee_bps, amount_in=hop.amount_in, amount_out=hop.amount_out) for hop in route.hops]
    return RouteOut(pool_ids=list(route.pool_ids), currencies=list(route.currencies), fee_bps=list(route.fee_bps), amount_in=route.amount_in, amount_out=route.amount_out, hops=hops)


def _liquidity_fields(result: pools.LiquidityChange) -> dict[str, Any]:
    return {"execution_id": result.execution_id, **result.to_output(), "transfer_ids": list(result.transfer_ids)}


@router.get("/pools", response_model=Page[PoolOut], summary="List active pools")
async def list_pools(engine: EngineDep, params: PoolPage) -> Page[PoolOut]:
    page = await pools.list_pools(engine, page=params.page, per_page=params.per_page)
    return Page[PoolOut].build(page, _pool)


@router.get("/pools/pair/{currency_x}/{currency_y}", response_model=PoolPairResponse, summary="Active pools of a currency pair")
async def get_pair_pools(engine: EngineDep, currency_x: CurrencyPath, currency_y: CurrencyPath) -> PoolPairResponse:
    found = await pools.get_pools_for_pair(engine, currency_x, currency_y)
    return PoolPairResponse(currency_a=min(currency_x, currency_y), currency_b=max(currency_x, currency_y), pools=[_pool(info) for info in found])


@router.get("/pools/{pool_id}", response_model=PoolOut, summary="One pool, active or dissolved")
async def get_pool(engine: EngineDep, pool_id: PoolIdPath) -> PoolOut:
    info = await pools.get_pool(engine, pool_id)
    if info is None:
        raise PoolNotFound(f"Pool {pool_id} was not found.")
    return _pool(info)


@router.get("/accounts/{account_id}/pools", response_model=Page[PositionOut], summary="An account's liquidity positions")
async def list_positions(engine: EngineDep, account_id: AccountPath, params: PositionPage) -> Page[PositionOut]:
    page = await pools.list_provider_positions(engine, account_id, page=params.page, per_page=params.per_page)
    return Page[PositionOut].build(page, _position)


@router.post("/swap/quote", response_model=QuoteResponse, summary="Quote a swap")
async def quote(body: QuoteRequest, engine: EngineDep, config: SwapConfigDep) -> QuoteResponse:
    result = await swap.quote_swap(engine, from_currency=body.from_currency, to_currency=body.to_currency, amount_in=body.amount_in, config=config)
    return QuoteResponse(
        from_currency=result.from_currency,
        to_currency=result.to_currency,
        from_symbol=result.from_symbol,
        to_symbol=result.to_symbol,
        amount_in=result.amount_in,
        amount_out=result.amount_out,
        routes=[_route(route) for route in result.routes],
        truncated=result.truncated,
        symbols={str(currency_id): symbol for currency_id, symbol in result.symbols.items()},
    )


@router.post("/swap", response_model=SwapResponse, summary="Swap currencies")
async def do_swap(body: SwapRequest, writer: WriterDep, engine: EngineDep, config: SwapConfigDep) -> SwapResponse:
    result = await swap.swap(
        engine,
        user_id=writer.account_id,
        from_currency=body.from_currency,
        to_currency=body.to_currency,
        amount_in=body.amount_in,
        min_out=body.min_out,
        config=config,
        idempotency_key=writer.idempotency_key,
    )
    writer.done(result)
    return SwapResponse(
        execution_id=result.execution_id,
        from_currency=result.from_currency,
        to_currency=result.to_currency,
        amount_in=result.amount_in,
        min_out=result.min_out,
        amount_out=result.amount_out,
        routes=[_route(route) for route in result.routes],
        transfer_ids=list(result.transfer_ids),
    )


@router.post("/pools", response_model=PoolCreatedResponse, summary="Create a pool")
async def create_pool(body: CreatePoolRequest, writer: WriterDep, engine: EngineDep) -> PoolCreatedResponse:
    result = await pools.create_pool(
        engine,
        user_id=writer.account_id,
        currency_x=body.currency_x,
        currency_y=body.currency_y,
        fee_bps=body.fee_bps,
        amount_x=body.amount_x,
        amount_y=body.amount_y,
        idempotency_key=writer.idempotency_key,
    )
    return PoolCreatedResponse(**_liquidity_fields(writer.done(result)))


@router.post("/pools/liquidity/add", response_model=LiquidityResponse, summary="Add liquidity to a pool")
async def add_liquidity(body: AddLiquidityRequest, writer: WriterDep, engine: EngineDep) -> LiquidityResponse:
    result = await pools.add_liquidity(
        engine,
        user_id=writer.account_id,
        currency_x=body.currency_x,
        currency_y=body.currency_y,
        fee_bps=body.fee_bps,
        amount_x_desired=body.amount_x,
        amount_y_desired=body.amount_y,
        amount_x_min=body.amount_x_min,
        amount_y_min=body.amount_y_min,
        min_shares=body.min_shares,
        idempotency_key=writer.idempotency_key,
    )
    return LiquidityResponse(**_liquidity_fields(writer.done(result)))


@router.post("/pools/liquidity/remove", response_model=LiquidityResponse, summary="Remove liquidity from a pool")
async def remove_liquidity(body: RemoveLiquidityRequest, writer: WriterDep, engine: EngineDep) -> LiquidityResponse:
    result = await pools.remove_liquidity(
        engine,
        user_id=writer.account_id,
        currency_x=body.currency_x,
        currency_y=body.currency_y,
        fee_bps=body.fee_bps,
        shares=body.shares,
        amount_x_min=body.amount_x_min,
        amount_y_min=body.amount_y_min,
        idempotency_key=writer.idempotency_key,
    )
    return LiquidityResponse(**_liquidity_fields(writer.done(result)))
