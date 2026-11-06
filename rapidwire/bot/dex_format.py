"""Display helpers shared by the /lp commands and the /swap confirmation: fee tier labels and the fee_tier parameter choices."""

from __future__ import annotations

from typing import Final

from discord import app_commands

from rapidwire.db.tables import FEE_TIERS_BPS

__all__ = ["FEE_TIER_CHOICES", "fee_tier_label"]


def fee_tier_label(fee_bps: int) -> str:
    """A fee tier in basis points as a percent without trailing zeros ("1%", "0.5%", "0.1%")."""
    whole, fraction = divmod(fee_bps, 100)
    return f"{whole}.{fraction:02d}".rstrip("0").rstrip(".") + "%"


# The choices of every fee_tier parameter, one per pool fee tier.
FEE_TIER_CHOICES: Final = [app_commands.Choice(name=fee_tier_label(tier), value=tier) for tier in FEE_TIERS_BPS]
