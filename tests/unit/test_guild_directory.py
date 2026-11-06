from __future__ import annotations

from datetime import timedelta
from typing import Any, cast

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import QueuePool

from rapidwire.config import GUILD_HEARTBEAT_INTERVAL_SECONDS, Settings
from rapidwire.contract.runtime import HAS_ROLE_TIMEOUT_SECONDS
from rapidwire.core.guild_directory import READER_POOL_SIZE, READER_POOL_TIMEOUT_SECONDS, DbGuildInfo, MemberSnapshot
from rapidwire.core.permissions import GuildInfo

NO_ENGINE = cast(AsyncEngine, None)


def test_member_snapshot_normalizes_roles() -> None:
    member = MemberSnapshot(5, cast(Any, [3, 3, 4]))
    assert member.role_ids == frozenset({3, 4})
    assert MemberSnapshot(5).role_ids == frozenset()


@pytest.mark.parametrize("user_id", [0, -1, 2**63, True, "5"])
def test_member_snapshot_rejects_bad_user_ids(user_id: Any) -> None:
    with pytest.raises(ValueError, match="user_id"):
        MemberSnapshot(user_id)


@pytest.mark.parametrize("role_id", [0, -1, 2**63, False])
def test_member_snapshot_rejects_bad_role_ids(role_id: Any) -> None:
    with pytest.raises(ValueError, match="role_id"):
        MemberSnapshot(5, frozenset({role_id}))


async def test_open_builds_a_small_engine_of_its_own() -> None:
    settings = Settings(_env_file=None, db_password="test-password", guild_directory_staleness_seconds=300)
    info: GuildInfo = DbGuildInfo.open(settings.database_url, settings)
    assert isinstance(info, DbGuildInfo)
    try:
        assert info.staleness == timedelta(seconds=300)
        assert info.engine.url == settings.database_url
        pool = info.engine.sync_engine.pool
        assert isinstance(pool, QueuePool)
        assert (pool.size(), pool.timeout()) == (READER_POOL_SIZE, READER_POOL_TIMEOUT_SECONDS)
        assert READER_POOL_TIMEOUT_SECONDS < HAS_ROLE_TIMEOUT_SECONDS
    finally:
        await info.close()
    assert Settings(_env_file=None, db_password="test-password").guild_directory_staleness_seconds >= 2 * GUILD_HEARTBEAT_INTERVAL_SECONDS


async def test_close_leaves_a_given_engine_alone() -> None:
    disposed: list[bool] = []

    class Engine:
        async def dispose(self) -> None:
            disposed.append(True)

    await DbGuildInfo(cast(AsyncEngine, Engine()), staleness=timedelta(seconds=60)).close()
    assert disposed == []


@pytest.mark.parametrize("staleness", [timedelta(0), timedelta(seconds=-1)])
def test_staleness_must_be_positive(staleness: timedelta) -> None:
    with pytest.raises(ValueError, match="positive"):
        DbGuildInfo(NO_ENGINE, staleness=staleness)
