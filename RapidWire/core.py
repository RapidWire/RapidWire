import mysql.connector
import re
import ast
from time import time
from typing import Optional, Dict, Any, List, Tuple, Literal
import asteval
from decimal import Decimal

from .config import Config
from .database import DatabaseConnection
from .models import UserModel, CurrencyModel, TransactionModel, ContractModel, APIKeyModel, ClaimModel, StakeModel, LiquidityPoolModel, LiquidityProviderModel
from .structs import Currency, Transaction, Claim, Stake, TransactionContext, ChainContext, LiquidityPool, LiquidityProvider
from .exceptions import (
    UserNotFound,
    CurrencyNotFound,
    InsufficientFunds,
    TransactionError,
    ContractError,
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
    'execute_contract': 15,
}


class ContractAPI:
    def __init__(self, rapidwire_instance: 'RapidWire', transaction_context: TransactionContext, chain_context: Optional[ChainContext] = None):
        self.core = rapidwire_instance
        self.tx = transaction_context
        self.chain_context = chain_context

    def get_balance(self, user_id: int, currency_id: int) -> int:
        return self.core.get_user(user_id).get_balance(currency_id).amount

    def get_transaction(self, tx_id: int) -> Optional[dict]:
        tx = self.core.Transactions.get(tx_id)
        return tx.dict() if tx else None

    def transfer(self, source: int, dest: int, currency: int, amount: int) -> dict:
        if source != self.tx.dest:
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

    def execute_contract(self, destination_id: int, currency_id: int, amount: int, input_data: Optional[str] = None) -> Tuple[dict, Optional[str]]:
        source_id = self.tx.dest

        callee_contract = self.core.Contracts.get(destination_id)
        if not callee_contract:
            raise ContractError(f"Contract not found at address {destination_id}")

        self.chain_context.total_cost += callee_contract.cost

        new_tx, message = self.core.transfer(
            source_id=source_id,
            destination_id=destination_id,
            currency_id=currency_id,
            amount=amount,
            input_data=input_data,
            chain_context=self.chain_context
        )
        return new_tx.dict(), message


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
        self.LiquidityPools = LiquidityPoolModel(self.db)
        self.LiquidityProviders = LiquidityProviderModel(self.db)
        self.Config = Config

    def get_user(self, user_id: int) -> UserModel:
        return UserModel(user_id, self.db)

    def _compound_interest(self, cursor, user_id: int, currency_id: int) -> Stake:
        stake = self.Stakes.get(user_id, currency_id)
        if not stake:
            return None

        currency = self.Currencies.get(currency_id)
        if not currency or currency.daily_interest_rate <= 0:
            return stake

        current_time = int(time())
        elapsed_seconds = current_time - stake.last_updated_at
        if elapsed_seconds <= SECONDS_IN_A_DAY:
            return stake

        days_passed = elapsed_seconds // SECONDS_IN_A_DAY
        reward = int(Decimal(stake.amount) * (Decimal(1) + currency.daily_interest_rate)**Decimal(days_passed) - Decimal(stake.amount))

        if reward > 0:
            new_amount = stake.amount + reward
            self.Stakes.update_amount(cursor, user_id, currency_id, new_amount, current_time)
            # We need to create a transaction for the reward
            cursor.execute(
                """
                INSERT INTO transaction (source, dest, currency_id, amount, inputData, timestamp)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (SYSTEM_USER_ID, user_id, currency_id, reward, "stake:reward", current_time)
            )
            # Update the supply
            self.Currencies.update_supply(cursor, currency_id, reward)
            return self.Stakes.get(user_id, currency_id)

        return stake

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
        execute_contract: bool = True,
        chain_context: Optional[ChainContext] = None
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
                        if chain_context is None:
                            # Start of a new chain
                            chain_context = ChainContext(
                                total_cost=contract.cost,
                                budget=contract.max_cost if contract.max_cost > 0 else Config.Contract.max_cost
                            )

                        if chain_context.total_cost > chain_context.budget:
                            raise TransactionError(f"Aggregated contract cost ({chain_context.total_cost}) exceeds budget ({chain_context.budget}).")

                        transaction_context = TransactionContext(
                            source=source_id,
                            dest=destination_id,
                            currency=currency_id,
                            amount=amount,
                            input_data=input_data,
                            transaction_id=transaction_id
                        )

                        api_handler = ContractAPI(self, transaction_context, chain_context)

                        contract_config = {
                            'augassign': True,
                            'if': True,
                            'ifexp': True,
                            'raise': True,
                        }

                        network_config = Config

                        user_symbols = {
                            'api': api_handler,
                            'tx': transaction_context,
                            'network_config': network_config,
                            'Cancel': TransactionCanceledByContract
                        }

                        aeval = asteval.Interpreter(minimal=True, use_numpy=False, user_symbols=user_symbols, nested_symtable=True, config=contract_config)

                        try:
                            aeval.eval(contract.script, show_errors=False)
                            if 'return_message' in aeval.symtable:
                                contract_message = str(aeval.symtable['return_message'])
                            if aeval.error:
                                err_dict:dict[str,str] = {}
                                aeval_error:list[asteval.astutils.ExceptionHolder] = aeval.error
                                for err in aeval_error:
                                    err_dict[str(err.exc.__name__)] = str(err.msg)
                                if 'TransactionCanceledByContract' in err_dict:
                                    raise TransactionCanceledByContract(err_dict['TransactionCanceledByContract'])
                                raise ContractError(err_dict)
                            
                        except TransactionCanceledByContract:
                            raise
                        except ContractError:
                            raise
                        except Exception as e:
                            raise TransactionError(f"{e.__class__.__name__}: {str(e)}")

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
        if amount <= 0:
            raise ValueError("Deposit amount must be positive.")

        currency = self.Currencies.get(currency_id)
        if not currency:
            raise CurrencyNotFound("Currency for staking not found.")

        try:
            with self.db as cursor:
                # First, compound any existing interest
                self._compound_interest(cursor, user_id, currency_id)

                # Then, perform the transfer from user to system
                source_balance = self.get_user(user_id).get_balance(currency_id)
                if source_balance.amount < amount:
                    raise InsufficientFunds("User has insufficient funds.")

                self.get_user(user_id)._update_balance(cursor, currency_id, -amount)

                # Upsert the stake
                self.Stakes.upsert(cursor, user_id, currency_id, amount, int(time()))

                # Create a transaction for the deposit
                cursor.execute(
                    """
                    INSERT INTO transaction (source, dest, currency_id, amount, inputData, timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (user_id, SYSTEM_USER_ID, currency_id, amount, "stake:deposit", int(time()))
                )
            return self.Stakes.get(user_id, currency_id)
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during stake deposit: {err}")

    def stake_withdraw(self, user_id: int, currency_id: int, amount: int) -> Transaction:
        if amount <= 0:
            raise ValueError("Withdrawal amount must be positive.")

        try:
            with self.db as cursor:
                stake = self._compound_interest(cursor, user_id, currency_id)
                if not stake or stake.amount < amount:
                    raise InsufficientFunds("Withdrawal amount exceeds staked balance.")

                # Reduce the stake amount
                new_stake_amount = stake.amount - amount
                if new_stake_amount > 0:
                    self.Stakes.update_amount(cursor, user_id, currency_id, new_stake_amount, int(time()))
                else:
                    self.Stakes.delete(cursor, user_id, currency_id)

                # Transfer funds back to the user
                self.get_user(user_id)._update_balance(cursor, currency_id, amount)

                # Create a transaction for the withdrawal
                cursor.execute(
                    """
                    INSERT INTO transaction (source, dest, currency_id, amount, inputData, timestamp)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    (SYSTEM_USER_ID, user_id, currency_id, amount, "stake:withdraw", int(time()))
                )
                tx_id = cursor.lastrowid
            return self.Transactions.get(tx_id)
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during stake withdrawal: {err}")

    def request_interest_rate_change(self, currency_id: int, new_rate: Decimal, user_id: int) -> Currency:
        currency = self.Currencies.get(currency_id)
        if not currency:
            raise CurrencyNotFound("Currency not found.")
        if currency.issuer_id != user_id:
            raise PermissionError("Only the currency issuer can change the interest rate.")
        if currency.rate_change_requested_at is not None:
            raise ValueError("An interest rate change is already pending.")

        return self.Currencies.request_rate_change(currency_id, new_rate)

    def apply_interest_rate_change(self, currency_id: int) -> Currency:
        currency = self.Currencies.get(currency_id)
        if not currency:
            raise CurrencyNotFound("Currency not found.")
        if currency.rate_change_requested_at is None or currency.new_daily_interest_rate is None:
            raise ValueError("No interest rate change is pending.")

        # Check if the timelock period has passed (e.g., 7 days)
        if time() - currency.rate_change_requested_at < self.Config.Staking.rate_change_timelock:
            raise PermissionError("The timelock period for the interest rate change has not passed yet.")

        return self.Currencies.apply_rate_change(currency)

    def set_contract(self, user_id: int, script: str, max_cost: Optional[int] = None):
        if max_cost is None:
            max_cost = Config.Contract.max_cost
        cost = self._calculate_contract_cost(script)
        return self.Contracts.set(user_id, script, cost, max_cost)

    def create_liquidity_pool(self, currency_a_id: int, currency_b_id: int, amount_a: int, amount_b: int, user_id: int) -> LiquidityPool:
        if self.LiquidityPools.get_by_currency_pair(currency_a_id, currency_b_id):
            raise ValueError("Liquidity pool already exists for this currency pair.")

        try:
            with self.db as cursor:
                user = self.get_user(user_id)
                user._update_balance(cursor, currency_a_id, -amount_a)
                user._update_balance(cursor, currency_b_id, -amount_b)

                initial_shares = int((Decimal(amount_a) * Decimal(amount_b)).sqrt())
                pool = self.LiquidityPools.create(currency_a_id, currency_b_id, amount_a, amount_b, initial_shares)
                self.LiquidityProviders.upsert(cursor, pool.pool_id, user_id, initial_shares)
            return pool
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during liquidity pool creation: {err}")

    def add_liquidity(self, symbol_a: str, symbol_b: str, amount_a: int, amount_b: int, user_id: int) -> Tuple[int, int]:
        pool = self.LiquidityPools.get_by_symbols(symbol_a, symbol_b)
        if not pool:
            raise ValueError("Liquidity pool not found.")

        if pool.reserve_a == 0 or pool.reserve_b == 0:
            shares_a = amount_a
            shares_b = amount_b
        else:
            shares_a = int(Decimal(amount_a) * Decimal(pool.total_shares) / Decimal(pool.reserve_a))
            shares_b = int(Decimal(amount_b) * Decimal(pool.total_shares) / Decimal(pool.reserve_b))

        shares_to_mint = min(shares_a, shares_b)

        try:
            with self.db as cursor:
                user = self.get_user(user_id)
                user._update_balance(cursor, pool.currency_a_id, -amount_a)
                user._update_balance(cursor, pool.currency_b_id, -amount_b)
                self.LiquidityPools.update_reserves(cursor, pool.pool_id, amount_a, amount_b, shares_to_mint)
                self.LiquidityProviders.upsert(cursor, pool.pool_id, user_id, shares_to_mint)
            return shares_to_mint
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error while adding liquidity: {err}")

    def remove_liquidity(self, symbol_a: str, symbol_b: str, shares: int, user_id: int) -> Tuple[int, int]:
        pool = self.LiquidityPools.get_by_symbols(symbol_a, symbol_b)
        if not pool:
            raise ValueError("Liquidity pool not found.")

        provider = self.LiquidityProviders.get_by_pool_and_user(pool.pool_id, user_id)
        if not provider or provider.shares < shares:
            raise InsufficientFunds("Insufficient shares.")

        amount_a = int(Decimal(shares) * Decimal(pool.reserve_a) / Decimal(pool.total_shares))
        amount_b = int(Decimal(shares) * Decimal(pool.reserve_b) / Decimal(pool.total_shares))

        try:
            with self.db as cursor:
                user = self.get_user(user_id)
                user._update_balance(cursor, pool.currency_a_id, amount_a)
                user._update_balance(cursor, pool.currency_b_id, amount_b)
                self.LiquidityPools.update_reserves(cursor, pool.pool_id, -amount_a, -amount_b, -shares)
                self.LiquidityProviders.upsert(cursor, pool_id, user_id, -shares)
            return amount_a, amount_b
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error while removing liquidity: {err}")

    def get_swap_rate(self, from_symbol: str, to_symbol: str, amount: int) -> int:
        pool = self.LiquidityPools.get_by_symbols(from_symbol, to_symbol)
        if not pool:
            raise ValueError("Liquidity pool not found.")

        from_currency = self.Currencies.get_by_symbol(from_symbol)

        if from_currency.currency_id == pool.currency_a_id:
            reserve_in = pool.reserve_a
            reserve_out = pool.reserve_b
        elif from_currency.currency_id == pool.currency_b_id:
            reserve_in = pool.reserve_b
            reserve_out = pool.reserve_a
        else:
            raise ValueError("Invalid currency for this pool.")

        amount_in_with_fee = Decimal(amount) * (Decimal(1) - self.Config.Swap.fee)
        return int(amount_in_with_fee * Decimal(reserve_out) / (Decimal(reserve_in) + amount_in_with_fee))

    def swap(self, from_symbol: str, to_symbol: str, amount: int, user_id: int) -> Tuple[int, int]:
        amount_out = self.get_swap_rate(from_symbol, to_symbol, amount)
        pool = self.LiquidityPools.get_by_symbols(from_symbol, to_symbol)

        from_currency = self.Currencies.get_by_symbol(from_symbol)

        if from_currency.currency_id == pool.currency_a_id:
            to_currency_id = pool.currency_b_id
            reserve_a_change = amount
            reserve_b_change = -amount_out
        else:
            to_currency_id = pool.currency_a_id
            reserve_a_change = -amount_out
            reserve_b_change = amount

        try:
            with self.db as cursor:
                user = self.get_user(user_id)
                source_balance = user.get_balance(from_currency.currency_id)
                if source_balance.amount < amount:
                    raise InsufficientFunds("Insufficient funds for swap.")
                user._update_balance(cursor, from_currency.currency_id, -amount)
                user._update_balance(cursor, to_currency_id, amount_out)
                self.LiquidityPools.update_reserves(cursor, pool.pool_id, reserve_a_change, reserve_b_change, 0)
            return amount_out, to_currency_id
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during swap: {err}")
