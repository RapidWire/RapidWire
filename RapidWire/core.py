import mysql.connector
import re
import json
from time import time
from typing import Optional, Dict, Any, List, Tuple, Literal
from decimal import Decimal
import hashlib

from .config import Config
from .vm import RapidWireVM
from .database import DatabaseConnection
from .models import (
    UserModel, CurrencyModel, ContractModel, APIKeyModel, ClaimModel,
    StakeModel, LiquidityPoolModel, LiquidityProviderModel, ContractVariableModel,
    NotificationPermissionModel, ExecutionModel, TransferModel, ContractHistoryModel,
    AllowanceModel, AllowanceLogModel
)
from .structs import (
    Currency, Claim, Stake, ExecutionContext, ChainContext, LiquidityPool,
    LiquidityProvider, Transfer
)
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
CONTRACT_OP_COSTS = {
    'add': 1, 'sub': 1, 'mul': 1, 'div': 1, 'mod': 1, 'concat': 1, 'eq': 1, 'gt': 1,
    'if': 1, 'exit': 0, 'cancel': 0,
    'transfer': 10, 'get_balance': 1, 'reply': 1,
    'store_get': 1, 'store_set': 3, 'store_get_other': 1,
    'approve': 3, 'transfer_from': 10,
    'get_currency': 1, 'get_transaction': 2, 'attr': 0,
    'create_claim': 3, 'pay_claim': 5, 'cancel_claim': 2,
    'exec': 15,
}


class ContractAPI:
    def __init__(self, rapidwire_instance: 'RapidWire', execution_context: ExecutionContext, chain_context: Optional[ChainContext] = None):
        self.core = rapidwire_instance
        self.ctx = execution_context
        self.chain_context = chain_context

    def get_balance(self, user_id: int, currency_id: int) -> int:
        return self.core.get_user(user_id).get_balance(currency_id).amount

    def transfer(self, source: int, dest: int, currency: int, amount: int) -> Transfer:
        if source != self.ctx.contract_owner_id:
            raise PermissionError("Contract can only initiate transfers from its own account.")
        return self.core.transfer(source, dest, currency, amount, execution_id=self.ctx.execution_id)

    def approve(self, spender: int, currency: int, amount: int):
        self.core.approve(self.ctx.contract_owner_id, spender, currency, amount, execution_id=self.ctx.execution_id)

    def transfer_from(self, sender: int, recipient: int, currency: int, amount: int) -> Transfer:
        return self.core.transfer_from(sender, recipient, currency, amount, self.ctx.contract_owner_id, execution_id=self.ctx.execution_id)

    def search_transfers(self, source: Optional[int] = None, dest: Optional[int] = None, currency: Optional[int] = None, page: int = 1) -> List[Transfer]:
        return self.core.search_transfers(source_id=source, dest_id=dest, currency_id=currency, page=page, limit=10)

    def get_transaction(self, tx_id: int) -> Optional[Transfer]:
        return self.core.Transfers.get(tx_id)

    def get_currency(self, currency_id: int) -> Optional[Currency]:
        curr = self.core.Currencies.get(currency_id=currency_id)
        return curr if curr else None

    def create_claim(self, claimant: int, payer: int, currency: int, amount: int, desc: Optional[str] = None) -> Claim:
        claim = self.core.Claims.create(claimant, payer, currency, amount, desc)
        return claim

    def get_claim(self, claim_id: int) -> Optional[Claim]:
        claim = self.core.Claims.get(claim_id)
        return claim if claim else None
    
    def pay_claim(self, claim_id: int, payer_id: int) -> Transfer:
        tx = self.core.pay_claim(claim_id, payer_id)
        return tx

    def cancel_claim(self, claim_id: int, user_id: int) -> Claim:
        claim = self.core.cancel_claim(claim_id, user_id)
        return claim

    def execute_contract(self, destination_id: int, input_data: Optional[str] = None) -> Optional[str]:
        source_id = self.ctx.contract_owner_id

        callee_contract = self.core.Contracts.get(destination_id)
        if not callee_contract:
            raise ContractError(f"Contract not found at address {destination_id}")

        self.chain_context.total_cost += callee_contract.cost

        execution_id = self.core.execute_contract(
            caller_id=source_id,
            contract_owner_id=destination_id,
            input_data=input_data
        )

        execution = self.core.Executions.get(execution_id)
        return execution.output_data if execution else None

    def get_variable(self, user_id: int|None, key: bytes) -> Optional[bytes]:
        if user_id is None:
            user_id = self.ctx.contract_owner_id
        variable = self.core.ContractVariables.get(user_id, key)
        return variable.value if variable else None

    def set_variable(self, key: bytes, value: bytes):
        if len(key) > 8:
            raise ValueError("Key must be 8 bytes or less.")
        if len(value) > 16:
            raise ValueError("Value must be 16 bytes or less.")

        user_variables = self.core.ContractVariables.get_all_for_user(self.ctx.contract_owner_id)
        if len(user_variables) >= 2000:
             # Check if the key already exists, if so, it's an update, not an insert
            if not any(v.key == key for v in user_variables):
                raise ValueError("Maximum of 2000 variables reached for this user.")

        self.core.ContractVariables.set(self.ctx.contract_owner_id, key, value)

    def cancel(self, reason: str):
        raise TransactionCanceledByContract(reason)


class RapidWire:
    def __init__(self, db_config: dict):
        self.connection = mysql.connector.connect(**db_config)
        self.db = DatabaseConnection(self.connection)
        self.Currencies = CurrencyModel(self.db)
        self.Contracts = ContractModel(self.db)
        self.APIKeys = APIKeyModel(self.db)
        self.Claims = ClaimModel(self.db)
        self.Stakes = StakeModel(self.db)
        self.LiquidityPools = LiquidityPoolModel(self.db)
        self.LiquidityProviders = LiquidityProviderModel(self.db)
        self.ContractVariables = ContractVariableModel(self.db)
        self.NotificationPermissions = NotificationPermissionModel(self.db)
        self.Executions = ExecutionModel(self.db)
        self.Transfers = TransferModel(self.db)
        self.ContractHistories = ContractHistoryModel(self.db)
        self.Allowances = AllowanceModel(self.db)
        self.AllowanceLogs = AllowanceLogModel(self.db)
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
        daily_rate = Decimal(currency.daily_interest_rate) / Decimal(10000)
        reward = int(Decimal(stake.amount) * (Decimal(1) + daily_rate)**Decimal(days_passed) - Decimal(stake.amount))

        if reward > 0:
            new_amount = stake.amount + reward
            self.Stakes.update_amount(cursor, user_id, currency_id, new_amount, current_time)
            # We need to create a transfer for the reward
            self.Transfers.create(cursor, SYSTEM_USER_ID, user_id, currency_id, reward)
            # Update the supply
            self.Currencies.update_supply(cursor, currency_id, reward)
            return self.Stakes.get(user_id, currency_id)

        return stake

    def _calculate_contract_cost(self, script: str) -> int:
        try:
            ops = json.loads(script)
            if not isinstance(ops, list):
                raise ValueError("Contract script must be a JSON list.")
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON in contract script.")

        def calculate_block_cost(block):
            cost = 0
            for cmd in block:
                op = cmd.get('op')
                cost += CONTRACT_OP_COSTS.get(op, 0)
                if op == 'if':
                    cost += calculate_block_cost(cmd.get('then', []))
                    cost += calculate_block_cost(cmd.get('else', []))
            return cost

        return calculate_block_cost(ops)

    def execute_contract(self, caller_id: int, contract_owner_id: int, input_data: Optional[str] = None) -> int:
        try:
            with self.db as cursor:
                execution_id = self.Executions.create(cursor, caller_id, contract_owner_id, input_data, 'pending')
                contract = self.Contracts.get(contract_owner_id)
                if not contract or not contract.script:
                    raise ContractError("Contract not found or script is empty.")

                chain_context = ChainContext(
                    total_cost=contract.cost,
                    budget=contract.max_cost if contract.max_cost > 0 else Config.Contract.max_cost
                )

                execution_context = ExecutionContext(
                    caller_id=caller_id,
                    contract_owner_id=contract_owner_id,
                    input=input_data,
                    execution_id=execution_id
                )

                api_handler = ContractAPI(self, execution_context, chain_context)

                system_vars = {
                    '_tx_source': caller_id,
                    '_tx_dest': contract_owner_id,
                    '_tx_input': input_data if input_data else ""
                }

                vm = RapidWireVM(json.loads(contract.script), api_handler, system_vars)

                try:
                    vm.run()
                    output_data = vm.return_message

                    self.Executions.update(cursor, execution_id, output_data, chain_context.total_cost, 'success')
                except TransactionCanceledByContract as e:
                    self.Executions.update(cursor, execution_id, str(e), chain_context.total_cost, 'reverted')
                    raise
                except ContractError as e:
                    self.Executions.update(cursor, execution_id, str(e), chain_context.total_cost, 'failed')
                    raise
                except Exception as e:
                    self.Executions.update(cursor, execution_id, f"{e.__class__.__name__}: {str(e)}", chain_context.total_cost, 'failed')
                    raise
            return execution_id
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during contract execution: {err}")

    def transfer(
        self,
        source_id: int,
        destination_id: int,
        currency_id: int,
        amount: int,
        execution_id: Optional[int] = None
    ) -> Transfer:
        if source_id == destination_id:
            raise ValueError("Source and destination cannot be the same.")
        if amount <= 0:
            raise ValueError("Transfer amount must be positive.")

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

                transfer_id = self.Transfers.create(cursor, source_id, destination_id, currency_id, amount, execution_id)

            transfer = self.Transfers.get(transfer_id)
            if not transfer:
                raise TransactionError("Failed to retrieve transfer record after creation.")
            return transfer
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during transfer: {err}")

    def create_currency(self, guild_id: int, name: str, symbol: str, supply: int, issuer_id: int, daily_interest_rate: int) -> Tuple[Currency, Optional[Transfer]]:
        new_currency = self.Currencies.create(guild_id, name, symbol, 0, issuer_id, daily_interest_rate)
        
        initial_tx = None
        if supply > 0:
            initial_tx = self.transfer(
                source_id=SYSTEM_USER_ID,
                destination_id=issuer_id,
                currency_id=guild_id,
                amount=supply
            )
            
        return self.Currencies.get(guild_id), initial_tx

    def mint_currency(self, currency_id: int, amount: int, minter_id: int) -> Transfer:
        return self.transfer(SYSTEM_USER_ID, minter_id, currency_id, amount)

    def burn_currency(self, currency_id: int, amount: int, burner_id: int) -> Transfer:
        return self.transfer(burner_id, SYSTEM_USER_ID, currency_id, amount)
        
    def delete_currency(self, currency_id: int) -> List[Transfer]:
        holders = self.Currencies.get_all_holders(currency_id)
        transactions = []
        
        for holder in holders:
            tx = self.transfer(holder.user_id, SYSTEM_USER_ID, currency_id, holder.amount)
            transactions.append(tx)
        
        self.Currencies.delete(currency_id)
        return transactions

    def cancel_delete_request(self, currency_id: int):
        self.Currencies.cancel_delete_request(currency_id)

    def pay_claim(self, claim_id: int, payer_id: int) -> Transfer:
        claim = self.Claims.get(claim_id)
        if not claim:
            raise ValueError("Claim not found.")
        if claim.payer_id != payer_id:
            raise PermissionError("You are not the designated payer for this claim.")
        if claim.status != 'pending':
            raise ValueError(f"This claim is not pending (status: {claim.status}).")

        tx = self.transfer(
            source_id=claim.payer_id,
            destination_id=claim.claimant_id,
            currency_id=claim.currency_id,
            amount=claim.amount
        )
        
        self.Claims.update_status(claim_id, 'paid')
        return tx

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

                # Create a transfer for the deposit
                self.Transfers.create(cursor, user_id, SYSTEM_USER_ID, currency_id, amount)
            return self.Stakes.get(user_id, currency_id)
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during stake deposit: {err}")

    def stake_withdraw(self, user_id: int, currency_id: int, amount: int) -> Transfer:
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

                # Create a transfer for the withdrawal
                transfer_id = self.Transfers.create(cursor, SYSTEM_USER_ID, user_id, currency_id, amount)
            return self.Transfers.get(transfer_id)
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during stake withdrawal: {err}")

    def request_interest_rate_change(self, currency_id: int, new_rate: int, user_id: int) -> Currency:
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
        if len(script) > self.Config.Contract.max_script_length:
            raise ValueError(f"Script length exceeds the maximum of {self.Config.Contract.max_script_length} characters.")
        if max_cost is None:
            max_cost = Config.Contract.max_cost
        cost = self._calculate_contract_cost(script)
        script_hash = hashlib.sha256(script.encode()).digest()

        try:
            with self.db as cursor:
                execution_id = self.Executions.create(cursor, user_id, 0, 'update_contract', 'pending')
                contract = self.Contracts.set(user_id, script, cost, max_cost)
                self.ContractHistories.create(cursor, execution_id, user_id, script_hash, cost)
                self.Executions.update(cursor, execution_id, None, 0, 'success')
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during contract update: {err}")
        
        return contract

    def approve(self, owner_id: int, spender_id: int, currency_id: int, amount: int, execution_id: Optional[int] = None):
        try:
            with self.db as cursor:
                self.Allowances.upsert(cursor, owner_id, spender_id, currency_id, amount)
                self.AllowanceLogs.create(cursor, owner_id, spender_id, currency_id, amount, execution_id)
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during approval: {err}")

    def transfer_from(self, source_id: int, destination_id: int, currency_id: int, amount: int, spender_id: int, execution_id: Optional[int] = None) -> Transfer:
        allowance = self.Allowances.get(source_id, spender_id, currency_id)
        if not allowance or allowance.amount < amount:
            raise InsufficientFunds("Spender does not have sufficient allowance.")

        try:
            with self.db as cursor:
                self.Allowances.spend(cursor, source_id, spender_id, currency_id, amount)
                return self.transfer(source_id, destination_id, currency_id, amount, execution_id)
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during transfer_from: {err}")

    def create_liquidity_pool(self, currency_a_id: int, currency_b_id: int, amount_a: int, amount_b: int, user_id: int) -> LiquidityPool:
        if amount_a <= 0 or amount_b <= 0:
            raise ValueError("Amounts must be positive.")

        if self.LiquidityPools.get_by_currency_pair(currency_a_id, currency_b_id):
            raise ValueError("Liquidity pool already exists for this currency pair.")

        try:
            with self.db as cursor:
                user = self.get_user(user_id)
                user._update_balance(cursor, currency_a_id, -amount_a)
                user._update_balance(cursor, currency_b_id, -amount_b)

                initial_shares = int((Decimal(amount_a) * Decimal(amount_b)).sqrt())
                pool = self.LiquidityPools.create(currency_a_id, currency_b_id, amount_a, amount_b, initial_shares)
                self.LiquidityProviders.add_shares(cursor, pool.pool_id, user_id, initial_shares)
            return pool
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during liquidity pool creation: {err}")

    def add_liquidity(self, symbol_a: str, symbol_b: str, amount_a_desired: int, amount_b_desired: int, user_id: int) -> Tuple[int, int]:
        pool = self.LiquidityPools.get_by_symbols(symbol_a, symbol_b)
        if not pool:
            raise ValueError("Liquidity pool not found.")

        if amount_a_desired <= 0 or amount_b_desired <= 0:
            raise ValueError("Amounts must be positive.")

        if pool.reserve_a == 0 or pool.reserve_b == 0:
            amount_a = amount_a_desired
            amount_b = amount_b_desired
            shares_to_mint = int((Decimal(amount_a) * Decimal(amount_b)).sqrt())
        else:
            amount_b_optimal = int(Decimal(amount_a_desired) * Decimal(pool.reserve_b) / Decimal(pool.reserve_a))
            if amount_b_optimal <= amount_b_desired:
                amount_a = amount_a_desired
                amount_b = amount_b_optimal
            else:
                amount_a_optimal = int(Decimal(amount_b_desired) * Decimal(pool.reserve_a) / Decimal(pool.reserve_b))
                amount_a = amount_a_optimal
                amount_b = amount_b_desired

            shares_to_mint = int(Decimal(amount_a) * Decimal(pool.total_shares) / Decimal(pool.reserve_a))

        try:
            with self.db as cursor:
                user = self.get_user(user_id)
                source_balance_a = user.get_balance(pool.currency_a_id)
                source_balance_b = user.get_balance(pool.currency_b_id)

                if source_balance_a.amount < amount_a or source_balance_b.amount < amount_b:
                    raise InsufficientFunds("Insufficient funds to add liquidity.")

                user._update_balance(cursor, pool.currency_a_id, -amount_a)
                user._update_balance(cursor, pool.currency_b_id, -amount_b)
                self.LiquidityPools.update_reserves(cursor, pool.pool_id, amount_a, amount_b, shares_to_mint)
                self.LiquidityProviders.add_shares(cursor, pool.pool_id, user_id, shares_to_mint)

                self.Transfers.create(cursor, user_id, SYSTEM_USER_ID, pool.currency_a_id, amount_a)
                self.Transfers.create(cursor, user_id, SYSTEM_USER_ID, pool.currency_b_id, amount_b)
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

                new_shares = provider.shares - shares
                if new_shares > 0:
                    self.LiquidityProviders.update_shares(cursor, pool.pool_id, user_id, new_shares)
                else:
                    self.LiquidityProviders.delete(cursor, pool.pool_id, user_id)

                self.Transfers.create(cursor, SYSTEM_USER_ID, user_id, pool.currency_a_id, amount_a)
                self.Transfers.create(cursor, SYSTEM_USER_ID, user_id, pool.currency_b_id, amount_b)
            return amount_a, amount_b
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error while removing liquidity: {err}")

    def search_transfers(self, **kwargs) -> List[Transfer]:
        return self.Transfers.search(**kwargs)

    def find_swap_route(self, from_symbol: str, to_symbol: str) -> List[LiquidityPool]:
        from_currency = self.Currencies.get_by_symbol(from_symbol)
        to_currency = self.Currencies.get_by_symbol(to_symbol)
        if not from_currency or not to_currency:
            raise CurrencyNotFound("One of the currencies for the swap was not found.")

        all_pools = self.LiquidityPools.get_all()

        # Quick check for a direct pool
        direct_pool = self.LiquidityPools.get_by_symbols(from_symbol, to_symbol)
        if direct_pool:
            return [direct_pool]

        # Graph representation: currency_id -> list of (neighbor_currency_id, pool)
        graph: dict[int, list[tuple[int, LiquidityPool]]] = {}
        for pool in all_pools:
            if pool.currency_a_id not in graph:
                graph[pool.currency_a_id] = []
            if pool.currency_b_id not in graph:
                graph[pool.currency_b_id] = []
            graph[pool.currency_a_id].append((pool.currency_b_id, pool))
            graph[pool.currency_b_id].append((pool.currency_a_id, pool))

        # BFS to find the shortest path
        queue = [(from_currency.currency_id, [])]
        visited = {from_currency.currency_id}

        while queue:
            current_currency_id, path = queue.pop(0)

            if current_currency_id == to_currency.currency_id:
                return path

            if current_currency_id in graph:
                for neighbor_currency_id, pool in graph[current_currency_id]:
                    if neighbor_currency_id not in visited:
                        visited.add(neighbor_currency_id)
                        new_path = path + [pool]
                        queue.append((neighbor_currency_id, new_path))

        raise ValueError("No swap route found between the specified currencies.")

    def get_swap_rate(self, amount_in: int, route: List[LiquidityPool], from_currency_id: int) -> int:
        current_amount = amount_in
        current_currency_id = from_currency_id

        for pool in route:
            if current_currency_id == pool.currency_a_id:
                reserve_in = pool.reserve_a
                reserve_out = pool.reserve_b
                current_currency_id = pool.currency_b_id
            elif current_currency_id == pool.currency_b_id:
                reserve_in = pool.reserve_b
                reserve_out = pool.reserve_a
                current_currency_id = pool.currency_a_id
            else:
                raise ValueError("Invalid currency for this pool in the route.")

            fee_rate = Decimal(self.Config.Swap.fee) / Decimal(10000)
            amount_in_with_fee = Decimal(current_amount) * (Decimal(1) - fee_rate)
            current_amount = int(amount_in_with_fee * Decimal(reserve_out) / (Decimal(reserve_in) + amount_in_with_fee))

        return current_amount

    def swap(self, from_symbol: str, to_symbol: str, amount: int, user_id: int) -> Tuple[int, int]:
        route = self.find_swap_route(from_symbol, to_symbol)
        from_currency = self.Currencies.get_by_symbol(from_symbol)

        amount_out = self.get_swap_rate(amount, route, from_currency.currency_id)

        current_currency_id = from_currency.currency_id
        amount_in = amount

        try:
            with self.db as cursor:
                user = self.get_user(user_id)
                source_balance = user.get_balance(current_currency_id)
                if source_balance.amount < amount:
                    raise InsufficientFunds("Insufficient funds for swap.")
                user._update_balance(cursor, current_currency_id, -amount)

                for i, pool in enumerate(route):
                    if current_currency_id == pool.currency_a_id:
                        next_currency_id = pool.currency_b_id
                        reserve_a_change = amount_in
                        reserve_b_change = -self.get_swap_rate(amount_in, [pool], current_currency_id)
                    else:
                        next_currency_id = pool.currency_a_id
                        reserve_a_change = -self.get_swap_rate(amount_in, [pool], current_currency_id)
                        reserve_b_change = amount_in

                    self.LiquidityPools.update_reserves(cursor, pool.pool_id, reserve_a_change, reserve_b_change, 0)

                    amount_in = abs(reserve_a_change) if current_currency_id == pool.currency_b_id else abs(reserve_b_change)
                    current_currency_id = next_currency_id

                user._update_balance(cursor, current_currency_id, amount_out)

                self.Transfers.create(cursor, user_id, SYSTEM_USER_ID, from_currency.currency_id, amount)
                self.Transfers.create(cursor, SYSTEM_USER_ID, user_id, current_currency_id, amount_out)
            return amount_out, current_currency_id
        except mysql.connector.Error as err:
            raise TransactionError(f"Database error during swap: {err}")
