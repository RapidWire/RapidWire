"""The per-process state every bot handler works from, built once when the bot is built."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.config import Settings
from rapidwire.contract.runtime import RuntimeConfig
from rapidwire.core.clock import Clock
from rapidwire.core.permissions import GuildInfo
from rapidwire.core.swap import SwapConfig

__all__ = ["BotContext"]


@dataclass(frozen=True)
class BotContext:
    """Settings, the database engine, the swap and contract runtime configurations, the GuildInfo services consult, and an optional clock; handlers read these here and never call get_settings().

    clock is None in production, so services resolve the time themselves; tests may set a FixedClock, and handlers pass now() as the services' now argument.
    """

    settings: Settings
    engine: AsyncEngine
    swap_config: SwapConfig
    runtime_config: RuntimeConfig
    guild_info: GuildInfo
    clock: Clock | None = None

    @classmethod
    def build(cls, settings: Settings, engine: AsyncEngine, guild_info: GuildInfo, *, clock: Clock | None = None) -> BotContext:
        """A context with the swap and runtime configurations derived from settings."""
        return cls(
            settings=settings,
            engine=engine,
            swap_config=SwapConfig.from_settings(settings),
            runtime_config=RuntimeConfig.from_settings(settings),
            guild_info=guild_info,
            clock=clock,
        )

    def now(self) -> datetime | None:
        """The clock's time, or None when no clock is set, for the now argument of core services."""
        return None if self.clock is None else self.clock.now()
