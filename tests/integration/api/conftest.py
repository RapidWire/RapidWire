"""Fixtures for API tests: test Settings, an app on the test database with an in-memory GuildInfo and a stubbed Discord REST API, an HTTP client for it, and token helpers."""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api.app import create_app
from rapidwire.api.backups import checksum_cache
from rapidwire.config import Settings
from rapidwire.core.permissions import InMemoryGuildInfo
from rapidwire.core.tokens import issue_token

OPERATOR_ID = 900_001
API_TEST_PASSWORD = "api-test-db-password"
API_TEST_DISCORD_TOKEN = "api-test-discord-token"

type AuthHeaders = Callable[[int], Awaitable[dict[str, str]]]

_USER_PATH = re.compile(r"^/api/v10/users/([0-9]+)$")


class DiscordStub:
    """A stand-in for the Discord REST API: GET /users/{id} answers {"id", "username"} from users or 404, handler (when set) answers every request instead, and requests records what was sent."""

    def __init__(self) -> None:
        self.users: dict[int, str] = {}
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.handler is not None:
            return self.handler(request)
        match = _USER_PATH.fullmatch(request.url.path)
        if request.method == "GET" and match is not None and int(match.group(1)) in self.users:
            user_id = int(match.group(1))
            return httpx.Response(200, json={"id": str(user_id), "username": self.users[user_id]})
        return httpx.Response(404, json={"message": "Unknown User", "code": 10013})


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def guild_info() -> InMemoryGuildInfo:
    return InMemoryGuildInfo()


@pytest.fixture
def discord() -> DiscordStub:
    return DiscordStub()


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    path = tmp_path / "backups"
    path.mkdir()
    return path


@pytest.fixture
def api_settings(backup_dir: Path) -> Settings:
    """Settings for the test app; the database fields are placeholders because the app uses the db fixture's engine."""
    return Settings(_env_file=None, db_password=API_TEST_PASSWORD, discord_token=API_TEST_DISCORD_TOKEN, operator_account_ids=[OPERATOR_ID], backup_dir=backup_dir)


@pytest_asyncio.fixture(loop_scope="session")
async def api_app(db: AsyncEngine, api_settings: Settings, guild_info: InMemoryGuildInfo, discord: DiscordStub) -> AsyncIterator[FastAPI]:
    """The API app on the emptied test database with its lifespan running and its startup hashing of backups finished, so tests that write backups never race it; routes added with include_router() during a test are served too."""
    app = create_app(api_settings, engine=db, guild_info=guild_info, discord_transport=httpx.MockTransport(discord))
    async with app.router.lifespan_context(app):
        warmup = checksum_cache(app.state).warmup
        assert warmup is not None
        assert await asyncio.to_thread(warmup.join, 10.0)
        yield app


@pytest_asyncio.fixture(loop_scope="session")
async def client(api_app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as http:
        yield http


@pytest.fixture
def auth_headers(db: AsyncEngine) -> AuthHeaders:
    """Issue a token for an account (revoking its earlier ones) and return the Authorization header that sends it."""

    async def issue(account_id: int) -> dict[str, str]:
        issued = await issue_token(db, account_id=account_id)
        return bearer(issued.token)

    return issue
