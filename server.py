import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_serializer
from typing import List, Optional, Literal
from decimal import Decimal
import httpx
import time

import config
from RapidWire import RapidWire, exceptions, structs

API_SERVER_VERSION = "1.0.0"

app = FastAPI(
    title="RapidWire API",
    description="API for interacting with the RapidWire bot features.",
    version=API_SERVER_VERSION
)

Rapid = RapidWire(db_config=config.MySQL.to_dict())
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
    
    key_data = Rapid.APIKeys.get_user_by_key(api_key)
    if not key_data:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid API Key"
        )
    return key_data.user_id

class ConfigResponse(BaseModel):
    decimal_places: int

class BalanceResponse(BaseModel):
    currency: structs.Currency
    amount: str

class TransferRequest(BaseModel):
    destination_id: int = Field(..., description="The Discord user ID of the recipient.")
    symbol: str = Field(..., description="The symbol of the currency to transfer.")
    amount: float = Field(..., gt=0, description="The amount of currency to transfer.")
    input_data: Optional[str] = Field(None, max_length=16, pattern=r"^[a-zA-Z0-9]*$", description="Alphanumeric data for the contract.")

class ClaimCreateRequest(BaseModel):
    payer_id: int = Field(..., description="The Discord user ID of the person to pay the claim.")
    symbol: str = Field(..., description="The symbol of the currency for the claim.")
    amount: float = Field(..., gt=0, description="The amount of currency for the claim.")
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

class UserStatsResponse(BaseModel):
    total_transactions: int
    first_transaction_timestamp: Optional[int] = None
    last_transaction_timestamp: Optional[int] = None

    @field_serializer('total_transactions', 'first_transaction_timestamp', 'last_transaction_timestamp')
    def serialize_integers(self, value: int, _info):
        return str(value) if value is not None else None

class StakeResponse(BaseModel):
    currency: structs.Currency
    stake: structs.Stake

@app.get("/version", response_model=SuccessResponse, tags=["Info"])
async def get_version():
    return SuccessResponse(message="RapidWire API", details={"version": API_SERVER_VERSION})

@app.get("/config", response_model=ConfigResponse, tags=["Config"])
async def get_config():
    return ConfigResponse(decimal_places=config.decimal_places)

@app.get("/user/{user_id}/name", response_model=UserNameResponse, tags=["User"])
async def get_user_name(user_id: int):
    username = await discord_user_cache.get(user_id)
    if username:
        return UserNameResponse(username=username)
    else:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found on Discord.")

@app.get("/user/{user_id}/stats", response_model=UserStatsResponse, tags=["User"])
async def get_user_stats(user_id: int):
    stats = Rapid.Transactions.get_user_stats(user_id)
    return UserStatsResponse(**stats)

@app.get("/balance/{user_id}", response_model=List[BalanceResponse], tags=["Account"])
async def get_my_balance(user_id: int):
    user = Rapid.get_user(user_id)
    balances = user.get_all_balances()
    response = []
    for bal in balances:
        currency = Rapid.Currencies.get(currency_id=bal.currency_id)
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
    stakes = Rapid.Stakes.get_for_user(user_id)
    response = []
    for stake in stakes:
        currency = Rapid.Currencies.get(currency_id=stake.currency_id)
        if currency:
            response.append(
                StakeResponse(
                    currency=currency,
                    stake=stake
                )
            )
    return response

@app.get("/account/history", response_model=List[structs.Transaction], tags=["Account"])
async def get_my_history(user_id: int = Depends(get_current_user_id), page: int = 1):
    return Rapid.Transactions.get_user_history(user_id, page=page)

@app.get("/script/{user_id}", response_model=ContractScriptResponse, tags=["Contract"])
async def get_contract_script(user_id: int):
    contract = Rapid.Contracts.get(user_id)
    if not contract or not contract.script:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contract not found for this user.")
    return ContractScriptResponse(script=contract.script, cost=contract.cost, max_cost=contract.max_cost)

@app.get("/currency/{symbol}", response_model=structs.Currency, tags=["Currency"])
async def get_currency_info(symbol: str):
    currency = Rapid.Currencies.get_by_symbol(symbol.upper())
    if not currency:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Currency not found")
    return currency

@app.get("/currency/id/{currency_id}", response_model=structs.Currency, tags=["Currency"])
async def get_currency_info_by_id(currency_id: int):
    currency = Rapid.Currencies.get(currency_id)
    if not currency:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Currency not found")
    return currency

@app.post("/currency/transfer", response_model=structs.Transaction, tags=["Currency"])
async def transfer_currency(request: TransferRequest, user_id: int = Depends(get_current_user_id)):
    currency = Rapid.Currencies.get_by_symbol(request.symbol.upper())
    if not currency:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Currency not found")

    try:
        int_amount = int(Decimal(str(request.amount)) * (10**config.decimal_places))
        tx, _ = Rapid.transfer(
            source_id=user_id,
            destination_id=request.destination_id,
            currency_id=currency.currency_id,
            amount=int_amount,
            input_data=request.input_data
        )
        return tx
    except exceptions.InsufficientFunds:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient funds")
    except exceptions.TransactionCanceledByContract as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Transaction canceled by contract: {e}")
    except exceptions.TransactionError as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Transaction error: {e}")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

@app.post("/claims/create", response_model=structs.Claim, tags=["Claims"])
async def create_claim(request: ClaimCreateRequest, user_id: int = Depends(get_current_user_id)):
    currency = Rapid.Currencies.get_by_symbol(request.symbol.upper())
    if not currency:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Currency not found")
    
    int_amount = int(Decimal(str(request.amount)) * (10**config.decimal_places))
    claim = Rapid.Claims.create(user_id, request.payer_id, currency.currency_id, int_amount, request.description)
    return claim

@app.get("/claims", response_model=List[structs.Claim], tags=["Claims"])
async def get_claims(user_id: int = Depends(get_current_user_id), page: int = 1):
    return Rapid.Claims.get_for_user(user_id, page=page)

@app.get("/claims/{claim_id}", response_model=structs.Claim, tags=["Claims"])
async def get_claim_details(claim_id: int):
    claim = Rapid.Claims.get(claim_id)
    if not claim:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Claim not found")
    return claim

@app.post("/claims/{claim_id}/pay", response_model=structs.Transaction, tags=["Claims"])
async def pay_claim(claim_id: int, user_id: int = Depends(get_current_user_id)):
    try:
        tx, _ = Rapid.pay_claim(claim_id, user_id)
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
        claim = Rapid.cancel_claim(claim_id, user_id)
        return claim
    except (ValueError, PermissionError) as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=f"Error during cancellation: {e}")

@app.get("/transaction/{transaction_id}", response_model=structs.Transaction, tags=["Transactions"])
async def search_transactions(transaction_id: int):
    if transaction_id <= 0: transaction_id = 0
    tx = Rapid.Transactions.get(transaction_id)
    if not tx:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Transaction not found")
    return tx

@app.get("/transactions/search", response_model=List[structs.Transaction], tags=["Transactions"])
async def search_transactions(
    source_id: Optional[int] = None,
    dest_id: Optional[int] = None,
    user_id: Optional[int] = None,
    currency_symbol: Optional[str] = None,
    start_timestamp: Optional[int] = None,
    end_timestamp: Optional[int] = None,
    min_amount: Optional[float] = None,
    max_amount: Optional[float] = None,
    input_data: Optional[str] = None,
    page: int = 1,
    limit: int = 10,
    sort_by: Literal["transaction_id", "timestamp", "amount"] = "transaction_id",
    sort_order: Literal["ASC", "DESC", "asc", "desc"] = "desc"
):
    search_params = {
        "source_id": source_id,
        "dest_id": dest_id,
        "user_id": user_id,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "input_data": input_data,
        "page": page,
        "limit": limit,
        "sort_by": sort_by,
        "sort_order": sort_order
    }

    if limit >= 20: limit = 20
    if limit <= 0: limit = 10

    if currency_symbol:
        currency = Rapid.Currencies.get_by_symbol(currency_symbol.upper())
        search_params["currency_id"] = currency.currency_id if currency else -1

    if min_amount is not None:
        search_params["min_amount"] = int(Decimal(str(min_amount)) * (10**config.decimal_places))
    if max_amount is not None:
        search_params["max_amount"] = int(Decimal(str(max_amount)) * (10**config.decimal_places))

    return Rapid.Transactions.search(**search_params)

if __name__ == "__main__":
    app.mount("/", StaticFiles(directory="web", html=True), name="web")
    uvicorn.run(
        app,
        host=config.APIServer.host,
        port=config.APIServer.port
    )
