"""Per-application state (settings, engine, swap and contract configuration, GuildInfo and the Discord REST client) built by the app lifespan, and the FastAPI dependencies that hand it to routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Final

import httpx
from fastapi import Depends, FastAPI, Request
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire import discord_rest
from rapidwire.api.discord_users import DiscordUsers
from rapidwire.config import Settings
from rapidwire.contract.runtime import RuntimeConfig
from rapidwire.core.guild_directory import DbGuildInfo
from rapidwire.core.permissions import GuildInfo
from rapidwire.core.swap import SwapConfig
from rapidwire.discord_rest import DISCORD_API_BASE_URL, DISCORD_TIMEOUT_SECONDS, DISCORD_USER_AGENT

__all__ = [
    "DISCORD_API_BASE_URL",
    "DISCORD_TIMEOUT_SECONDS",
    "DISCORD_USER_AGENT",
    "ApiState",
    "DiscordClientDep",
    "DiscordUsersDep",
    "EngineDep",
    "GuildInfoDep",
    "RuntimeConfigDep",
    "SettingsDep",
    "StateDep",
    "SwapConfigDep",
    "default_guild_info",
    "discord_client",
    "get_state",
    "set_state",
]

_STATE_ATTRIBUTE: Final = "rapidwire"


@dataclass(frozen=True)
class ApiState:
    """Everything a request handler needs from its application; discord carries the bot token as its Authorization header when one is configured (discord_configured), so handlers never read the token themselves, and discord_users is the username cache over it, created and closed by the lifespan."""

    settings: Settings
    engine: AsyncEngine
    swap_config: SwapConfig
    runtime_config: RuntimeConfig
    guild_info: GuildInfo
    discord: httpx.AsyncClient
    discord_configured: bool
    discord_users: DiscordUsers


def default_guild_info(engine: AsyncEngine, settings: Settings) -> DbGuildInfo:
    """The GuildInfo of a production API process: the guild directory the bot keeps in the database, read through its own small engine on engine's database; the caller closes it."""
    return DbGuildInfo.open(engine.url, settings)


def discord_client(settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """The shared Discord REST client (rapidwire.discord_rest) with the bot Authorization header when RAPIDWIRE_DISCORD_TOKEN is set."""
    token = settings.discord_token.get_secret_value() if settings.discord_token is not None else None
    return discord_rest.discord_client(token, transport=transport)


def set_state(app: FastAPI, state: ApiState | None) -> None:
    setattr(app.state, _STATE_ATTRIBUTE, state)


async def get_state(request: Request) -> ApiState:
    """The ApiState of the application serving request; RuntimeError when its lifespan has not started."""
    state = getattr(request.app.state, _STATE_ATTRIBUTE, None)
    if not isinstance(state, ApiState):
        raise RuntimeError("the API state is not initialised; the application lifespan has not started")
    return state


# Every dependency is a coroutine function, so FastAPI runs it on the event loop instead of in its threadpool.
async def _settings(state: Annotated[ApiState, Depends(get_state)]) -> Settings:
    return state.settings


async def _engine(state: Annotated[ApiState, Depends(get_state)]) -> AsyncEngine:
    return state.engine


async def _swap_config(state: Annotated[ApiState, Depends(get_state)]) -> SwapConfig:
    return state.swap_config


async def _runtime_config(state: Annotated[ApiState, Depends(get_state)]) -> RuntimeConfig:
    return state.runtime_config


async def _guild_info(state: Annotated[ApiState, Depends(get_state)]) -> GuildInfo:
    return state.guild_info


async def _discord(state: Annotated[ApiState, Depends(get_state)]) -> httpx.AsyncClient:
    return state.discord


async def _discord_users(state: Annotated[ApiState, Depends(get_state)]) -> DiscordUsers:
    return state.discord_users


StateDep = Annotated[ApiState, Depends(get_state)]
SettingsDep = Annotated[Settings, Depends(_settings)]
EngineDep = Annotated[AsyncEngine, Depends(_engine)]
SwapConfigDep = Annotated[SwapConfig, Depends(_swap_config)]
RuntimeConfigDep = Annotated[RuntimeConfig, Depends(_runtime_config)]
GuildInfoDep = Annotated[GuildInfo, Depends(_guild_info)]
DiscordClientDep = Annotated[httpx.AsyncClient, Depends(_discord)]
DiscordUsersDep = Annotated[DiscordUsers, Depends(_discord_users)]
