from pydantic import BaseModel, Field, field_serializer
from typing import Optional, Literal
from decimal import Decimal

class Currency(BaseModel):
    currency_id: int
    name: str
    symbol: str
    issuer_id: int = Field(alias="issuer")
    supply: int
    minting_renounced: bool
    delete_requested_at: Optional[int] = None
    daily_interest_rate: int
    new_daily_interest_rate: Optional[int] = None
    rate_change_requested_at: Optional[int] = None

    @field_serializer('currency_id', 'issuer_id', 'supply', 'delete_requested_at', 'rate_change_requested_at')
    def serialize_integers(self, value: int|None, _info):
        if value is None:
            return value
        return str(value)

class Balance(BaseModel):
    user_id: int
    currency_id: int
    amount: int

class APIKey(BaseModel):
    user_id: int
    api_key: str

class Contract(BaseModel):
    user_id: int
    script: str
    cost: int
    max_cost: int

    @field_serializer('user_id', 'cost', 'max_cost')
    def serialize_integers(self, value: int, _info):
        return str(value)

class Claim(BaseModel):
    claim_id: int
    claimant_id: int
    payer_id: int
    currency_id: int
    amount: int
    status: Literal['pending', 'paid', 'canceled']
    created_at: int
    description: Optional[str] = None

    @field_serializer('claim_id', 'claimant_id', 'payer_id', 'currency_id', 'amount', 'created_at')
    def serialize_integers(self, value: int, _info):
        return str(value)

class Stake(BaseModel):
    user_id: int
    currency_id: int
    amount: int
    last_updated_at: int

    @field_serializer('user_id', 'currency_id', 'amount', 'last_updated_at')
    def serialize_integers(self, value: int, _info):
        return str(value)

class ExecutionContext(BaseModel):
    caller_id: int
    contract_owner_id: int
    input: Optional[str] = None
    execution_id: Optional[int] = None

class Execution(BaseModel):
    execution_id: int
    caller_id: int
    contract_owner_id: int
    input_data: Optional[str]
    output_data: Optional[str]
    cost: int
    status: Literal['pending', 'success', 'failed', 'reverted']
    timestamp: int

    @field_serializer('execution_id', 'caller_id', 'contract_owner_id', 'cost', 'timestamp')
    def serialize_integers(self, value: int, _info):
        return str(value)

class Transfer(BaseModel):
    transfer_id: int
    execution_id: Optional[int]
    source_id: int
    dest_id: int
    currency_id: int
    amount: int
    timestamp: int

    @field_serializer('transfer_id', 'execution_id', 'source_id', 'dest_id', 'currency_id', 'amount', 'timestamp')
    def serialize_integers(self, value: int | None, _info):
        if value is None:
            return value
        return str(value)

class ContractHistory(BaseModel):
    history_id: int
    execution_id: int
    user_id: int
    script_hash: bytes
    cost: int
    created_at: int

    @field_serializer('history_id', 'execution_id', 'user_id', 'cost', 'created_at')
    def serialize_integers(self, value: int, _info):
        return str(value)

class Allowance(BaseModel):
    owner_id: int
    spender_id: int
    currency_id: int
    amount: int
    last_updated_at: int

    @field_serializer('owner_id', 'spender_id', 'currency_id', 'amount', 'last_updated_at')
    def serialize_integers(self, value: int, _info):
        return str(value)

class AllowanceLog(BaseModel):
    log_id: int
    execution_id: Optional[int]
    owner_id: int
    spender_id: int
    currency_id: int
    amount: int
    timestamp: int

    @field_serializer('log_id', 'execution_id', 'owner_id', 'spender_id', 'currency_id', 'amount', 'timestamp')
    def serialize_integers(self, value: int | None, _info):
        if value is None:
            return value
        return str(value)

class ChainContext(BaseModel):
    total_cost: int
    budget: int
    depth: int = 0

class LiquidityPool(BaseModel):
    pool_id: int
    currency_a_id: int
    currency_b_id: int
    reserve_a: int
    reserve_b: int
    total_shares: int

    @field_serializer('pool_id', 'currency_a_id', 'currency_b_id', 'reserve_a', 'reserve_b', 'total_shares')
    def serialize_integers(self, value: int, _info):
        return str(value)

class LiquidityProvider(BaseModel):
    provider_id: int
    pool_id: int
    user_id: int
    shares: int

    @field_serializer('provider_id', 'pool_id', 'user_id', 'shares')
    def serialize_integers(self, value: int, _info):
        return str(value)

class ContractVariable(BaseModel):
    user_id: int
    key: str
    value: int | str

class NotificationPermission(BaseModel):
    user_id: int
    allowed_user_id: int

class DiscordPermission(BaseModel):
    guild_id: int
    user_id: int
