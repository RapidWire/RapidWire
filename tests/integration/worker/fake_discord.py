"""A fake Discord REST API for the dispatcher tests, served through httpx.MockTransport: DM channels, channels with their guilds, messages with nonce deduplication, role changes, and scripted responses that take precedence."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from rapidwire.discord_rest import DISCORD_API_BASE_URL

PREFIX = httpx.URL(DISCORD_API_BASE_URL).path


@dataclass
class Posted:
    channel_id: int
    message_id: int
    body: dict[str, Any]


@dataclass
class FakeDiscord:
    channels: dict[int, int | None] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    posted: list[Posted] = field(default_factory=list)
    roles: list[tuple[str, int, int, int, str | None]] = field(default_factory=list)
    scripted: dict[tuple[str, str], list[httpx.Response]] = field(default_factory=dict)
    nonces: dict[tuple[int, str], int] = field(default_factory=dict)
    dm_channels: dict[int, int] = field(default_factory=dict)
    delay: float = 0.0
    next_id: int = 990_000_000_000_000_000

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def script(self, method: str, path: str, *responses: httpx.Response) -> None:
        """Answer the next requests to method path (relative to the API base, such as channels/1/messages) with responses, in order."""
        self.scripted.setdefault((method, path), []).extend(responses)

    def paths(self) -> list[tuple[str, str]]:
        return [(request.method, request.url.path.removeprefix(PREFIX + "/")) for request in self.requests]

    def _id(self) -> int:
        self.next_id += 1
        return self.next_id

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.delay:
            await asyncio.sleep(self.delay)
        path = request.url.path.removeprefix(PREFIX + "/")
        queue = self.scripted.get((request.method, path))
        if queue:
            return queue.pop(0)
        body: dict[str, Any] = json.loads(request.content) if request.content else {}
        if request.method == "POST" and path == "users/@me/channels":
            recipient = int(body["recipient_id"])
            channel_id = self.dm_channels.setdefault(recipient, self._id())
            self.channels[channel_id] = None
            return httpx.Response(200, json={"id": str(channel_id), "type": 1})
        if match := re.fullmatch(r"channels/(\d+)", path):
            channel_id = int(match[1])
            if request.method == "GET":
                if channel_id not in self.channels:
                    return httpx.Response(404, json={"code": 10003, "message": "Unknown Channel"})
                guild = self.channels[channel_id]
                return httpx.Response(200, json={"id": str(channel_id)} | ({"guild_id": str(guild)} if guild is not None else {}))
        if (match := re.fullmatch(r"channels/(\d+)/messages", path)) and request.method == "POST":
            channel_id = int(match[1])
            if channel_id not in self.channels:
                return httpx.Response(404, json={"code": 10003, "message": "Unknown Channel"})
            nonce = body.get("nonce")
            if body.get("enforce_nonce") and nonce is not None and (channel_id, nonce) in self.nonces:
                return httpx.Response(200, json={"id": str(self.nonces[(channel_id, nonce)]), "channel_id": str(channel_id)})
            message_id = self._id()
            if nonce is not None:
                self.nonces[(channel_id, nonce)] = message_id
            self.posted.append(Posted(channel_id=channel_id, message_id=message_id, body=body))
            return httpx.Response(200, json={"id": str(message_id), "channel_id": str(channel_id)})
        if (match := re.fullmatch(r"guilds/(\d+)/members/(\d+)/roles/(\d+)", path)) and request.method in ("PUT", "DELETE"):
            self.roles.append((request.method, int(match[1]), int(match[2]), int(match[3]), request.headers.get("X-Audit-Log-Reason")))
            return httpx.Response(204)
        return httpx.Response(404, json={"code": 0, "message": "404: Not Found"})
