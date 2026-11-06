from __future__ import annotations

import inspect
import threading
import time
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from rapidwire.api import backups
from rapidwire.api.app import create_app
from rapidwire.api.backups import ChecksumCache, checksum_cache
from rapidwire.api.deps import get_state
from rapidwire.api.discord_users import DiscordUnavailable, DiscordUsers
from rapidwire.config import Settings
from rapidwire.core.permissions import InMemoryGuildInfo


def _settings(backup_dir: Path) -> Settings:
    return Settings(_env_file=None, db_password="password-1234", discord_token="unit-discord-token", backup_dir=backup_dir)


def _app(tmp_path: Path, transport: httpx.AsyncBaseTransport | None = None) -> FastAPI:
    # The engine built from these settings never connects, because no route used here touches the database.
    return create_app(_settings(tmp_path), guild_info=InMemoryGuildInfo(), discord_transport=transport)


def _runs_on_the_event_loop(call: object) -> bool:
    if inspect.iscoroutinefunction(call) or inspect.isasyncgenfunction(call):
        return True
    # A dependency that is an instance, such as HTTPBearer, runs on the loop when its class's __call__ is a coroutine function.
    method = type(call).__call__
    return not inspect.isroutine(call) and (inspect.iscoroutinefunction(method) or inspect.isasyncgenfunction(method))


def _sync_calls(dependant: Dependant, path: str) -> list[str]:
    found = [] if dependant.call is None or _runs_on_the_event_loop(dependant.call) else [f"{path}: {getattr(dependant.call, '__qualname__', dependant.call)!r}"]
    for sub in dependant.dependencies:
        found.extend(_sync_calls(sub, path))
    return found


def test_no_route_or_dependency_runs_in_the_threadpool(tmp_path: Path) -> None:
    app = _app(tmp_path)
    routes = [context for context in iter_route_contexts(app.routes) if isinstance(context.original_route, APIRoute)]
    assert len(routes) > 50
    found: list[str] = []
    for context in routes:
        assert isinstance(context.original_route, APIRoute)
        found.extend(_sync_calls(context.original_route.dependant, str(context.path)))
    assert found == []


def test_the_walk_finds_a_sync_dependency() -> None:
    def sync_dependency() -> int:
        return 1

    async def endpoint() -> None:
        return None

    dependant = Dependant(call=endpoint, dependencies=[Dependant(call=sync_dependency)])
    assert len(_sync_calls(dependant, "/x")) == 1


async def test_the_lifespan_builds_one_username_cache_and_closes_it(tmp_path: Path) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        user_id = request.url.path.rsplit("/", 1)[1]
        return httpx.Response(200, json={"id": user_id, "username": f"user{user_id}"})

    app = _app(tmp_path, httpx.MockTransport(answer))
    async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as http:
        users = app.state.rapidwire.discord_users
        assert isinstance(users, DiscordUsers)
        for user_id in (11, 12):
            response = await http.get(f"/v1/users/{user_id}/name")
            assert response.json() == {"account_id": str(user_id), "username": f"user{user_id}"}
        assert app.state.rapidwire.discord_users is users
        assert 11 in users
        assert 12 in users
    assert app.state.rapidwire is None
    # A closed cache no longer asks Discord, so an uncached user is unavailable.
    with pytest.raises(DiscordUnavailable):
        await users.username(13)
    assert inspect.iscoroutinefunction(get_state)


async def test_shutdown_waits_for_the_checksum_warmup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = threading.Event()
    finished = threading.Event()

    def slow_scan(directory: Path, cache: ChecksumCache, stop: threading.Event | None) -> None:
        started.set()
        assert stop is not None
        stop.wait(10.0)
        # The file being hashed when stop() is called takes a little longer.
        time.sleep(0.2)
        finished.set()

    monkeypatch.setattr(backups, "_scan", slow_scan)
    app = _app(tmp_path)
    async with app.router.lifespan_context(app):
        assert started.wait(10.0)
        warmup = checksum_cache(app.state).warmup
        assert warmup is not None
        assert not finished.is_set()
    assert finished.is_set()
    assert warmup.join(0)
