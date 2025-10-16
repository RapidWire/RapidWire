import mysql.connector
import re
import ast
from time import time
from typing import Optional, Dict, Any, List, Tuple, Literal
from asteval import Interpreter
from decimal import Decimal

from .config import Config
from .database import DatabaseConnection
from .models import UserModel, CurrencyModel, TransactionModel, ContractModel, APIKeyModel, ClaimModel, StakeModel
from .structs import Currency, Transaction, Claim, Stake
from .exceptions import (
    UserNotFound,
    CurrencyNotFound,
    InsufficientFunds,
    TransactionError,
    TransactionCanceledByContract,
)

SYSTEM_USER_ID = 0
SECONDS_IN_A_DAY = 86400
CONTRACT_METHOD_COSTS = {
    'get_balance': 1,
    'get_transaction': 2,
    'transfer': 10,
    'search_transactions': 5,
    'get_currency': 1,
    'create_claim': 3,
    'get_claim': 2,
    'pay_claim': 5,
    'cancel_claim': 2,
}


class ContractAPI:
    def __init__(self, rapidwire_instance: 'RapidWire', transaction_context: Dict[str, Any]):
        self.core = rapidwire_instance
        self.tx = transaction_context

    def get_balance(self, user_id: int, currency_id: int) -> int:
        return self.core.get_user(user_id).get_balance(currency_id).amount

    def get_transaction(self, tx_id: int) -> Optional[dict]:
        tx = self.core.Transactions.get(tx_id)
        return tx.dict() if tx else None

    def transfer(self, source: int, dest: int, currency: int, amount: int) -> dict:
        if source != self.tx['dest']:
            raise PermissionError("Contract can only initiate transfers from its own account.")
        new_tx, _ = self.core.transfer(source, dest, currency, amount, execute_contract=False)
        return new_tx.dict()

    def search_transactions(self, source: Optional[int] = None, dest: Optional[int] = None, currency: Optional[int] = None, page: int = 1) -> List[dict]:
        txs = self.core.Transactions.search(source_id=source, dest_id=dest, currency_id=currency, page=page, limit=10)
        return [tx.dict() for tx in txs]

    def get_currency(self, currency_id: int) -> Optional[dict]:
        curr = self.core.Currencies.get(currency_id=currency_id)
        return curr.dict() if curr else None

    def create_claim(self, claimant: int, payer: int, currency: int, amount: int, desc: Optional[str] = None) -> dict:
        claim = self.core.Claims.create(claimant, payer, currency, amount, desc)
        return claim.dict()

    def get_claim(self, claim_id: int) -> Optional[dict]:
        claim = self.core.Claims.get(claim_id)
        return claim.dict() if claim else None
    
    def pay_claim(self, claim_id: int, payer_id: int) -> dict:
        tx, _ = self.core.pay_claim(claim_id, payer_id)
        return tx.dict()

    def cancel_claim(self, claim_id: int, user_id: int) -> dict:
        claim = self.core.cancel_claim(claim_id, user_id)
        return claim.dict()


class RapidWire:
    def __init__(self, db_config: dict):
        self.connection = mysql.connector.connect(**db_config)
        self.db = DatabaseConnection(self.connection)
        self.Currencies = CurrencyModel(self.db)
        self.Transactions = TransactionModel(self.db)
        self.Contracts = ContractModel(self.db)
        self.APIKeys = APIKeyModel(self.db)
        self.Claims = ClaimModel(self.db)
        self.Stakes = StakeModel(self.db)

    def get_user(self, user_id: int) -> UserModel:
        return UserModel(user_id, self.db)

    def _calculate_contract_cost(self, script: str) -> int:
        try:
            tree = ast.parse(script)
        except SyntaxError:
            raise ValueError("Invalid syntax in contract script.")

        cost = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if isinstance(node.func.value, ast.Name) and node.func.value.id == 'api':
                    method_name = node.func.attr
                    cost += CONTRACT_METHOD_COSTS.get(method_name, 0)
        return cost

    def transfer(
        self,
        source_id: int,
        destination_id: int,
        currency_id: int,
        amount: int,
        input_data: Optional[str] = None,
        execute_contract: bool = True
    ) -> Tuple[Transaction, Optional[str]]:
        if source_id == destination_id:
            raise ValueError("Source and destination cannot be the same.")
        if amount <= 0:
            raise ValueError("Transfer amount must be positive.")
        if input_data and not re.match(r'^[a-zA-Z0-9:]*$', input_data):
            raise ValueError("input_data must be alphanumeric or colon.")

        contract_message = None
        try:
            with self.db as cursor:
                source = self.get_user(source_id)
                destination = self.get_user(destination_id)

                if source_id != SYSTEM_USER_ID:
                    source_balance = source.get_balance(currency_id)
                    if source_balance.amount < amount:
                        raise InsufficientFunds("Source user has insufficient funds.")

                if source_id == SYSTEM_USER_ID:
                    self.Currencies.update_supply(cursor, currency_id, amount)
                    destination._update_balance(cursor, currency_id, amount)
                elif destination_id == SYSTEM_USER_ID:
                    source._update_balance(cursor, currency_id, -amount)
                    self.Currencies.update_supply(cursor, currency_id, -amount)
                else:
                    source._update_balance(cursor, currency_id, -amount)
                    destination._update_balance(cursor, currency_id, amount)

                cursor.execute(
                    """
                    INSERT INTO transaction (source, dest, currency_id, amount, inputData, timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (source_id, destination_id, currency_id, amount, input_data, int(time()))
                )
                transaction_id = cursor.lastrowid

                if execute_contract:
                    contract = self.Contracts.get(destination_id)
                    if contract and contract.script:
                        if contract.cost > Config.Contract.max_cost:
                            raise TransactionError(f"Contract cost ({contract.cost}) exceeds network max cost ({Config.Contract.max_cost}).")
                        if contract.max_cost > 0 and contract.cost > contract.max_cost:
                            raise TransactionError(f"Contract cost ({contract.cost}) exceeds user-defined max cost ({contract.max_cost}).")

                        transaction_context = {
                            'source': source_id,
                            'dest': destination_id,
                            'currency': currency_id,
                            'amount': amount,
                            'input_data': input_data,
                            'transaction_id': transaction_id
                        }

                        api_handler = ContractAPI(self, transaction_context)

                        contract_config = {
                            'augassign': True,
                            'if': True,
                            'ifexp': True,
                            'raise': True,
                            'formattedvalue': True
                        }

                        user_symbols = {
                            'api': api_handler,
                            'tx': transaction_context,
                            'Cancel': TransactionCanceledByContract
                        }

                        aeval = Interpreter(minimal=True, use_numpy=False, user_symbols=user_symbols, nested_symtable=True, config=contract_config)

                        try:
                            aeval.eval(contract.script, show_errors=False, raise_errors=True)
                            if 'return_message' in aeval.symtable:
                                contract_message = str(aeval.symtable['return_message'])
                        except TransactionCanceledByContract:
                            raise
                        except Exception as e:
                            raise TransactionError(f"Contract execution failed: {e}")

                cursor.execute("SELECT * FROM transaction WHERE transaction_id = %s", (transaction_id,))
                result = cursor.fetchone()
                return (Transaction(**result), contract_message)

        except TransactionCanceledByContract:
            raise
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during transfer: {err}")

    def create_currency(self, guild_id: int, name: str, symbol: str, supply: int, issuer_id: int, daily_interest_rate: Decimal) -> Tuple[Currency, Optional[Transaction]]:
        new_currency = self.Currencies.create(guild_id, name, symbol, 0, issuer_id, daily_interest_rate)
        
        initial_tx = None
        if supply > 0:
            initial_tx, _ = self.transfer(
                source_id=SYSTEM_USER_ID,
                destination_id=issuer_id,
                currency_id=guild_id,
                amount=supply,
                input_data="create"
            )
            
        return self.Currencies.get(guild_id), initial_tx

    def mint_currency(self, currency_id: int, amount: int, minter_id: int) -> Transaction:
        tx, _ = self.transfer(SYSTEM_USER_ID, minter_id, currency_id, amount, "mint")
        return tx

    def burn_currency(self, currency_id: int, amount: int, burner_id: int) -> Transaction:
        tx, _ = self.transfer(burner_id, SYSTEM_USER_ID, currency_id, amount, "burn")
        return tx
        
    def delete_currency(self, currency_id: int) -> List[Transaction]:
        holders = self.Currencies.get_all_holders(currency_id)
        transactions = []
        
        for holder in holders:
            tx, _ = self.transfer(holder.user_id, SYSTEM_USER_ID, currency_id, holder.amount, "delete:burn")
            transactions.append(tx)
        
        self.Currencies.delete(currency_id)
        return transactions

    def pay_claim(self, claim_id: int, payer_id: int) -> Tuple[Transaction, Optional[str]]:
        claim = self.Claims.get(claim_id)
        if not claim:
            raise ValueError("Claim not found.")
        if claim.payer_id != payer_id:
            raise PermissionError("You are not the designated payer for this claim.")
        if claim.status != 'pending':
            raise ValueError(f"This claim is not pending (status: {claim.status}).")

        tx, msg = self.transfer(
            source_id=claim.payer_id,
            destination_id=claim.claimant_id,
            currency_id=claim.currency_id,
            amount=claim.amount,
            input_data=f"claim:{claim_id}"
        )
        
        self.Claims.update_status(claim_id, 'paid')
        return tx, msg

    def cancel_claim(self, claim_id: int, user_id: int) -> Claim:
        claim = self.Claims.get(claim_id)
        if not claim:
            raise ValueError("Claim not found.")
        if claim.payer_id != user_id and claim.claimant_id != user_id:
            raise PermissionError("You are not authorized to cancel this claim.")
        if claim.status != 'pending':
            raise ValueError(f"Only pending claims can be canceled (status: {claim.status}).")
        
        return self.Claims.update_status(claim_id, 'canceled')

    def stake_deposit(self, user_id: int, currency_id: int, amount: int) -> Stake:
        currency = self.Currencies.get(currency_id)
        if not currency:
            raise CurrencyNotFound("Currency for staking not found.")
        
        self.transfer(user_id, SYSTEM_USER_ID, currency_id, amount, "stake:deposit")
        stake = self.Stakes.create(user_id, currency_id, amount, currency.daily_interest_rate)
        return stake

    def stake_withdraw(self, stake_id: int, user_id: int) -> Tuple[int, Transaction]:
        stake = self.Stakes.get(stake_id)
        if not stake:
            raise ValueError("Stake not found.")
        if stake.user_id != user_id:
            raise PermissionError("You do not own this stake.")
            
        days_staked = (int(time()) - stake.staked_at) // SECONDS_IN_A_DAY
        reward = 0
        if days_staked > 0:
            reward = int(Decimal(stake.amount) * Decimal(days_staked) * stake.daily_interest_rate)
        
        total_payout = stake.amount + reward
        
        tx, _ = self.transfer(SYSTEM_USER_ID, user_id, stake.currency_id, total_payout, f"stake:withdraw:{stake_id}")
        self.Stakes.delete(stake_id)
        return reward, tx

    def update_daily_interest_rate(self, currency_id: int, new_rate: Decimal):
        try:
            with self.db as cursor:
                cursor.callproc('sp_update_interest_rate', (currency_id, new_rate))
        except mysql.connector.Error as err:
            raise TransactionError(f"Failed to update interest rate via stored procedure: {err}")

    def set_contract(self, user_id: int, script: str, max_cost: Optional[int] = None):
        if max_cost is None:
            max_cost = Config.Contract.max_cost
        cost = self._calculate_contract_cost(script)
        return self.Contracts.set(user_id, script, cost, max_cost)
