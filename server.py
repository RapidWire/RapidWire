import uvicorn
from fastapi import FastAPI, Depends, HTTPException, Security, status
from fastapi.security.api_key import APIKeyHeader
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import List, Optional
from decimal import Decimal

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

@app.get("/version", response_model=SuccessResponse, tags=["Info"])
async def get_version():
    return SuccessResponse(message="RapidWire API", details={"version": API_SERVER_VERSION})

@app.get("/config", response_model=ConfigResponse, tags=["Config"])
async def get_config():
    return ConfigResponse(decimal_places=config.decimal_places)

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

@app.get("/account/history", response_model=List[structs.Transaction], tags=["Account"])
async def get_my_history(user_id: int = Depends(get_current_user_id), page: int = 1):
    return Rapid.Transactions.get_user_history(user_id, page=page)

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
    limit: int = 10
):
    search_params = {
        "source_id": source_id,
        "dest_id": dest_id,
        "user_id": user_id,
        "start_timestamp": start_timestamp,
        "end_timestamp": end_timestamp,
        "input_data": input_data,
        "page": page,
        "limit": limit
    }

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
