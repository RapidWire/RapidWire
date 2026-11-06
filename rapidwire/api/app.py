"""The FastAPI application factory: the /v1 routers, OpenAPI at /openapi.json with docs at /docs, the error envelope, the request body cap, and the static web explorer at / for every other path."""

from __future__ import annotations

import logging
import re
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final

import anyio
import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette._utils import get_route_path
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, Response
from starlette.routing import Match, Mount
from starlette.types import Scope

from rapidwire import __version__
from rapidwire.api.backups import checksum_cache, start_checksum_warmup
from rapidwire.api.body_limit import DEFAULT_BODY_LIMIT, BodyLimitMiddleware, route_body_limits
from rapidwire.api.deps import ApiState, default_guild_info, discord_client, set_state
from rapidwire.api.discord_users import DiscordUsers
from rapidwire.api.errors import ErrorResponse, install_error_handlers
from rapidwire.api.routers import ROUTERS
from rapidwire.config import Settings
from rapidwire.contract.runtime import RuntimeConfig
from rapidwire.core.guild_directory import DbGuildInfo
from rapidwire.core.permissions import GuildInfo
from rapidwire.core.swap import SwapConfig
from rapidwire.db.engine import create_engine

__all__ = ["API_PREFIX", "WARMUP_JOIN_TIMEOUT_SECONDS", "WEB_DIR", "WEB_MEDIA_TYPES", "WEB_RESPONSE_HEADERS", "create_app"]

API_PREFIX: Final = "/v1"
WEB_DIR: Final = Path(__file__).resolve().parent.parent / "web"
# Headers on every web explorer response: the pages load only their own scripts and styles, send API requests only to this origin, use no device features, and are revalidated with their ETag on every load.
WEB_RESPONSE_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "content-security-policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
        ),
        "x-content-type-options": "nosniff",
        "referrer-policy": "no-referrer",
        "cross-origin-opener-policy": "same-origin",
        "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "cache-control": "no-cache",
    }
)
# Media types of the web explorer's files by suffix, fixed so the served types do not depend on the host's mime.types.
WEB_MEDIA_TYPES: Final[Mapping[str, str]] = MappingProxyType(
    {
        ".html": "text/html; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
    }
)
# The only request paths the web mount serves files for: "/" or segments of plain file name characters that do not start with a dot.
_CANONICAL_WEB_PATH: Final = re.compile(r"/(?:[A-Za-z0-9_-][A-Za-z0-9._-]*(?:/[A-Za-z0-9_-][A-Za-z0-9._-]*)*)?")
_NOT_FOUND_PAGE: Final = "404.html"
# How long shutdown waits for the backup checksum warm-up to finish the file it is hashing.
WARMUP_JOIN_TIMEOUT_SECONDS: Final = 10.0

log = logging.getLogger("rapidwire.api")

_ERROR_RESPONSES: Final[dict[int | str, dict[str, Any]]] = {
    422: {"model": ErrorResponse, "description": "The request is invalid (error code validation_error with details, or a domain error)."},
    "default": {"model": ErrorResponse, "description": "Any other error, in the same envelope."},
}


def _is_canonical_web_path(scope: Scope) -> bool:
    """True when the request path as sent (the raw path when the server provides it) is "/" or slash-separated plain file names, so no encoded or dot segment, empty segment, backslash or trailing slash can reach a file under another spelling."""
    raw = scope.get("raw_path")
    path = raw.decode("latin-1") if isinstance(raw, bytes) else str(scope.get("path", ""))
    return _CANONICAL_WEB_PATH.fullmatch(path) is not None


class _WebFiles(StaticFiles):
    """The web explorer's files, served with WEB_RESPONSE_HEADERS and with the media types of WEB_MEDIA_TYPES instead of the platform's guesses; a GET or HEAD of a non-canonical path or of the 404 page's own path gets the 404 page with status 404, and an HTTP error such as the 405 for another method carries WEB_RESPONSE_HEADERS too."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        try:
            if scope["method"] in ("GET", "HEAD") and (path == _NOT_FOUND_PAGE or not _is_canonical_web_path(scope)):
                response = await self._not_found()
            else:
                response = await super().get_response(path, scope)
        except HTTPException as exc:
            raise HTTPException(exc.status_code, exc.detail, headers={**WEB_RESPONSE_HEADERS, **(exc.headers or {})}) from exc
        if isinstance(response, FileResponse):
            media_type = WEB_MEDIA_TYPES.get(Path(response.path).suffix.lower())
            if media_type is not None:
                response.media_type = media_type
                response.headers["content-type"] = media_type
        for name, value in WEB_RESPONSE_HEADERS.items():
            response.headers[name] = value
        return response

    async def _not_found(self) -> Response:
        full_path, stat_result = await anyio.to_thread.run_sync(self.lookup_path, _NOT_FOUND_PAGE)
        if stat_result is None:
            raise HTTPException(status_code=404)
        return FileResponse(full_path, stat_result=stat_result, status_code=404)


class _WebMount(Mount):
    """The static site mount, which never matches the API prefix, so unknown /v1 paths and wrong methods there get the router's JSON 404 and 405."""

    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        path = get_route_path(scope)
        if path == API_PREFIX or path.startswith(API_PREFIX + "/"):
            return Match.NONE, {}
        return super().matches(scope)


def create_app(
    settings: Settings,
    *,
    engine: AsyncEngine | None = None,
    guild_info: GuildInfo | None = None,
    discord_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the API application; its lifespan creates the engine from settings unless one is given (a given engine is not disposed), uses guild_info or else the database guild directory on a small engine of its own (closed with the lifespan), opens the Discord REST client over discord_transport when given with one username cache over it, and hashes the existing backups in a background thread so the first listing finds their checksums cached; shutdown stops that thread and waits up to WARMUP_JOIN_TIMEOUT_SECONDS for it."""
    swap_config = SwapConfig.from_settings(settings)
    runtime_config = RuntimeConfig.from_settings(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        db = create_engine(settings.database_url) if engine is None else engine
        directory: DbGuildInfo | None = None
        guilds: GuildInfo
        if guild_info is None:
            guilds = directory = default_guild_info(db, settings)
        else:
            guilds = guild_info
        warmup = start_checksum_warmup(settings.backup_dir, checksum_cache(app.state))
        try:
            async with discord_client(settings, discord_transport) as discord:
                configured = settings.discord_token is not None
                users = DiscordUsers(discord, configured=configured)
                set_state(
                    app,
                    ApiState(
                        settings=settings,
                        engine=db,
                        swap_config=swap_config,
                        runtime_config=runtime_config,
                        guild_info=guilds,
                        discord=discord,
                        discord_configured=configured,
                        discord_users=users,
                    ),
                )
                try:
                    yield
                finally:
                    set_state(app, None)
                    await users.aclose()
        finally:
            warmup.stop()
            if not await anyio.to_thread.run_sync(warmup.join, WARMUP_JOIN_TIMEOUT_SECONDS):
                log.warning("the backup checksum warm-up did not finish within %.0f seconds of shutdown", WARMUP_JOIN_TIMEOUT_SECONDS)
            if directory is not None:
                await directory.close()
            if engine is None:
                await db.dispose()

    app = FastAPI(
        title="RapidWire",
        version=__version__,
        description='The RapidWire ledger, DEX and contract API. Amounts and IDs are JSON strings; every error uses the envelope {"error": {"code", "message"}}.',
        openapi_url="/openapi.json",
        docs_url="/docs",
        swagger_ui_oauth2_redirect_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )
    install_error_handlers(app)
    for router in ROUTERS:
        app.include_router(router, responses=_ERROR_RESPONSES)
    app.router.routes.append(_WebMount("/", app=_WebFiles(directory=WEB_DIR, html=True), name="web"))
    # Added last, so it is the outermost middleware and refuses an oversized body before any other layer runs.
    app.add_middleware(BodyLimitMiddleware, default=DEFAULT_BODY_LIMIT, limits=route_body_limits(app.routes, settings))
    return app
