from typing import List, Optional, Literal, Dict, Union
from pydantic import BaseModel, Field, field_serializer
import httpx

# --- Structs (from RapidWire/structs.py) ---

class Currency(BaseModel):
    currency_id: int
    name: str
    symbol: str
    issuer_id: int = Field(alias="issuer")
    supply: int
    minting_renounced: bool
    delete_requested_at: Optional[int] = None
    hourly_interest_rate: int
    new_hourly_interest_rate: Optional[int] = None
    rate_change_requested_at: Optional[int] = None

class Balance(BaseModel):
    user_id: int
    currency_id: int
    amount: int

class Contract(BaseModel):
    user_id: int
    script: str
    cost: int
    max_cost: int
    locked_until: int

class Claim(BaseModel):
    claim_id: int
    claimant_id: int
    payer_id: int
    currency_id: int
    amount: int
    status: Literal['pending', 'paid', 'canceled']
    created_at: int
    description: Optional[str] = None

class Stake(BaseModel):
    user_id: int
    currency_id: int
    amount: int
    last_updated_at: int

class Execution(BaseModel):
    execution_id: int
    caller_id: int
    contract_owner_id: int
    input_data: Optional[str]
    output_data: Optional[str]
    cost: int
    status: Literal['pending', 'success', 'failed', 'reverted']
    timestamp: int

class Transfer(BaseModel):
    transfer_id: int
    execution_id: Optional[int]
    source_id: int
    dest_id: int
    currency_id: int
    amount: int
    timestamp: int

class ContractHistory(BaseModel):
    history_id: int
    execution_id: int
    user_id: int
    script_hash: bytes
    cost: int
    created_at: int
    
    @field_serializer('script_hash')
    def serialize_bytes(self, value: bytes, _info):
        return value.hex()

class Allowance(BaseModel):
    owner_id: int
    spender_id: int
    currency_id: int
    amount: int
    last_updated_at: int

class LiquidityPool(BaseModel):
    pool_id: int
    currency_a_id: int
    currency_b_id: int
    reserve_a: int
    reserve_b: int
    total_shares: int

class LiquidityProvider(BaseModel):
    provider_id: int
    pool_id: int
    user_id: int
    shares: int

class ContractVariable(BaseModel):
    user_id: int
    key: str
    value: Union[int, str]

# --- Request/Response Models (from server.py) ---

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
    currency: Currency
    amount: str

class TransferRequest(BaseModel):
    destination_id: int = Field(..., description="The Discord user ID of the recipient.")
    currency_id: int = Field(..., description="The ID of the currency to transfer.")
    amount: int = Field(..., gt=0, description="The amount of currency to transfer.")

class TransferFromResponse(BaseModel):
    transfer: Transfer
    execution_id: int

class ContractExecutionRequest(BaseModel):
    contract_owner_id: int = Field(..., description="The Discord user ID of the contract owner.")
    input_data: Optional[str] = Field(None, max_length=127, description="Alphanumeric data for the contract.")

class TransferFromRequest(BaseModel):
    source_id: int = Field(..., description="The Discord user ID of the source.")
    destination_id: int = Field(..., description="The Discord user ID of the recipient.")
    currency_id: int = Field(..., description="The ID of the currency to transfer.")
    amount: int = Field(..., gt=0, description="The amount of currency to transfer.")

class TransferResponse(BaseModel):
    transfer: Optional[Transfer] = None
    execution_id: Optional[int] = None

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

class StakeResponse(BaseModel):
    currency: Currency
    stake: Stake

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
    currency_out_id: str
    execution_id: int

class RouteResponse(BaseModel):
    route: List[LiquidityPool]

class ContractUpdateRequest(BaseModel):
    script: str
    max_cost: Optional[int] = None
    lock_hours: Optional[int] = None

class ContractUpdateResponse(BaseModel):
    contract: Contract

class ApproveRequest(BaseModel):
    spender_id: int
    currency_id: int
    amount: int = Field(..., ge=0)

# --- Client ---

class RapidWireClient:
    def __init__(self, base_url: str, api_key: str):
        self.base_url = base_url.rstrip("/")
        self.headers = {"API-Key": api_key}
        self.client = httpx.Client(headers=self.headers, base_url=self.base_url, timeout=30.0)

    def get_version(self) -> SuccessResponse:
        resp = self.client.get("/version")
        resp.raise_for_status()
        return SuccessResponse(**resp.json())

    def get_config(self) -> ConfigResponse:
        resp = self.client.get("/config")
        resp.raise_for_status()
        return ConfigResponse(**resp.json())

    def get_user_name(self, user_id: int) -> UserNameResponse:
        resp = self.client.get(f"/user/{user_id}/name")
        resp.raise_for_status()
        return UserNameResponse(**resp.json())

    def get_user_stats(self, user_id: int) -> UserStatsResponse:
        resp = self.client.get(f"/user/{user_id}/stats")
        resp.raise_for_status()
        return UserStatsResponse(**resp.json())

    def get_balance(self, user_id: int) -> List[BalanceResponse]:
        resp = self.client.get(f"/balance/{user_id}")
        resp.raise_for_status()
        return [BalanceResponse(**item) for item in resp.json()]

    def get_stakes(self, user_id: int) -> List[StakeResponse]:
        resp = self.client.get(f"/stakes/{user_id}")
        resp.raise_for_status()
        return [StakeResponse(**item) for item in resp.json()]

    def get_account_history(self, page: int = 1) -> List[Transfer]:
        resp = self.client.get("/account/history", params={"page": page})
        resp.raise_for_status()
        return [Transfer(**item) for item in resp.json()]

    def get_contract_script(self, user_id: int) -> ContractScriptResponse:
        resp = self.client.get(f"/script/{user_id}")
        resp.raise_for_status()
        return ContractScriptResponse(**resp.json())

    def execute_contract(self, contract_owner_id: int, input_data: Optional[str] = None) -> ContractExecutionResponse:
        request = ContractExecutionRequest(contract_owner_id=contract_owner_id, input_data=input_data)
        resp = self.client.post("/contract/execute", json=request.model_dump())
        resp.raise_for_status()
        return ContractExecutionResponse(**resp.json())

    def get_contract_variables(self, user_id: int) -> List[ContractVariable]:
        resp = self.client.get(f"/contract/variables/{user_id}")
        resp.raise_for_status()
        return [ContractVariable(**item) for item in resp.json()]

    def get_contract_variable(self, user_id: int, key: str) -> ContractVariable:
        resp = self.client.get(f"/contract/variable/{user_id}/{key}")
        resp.raise_for_status()
        return ContractVariable(**resp.json())

    def get_contract_history(self, user_id: int) -> List[ContractHistory]:
        resp = self.client.get(f"/contract/history/{user_id}")
        resp.raise_for_status()
        return [ContractHistory(**item) for item in resp.json()]

    def get_execution(self, execution_id: int) -> Execution:
        resp = self.client.get(f"/executions/{execution_id}")
        resp.raise_for_status()
        return Execution(**resp.json())

    def get_currency(self, currency_id: int) -> Currency:
        resp = self.client.get(f"/currency/{currency_id}")
        resp.raise_for_status()
        return Currency(**resp.json())

    def get_currency_by_symbol(self, symbol: str) -> Currency:
        resp = self.client.get(f"/currency/symbol/{symbol}")
        resp.raise_for_status()
        return Currency(**resp.json())

    def transfer_currency(self, destination_id: int, currency_id: int, amount: int) -> TransferResponse:
        request = TransferRequest(
            destination_id=destination_id,
            currency_id=currency_id,
            amount=amount
        )
        resp = self.client.post("/currency/transfer", json=request.model_dump())
        resp.raise_for_status()
        return TransferResponse(**resp.json())

    def transfer_from_currency(self, source_id: int, destination_id: int, currency_id: int, amount: int) -> TransferFromResponse:
        request = TransferFromRequest(
            source_id=source_id,
            destination_id=destination_id,
            currency_id=currency_id,
            amount=amount
        )
        resp = self.client.post("/currency/transfer_from", json=request.model_dump())
        resp.raise_for_status()
        return TransferFromResponse(**resp.json())

    def approve_allowance(self, spender_id: int, currency_id: int, amount: int) -> SuccessResponse:
        request = ApproveRequest(spender_id=spender_id, currency_id=currency_id, amount=amount)
        resp = self.client.post("/currency/approve", json=request.model_dump())
        resp.raise_for_status()
        return SuccessResponse(**resp.json())

    def get_allowance(self, owner_id: int, spender_id: int, currency_id: int) -> Allowance:
        resp = self.client.get(f"/currency/allowance/{owner_id}/{spender_id}/{currency_id}")
        resp.raise_for_status()
        return Allowance(**resp.json())

    def create_claim(self, payer_id: int, currency_id: int, amount: int, description: Optional[str] = None) -> Claim:
        request = ClaimCreateRequest(
            payer_id=payer_id,
            currency_id=currency_id,
            amount=amount,
            description=description
        )
        resp = self.client.post("/claims/create", json=request.model_dump())
        resp.raise_for_status()
        return Claim(**resp.json())

    def get_claims(self, page: int = 1) -> List[Claim]:
        resp = self.client.get("/claims", params={"page": page})
        resp.raise_for_status()
        return [Claim(**item) for item in resp.json()]

    def update_contract(self, script: str, max_cost: Optional[int] = None, lock_hours: Optional[int] = None) -> ContractUpdateResponse:
        request = ContractUpdateRequest(script=script, max_cost=max_cost, lock_hours=lock_hours)
        resp = self.client.post("/contract/update", json=request.model_dump())
        resp.raise_for_status()
        return ContractUpdateResponse(**resp.json())

    def get_claim(self, claim_id: int) -> Claim:
        resp = self.client.get(f"/claims/{claim_id}")
        resp.raise_for_status()
        return Claim(**resp.json())

    def pay_claim(self, claim_id: int) -> Transfer:
        resp = self.client.post(f"/claims/{claim_id}/pay")
        resp.raise_for_status()
        return Transfer(**resp.json())

    def cancel_claim(self, claim_id: int) -> Claim:
        resp = self.client.post(f"/claims/{claim_id}/cancel")
        resp.raise_for_status()
        return Claim(**resp.json())

    def search_transfers(self,
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
                         ) -> List[Transfer]:
        params = {
            "source_id": source_id,
            "dest_id": dest_id,
            "user_id": user_id,
            "currency_id": currency_id,
            "start_timestamp": start_timestamp,
            "end_timestamp": end_timestamp,
            "min_amount": min_amount,
            "max_amount": max_amount,
            "input_data": input_data,
            "page": page,
            "limit": limit,
            "sort_by": sort_by,
            "sort_order": sort_order
        }
        # Filter out None values
        params = {k: v for k, v in params.items() if v is not None}
        
        resp = self.client.get("/transfers/search", params=params)
        resp.raise_for_status()
        return [Transfer(**item) for item in resp.json()]

    def get_transfer(self, transfer_id: int) -> Transfer:
        resp = self.client.get(f"/transfer/{transfer_id}")
        resp.raise_for_status()
        return Transfer(**resp.json())

    def add_liquidity(self, currency_a_id: int, currency_b_id: int, amount_a: int, amount_b: int) -> AddLiquidityResponse:
        request = AddLiquidityRequest(currency_a_id=currency_a_id, currency_b_id=currency_b_id, amount_a=amount_a, amount_b=amount_b)
        resp = self.client.post("/pools/add_liquidity", json=request.model_dump())
        resp.raise_for_status()
        return AddLiquidityResponse(**resp.json())

    def remove_liquidity(self, currency_a_id: int, currency_b_id: int, shares: int) -> RemoveLiquidityResponse:
        request = RemoveLiquidityRequest(currency_a_id=currency_a_id, currency_b_id=currency_b_id, shares=shares)
        resp = self.client.post("/pools/remove_liquidity", json=request.model_dump())
        resp.raise_for_status()
        return RemoveLiquidityResponse(**resp.json())

    def get_pools(self) -> List[LiquidityPool]:
        resp = self.client.get("/pools")
        resp.raise_for_status()
        return [LiquidityPool(**item) for item in resp.json()]

    def get_pool(self, currency_a_id: int, currency_b_id: int) -> LiquidityPool:
        resp = self.client.get(f"/pools/{currency_a_id}/{currency_b_id}")
        resp.raise_for_status()
        return LiquidityPool(**resp.json())

    def get_provider_info(self, user_id: int) -> List[LiquidityProvider]:
        resp = self.client.get(f"/pools/provider/{user_id}")
        resp.raise_for_status()
        return [LiquidityProvider(**item) for item in resp.json()]

    def get_swap_rate(self, currency_from_id: int, currency_to_id: int, amount: int) -> SwapRateResponse:
        request = SwapRequest(currency_from_id=currency_from_id, currency_to_id=currency_to_id, amount=amount)
        resp = self.client.post("/swap/rate", json=request.model_dump())
        resp.raise_for_status()
        return SwapRateResponse(**resp.json())

    def swap(self, currency_from_id: int, currency_to_id: int, amount: int) -> SwapResponse:
        request = SwapRequest(currency_from_id=currency_from_id, currency_to_id=currency_to_id, amount=amount)
        resp = self.client.post("/swap", json=request.model_dump())
        resp.raise_for_status()
        return SwapResponse(**resp.json())

    def get_swap_route(self, currency_from_id: int, currency_to_id: int) -> RouteResponse:
        resp = self.client.get(f"/swap/route/{currency_from_id}/{currency_to_id}")
        resp.raise_for_status()
        return RouteResponse(**resp.json())

    def close(self):
        self.client.close()
    
    def __enter__(self):
        return self
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
