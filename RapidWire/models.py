from typing import List, Optional, Literal
from time import time
import aiomysql
import secrets
import string
import zlib
from decimal import Decimal

from .database import DatabaseConnection
from .structs import (
    Balance, Currency, Contract, APIKey, Claim, Stake, LiquidityPool,
    LiquidityProvider, ContractVariable, NotificationPermission, Execution,
    Transfer, ContractHistory, Allowance, AllowanceLog, DiscordPermission
)
from .exceptions import UserNotFound, CurrencyNotFound, InsufficientFunds, DuplicateEntryError

class UserModel:
    def __init__(self, user_id: int, db_connection: DatabaseConnection):
        self.user_id = user_id
        self.db = db_connection

    async def get_balance(self, currency_id: int, for_update: bool = False) -> Balance:
        async with self.db as cursor:
            query = "SELECT * FROM balance WHERE user_id = %s AND currency_id = %s"
            if for_update:
                query += " FOR UPDATE"

            await cursor.execute(
                query,
                (self.user_id, currency_id)
            )
            result = await cursor.fetchone()
            if not result:
                return Balance(user_id=self.user_id, currency_id=currency_id, amount=0)
            return Balance(**result)

    async def get_all_balances(self) -> List[Balance]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM balance WHERE user_id = %s AND amount > 0", (self.user_id,))
            results = await cursor.fetchall()
            return [Balance(**row) for row in results]

    async def _update_balance(self, cursor, currency_id: int, amount_change: int):
        await cursor.execute(
            """
            INSERT INTO balance (user_id, currency_id, amount)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE amount = amount + %s
            """,
            (self.user_id, currency_id, max(0, amount_change), amount_change)
        )
        
        await cursor.execute("DELETE FROM balance WHERE user_id = %s AND currency_id = %s AND amount = 0", (self.user_id, currency_id))

class CurrencyModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def get(self, currency_id: int) -> Optional[Currency]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM currency WHERE currency_id = %s", (currency_id,))
            result = await cursor.fetchone()
            return Currency(**result) if result else None
    
    async def get_by_symbol(self, symbol: str) -> Optional[Currency]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM currency WHERE symbol = %s", (symbol,))
            result = await cursor.fetchone()
            return Currency(**result) if result else None

    async def get_all_holders(self, currency_id: int) -> List[Balance]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM balance WHERE currency_id = %s AND amount > 0 AND user_id != 0", (currency_id,))
            results = await cursor.fetchall()
            return [Balance(**row) for row in results]

    async def create(self, guild_id: int, name: str, symbol: str, supply: int, issuer_id: int, hourly_interest_rate: int) -> Currency:
        try:
            async with self.db as cursor:
                await cursor.execute(
                    "INSERT INTO currency (currency_id, name, symbol, issuer, supply, hourly_interest_rate) VALUES (%s, %s, %s, %s, %s, %s)",
                    (guild_id, name, symbol, issuer_id, supply, hourly_interest_rate)
                )
            return await self.get(currency_id=guild_id)
        except aiomysql.Error as e:
            # Check for integrity error (error code 1062 for duplicate entry)
            if e.args[0] == 1062:
                raise DuplicateEntryError("A currency already exists in this server or that symbol is taken.")
            raise e

    async def update_supply(self, cursor, currency_id: int, amount_change: int):
        await cursor.execute("UPDATE currency SET supply = supply + %s WHERE currency_id = %s", (amount_change, currency_id))

    async def renounce_minting(self, currency_id: int) -> Optional[Currency]:
        from .exceptions import RenouncedError
        currency = await self.get(currency_id)
        if not currency:
            return None
        if currency.minting_renounced:
            raise RenouncedError("Minting and rate changes have already been renounced.")

        async with self.db as cursor:
            await cursor.execute("UPDATE currency SET minting_renounced = 1 WHERE currency_id = %s", (currency_id,))
            if cursor.rowcount == 0: return None
        return await self.get(currency_id)

    async def request_delete(self, currency_id: int) -> Optional[Currency]:
        async with self.db as cursor:
            await cursor.execute("UPDATE currency SET delete_requested_at = %s WHERE currency_id = %s", (int(time()), currency_id))
            if cursor.rowcount == 0: return None
        return await self.get(currency_id)

    async def cancel_delete_request(self, currency_id: int) -> Optional[Currency]:
        async with self.db as cursor:
            await cursor.execute("UPDATE currency SET delete_requested_at = NULL WHERE currency_id = %s", (currency_id,))
            if cursor.rowcount == 0: return None
        return await self.get(currency_id)

    async def delete(self, currency_id: int):
         async with self.db as cursor:
            await cursor.execute("DELETE FROM currency WHERE currency_id = %s", (currency_id,))

    async def request_rate_change(self, currency_id: int, new_rate: int) -> Optional[Currency]:
        async with self.db as cursor:
            await cursor.execute(
                "UPDATE currency SET new_hourly_interest_rate = %s, rate_change_requested_at = %s WHERE currency_id = %s",
                (new_rate, int(time()), currency_id)
            )
            if cursor.rowcount == 0: return None
        return await self.get(currency_id)

    async def apply_rate_change(self, currency: Currency) -> Optional[Currency]:
        async with self.db as cursor:
            await cursor.execute(
                "UPDATE currency SET hourly_interest_rate = %s, new_hourly_interest_rate = NULL, rate_change_requested_at = NULL WHERE currency_id = %s",
                (currency.new_hourly_interest_rate, currency.currency_id)
            )
            if cursor.rowcount == 0: return None
        return await self.get(currency.currency_id)

class ContractModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def get(self, user_id: int) -> Optional[Contract]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM contract WHERE user_id = %s", (user_id,))
            result = await cursor.fetchone()
            if result:
                script = bytes(result['script'])
                try:
                    result['script'] = zlib.decompress(script).decode('utf-8')
                except zlib.error as e:
                    try:
                        result['script'] = script.decode('utf-8')
                    except:
                        raise e
                return Contract(**result)
            return None

    async def set(self, user_id: int, script: str, cost: int, max_cost: int, locked_until: int = 0) -> Contract:
        compressed_script = zlib.compress(script.encode('utf-8'))
        async with self.db as cursor:
            await cursor.execute(
                """
                INSERT INTO contract (user_id, script, cost, max_cost, locked_until)
                VALUES (%s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE script = VALUES(script), cost = VALUES(cost), max_cost = VALUES(max_cost), locked_until = VALUES(locked_until)
                """,
                (user_id, compressed_script, cost, max_cost, locked_until)
            )
        return await self.get(user_id)

class APIKeyModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    def _generate_key(self, length: int = 24) -> str:
        alphabet = string.ascii_letters + string.digits
        return ''.join(secrets.choice(alphabet) for _ in range(length))

    async def get(self, user_id: int) -> Optional[APIKey]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM api_key WHERE user_id = %s", (user_id,))
            result = await cursor.fetchone()
            return APIKey(**result) if result else None

    async def get_user_by_key(self, api_key: str) -> Optional[APIKey]:
        async with self.db as cursor:
            await cursor.execute("SELECT user_id, api_key FROM api_key WHERE api_key = %s", (api_key,))
            result = await cursor.fetchone()
            return APIKey(**result) if result else None

    async def create(self, user_id: int) -> APIKey:
        new_key = self._generate_key()
        async with self.db as cursor:
            await cursor.execute(
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

    async def get(self, claim_id: int) -> Optional[Claim]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM claims WHERE claim_id = %s", (claim_id,))
            result = await cursor.fetchone()
            return Claim(**result) if result else None

    async def get_for_user(self, user_id: int, page: int = 1, limit: int = 10) -> List[Claim]:
        offset = (page - 1) * limit
        async with self.db as cursor:
            await cursor.execute(
                "SELECT * FROM claims WHERE claimant_id = %s OR payer_id = %s ORDER BY created_at DESC LIMIT %s OFFSET %s",
                (user_id, user_id, limit, offset)
            )
            results = await cursor.fetchall()
            return [Claim(**row) for row in results]

    async def get_claims_created_after(self, timestamp: int) -> List[Claim]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM claims WHERE created_at > %s", (timestamp,))
            results = await cursor.fetchall()
            return [Claim(**row) for row in results]

    async def create(self, claimant_id: int, payer_id: int, currency_id: int, amount: int, description: Optional[str]) -> Claim:
        async with self.db as cursor:
            await cursor.execute(
                """
                INSERT INTO claims (claimant_id, payer_id, currency_id, amount, created_at, description)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                (claimant_id, payer_id, currency_id, amount, int(time()), description)
            )
            claim_id = cursor.lastrowid
        return await self.get(claim_id)

    async def update_status(self, claim_id: int, status: Literal['paid', 'canceled']) -> Optional[Claim]:
        async with self.db as cursor:
            await cursor.execute("UPDATE claims SET status = %s WHERE claim_id = %s", (status, claim_id))
            if cursor.rowcount == 0:
                return None
        return await self.get(claim_id)

class StakeModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def get(self, user_id: int, currency_id: int) -> Optional[Stake]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM staking WHERE user_id = %s AND currency_id = %s", (user_id, currency_id))
            result = await cursor.fetchone()
            return Stake(**result) if result else None

    async def get_for_user(self, user_id: int) -> List[Stake]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM staking WHERE user_id = %s", (user_id,))
            results = await cursor.fetchall()
            return [Stake(**row) for row in results]

    async def upsert(self, cursor, user_id: int, currency_id: int, amount_change: int, last_updated_at: int):
        await cursor.execute(
            """
            INSERT INTO staking (user_id, currency_id, amount, last_updated_at)
            VALUES (%s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE amount = amount + %s, last_updated_at = %s
            """,
            (user_id, currency_id, amount_change, last_updated_at, amount_change, last_updated_at)
        )

    async def update_amount(self, cursor, user_id: int, currency_id: int, new_amount: int, last_updated_at: int):
        await cursor.execute(
            "UPDATE staking SET amount = %s, last_updated_at = %s WHERE user_id = %s AND currency_id = %s",
            (new_amount, last_updated_at, user_id, currency_id)
        )

    async def delete(self, cursor, user_id: int, currency_id: int):
        await cursor.execute("DELETE FROM staking WHERE user_id = %s AND currency_id = %s", (user_id, currency_id))


class LiquidityPoolModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def get(self, pool_id: int, for_update: bool = False) -> Optional[LiquidityPool]:
        async with self.db as cursor:
            query = "SELECT * FROM liquidity_pool WHERE pool_id = %s"
            if for_update:
                query += " FOR UPDATE"

            await cursor.execute(query, (pool_id,))
            result = await cursor.fetchone()
            return LiquidityPool(**result) if result else None

    async def get_all(self) -> List[LiquidityPool]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM liquidity_pool")
            results = await cursor.fetchall()
            return [LiquidityPool(**row) for row in results]

    async def get_by_currency_pair(self, currency_a_id: int, currency_b_id: int) -> Optional[LiquidityPool]:
        async with self.db as cursor:
            await cursor.execute(
                "SELECT * FROM liquidity_pool WHERE (currency_a_id = %s AND currency_b_id = %s) OR (currency_a_id = %s AND currency_b_id = %s)",
                (currency_a_id, currency_b_id, currency_b_id, currency_a_id)
            )
            result = await cursor.fetchone()
            return LiquidityPool(**result) if result else None

    async def get_by_symbols(self, symbol_a: str, symbol_b: str) -> Optional[LiquidityPool]:
        async with self.db as cursor:
            await cursor.execute(
                "SELECT currency_id FROM currency WHERE symbol = %s", (symbol_a,)
            )
            res_a = await cursor.fetchone()
            await cursor.execute(
                "SELECT currency_id FROM currency WHERE symbol = %s", (symbol_b,)
            )
            res_b = await cursor.fetchone()

            if not res_a or not res_b:
                return None

            return await self.get_by_currency_pair(res_a["currency_id"], res_b["currency_id"])

    async def create(self, currency_a_id: int, currency_b_id: int, reserve_a: int, reserve_b: int, total_shares: int) -> LiquidityPool:
        async with self.db as cursor:
            await cursor.execute(
                "INSERT INTO liquidity_pool (currency_a_id, currency_b_id, reserve_a, reserve_b, total_shares) VALUES (%s, %s, %s, %s, %s)",
                (currency_a_id, currency_b_id, reserve_a, reserve_b, total_shares)
            )
            pool_id = cursor.lastrowid
        return await self.get(pool_id)

    async def update_reserves(self, cursor, pool_id: int, reserve_a_change: int, reserve_b_change: int, shares_change: int):
        await cursor.execute(
            "UPDATE liquidity_pool SET reserve_a = reserve_a + %s, reserve_b = reserve_b + %s, total_shares = total_shares + %s WHERE pool_id = %s",
            (reserve_a_change, reserve_b_change, shares_change, pool_id)
        )

class LiquidityProviderModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def get(self, provider_id: int) -> Optional[LiquidityProvider]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM liquidity_provider WHERE provider_id = %s", (provider_id,))
            result = await cursor.fetchone()
            return LiquidityProvider(**result) if result else None

    async def get_by_pool_and_user(self, pool_id: int, user_id: int) -> Optional[LiquidityProvider]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM liquidity_provider WHERE pool_id = %s AND user_id = %s", (pool_id, user_id))
            result = await cursor.fetchone()
            return LiquidityProvider(**result) if result else None

    async def add_shares(self, cursor, pool_id: int, user_id: int, shares_change: int):
        await cursor.execute(
            """
            INSERT INTO liquidity_provider (pool_id, user_id, shares)
            VALUES (%s, %s, %s)
            ON DUPLICATE KEY UPDATE shares = shares + %s
            """,
            (pool_id, user_id, shares_change, shares_change)
        )

    async def update_shares(self, cursor, pool_id: int, user_id: int, new_shares: int):
        await cursor.execute(
            "UPDATE liquidity_provider SET shares = %s WHERE pool_id = %s AND user_id = %s",
            (new_shares, pool_id, user_id)
        )

    async def delete(self, cursor, pool_id: int, user_id: int):
        await cursor.execute(
            "DELETE FROM liquidity_provider WHERE pool_id = %s AND user_id = %s",
            (pool_id, user_id)
        )

class ContractVariableModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def get(self, user_id: int, key: str) -> Optional[ContractVariable]:
        async with self.db as cursor:
            await cursor.execute(
                "SELECT * FROM contract_storage WHERE user_id = %s AND `key` = %s",
                (user_id, key)
            )
            result = await cursor.fetchone()
            if result:
                return ContractVariable(**result)
            return None

    async def set(self, user_id: int, key: str, value: int | str):
        async with self.db as cursor:
            # Always store as string
            await cursor.execute(
                """
                INSERT INTO contract_storage (user_id, `key`, `value`)
                VALUES (%s, %s, %s)
                ON DUPLICATE KEY UPDATE `value` = VALUES(`value`)
                """,
                (user_id, key, str(value))
            )

    async def get_all_for_user(self, user_id: int) -> List[ContractVariable]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM contract_storage WHERE user_id = %s", (user_id,))
            results = await cursor.fetchall()
            return [ContractVariable(**row) for row in results]

class NotificationPermissionModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def add(self, user_id: int, allowed_user_id: int):
        async with self.db as cursor:
            await cursor.execute(
                "INSERT INTO notification_permissions (user_id, allowed_user_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE user_id = user_id",
                (user_id, allowed_user_id)
            )

    async def remove(self, user_id: int, allowed_user_id: int):
        async with self.db as cursor:
            await cursor.execute(
                "DELETE FROM notification_permissions WHERE user_id = %s AND allowed_user_id = %s",
                (user_id, allowed_user_id)
            )

    async def get_for_user(self, user_id: int) -> List[NotificationPermission]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM notification_permissions WHERE user_id = %s", (user_id,))
            results = await cursor.fetchall()
            return [NotificationPermission(**row) for row in results]

    async def check(self, user_id: int, allowed_user_id: int) -> bool:
        async with self.db as cursor:
            await cursor.execute("SELECT 1 FROM notification_permissions WHERE user_id = %s AND allowed_user_id = %s", (user_id, allowed_user_id))
            return await cursor.fetchone() is not None

class ExecutionModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def get(self, execution_id: int) -> Optional[Execution]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM execution WHERE execution_id = %s", (execution_id,))
            result = await cursor.fetchone()
            return Execution(**result) if result else None

    async def create(self, cursor, caller_id: int, contract_owner_id: int, input_data: Optional[str], status: str) -> int:
        await cursor.execute(
            """
            INSERT INTO execution (caller_id, contract_owner_id, input_data, status, timestamp)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (caller_id, contract_owner_id, input_data, status, int(time()))
        )
        return cursor.lastrowid

    async def update(self, cursor, execution_id: int, output_data: Optional[str], cost: int, status: str):
        await cursor.execute(
            """
            UPDATE execution
            SET output_data = %s, cost = %s, status = %s
            WHERE execution_id = %s
            """,
            (output_data, cost, status, execution_id)
        )

class TransferModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def get(self, transfer_id: int) -> Optional[Transfer]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM transfer WHERE transfer_id = %s", (transfer_id,))
            result = await cursor.fetchone()
            return Transfer(**result) if result else None

    async def create(self, cursor, source_id: int, dest_id: int, currency_id: int, amount: int, execution_id: Optional[int] = None) -> int:
        await cursor.execute("SELECT id FROM transfer_sequence WHERE id = 1 FOR UPDATE")
        await cursor.fetchone()
        await cursor.execute("SELECT COALESCE(MAX(transfer_id), 0) + 1 AS next_id FROM transfer")
        res = await cursor.fetchone()
        next_id = res['next_id']

        await cursor.execute(
            """
            INSERT INTO transfer (transfer_id, execution_id, source_id, dest_id, currency_id, amount, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (next_id, execution_id, source_id, dest_id, currency_id, amount, int(time()))
        )
        return next_id

    async def search(
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
        limit: int = 10,
        sort_by: str = "transfer_id",
        sort_order: str = "desc"
    ) -> List[Transfer]:
        offset = (page - 1) * limit
        conditions = []
        params = []

        base_query = "SELECT t.* FROM transfer t"
        if input_data is not None:
            base_query += " JOIN execution e ON t.execution_id = e.execution_id"
            conditions.append("e.input_data = %s")
            params.append(input_data)

        if source_id is not None:
            conditions.append("t.source_id = %s")
            params.append(source_id)
        if dest_id is not None:
            conditions.append("t.dest_id = %s")
            params.append(dest_id)
        if currency_id is not None:
            conditions.append("t.currency_id = %s")
            params.append(currency_id)
        if user_id is not None:
            conditions.append("(t.source_id = %s OR t.dest_id = %s)")
            params.extend([user_id, user_id])
        if start_timestamp is not None:
            conditions.append("t.timestamp >= %s")
            params.append(start_timestamp)
        if end_timestamp is not None:
            conditions.append("t.timestamp <= %s")
            params.append(end_timestamp)
        if min_amount is not None:
            conditions.append("t.amount >= %s")
            params.append(min_amount)
        if max_amount is not None:
            conditions.append("t.amount <= %s")
            params.append(max_amount)

        allowed_sort_by = ["transfer_id", "timestamp", "amount"]
        if sort_by not in allowed_sort_by:
            sort_by = "transfer_id"

        sort_order = "ASC" if sort_order.upper() == "ASC" else "DESC"
        order_by_clause = f"ORDER BY t.{sort_by} {sort_order}"

        where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        query = f"{base_query} {where_clause} {order_by_clause} LIMIT %s OFFSET %s"
        params.extend([limit, offset])

        async with self.db as cursor:
            await cursor.execute(query, tuple(params))
            results = await cursor.fetchall()
            return [Transfer(**row) for row in results]

    async def get_user_stats(self, user_id: int) -> dict:
        query = """
            SELECT
                COUNT(*) as total_transfers,
                MIN(timestamp) as first_transfer_timestamp,
                MAX(timestamp) as last_transfer_timestamp
            FROM transfer
            WHERE source_id = %s OR dest_id = %s
        """
        params = (user_id, user_id)
        async with self.db as cursor:
            await cursor.execute(query, params)
            result = await cursor.fetchone()
            if not result or result['total_transfers'] == 0:
                return {"total_transfers": 0, "first_transfer_timestamp": None, "last_transfer_timestamp": None}
            return result

class ContractHistoryModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def get(self, history_id: int) -> Optional[ContractHistory]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM contract_history WHERE history_id = %s", (history_id,))
            result = await cursor.fetchone()
            return ContractHistory(**result) if result else None

    async def create(self, cursor, execution_id: int, user_id: int, script_hash: bytes, cost: int):
        await cursor.execute(
            """
            INSERT INTO contract_history (execution_id, user_id, script_hash, cost, created_at)
            VALUES (%s, %s, %s, %s, %s)
            """,
            (execution_id, user_id, script_hash, cost, int(time()))
        )

    async def get_for_user(self, user_id: int) -> List[ContractHistory]:
        async with self.db as cursor:
            await cursor.execute("SELECT * FROM contract_history WHERE user_id = %s ORDER BY created_at DESC", (user_id,))
            results = await cursor.fetchall()
            return [ContractHistory(**row) for row in results]

class AllowanceModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def get(self, owner_id: int, spender_id: int, currency_id: int) -> Optional[Allowance]:
        async with self.db as cursor:
            await cursor.execute(
                "SELECT * FROM allowance WHERE owner_id = %s AND spender_id = %s AND currency_id = %s",
                (owner_id, spender_id, currency_id)
            )
            result = await cursor.fetchone()
            return Allowance(**result) if result else None

    async def upsert(self, cursor, owner_id: int, spender_id: int, currency_id: int, amount: int):
        await cursor.execute(
            """
            INSERT INTO allowance (owner_id, spender_id, currency_id, amount, last_updated_at)
            VALUES (%s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE amount = VALUES(amount), last_updated_at = VALUES(last_updated_at)
            """,
            (owner_id, spender_id, currency_id, amount, int(time()))
        )

    async def spend(self, cursor, owner_id: int, spender_id: int, currency_id: int, amount: int):
        await cursor.execute(
            "UPDATE allowance SET amount = amount - %s WHERE owner_id = %s AND spender_id = %s AND currency_id = %s",
            (amount, owner_id, spender_id, currency_id)
        )

class AllowanceLogModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def create(self, cursor, owner_id: int, spender_id: int, currency_id: int, amount: int, execution_id: Optional[int] = None):
        await cursor.execute(
            """
            INSERT INTO allowance_log (execution_id, owner_id, spender_id, currency_id, amount, timestamp)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (execution_id, owner_id, spender_id, currency_id, amount, int(time()))
        )

class DiscordPermissionModel:
    def __init__(self, db_connection: DatabaseConnection):
        self.db = db_connection

    async def add(self, guild_id: int, user_id: int):
        async with self.db as cursor:
            await cursor.execute(
                "INSERT INTO discord_permissions (guild_id, user_id) VALUES (%s, %s) ON DUPLICATE KEY UPDATE user_id = user_id",
                (guild_id, user_id)
            )

    async def remove(self, guild_id: int, user_id: int):
        async with self.db as cursor:
            await cursor.execute(
                "DELETE FROM discord_permissions WHERE guild_id = %s AND user_id = %s",
                (guild_id, user_id)
            )

    async def check(self, guild_id: int, user_id: int) -> bool:
        async with self.db as cursor:
            await cursor.execute("SELECT 1 FROM discord_permissions WHERE guild_id = %s AND user_id = %s", (guild_id, user_id))
            return await cursor.fetchone() is not None

    async def get_all(self, guild_id: int) -> List[DiscordPermission]:
         async with self.db as cursor:
            await cursor.execute("SELECT * FROM discord_permissions WHERE guild_id = %s", (guild_id,))
            results = await cursor.fetchall()
            return [DiscordPermission(**row) for row in results]
