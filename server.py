import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_serializer
from typing import List, Optional, Literal
from decimal import Decimal
import httpx
import time
from contextlib import asynccontextmanager

import config
from RapidWire import RapidWire, exceptions, structs

API_SERVER_VERSION = "1.0.1"

Rapid = RapidWire(db_config=config.MySQL.to_dict())
Rapid.Config = config.RapidWireConfig

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await Rapid.initialize()
    yield
    # Shutdown
    await Rapid.close()

app = FastAPI(
    title="RapidWire API",
    description="API for interacting with the RapidWire bot features.",
    version=API_SERVER_VERSION,
    lifespan=lifespan
)

API_KEY_HEADER = APIKeyHeader(name="API-Key", auto_error=False)

class DiscordUserCache:
    def __init__(self, capacity=100, ttl_seconds=86400):
        self.cache:dict[int, tuple[str, int]] = {}
        self.id_order:list[int] = []
        self.capacity = capacity
        self.ttl = ttl_seconds
        self.httpx_client = httpx.AsyncClient(headers={"Authorization": f"Bot {config.Discord.token}"})

    async def _get_discord_user_name(self, user_id: int) -> Optional[str]:
        url = f"https://discord.com/api/v10/users/{user_id}"
        headers = {"Authorization": f"Bot {config.Discord.token}"}

        response = await self.httpx_client.get(url, headers=headers)
        if response.status_code == 200:
            user_data = response.json()
            return user_data.get("username")
        return None 

    async def get(self, user_id: int) -> Optional[str]:
        if user_id not in self.id_order:
            return await self.set(user_id)
        else:
            username, timestamp = self.cache[user_id]
            if time.time() - timestamp > self.ttl:
                new_username = await self.set(user_id)
                return new_username if new_username else username
            return username

    async def set(self, user_id: int) -> Optional[str]:
        if len(self.cache) >= self.capacity:
            del self.cache[self.id_order.pop()]
        discord_user = await self._get_discord_user_name(user_id)
        if discord_user is None:
            return None
        self.cache[user_id] = (discord_user, int(time.time()))
        try:
            self.id_order.remove(user_id)
        except:
            pass
        self.id_order.insert(0, user_id)
        return discord_user

discord_user_cache = DiscordUserCache()

async def get_current_user_id(api_key: str = Security(API_KEY_HEADER)) -> int:
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="API-Key header is missing"
        )
    
    key_data = await Rapid.APIKeys.get_user_by_key(api_key)
    if not key_data:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API Key"
        )
    return key_data.user_id

class ConfigResponseContract(BaseModel):
    max_cost: int
    max_script_length: int
    max_script_size: int
    max_recursion_depth: int

class ConfigResponseStaking(BaseModel):
    rate_change_timelock: int

class ConfigResponseSwap(BaseModel):
    fee: int

class ConfigResponseGas(BaseModel):
    currency_id: int
    price: int

class ConfigResponse(BaseModel):
    contract: ConfigResponseContract
    staking: ConfigResponseStaking
    swap: ConfigResponseSwap
    gas: ConfigResponseGas
    decimal_places: int

class BalanceResponse(BaseModel):
    currency: structs.Currency
    amount: str

class TransferRequest(BaseModel):
    destination_id: int = Field(..., description="The Discord user ID of the recipient.")
    currency_id: int = Field(..., description="The ID of the currency to transfer.")
    amount: int = Field(..., gt=0, description="The amount of currency to transfer.")

class TransferFromRequest(BaseModel):
    source_id: int = Field(..., description="The Discord user ID of the source.")
    destination_id: int = Field(..., description="The Discord user ID of the recipient.")
    currency_id: int = Field(..., description="The ID of the currency to transfer.")
    amount: int = Field(..., gt=0, description="The amount of currency to transfer.")

class ContractExecutionRequest(BaseModel):
    contract_owner_id: int = Field(..., description="The Discord user ID of the contract owner.")
    input_data: Optional[str] = Field(None, max_length=127, description="Alphanumeric data for the contract.")

class TransferResponse(BaseModel):
    transfer: Optional[structs.Transfer] = None
    execution_id: Optional[int] = None

class TransferFromResponse(BaseModel):
    transfer: structs.Transfer
    execution_id: int

class ContractExecutionResponse(BaseModel):
    execution_id: int
    output_data: Optional[str]

class ClaimCreateRequest(BaseModel):
    payer_id: int = Field(..., description="The Discord user ID of the person to pay the claim.")
    currency_id: int = Field(..., description="The ID of the currency for the claim.")
    amount: int = Field(..., gt=0, description="The amount of currency for the claim.")
    description: Optional[str] = Field(None, max_length=100, description="Description of the claim.")

class SuccessResponse(BaseModel):
    message: str
    details: Optional[dict] = None

class UserNameResponse(BaseModel):
    username: str

class ContractScriptResponse(BaseModel):
    script: Optional[str] = None
    cost: Optional[int] = None
    max_cost: Optional[int] = None
    locked_until: Optional[int] = None

class UserStatsResponse(BaseModel):
    total_transfers: int
    first_transfer_timestamp: Optional[int] = None
    last_transfer_timestamp: Optional[int] = None

    @field_serializer('total_transfers', 'first_transfer_timestamp', 'last_transfer_timestamp')
    def serialize_integers(self, value: int, _info):
        return str(value) if value is not None else None

class StakeResponse(BaseModel):
    currency: structs.Currency
    stake: structs.Stake

class AddLiquidityRequest(BaseModel):
    currency_a_id: int
    currency_b_id: int
    amount_a: int = Field(..., gt=0)
    amount_b: int = Field(..., gt=0)

class RemoveLiquidityRequest(BaseModel):
    currency_a_id: int
    currency_b_id: int
    shares: int = Field(..., gt=0)

class SwapRequest(BaseModel):
    currency_from_id: int
    currency_to_id: int
    amount: int = Field(..., gt=0)

class AddLiquidityResponse(BaseModel):
    shares_minted: str

class RemoveLiquidityResponse(BaseModel):
    amount_a_received: str
    amount_b_received: str

class SwapRateResponse(BaseModel):
    amount_out: str

class SwapResponse(BaseModel):
    amount_out: str
    currency_out_id: int
    execution_id: int

class RouteResponse(BaseModel):
    route: List[structs.LiquidityPool]

class ContractUpdateRequest(BaseModel):
    script: str
    max_cost: Optional[int] = None
    lock_hours: Optional[int] = None

class ContractUpdateResponse(BaseModel):
    contract: structs.Contract

class ApproveRequest(BaseModel):
    spender_id: int
    currency_id: int
    amount: int = Field(..., ge=0)

@app.get("/version", response_model=SuccessResponse, tags=["Info"])
async def get_version():
    return SuccessResponse(message="RapidWire API", details={"version": API_SERVER_VERSION})

@app.get("/config", response_model=ConfigResponse, tags=["Config"])
async def get_config():
    return ConfigResponse(
        contract=ConfigResponseContract(
            max_cost=Rapid.Config.Contract.max_cost,
            max_script_length=Rapid.Config.Contract.max_script_length,
            max_script_size=Rapid.Config.Contract.max_script_size,
            max_recursion_depth=Rapid.Config.Contract.max_recursion_depth
        ),
        staking=ConfigResponseStaking(
            rate_change_timelock=Rapid.Config.Staking.rate_change_timelock
        ),
        swap=ConfigResponseSwap(
            fee=Rapid.Config.Swap.fee
        ),
        gas=ConfigResponseGas(
            currency_id=Rapid.Config.Gas.currency_id,
            price=Rapid.Config.Gas.price
        ),
        decimal_places=Rapid.Config.decimal_places
    )

@app.get("/user/{user_id}/name", response_model=UserNameResponse, tags=["User"])
async def get_user_name(user_id: int):
    username = await discord_user_cache.get(user_id)
    if username:
        return UserNameResponse(username=username)
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found on Discord.")

@app.get("/user/{user_id}/stats", response_model=UserStatsResponse, tags=["User"])
async def get_user_stats(user_id: int):
    stats = await Rapid.Transfers.get_user_stats(user_id)
    return UserStatsResponse(**stats)

@app.get("/balance/{user_id}", response_model=List[BalanceResponse], tags=["Account"])
async def get_my_balance(user_id: int):
    user = Rapid.get_user(user_id)
    balances = await user.get_all_balances()
    response = []
    for bal in balances:
        currency = await Rapid.Currencies.get(currency_id=bal.currency_id)
        if currency:
            response.append(
                BalanceResponse(
                    currency=currency,
                    amount=str(bal.amount)
                )
            )
    return response

@app.get("/stakes/{user_id}", response_model=List[StakeResponse], tags=["Staking"])
async def get_user_stakes(user_id: int):
    stakes = await Rapid.Stakes.get_for_user(user_id)
    response = []
    for stake in stakes:
        currency = await Rapid.Currencies.get(currency_id=stake.currency_id)
        if currency:
            response.append(
                StakeResponse(
                    currency=currency,
                    stake=stake
                )
            )
    return response

@app.get("/account/history", response_model=List[structs.Transfer], tags=["Account"])
async def get_my_history(user_id: int = Depends(get_current_user_id), page: int = 1):
    return await Rapid.search_transfers(user_id=user_id, page=page)

@app.get("/script/{user_id}", response_model=ContractScriptResponse, tags=["Contract"])
async def get_contract_script(user_id: int):
    contract = await Rapid.Contracts.get(user_id)
    if not contract or not contract.script:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contract not found for this user.")
    return ContractScriptResponse(script=contract.script, cost=contract.cost, max_cost=contract.max_cost, locked_until=contract.locked_until)

@app.post("/contract/execute", response_model=ContractExecutionResponse, tags=["Contract"])
async def execute_contract(request: ContractExecutionRequest, user_id: int = Depends(get_current_user_id)):
    try:
        execution_id, output_data = await Rapid.execute_contract(
            caller_id=user_id,
            contract_owner_id=request.contract_owner_id,
            input_data=request.input_data
        )
        return ContractExecutionResponse(execution_id=execution_id, output_data=output_data)
    except exceptions.ContractError as e:
        error_msg = str(e)
        if "Contract not found" in error_msg:
             raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=error_msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Contract execution error: {e}")
    except exceptions.TransactionCanceledByContract as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Transaction canceled by contract: {e}")
    except exceptions.TransactionError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Transaction error: {e}")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@app.get("/contract/variables/{user_id}", response_model=List[structs.ContractVariable], tags=["Contract"])
async def get_contract_variables(user_id: int):
    return await Rapid.ContractVariables.get_all_for_user(user_id)

@app.get("/contract/variable/{user_id}/{key}", response_model=structs.ContractVariable, tags=["Contract"])
async def get_contract_variable(user_id: int, key: str):
    variable = await Rapid.ContractVariables.get(user_id, key)
    if not variable:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Variable not found")
    return variable

@app.get("/contract/history/{user_id}", response_model=List[structs.ContractHistory], tags=["Contract"])
async def get_contract_history(user_id: int):
    return await Rapid.ContractHistories.get_for_user(user_id)

@app.get("/executions/{execution_id}", response_model=structs.Execution, tags=["Contract"])
async def get_execution(execution_id: int):
    execution = await Rapid.Executions.get(execution_id)
    if not execution:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Execution not found")
    return execution

@app.get("/currency/{currency_id}", response_model=structs.Currency, tags=["Currency"])
async def get_currency_info_by_id(currency_id: int):
    currency = await Rapid.Currencies.get(currency_id)
    if not currency:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Currency not found")
    return currency

@app.get("/currency/symbol/{symbol}", response_model=structs.Currency, tags=["Currency"])
async def get_currency_info(symbol: str):
    currency = await Rapid.Currencies.get_by_symbol(symbol.upper())
    if not currency:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Currency not found")
    return currency

@app.post("/currency/transfer", response_model=TransferResponse, tags=["Currency"])
async def transfer_currency(request: TransferRequest, user_id: int = Depends(get_current_user_id)):
    currency = await Rapid.Currencies.get(request.currency_id)
    if not currency:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Currency not found")

    try:
        tx = await Rapid.transfer(
            source_id=user_id,
            destination_id=request.destination_id,
            currency_id=currency.currency_id,
            amount=request.amount
        )
        return TransferResponse(transfer=tx)
    except exceptions.InsufficientFunds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient funds")
    except exceptions.TransactionError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Transaction error: {e}")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@app.post("/currency/transfer_from", response_model=TransferFromResponse, tags=["Currency"])
async def transfer_from_currency(request: TransferFromRequest, user_id: int = Depends(get_current_user_id)):
    currency = await Rapid.Currencies.get(request.currency_id)
    if not currency:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Currency not found")

    try:
        execution_id, transfer = await Rapid.execute_transfer_from(
            caller_id=user_id,
            source_id=request.source_id,
            destination_id=request.destination_id,
            currency_id=currency.currency_id,
            amount=request.amount
        )
        return TransferFromResponse(transfer=transfer, execution_id=execution_id)
    except exceptions.InsufficientFunds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient funds or allowance")
    except exceptions.TransactionError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Transaction error: {e}")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@app.post("/currency/approve", response_model=SuccessResponse, tags=["Currency"])
async def approve_allowance(request: ApproveRequest, user_id: int = Depends(get_current_user_id)):
    currency = await Rapid.Currencies.get(request.currency_id)
    if not currency:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Currency not found")

    try:
        await Rapid.approve(user_id, request.spender_id, currency.currency_id, request.amount)
        return SuccessResponse(message="Allowance updated successfully.")
    except exceptions.TransactionError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Transaction error: {e}")

@app.get("/currency/allowance/{owner_id}/{spender_id}/{currency_id}", response_model=structs.Allowance, tags=["Currency"])
async def get_allowance(owner_id: int, spender_id: int, currency_id: int):
    allowance = await Rapid.Allowances.get(owner_id, spender_id, currency_id)
    if not allowance:
        # Return zero allowance effectively
        return structs.Allowance(owner_id=owner_id, spender_id=spender_id, currency_id=currency_id, amount=0, last_updated_at=0)
    return allowance

@app.post("/claims/create", response_model=structs.Claim, tags=["Claims"])
async def create_claim(request: ClaimCreateRequest, user_id: int = Depends(get_current_user_id)):
    currency = await Rapid.Currencies.get(request.currency_id)
    if not currency:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Currency not found")
    
    claim = await Rapid.Claims.create(user_id, request.payer_id, currency.currency_id, request.amount, request.description)
    return claim

@app.get("/claims", response_model=List[structs.Claim], tags=["Claims"])
async def get_claims(user_id: int = Depends(get_current_user_id), page: int = 1):
    return await Rapid.Claims.get_for_user(user_id, page=page)

@app.get("/claims/{claim_id}", response_model=structs.Claim, tags=["Claims"])
async def get_claim_details(claim_id: int):
    claim = await Rapid.Claims.get(claim_id)
    if not claim:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    return claim

@app.post("/claims/{claim_id}/pay", response_model=structs.Transfer, tags=["Claims"])
async def pay_claim(claim_id: int, user_id: int = Depends(get_current_user_id)):
    try:
        tx = await Rapid.pay_claim(claim_id, user_id)
        return tx
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except exceptions.InsufficientFunds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient funds")
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error during payment: {e}")

@app.post("/claims/{claim_id}/cancel", response_model=structs.Claim, tags=["Claims"])
async def cancel_claim(claim_id: int, user_id: int = Depends(get_current_user_id)):
    try:
        claim = await Rapid.cancel_claim(claim_id, user_id)
        return claim
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error during cancellation: {e}")

@app.get("/transfers/search", response_model=List[structs.Transfer], tags=["Transfers"])
async def search_transfers(
    source_id: Optional[int] = None,
    dest_id: Optional[int] = None,
    user_id: Optional[int] = None,
    currency_id: Optional[int] = None,
    start_timestamp: Optional[int] = None,
    end_timestamp: Optional[int] = None,
    min_amount: Optional[int] = None,
    max_amount: Optional[int] = None,
    input_data: Optional[str] = None,
    page: int = 1,
    limit: int = 10,
    sort_by: Literal["transfer_id", "timestamp", "amount"] = "transfer_id",
    sort_order: Literal["ASC", "DESC", "asc", "desc"] = "desc"
):
    if limit >= 20: limit = 20
    if limit <= 0: limit = 10

    search_params = {
        "source_id": source_id,
        "dest_id": dest_id,
        "user_id": user_id,
        "currency_id": currency_id,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "input_data": input_data,
        "page": page,
        "limit": limit,
        "sort_by": sort_by,
        "sort_order": sort_order,
        "min_amount": min_amount,
        "max_amount": max_amount
    }

    return await Rapid.search_transfers(**search_params)

@app.get("/transfer/{transfer_id}", response_model=structs.Transfer, tags=["Transfers"])
async def get_transfer(transfer_id: int):
    tx = await Rapid.Transfers.get(transfer_id)
    if not tx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transfer not found")
    return tx

@app.post("/pools/add_liquidity", response_model=AddLiquidityResponse, tags=["DEX"])
async def add_liquidity(request: AddLiquidityRequest, user_id: int = Depends(get_current_user_id)):
    currency_a = await Rapid.Currencies.get(request.currency_a_id)
    currency_b = await Rapid.Currencies.get(request.currency_b_id)
    if not currency_a or not currency_b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more currencies not found")

    try:
        shares = await Rapid.add_liquidity(currency_a.currency_id, currency_b.currency_id, request.amount_a, request.amount_b, user_id)
        return AddLiquidityResponse(shares_minted=str(shares))
    except (exceptions.InsufficientFunds, ValueError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except exceptions.TransactionError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.post("/pools/remove_liquidity", response_model=RemoveLiquidityResponse, tags=["DEX"])
async def remove_liquidity(request: RemoveLiquidityRequest, user_id: int = Depends(get_current_user_id)):
    currency_a = await Rapid.Currencies.get(request.currency_a_id)
    currency_b = await Rapid.Currencies.get(request.currency_b_id)
    if not currency_a or not currency_b:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more currencies not found")

    try:
        amount_a, amount_b = await Rapid.remove_liquidity(currency_a.currency_id, currency_b.currency_id, request.shares, user_id)
        return RemoveLiquidityResponse(amount_a_received=str(amount_a), amount_b_received=str(amount_b))
    except (exceptions.InsufficientFunds, ValueError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except exceptions.TransactionError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.get("/pools", response_model=List[structs.LiquidityPool], tags=["DEX"])
async def get_all_pools():
    return await Rapid.LiquidityPools.get_all()

@app.get("/pools/{currency_a_id}/{currency_b_id}", response_model=structs.LiquidityPool, tags=["DEX"])
async def get_pool(currency_a_id: int, currency_b_id: int):
    pool = await Rapid.LiquidityPools.get_by_currency_pair(currency_a_id, currency_b_id)
    if not pool:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Liquidity pool not found")
    return pool

@app.get("/pools/provider/{user_id}", response_model=List[structs.LiquidityProvider], tags=["DEX"])
async def get_provider_info(user_id: int):
    return await Rapid.LiquidityProviders.get_for_user(user_id)

@app.post("/swap/rate", response_model=SwapRateResponse, tags=["DEX"])
async def get_swap_rate(request: SwapRequest):
    currency_from = await Rapid.Currencies.get(request.currency_from_id)
    currency_to = await Rapid.Currencies.get(request.currency_to_id)
    if not currency_from or not currency_to:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more currencies not found")

    try:
        route = await Rapid.find_swap_route(currency_from.currency_id, currency_to.currency_id)
        amount_out = Rapid.get_swap_rate(request.amount, route, request.currency_from_id)
        return SwapRateResponse(amount_out=str(amount_out))
    except (ValueError, exceptions.CurrencyNotFound) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@app.post("/swap", response_model=SwapResponse, tags=["DEX"])
async def execute_swap(request: SwapRequest, user_id: int = Depends(get_current_user_id)):
    currency_from = await Rapid.Currencies.get(request.currency_from_id)
    currency_to = await Rapid.Currencies.get(request.currency_to_id)
    if not currency_from or not currency_to:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more currencies not found")

    try:
        execution_id, amount_out, currency_out_id = await Rapid.execute_swap(user_id, currency_from.currency_id, currency_to.currency_id, request.amount)
        currency_out = await Rapid.Currencies.get(currency_out_id)
        return SwapResponse(amount_out=str(amount_out), currency_out_id=currency_out.currency_id, execution_id=execution_id)
    except (exceptions.InsufficientFunds, ValueError, exceptions.CurrencyNotFound) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except exceptions.TransactionError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

@app.get("/swap/route/{currency_from_id}/{currency_to_id}", response_model=RouteResponse, tags=["DEX"])
async def get_swap_route(currency_from_id: int, currency_to_id: int):
    currency_from = await Rapid.Currencies.get(currency_from_id)
    currency_to = await Rapid.Currencies.get(currency_to_id)
    if not currency_from or not currency_to:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more currencies not found")

    try:
        route = await Rapid.find_swap_route(currency_from.currency_id, currency_to.currency_id)
        return RouteResponse(route=route)
    except (ValueError, exceptions.CurrencyNotFound) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@app.post("/contract/update", response_model=ContractUpdateResponse, tags=["Contract"])
async def update_contract(request: ContractUpdateRequest, user_id: int = Depends(get_current_user_id)):
    try:
        contract = await Rapid.set_contract(
            user_id=user_id,
            script=request.script,
            max_cost=request.max_cost,
            lock_hours=request.lock_hours
        )
        return ContractUpdateResponse(contract=contract)
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except exceptions.TransactionError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=str(e))

app.mount("/", StaticFiles(directory="web", html=True), name="web")

if __name__ == "__main__":
    uvicorn.run(
        app,
        host=config.APIServer.host,
        port=config.APIServer.port
    )
