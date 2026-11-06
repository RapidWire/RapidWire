"""Extension points that later features register into core operations; the currency deletion cascade calls the registered pool dissolver, which does nothing until the DEX registers one."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncConnection

from rapidwire.core.ledger import BalanceKey, LedgerTx

__all__ = ["NoopPoolDissolver", "PoolDissolution", "PoolDissolver", "get_pool_dissolver", "register_pool_dissolver"]


@dataclass(frozen=True)
class PoolDissolution:
    """What a dissolution did: the pools it dissolved, and the custody balances of the deleted currency it burned and their total."""

    pools_dissolved: int = 0
    balances_burned: int = 0
    burned_amount: int = 0


class PoolDissolver(Protocol):
    """Dissolves every pool containing a currency inside the deletion transaction, in three calls made in the order below so they follow the ledger's lock order."""

    async def partner_currencies(self, conn: AsyncConnection, currency_id: int) -> set[int]:
        """The other currencies of the pools containing currency_id, asked before and again after the cascade locks them FOR SHARE alongside the deleted currency FOR UPDATE, in ascending order.

        Every write that creates a pool or changes which currencies a pool holds must first lock both pool currencies with lock_currencies(), so the deletion's lock on its currency keeps this set stable.
        """
        ...

    async def balance_keys(self, tx: LedgerTx, currency_id: int) -> set[BalanceKey]:
        """Lock the pool rows (liquidity provider share and position rows included) FOR UPDATE in ascending order and return every (account_id, currency_id) balance the dissolution will move; called after the claim, allowance and stake rows, and the cascade locks the keys in one lock_balances() call with its own."""
        ...

    async def dissolve_pools_for_currency(self, tx: LedgerTx, execution_id: int, currency_id: int) -> PoolDissolution:
        """Dissolve the pools locked by balance_keys(), moving only balances already locked and recording transfers on execution_id, and report what was burned of currency_id; reserves of the deleted currency left in pool custody are burned by the cascade and counted there."""
        ...


class NoopPoolDissolver:
    """The fallback dissolver, used while no DEX is registered."""

    async def partner_currencies(self, conn: AsyncConnection, currency_id: int) -> set[int]:
        return set()

    async def balance_keys(self, tx: LedgerTx, currency_id: int) -> set[BalanceKey]:
        return set()

    async def dissolve_pools_for_currency(self, tx: LedgerTx, execution_id: int, currency_id: int) -> PoolDissolution:
        return PoolDissolution()


_pool_dissolver: PoolDissolver = NoopPoolDissolver()


def register_pool_dissolver(dissolver: PoolDissolver) -> PoolDissolver:
    """Install dissolver as the process-wide pool dissolver and return the previous one."""
    global _pool_dissolver
    previous, _pool_dissolver = _pool_dissolver, dissolver
    return previous


def get_pool_dissolver() -> PoolDissolver:
    return _pool_dissolver
