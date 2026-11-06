"""Invariant values recorded in an export manifest: per currency its supply (the negated balance of its issuance account), the sum of all its balances (zero when the ledger is consistent) and its stake total, and per pool its reserves and shares; computed either from the database's invariant report or from exported rows."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

from rapidwire.backup.encoding import Encoding, TableSpec
from rapidwire.core.invariants import InvariantReport

__all__ = ["TOTALS_COLUMNS", "TOTALS_TABLES", "CurrencyTotals", "InvariantTotals", "PoolTotals", "TotalsBuilder", "totals_column_problems", "totals_from_report"]

# The columns, with their encodings, that the totals are computed from, per exported table.
TOTALS_COLUMNS: Final[dict[str, dict[str, Encoding]]] = {
    "balance": {"account_id": Encoding.BIGINT, "currency_id": Encoding.BIGINT, "amount": Encoding.DECIMAL},
    "currency": {"currency_id": Encoding.BIGINT, "issuance_account_id": Encoding.BIGINT},
    "pool": {"pool_id": Encoding.BIGINT, "reserve_a": Encoding.DECIMAL, "reserve_b": Encoding.DECIMAL, "total_shares": Encoding.DECIMAL, "locked_shares": Encoding.DECIMAL},
    "pool_share": {"pool_id": Encoding.BIGINT, "shares": Encoding.DECIMAL},
    "stake": {"currency_id": Encoding.BIGINT, "amount": Encoding.DECIMAL},
}
TOTALS_TABLES: Final = frozenset(TOTALS_COLUMNS)


def totals_column_problems(spec: TableSpec) -> list[str]:
    """How the columns of a TOTALS_TABLES table differ from what the totals are computed from."""
    present = {item.name: item.encoding for item in spec.columns}
    return [
        f"table {spec.name} has column {name} as {present.get(name, 'missing')}, expected {encoding}"
        for name, encoding in TOTALS_COLUMNS.get(spec.name, {}).items()
        if present.get(name) is not encoding
    ]


@dataclass(frozen=True)
class CurrencyTotals:
    supply: int
    balance_sum: int
    stake_total: int


@dataclass(frozen=True)
class PoolTotals:
    reserve_a: int
    reserve_b: int
    total_shares: int
    locked_shares: int
    provider_shares: int


@dataclass(frozen=True)
class InvariantTotals:
    currencies: dict[int, CurrencyTotals]
    pools: dict[int, PoolTotals]

    def to_json(self) -> dict[str, Any]:
        """The manifest form: lists in ascending id order with every integer as a decimal string."""
        return {
            "currencies": [
                {"currency_id": str(currency_id), "supply": str(item.supply), "balance_sum": str(item.balance_sum), "stake_total": str(item.stake_total)}
                for currency_id, item in sorted(self.currencies.items())
            ],
            "pools": [
                {
                    "pool_id": str(pool_id),
                    "reserve_a": str(item.reserve_a),
                    "reserve_b": str(item.reserve_b),
                    "total_shares": str(item.total_shares),
                    "locked_shares": str(item.locked_shares),
                    "provider_shares": str(item.provider_shares),
                }
                for pool_id, item in sorted(self.pools.items())
            ],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> InvariantTotals:
        """Parse the manifest form; malformed input raises ValueError, KeyError or TypeError."""
        currencies = {int(item["currency_id"]): CurrencyTotals(supply=int(item["supply"]), balance_sum=int(item["balance_sum"]), stake_total=int(item["stake_total"])) for item in data["currencies"]}
        pools = {
            int(item["pool_id"]): PoolTotals(
                reserve_a=int(item["reserve_a"]),
                reserve_b=int(item["reserve_b"]),
                total_shares=int(item["total_shares"]),
                locked_shares=int(item["locked_shares"]),
                provider_shares=int(item["provider_shares"]),
            )
            for item in data["pools"]
        }
        if len(currencies) != len(data["currencies"]) or len(pools) != len(data["pools"]):
            raise ValueError("invariant totals list an id twice")
        return cls(currencies=currencies, pools=pools)

    def differences(self, other: InvariantTotals) -> list[str]:
        """Describe every value of other that differs from this one, which is taken as the expected side."""
        found: list[str] = []
        for label, expected, actual in (("currency", self.currencies, other.currencies), ("pool", self.pools, other.pools)):
            for key in sorted(expected.keys() | actual.keys()):
                want: Any = expected.get(key)
                got: Any = actual.get(key)
                if want is None or got is None:
                    found.append(f"{label} {key} is {'missing from the data' if got is None else 'not in the manifest'}")
                elif want != got:
                    fields = [name for name in want.__dataclass_fields__ if getattr(want, name) != getattr(got, name)]
                    found.append(f"{label} {key}: " + ", ".join(f"{name} is {getattr(got, name)}, manifest says {getattr(want, name)}" for name in fields))
        return found


def totals_from_report(report: InvariantReport) -> InvariantTotals:
    currencies = {item.currency_id: CurrencyTotals(supply=item.supply, balance_sum=item.balance_sum, stake_total=item.stake_total) for item in report.currencies}
    pools = {
        item.pool_id: PoolTotals(
            reserve_a=item.reserve_a,
            reserve_b=item.reserve_b,
            total_shares=item.total_shares,
            locked_shares=item.locked_shares,
            provider_shares=item.provider_shares,
        )
        for item in report.pools
    }
    return InvariantTotals(currencies=currencies, pools=pools)


class TotalsBuilder:
    """Accumulates InvariantTotals from decoded rows of the TOTALS_TABLES, fed in any order; a currency's supply is the negated balance of the account its issuance_account_id names, whatever kind of account that is, as in the invariant verifier."""

    def __init__(self) -> None:
        self._issuance: dict[int, int] = {}
        self._sums: dict[int, int] = defaultdict(int)
        self._balances: dict[tuple[int, int], int] = {}
        self._stakes: dict[int, int] = defaultdict(int)
        self._pools: dict[int, tuple[int, int, int, int]] = {}
        self._shares: dict[int, int] = defaultdict(int)

    def add(self, table_name: str, row: Mapping[str, Any]) -> None:
        match table_name:
            case "currency":
                self._issuance[row["currency_id"]] = row["issuance_account_id"]
            case "balance":
                self._sums[row["currency_id"]] += row["amount"]
                self._balances[(row["account_id"], row["currency_id"])] = row["amount"]
            case "stake":
                self._stakes[row["currency_id"]] += row["amount"]
            case "pool":
                self._pools[row["pool_id"]] = (row["reserve_a"], row["reserve_b"], row["total_shares"], row["locked_shares"])
            case "pool_share":
                self._shares[row["pool_id"]] += row["shares"]
            case _:
                pass

    def build(self) -> InvariantTotals:
        currencies = {
            currency_id: CurrencyTotals(
                supply=-self._balances.get((issuance, currency_id), 0),
                balance_sum=self._sums.get(currency_id, 0),
                stake_total=self._stakes.get(currency_id, 0),
            )
            for currency_id, issuance in self._issuance.items()
        }
        pools = {
            pool_id: PoolTotals(reserve_a=a, reserve_b=b, total_shares=total, locked_shares=locked, provider_shares=self._shares.get(pool_id, 0))
            for pool_id, (a, b, total, locked) in self._pools.items()
        }
        return InvariantTotals(currencies=currencies, pools=pools)

    def unbalanced(self) -> dict[int, int]:
        """Currencies whose balances do not sum to zero, with their sums."""
        return {currency_id: total for currency_id, total in sorted(self._sums.items()) if total != 0}
