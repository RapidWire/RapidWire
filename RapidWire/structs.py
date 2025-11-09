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

class Transaction(BaseModel):
    transaction_id: int
    source_id: int
    dest_id: int
    currency_id: int
    amount: int
    input_data: Optional[str]
    timestamp: int

    @field_serializer('transaction_id', 'source_id', 'dest_id', 'currency_id', 'amount', 'timestamp')
    def serialize_integers(self, value: int, _info):
        return str(value)

class APIKey(BaseModel):
    user_id: int
    api_key: str

class Contract(BaseModel):
    user_id: int
    script: str
    cost: int
    max_cost: int

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

class TransactionContext(BaseModel):
    source: int
    dest: int
    currency: int
    amount: int
    input_data: Optional[str] = None
    transaction_id: int

class ChainContext(BaseModel):
    total_cost: int
    budget: int

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
    key: bytes
    value: bytes

class NotificationPermission(BaseModel):
    user_id: int
    allowed_user_id: int
