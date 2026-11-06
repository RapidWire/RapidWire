"""The API fixtures for the client tests, shared with tests/integration/api, plus clients bound to the test app."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable

import httpx
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.core.tokens import issue_token
from rapidwire_client import AsyncRapidWireClient
from tests.integration.api.conftest import api_app, api_settings, auth_headers, backup_dir, client, discord, guild_info

__all__ = ["BASE_URL", "ClientFactory", "api_app", "api_settings", "auth_headers", "backup_dir", "client", "discord", "guild_info", "rapidwire"]

BASE_URL = "http://test"

type ClientFactory = Callable[[int | None], Awaitable[AsyncRapidWireClient]]


async def issue(db: AsyncEngine, account_id: int) -> str:
    return (await issue_token(db, account_id=account_id)).token


@pytest_asyncio.fixture(loop_scope="session")
async def rapidwire(api_app: FastAPI, db: AsyncEngine) -> AsyncIterator[ClientFactory]:
    """A factory of async clients on the test app: rapidwire(account_id) issues a token for the account, rapidwire(None) is anonymous; all are closed afterwards."""
    opened: list[AsyncRapidWireClient] = []

    async def make(account_id: int | None) -> AsyncRapidWireClient:
        token = None if account_id is None else await issue(db, account_id)
        made = AsyncRapidWireClient(BASE_URL, token, transport=httpx.ASGITransport(app=api_app))
        opened.append(made)
        return made

    yield make
    for made in opened:
        await made.aclose()
