from typing import List, Optional, Literal
from time import time
import mysql.connector
import secrets
import string
from decimal import Decimal

from .database import DatabaseConnection
from .structs import Balance, Currency, Transaction, Contract, APIKey, Claim, Stake, LiquidityPool, LiquidityProvider
from .exceptions import UserNotFound, CurrencyNotFound, InsufficientFunds, DuplicateEntryError

class UserModel:
    def __init__(self, user_id: int, db_connection: DatabaseConnection):
        self.user_id = user_id
        self.db = db_connection

    def get_balance(self, currency_id: int) -> Balance:
        with self.db as cursor:
            cursor.execute(
                "SELECT * FROM balance WHERE user_id = %s AND currency_id = %s",
                (self.user_id, currency_id)
            )
            result = cursor.fetchone()
            if not result:
                return Balance(user_id=self.user_id, currency_id=currency_id, amount=0)
            return Balance(**result)

    def get_all_balances(self) -> List[Balance]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM balance WHERE user_id = %s AND amount > 0", (self.user_id,))
            results = cursor.fetchall()
            return [Balance(**row) for row in results]

    def _update_balance(self, cursor, currency_id: int, amount_change: int):
        cursor.execute(
            """
            INSERT INTO balance (user_id, currency_id, amount)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE amount = amount + %s
            """,
            (self.user_id, currency_id, max(0, amount_change), amount_change)
        )

class CurrencyModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    def get(self, currency_id: int) -> Optional[Currency]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM currency WHERE currency_id = %s", (currency_id,))
            result = cursor.fetchone()
            return Currency(**result) if result else None
    
    def get_by_symbol(self, symbol: str) -> Optional[Currency]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM currency WHERE symbol = %s", (symbol,))
            result = cursor.fetchone()
            return Currency(**result) if result else None

    def get_all_holders(self, currency_id: int) -> List[Balance]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM balance WHERE currency_id = %s AND amount > 0 AND user_id != 0", (currency_id,))
            results = cursor.fetchall()
            return [Balance(**row) for row in results]

    def create(self, guild_id: int, name: str, symbol: str, supply: int, issuer_id: int, daily_interest_rate: Decimal) -> Currency:
        try:
            with self.db as cursor:
                cursor.execute(
                    "INSERT INTO currency (currency_id, name, symbol, issuer, supply, daily_interest_rate) VALUES (%s, %s, %s, %s, %s, %s)",
                    (guild_id, name, symbol, issuer_id, supply, daily_interest_rate)
                )
            return self.get(currency_id=guild_id)
        except mysql.connector.errors.IntegrityError:
            raise DuplicateEntryError("A currency already exists in this server or that symbol is taken.")

    def update_supply(self, cursor, currency_id: int, amount_change: int):
        cursor.execute("UPDATE currency SET supply = supply + %s WHERE currency_id = %s", (amount_change, currency_id))

    def renounce_minting(self, currency_id: int) -> Optional[Currency]:
        with self.db as cursor:
            cursor.execute("UPDATE currency SET minting_renounced = 1 WHERE currency_id = %s", (currency_id,))
            if cursor.rowcount == 0: return None
        return self.get(currency_id)

    def request_delete(self, currency_id: int) -> Optional[Currency]:
        with self.db as cursor:
            cursor.execute("UPDATE currency SET delete_requested_at = %s WHERE currency_id = %s", (int(time()), currency_id))
            if cursor.rowcount == 0: return None
        return self.get(currency_id)

    def delete(self, currency_id: int):
         with self.db as cursor:
            cursor.execute("DELETE FROM currency WHERE currency_id = %s", (currency_id,))

    def request_rate_change(self, currency_id: int, new_rate: Decimal) -> Optional[Currency]:
        with self.db as cursor:
            cursor.execute(
                "UPDATE currency SET new_daily_interest_rate = %s, rate_change_requested_at = %s WHERE currency_id = %s",
                (new_rate, int(time()), currency_id)
            )
            if cursor.rowcount == 0: return None
        return self.get(currency_id)

    def apply_rate_change(self, currency: Currency) -> Optional[Currency]:
        with self.db as cursor:
            cursor.execute(
                "UPDATE currency SET daily_interest_rate = %s, new_daily_interest_rate = NULL, rate_change_requested_at = NULL WHERE currency_id = %s",
                (currency.new_daily_interest_rate, currency.currency_id)
            )
            if cursor.rowcount == 0: return None
        return self.get(currency.currency_id)

class TransactionModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    def get(self, transaction_id: int) -> Optional[Transaction]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM transaction WHERE transaction_id = %s", (transaction_id,))
            result = cursor.fetchone()
            return Transaction(**result) if result else None
    
    def get_user_history(self, user_id: int, page: int = 1, limit: int = 10) -> List[Transaction]:
        offset = (page - 1) * limit
        with self.db as cursor:
            cursor.execute(
                "SELECT * FROM transaction WHERE source = %s OR dest = %s ORDER BY timestamp DESC LIMIT %s OFFSET %s",
                (user_id, user_id, limit, offset)
            )
            results = cursor.fetchall()
            return [Transaction(**row) for row in results]

    def search(
        self,
        source_id: Optional[int] = None,
        dest_id: Optional[int] = None,
        currency_id: Optional[int] = None,
        user_id: Optional[int] = None,
        start_timestamp: Optional[int] = None,
        end_timestamp: Optional[int] = None,
        min_amount: Optional[int] = None,
        max_amount: Optional[int] = None,
        input_data: Optional[str] = None,
        page: int = 1,
        limit: int = 10
    ) -> List[Transaction]:
        offset = (page - 1) * limit
        conditions = []
        params = []

        if source_id is not None:
            conditions.append("source = %s")
            params.append(source_id)
        if dest_id is not None:
            conditions.append("dest = %s")
            params.append(dest_id)
        if currency_id is not None:
            conditions.append("currency_id = %s")
            params.append(currency_id)
        if user_id is not None:
            conditions.append("(source = %s OR dest = %s)")
            params.extend([user_id, user_id])
        if start_timestamp is not None:
            conditions.append("timestamp >= %s")
            params.append(start_timestamp)
        if end_timestamp is not None:
            conditions.append("timestamp <= %s")
            params.append(end_timestamp)
        if min_amount is not None:
            conditions.append("amount >= %s")
            params.append(min_amount)
        if max_amount is not None:
            conditions.append("amount <= %s")
            params.append(max_amount)
        if input_data is not None:
            conditions.append("inputData = %s")
            params.append(input_data)

        if not conditions:
            # Add a condition that's always true to prevent an empty WHERE clause
            # and still allow for pagination, etc.
            query = "SELECT * FROM transaction ORDER BY timestamp DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])
        else:
            query = f"SELECT * FROM transaction WHERE {' AND '.join(conditions)} ORDER BY timestamp DESC LIMIT %s OFFSET %s"
            params.extend([limit, offset])

        with self.db as cursor:
            cursor.execute(query, tuple(params))
            results = cursor.fetchall()
            return [Transaction(**row) for row in results]

class ContractModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    def get(self, user_id: int) -> Optional[Contract]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM contract WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            return Contract(**result) if result else None

    def set(self, user_id: int, script: str, cost: int, max_cost: int) -> Contract:
        with self.db as cursor:
            cursor.execute(
                """
                INSERT INTO contract (user_id, script, cost, max_cost)
                VALUES (%s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE script = VALUES(script), cost = VALUES(cost), max_cost = VALUES(max_cost)
                """,
                (user_id, script, cost, max_cost)
            )
        return self.get(user_id)

class APIKeyModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    def _generate_key(self, length: int = 24) -> str:
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    def get(self, user_id: int) -> Optional[APIKey]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM api_key WHERE user_id = %s", (user_id,))
            result = cursor.fetchone()
            return APIKey(**result) if result else None

    def get_user_by_key(self, api_key: str) -> Optional[APIKey]:
        with self.db as cursor:
            cursor.execute("SELECT user_id, api_key FROM api_key WHERE api_key = %s", (api_key,))
            result = cursor.fetchone()
            return APIKey(**result) if result else None

    def create(self, user_id: int) -> APIKey:
        new_key = self._generate_key()
        with self.db as cursor:
            cursor.execute(
                """
                INSERT INTO api_key (user_id, api_key)
                VALUES (%s, %s)
                ON DUPLICATE KEY UPDATE api_key = VALUES(api_key)
                """,
                (user_id, new_key)
            )
        return APIKey(user_id=user_id, api_key=new_key)

class ClaimModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    def get(self, claim_id: int) -> Optional[Claim]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM claims WHERE claim_id = %s", (claim_id,))
            result = cursor.fetchone()
            return Claim(**result) if result else None

    def get_for_user(self, user_id: int, page: int = 1, limit: int = 10) -> List[Claim]:
        offset = (page - 1) * limit
        with self.db as cursor:
            cursor.execute(
                "SELECT * FROM claims WHERE claimant_id = %s OR payer_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (user_id, user_id, limit, offset)
            )
            results = cursor.fetchall()
            return [Claim(**row) for row in results]

    def create(self, claimant_id: int, payer_id: int, currency_id: int, amount: int, description: Optional[str]) -> Claim:
        with self.db as cursor:
            cursor.execute(
                """
                INSERT INTO claims (claimant_id, payer_id, currency_id, amount, created_at, description)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (claimant_id, payer_id, currency_id, amount, int(time()), description)
            )
            claim_id = cursor.lastrowid
        return self.get(claim_id)

    def update_status(self, claim_id: int, status: Literal['paid', 'canceled']) -> Optional[Claim]:
        with self.db as cursor:
            cursor.execute("UPDATE claims SET status = %s WHERE claim_id = %s", (status, claim_id))
            if cursor.rowcount == 0:
                return None
        return self.get(claim_id)

class StakeModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    def get(self, user_id: int, currency_id: int) -> Optional[Stake]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM staking WHERE user_id = %s AND currency_id = %s", (user_id, currency_id))
            result = cursor.fetchone()
            return Stake(**result) if result else None

    def get_for_user(self, user_id: int) -> List[Stake]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM staking WHERE user_id = %s", (user_id,))
            results = cursor.fetchall()
            return [Stake(**row) for row in results]

    def upsert(self, cursor, user_id: int, currency_id: int, amount_change: int, last_updated_at: int):
        cursor.execute(
            """
            INSERT INTO staking (user_id, currency_id, amount, last_updated_at)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE amount = amount + %s, last_updated_at = %s
            """,
            (user_id, currency_id, amount_change, last_updated_at, amount_change, last_updated_at)
        )

    def update_amount(self, cursor, user_id: int, currency_id: int, new_amount: int, last_updated_at: int):
        cursor.execute(
            "UPDATE staking SET amount = %s, last_updated_at = %s WHERE user_id = %s AND currency_id = %s",
            (new_amount, last_updated_at, user_id, currency_id)
        )

    def delete(self, cursor, user_id: int, currency_id: int):
        cursor.execute("DELETE FROM staking WHERE user_id = %s AND currency_id = %s", (user_id, currency_id))


class LiquidityPoolModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    def get(self, pool_id: int) -> Optional[LiquidityPool]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM liquidity_pool WHERE pool_id = %s", (pool_id,))
            result = cursor.fetchone()
            return LiquidityPool(**result) if result else None

    def get_by_currency_pair(self, currency_a_id: int, currency_b_id: int) -> Optional[LiquidityPool]:
        with self.db as cursor:
            cursor.execute(
                "SELECT * FROM liquidity_pool WHERE (currency_a_id = %s AND currency_b_id = %s) OR (currency_a_id = %s AND currency_b_id = %s)",
                (currency_a_id, currency_b_id, currency_b_id, currency_a_id)
            )
            result = cursor.fetchone()
            return LiquidityPool(**result) if result else None

    def get_by_symbols(self, symbol_a: str, symbol_b: str) -> Optional[LiquidityPool]:
        with self.db as cursor:
            cursor.execute(
                "SELECT currency_id FROM currency WHERE symbol = %s", (symbol_a,)
            )
            res_a = cursor.fetchone()
            cursor.execute(
                "SELECT currency_id FROM currency WHERE symbol = %s", (symbol_b,)
            )
            res_b = cursor.fetchone()

            if not res_a or not res_b:
                return None

            return self.get_by_currency_pair(res_a["currency_id"], res_b["currency_id"])

    def create(self, currency_a_id: int, currency_b_id: int, reserve_a: int, reserve_b: int, total_shares: int) -> LiquidityPool:
        with self.db as cursor:
            cursor.execute(
                "INSERT INTO liquidity_pool (currency_a_id, currency_b_id, reserve_a, reserve_b, total_shares) VALUES (%s, %s, %s, %s, %s)",
                (currency_a_id, currency_b_id, reserve_a, reserve_b, total_shares)
            )
            pool_id = cursor.lastrowid
        return self.get(pool_id)

    def update_reserves(self, cursor, pool_id: int, reserve_a_change: int, reserve_b_change: int, shares_change: int):
        cursor.execute(
            "UPDATE liquidity_pool SET reserve_a = reserve_a + %s, reserve_b = reserve_b + %s, total_shares = total_shares + %s WHERE pool_id = %s",
            (reserve_a_change, reserve_b_change, shares_change, pool_id)
        )

class LiquidityProviderModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    def get(self, provider_id: int) -> Optional[LiquidityProvider]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM liquidity_provider WHERE provider_id = %s", (provider_id,))
            result = cursor.fetchone()
            return LiquidityProvider(**result) if result else None

    def get_by_pool_and_user(self, pool_id: int, user_id: int) -> Optional[LiquidityProvider]:
        with self.db as cursor:
            cursor.execute("SELECT * FROM liquidity_provider WHERE pool_id = %s AND user_id = %s", (pool_id, user_id))
            result = cursor.fetchone()
            return LiquidityProvider(**result) if result else None

    def add_shares(self, cursor, pool_id: int, user_id: int, shares_change: int):
        cursor.execute(
            """
            INSERT INTO liquidity_provider (pool_id, user_id, shares)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE shares = shares + %s
            """,
            (pool_id, user_id, shares_change, shares_change)
        )

    def update_shares(self, cursor, pool_id: int, user_id: int, new_shares: int):
        cursor.execute(
            "UPDATE liquidity_provider SET shares = %s WHERE pool_id = %s AND user_id = %s",
            (new_shares, pool_id, user_id)
        )

    def delete(self, cursor, pool_id: int, user_id: int):
        cursor.execute(
            "DELETE FROM liquidity_provider WHERE pool_id = %s AND user_id = %s",
            (pool_id, user_id)
        )
