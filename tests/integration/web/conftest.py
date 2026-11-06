"""Fixtures for the headless browser test: one Chromium per module, and the API app on the seeded test database served by uvicorn on a free local port inside the test event loop."""

from __future__ import annotations

import asyncio
import contextlib
import os
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

import httpx
import pytest
import pytest_asyncio
import uvicorn
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.api.app import create_app
from rapidwire.config import Settings
from rapidwire.contract.runtime import RuntimeConfig
from rapidwire.core.permissions import InMemoryGuildInfo
from tests.conftest import truncate_all
from tests.integration.web.seed import OPERATOR, Seed, seed_explorer

if TYPE_CHECKING:
    from playwright.async_api import Browser

REQUIRE_BROWSER_ENV = "RAPIDWIRE_TEST_REQUIRE_BROWSER"
SERVER_START_TIMEOUT_SECONDS = 20.0
SERVER_STOP_TIMEOUT_SECONDS = 10.0
WEB_TEST_PASSWORD = "web-test-db-password"


def _unavailable(reason: str) -> NoReturn:
    """Skip the module, or fail it when RAPIDWIRE_TEST_REQUIRE_BROWSER=1 so CI cannot pass without running the browser."""
    if os.environ.get(REQUIRE_BROWSER_ENV) == "1":
        pytest.fail(f"{reason} ({REQUIRE_BROWSER_ENV}=1)")
    pytest.skip(reason)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def browser() -> AsyncIterator[Browser]:
    """A headless Chromium shared by the module's tests."""
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError as error:
        _unavailable(f"playwright is not installed: {error}")
    manager = await async_playwright().start()
    try:
        try:
            instance = await manager.chromium.launch()
        except PlaywrightError as error:
            _unavailable(f"Chromium cannot be launched (run `playwright install chromium`): {error.message.splitlines()[0]}")
        try:
            yield instance
        finally:
            await instance.close()
    finally:
        await manager.stop()


@dataclass
class Site:
    """The served app: its base URL, the seed, the backup directory, and the Discord REST requests the app made."""

    base_url: str
    seed: Seed
    backup_dir: Path
    discord_requests: list[httpx.Request] = field(default_factory=list)


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def site(engine: AsyncEngine, tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[Site]:
    """The API app with its lifespan running on the emptied and seeded test database, served at http://127.0.0.1:<port>; there is no Discord token, so username lookups answer 503."""
    await truncate_all(engine)
    backup_dir = tmp_path_factory.mktemp("web-backups")
    settings = Settings(_env_file=None, db_password=WEB_TEST_PASSWORD, operator_account_ids=[OPERATOR], backup_dir=backup_dir)
    seed = await seed_explorer(engine, backup_dir, runtime=RuntimeConfig.from_settings(settings))
    requests: list[httpx.Request] = []

    def discord_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(500)

    app = create_app(settings, engine=engine, guild_info=InMemoryGuildInfo(), discord_transport=httpx.MockTransport(discord_handler))
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False, lifespan="on"))
    sock: socket.socket | None = None
    task: asyncio.Task[None] | None = None
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        task = asyncio.create_task(server.serve(sockets=[sock]))
        async with asyncio.timeout(SERVER_START_TIMEOUT_SECONDS):
            while not server.started:
                if task.done():
                    task.result()
                    pytest.fail("uvicorn exited before it started serving")
                await asyncio.sleep(0.05)
        yield Site(base_url=f"http://127.0.0.1:{port}", seed=seed, backup_dir=backup_dir, discord_requests=requests)
    finally:
        server.should_exit = True
        stopped = task is None or await _stop(task)
        if sock is not None:
            sock.close()
    if not stopped:
        pytest.fail(f"uvicorn did not stop within {SERVER_STOP_TIMEOUT_SECONDS:.0f}s and was cancelled")


async def _stop(task: asyncio.Task[None]) -> bool:
    """Wait up to SERVER_STOP_TIMEOUT_SECONDS for the server task to finish after should_exit and re-raise its error; cancel it and return False when it does not finish, so a server stuck in startup or shutdown cannot hang the teardown."""
    done, _ = await asyncio.wait({task}, timeout=SERVER_STOP_TIMEOUT_SECONDS)
    if not done:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        return False
    task.result()
    return True
