from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Iterator
from contextlib import AsyncExitStack
from datetime import timedelta
from typing import Any

import pytest
from alembic import command
from sqlalchemy import Connection, delete, func, insert, select, text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from rapidwire.api.app import create_app
from rapidwire.config import Settings
from rapidwire.core import currency as cur
from rapidwire.core import guild_directory as gd
from rapidwire.core.clock import FixedClock
from rapidwire.core.errors import GuildUnavailable, PermissionDenied
from rapidwire.core.guild_directory import DbGuildInfo, MemberSnapshot
from rapidwire.core.joblock import database_now
from rapidwire.core.ledger import retry_delay
from rapidwire.core.permissions import GuildInfo, InMemoryGuildInfo, snapshot_guild
from rapidwire.db.engine import create_engine
from rapidwire.db.migrate import head_revision, make_config
from rapidwire.db.tables import guild_administrator_role, guild_directory_state, guild_member_role, guild_member_snapshot, guild_snapshot

GUILD = 5001
OTHER_GUILD = 5002
OWNER = 101
ADMIN = 102
MEMBER = 103
NEW_OWNER = 104
STRANGER = 199
ADMIN_ROLE = 7001
ROLE_A = 7002
ROLE_B = 7003
STALENESS = timedelta(seconds=120)
# A lookup wait far beyond any scheduling delay, for checks that a read does not wait for it or still has time to use a freed connection.
SLOW_LOOKUP_WAIT_SECONDS = 30.0
DIRECTORY_TABLES = (guild_directory_state, guild_snapshot, guild_administrator_role, guild_member_snapshot, guild_member_role)


@pytest.fixture
def retries(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """The attempt numbers of every deadlock retry the writers make during the test."""
    seen: list[int] = []

    def counting(attempt: int) -> float:
        seen.append(attempt)
        return retry_delay(attempt)

    monkeypatch.setattr(gd, "retry_delay", counting)
    yield seen


def _info(engine: AsyncEngine) -> DbGuildInfo:
    return DbGuildInfo(engine, staleness=STALENESS)


async def _seed(engine: AsyncEngine) -> None:
    """GUILD owned by OWNER with ADMIN_ROLE as its administrator role, held by ADMIN; MEMBER holds ROLE_A; OWNER holds ROLE_B."""
    members = [MemberSnapshot(OWNER, frozenset({ROLE_B})), MemberSnapshot(ADMIN, frozenset({ADMIN_ROLE, ROLE_A})), MemberSnapshot(MEMBER, frozenset({ROLE_A}))]
    await gd.replace_guild(engine, guild_id=GUILD, owner_id=OWNER, administrator_role_ids=[ADMIN_ROLE], members=members)


async def _counts(engine: AsyncEngine) -> dict[str, int]:
    async with engine.connect() as conn:
        return {table.name: (await conn.execute(select(func.count()).select_from(table))).scalar_one() for table in DIRECTORY_TABLES}


async def _heartbeat_aged(engine: AsyncEngine, age: timedelta) -> None:
    async with engine.connect() as conn:
        now = await database_now(conn)
    await gd.record_heartbeat(engine, now=now - age)


async def _answers(info: GuildInfo, guild_id: int = GUILD) -> tuple[Any, ...]:
    return (
        await info.guild_owner(guild_id),
        await info.is_administrator(guild_id, ADMIN),
        await info.is_administrator(guild_id, MEMBER),
        await info.has_role(guild_id, MEMBER, ROLE_A),
        await info.has_role(guild_id, OWNER, ROLE_B),
    )


async def test_tables_are_truncated_by_the_db_fixture(db: AsyncEngine) -> None:
    assert set((await _counts(db)).values()) == {0}


async def test_no_heartbeat_means_unavailable(db: AsyncEngine) -> None:
    await _seed(db)
    info = _info(db)
    assert await _answers(info) == (None, False, False, False, False)
    assert await gd.get_heartbeat(db) is None
    await gd.record_heartbeat(db, bot_user_id=999)
    assert await _answers(info) == (OWNER, True, False, True, True)
    heartbeat = await gd.get_heartbeat(db)
    assert heartbeat is not None and heartbeat.bot_user_id == 999
    await gd.clear_heartbeat(db)
    assert await _answers(info) == (None, False, False, False, False)
    assert await gd.get_heartbeat(db) is None


async def test_stale_heartbeat_means_unavailable(db: AsyncEngine) -> None:
    await _seed(db)
    info = _info(db)
    await _heartbeat_aged(db, STALENESS + timedelta(seconds=5))
    assert await _answers(info) == (None, False, False, False, False)
    await _heartbeat_aged(db, STALENESS - timedelta(seconds=5))
    assert await _answers(info) == (OWNER, True, False, True, True)


async def test_heartbeat_keeps_bot_user_unless_given(db: AsyncEngine) -> None:
    await gd.record_heartbeat(db, bot_user_id=999)
    await gd.record_heartbeat(db)
    heartbeat = await gd.get_heartbeat(db)
    assert heartbeat is not None and heartbeat.bot_user_id == 999


async def test_injected_clock_measures_staleness(db: AsyncEngine) -> None:
    await _seed(db)
    async with db.connect() as conn:
        start = await database_now(conn)
    clock = FixedClock(start)
    info = DbGuildInfo(db, staleness=STALENESS, clock=clock)
    await gd.record_heartbeat(db, now=start)
    assert await info.guild_owner(GUILD) == OWNER
    clock.advance(STALENESS - timedelta(microseconds=1))
    assert await info.guild_owner(GUILD) == OWNER
    clock.advance(timedelta(microseconds=1))
    assert await info.guild_owner(GUILD) is None


async def test_absent_guild_and_non_member(db: AsyncEngine) -> None:
    await _seed(db)
    await gd.record_heartbeat(db)
    info = _info(db)
    assert await info.guild_owner(OTHER_GUILD) is None
    assert not await info.is_administrator(OTHER_GUILD, ADMIN)
    assert not await info.has_role(OTHER_GUILD, ADMIN, ROLE_A)
    assert not await info.is_administrator(GUILD, STRANGER)
    assert not await info.has_role(GUILD, STRANGER, ROLE_A)
    assert not await info.has_role(GUILD, STRANGER, GUILD)
    assert not await info.has_role(GUILD, MEMBER, ROLE_B)
    for bad in (0, -1, 2**63, True):
        assert await info.guild_owner(bad) is None
        assert not await info.is_administrator(GUILD, bad)
        assert not await info.has_role(GUILD, MEMBER, bad)


async def test_fresh_answers(db: AsyncEngine) -> None:
    await _seed(db)
    await gd.record_heartbeat(db)
    info = _info(db)
    assert await info.guild_owner(GUILD) == OWNER
    assert await info.is_administrator(GUILD, ADMIN)
    # The owner is not implicitly an administrator; callers check ownership themselves.
    assert not await info.is_administrator(GUILD, OWNER)
    assert not await info.is_administrator(GUILD, MEMBER)
    assert await info.has_role(GUILD, ADMIN, ADMIN_ROLE)
    assert await info.has_role(GUILD, ADMIN, ROLE_A)
    assert not await info.has_role(GUILD, ADMIN, ROLE_B)
    # Every member holds @everyone, whose ID is the guild's.
    assert await info.has_role(GUILD, MEMBER, GUILD)


async def test_everyone_role_with_administrator_makes_every_member_an_administrator(db: AsyncEngine) -> None:
    await _seed(db)
    await gd.record_heartbeat(db)
    info = _info(db)
    assert await gd.set_role_administrator(db, guild_id=GUILD, role_id=GUILD, is_administrator=True)
    assert await info.is_administrator(GUILD, MEMBER)
    assert await info.is_administrator(GUILD, OWNER)
    assert not await info.is_administrator(GUILD, STRANGER)
    with pytest.raises(ValueError, match="@everyone"):
        await gd.remove_role(db, guild_id=GUILD, role_id=GUILD)


async def test_everyone_role_is_not_stored_per_member(db: AsyncEngine) -> None:
    await gd.replace_guild(db, guild_id=GUILD, owner_id=OWNER, administrator_role_ids=[], members=[MemberSnapshot(MEMBER, frozenset({GUILD, ROLE_A}))])
    await gd.upsert_member(db, guild_id=GUILD, user_id=ADMIN, role_ids=[GUILD])
    async with db.connect() as conn:
        rows = (await conn.execute(select(guild_member_role.c.user_id, guild_member_role.c.role_id))).all()
    assert [tuple(row) for row in rows] == [(MEMBER, ROLE_A)]


async def test_owner_change(db: AsyncEngine) -> None:
    await _seed(db)
    await gd.record_heartbeat(db)
    info = _info(db)
    await gd.upsert_guild(db, guild_id=GUILD, owner_id=NEW_OWNER)
    assert await info.guild_owner(GUILD) == NEW_OWNER
    assert await info.is_administrator(GUILD, ADMIN)
    await gd.upsert_guild(db, guild_id=OTHER_GUILD, owner_id=OWNER)
    assert await info.guild_owner(OTHER_GUILD) == OWNER
    assert not await info.has_role(OTHER_GUILD, OWNER, OTHER_GUILD)


async def test_replace_guild_replaces_members_and_roles(db: AsyncEngine) -> None:
    await _seed(db)
    await gd.record_heartbeat(db)
    info = _info(db)
    await gd.replace_guild(db, guild_id=GUILD, owner_id=NEW_OWNER, administrator_role_ids=[ROLE_A], members=[MemberSnapshot(MEMBER, frozenset({ROLE_A}))])
    assert await info.guild_owner(GUILD) == NEW_OWNER
    assert await info.is_administrator(GUILD, MEMBER)
    assert not await info.is_administrator(GUILD, ADMIN)
    assert not await info.has_role(GUILD, ADMIN, ROLE_A)
    assert not await info.has_role(GUILD, OWNER, ROLE_B)
    assert await _counts(db) == {"guild_directory_state": 1, "guild_snapshot": 1, "guild_administrator_role": 1, "guild_member_snapshot": 1, "guild_member_role": 1}


async def test_replace_guild_is_atomic_for_concurrent_readers(db: AsyncEngine) -> None:
    size = 3 * gd.WRITE_BATCH_SIZE + 17
    old = [MemberSnapshot(user_id, frozenset({ROLE_A, ROLE_B})) for user_id in range(1, size + 1)]
    new = [MemberSnapshot(user_id, frozenset({ROLE_A})) for user_id in range(size + 1, 2 * size + 1)]
    await gd.replace_guild(db, guild_id=GUILD, owner_id=OWNER, administrator_role_ids=[ROLE_B], members=old)
    await gd.record_heartbeat(db)
    observed: list[tuple[int, int, int, int]] = []
    done = asyncio.Event()

    def count(table: Any) -> Any:
        return select(func.count()).select_from(table).where(table.c.guild_id == GUILD).scalar_subquery()

    newest = select(func.max(guild_member_snapshot.c.user_id)).where(guild_member_snapshot.c.guild_id == GUILD).scalar_subquery()
    sample = select(count(guild_member_snapshot), count(guild_member_role), count(guild_administrator_role), newest)

    async def reader() -> None:
        async with db.connect() as conn:
            while not done.is_set():
                members, roles, admins, last = (await conn.execute(sample)).one()
                await conn.rollback()
                observed.append((members, roles, admins, last))
                await asyncio.sleep(0)

    async def writer() -> None:
        try:
            await gd.replace_guild(db, guild_id=GUILD, owner_id=OWNER, administrator_role_ids=[ROLE_A, ADMIN_ROLE], members=new)
        finally:
            done.set()

    await asyncio.gather(reader(), writer())
    assert observed
    assert set(observed) <= {(size, 2 * size, 1, size), (size, size, 2, 2 * size)}
    info = _info(db)
    assert await info.has_role(GUILD, 2 * size, ROLE_A)
    assert await info.is_administrator(GUILD, size + 1)
    assert not await info.has_role(GUILD, size, ROLE_A)
    counts = await _counts(db)
    assert (counts["guild_member_snapshot"], counts["guild_member_role"], counts["guild_administrator_role"]) == (size, size, 2)


async def test_replace_guild_reader_sees_old_state_until_commit(db: AsyncEngine, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger="rapidwire.core.guild_directory")
    await _seed(db)
    await gd.record_heartbeat(db)
    info = _info(db)
    async with db.begin() as conn:
        await conn.execute(select(guild_snapshot.c.guild_id).where(guild_snapshot.c.guild_id == GUILD).with_for_update())
        await conn.execute(delete(guild_member_role).where(guild_member_role.c.guild_id == GUILD))
        await conn.execute(delete(guild_member_snapshot).where(guild_member_snapshot.c.guild_id == GUILD))
        # The reader neither waits for the open writer nor sees its uncommitted deletions; a read that waited for the writer's locks would end in a lock wait timeout, which the reader answers as unknown.
        assert await info.has_role(GUILD, MEMBER, ROLE_A)
        assert await info.is_administrator(GUILD, ADMIN)
        assert await info.guild_owner(GUILD) == OWNER
        assert [record.getMessage() for record in caplog.records if record.name == "rapidwire.core.guild_directory"] == []
        await conn.rollback()


async def test_replace_guild_validates_before_writing(db: AsyncEngine) -> None:
    with pytest.raises(ValueError, match="more than once"):
        await gd.replace_guild(db, guild_id=GUILD, owner_id=OWNER, administrator_role_ids=[], members=[MemberSnapshot(MEMBER), MemberSnapshot(MEMBER)])
    with pytest.raises(ValueError, match="owner_id"):
        await gd.replace_guild(db, guild_id=GUILD, owner_id=0, administrator_role_ids=[], members=[])
    with pytest.raises(ValueError, match="role_id"):
        await gd.replace_guild(db, guild_id=GUILD, owner_id=OWNER, administrator_role_ids=[2**63], members=[])
    with pytest.raises(ValueError, match="guild_id"):
        await gd.upsert_member(db, guild_id=True, user_id=MEMBER, role_ids=[])
    with pytest.raises(ValueError, match="bot_user_id"):
        await gd.record_heartbeat(db, bot_user_id=0)
    assert set((await _counts(db)).values()) == {0}


async def test_remove_guild_cascades(db: AsyncEngine) -> None:
    await _seed(db)
    await gd.replace_guild(db, guild_id=OTHER_GUILD, owner_id=NEW_OWNER, administrator_role_ids=[ROLE_A], members=[MemberSnapshot(MEMBER, frozenset({ROLE_A}))])
    await gd.record_heartbeat(db)
    info = _info(db)
    assert await gd.remove_guild(db, guild_id=GUILD)
    assert not await gd.remove_guild(db, guild_id=GUILD)
    assert await _answers(info) == (None, False, False, False, False)
    assert await info.is_administrator(OTHER_GUILD, MEMBER)
    async with db.connect() as conn:
        for table in DIRECTORY_TABLES[1:]:
            assert (await conn.execute(select(func.count()).select_from(table).where(table.c.guild_id == GUILD))).scalar_one() == 0


async def test_schema_cascades_from_the_guild_row(db: AsyncEngine) -> None:
    await _seed(db)
    async with db.begin() as conn:
        await conn.execute(delete(guild_snapshot).where(guild_snapshot.c.guild_id == GUILD))
    assert set((await _counts(db)).values()) == {0}


async def test_prune_guilds(db: AsyncEngine) -> None:
    for guild_id in (GUILD, OTHER_GUILD, 5003):
        await gd.replace_guild(db, guild_id=guild_id, owner_id=OWNER, administrator_role_ids=[], members=[MemberSnapshot(MEMBER)])
    assert await gd.prune_guilds(db, keep_guild_ids=[OTHER_GUILD, 9999]) == [GUILD, 5003]
    async with db.connect() as conn:
        assert (await conn.execute(select(guild_snapshot.c.guild_id))).scalars().all() == [OTHER_GUILD]
    assert await gd.prune_guilds(db, keep_guild_ids=[OTHER_GUILD]) == []


async def test_member_updates(db: AsyncEngine) -> None:
    await _seed(db)
    await gd.record_heartbeat(db)
    info = _info(db)
    assert await gd.upsert_member(db, guild_id=GUILD, user_id=MEMBER, role_ids=[ADMIN_ROLE, ROLE_B])
    assert await info.is_administrator(GUILD, MEMBER)
    assert await info.has_role(GUILD, MEMBER, ROLE_B)
    assert not await info.has_role(GUILD, MEMBER, ROLE_A)
    assert await gd.upsert_member(db, guild_id=GUILD, user_id=STRANGER, role_ids=[])
    assert await info.has_role(GUILD, STRANGER, GUILD)
    assert not await gd.upsert_member(db, guild_id=OTHER_GUILD, user_id=MEMBER, role_ids=[ROLE_A])
    assert await gd.remove_member(db, guild_id=GUILD, user_id=MEMBER)
    assert not await gd.remove_member(db, guild_id=GUILD, user_id=MEMBER)
    assert not await gd.remove_member(db, guild_id=OTHER_GUILD, user_id=MEMBER)
    assert not await info.is_administrator(GUILD, MEMBER)
    assert not await info.has_role(GUILD, MEMBER, GUILD)
    async with db.connect() as conn:
        assert (await conn.execute(select(func.count()).select_from(guild_member_role).where(guild_member_role.c.user_id == MEMBER))).scalar_one() == 0


async def test_role_permission_changes_flip_many_members(db: AsyncEngine) -> None:
    members = [MemberSnapshot(user_id, frozenset({ROLE_A})) for user_id in range(1000, 1050)]
    await gd.replace_guild(db, guild_id=GUILD, owner_id=OWNER, administrator_role_ids=[], members=[*members, MemberSnapshot(MEMBER, frozenset({ROLE_B}))])
    await gd.record_heartbeat(db)
    info = _info(db)
    assert not await info.is_administrator(GUILD, 1000)
    assert await gd.set_role_administrator(db, guild_id=GUILD, role_id=ROLE_A, is_administrator=True)
    assert await gd.set_role_administrator(db, guild_id=GUILD, role_id=ROLE_A, is_administrator=True)
    assert all([await info.is_administrator(GUILD, member.user_id) for member in members])
    assert not await info.is_administrator(GUILD, MEMBER)
    assert await gd.set_role_administrator(db, guild_id=GUILD, role_id=ROLE_A, is_administrator=False)
    assert not any([await info.is_administrator(GUILD, member.user_id) for member in members])
    assert await gd.set_role_administrator(db, guild_id=GUILD, role_id=ROLE_A, is_administrator=True)
    assert await gd.remove_role(db, guild_id=GUILD, role_id=ROLE_A)
    assert not await info.is_administrator(GUILD, 1000)
    assert not await info.has_role(GUILD, 1000, ROLE_A)
    assert await info.has_role(GUILD, MEMBER, ROLE_B)
    assert not await gd.set_role_administrator(db, guild_id=OTHER_GUILD, role_id=ROLE_A, is_administrator=True)
    assert not await gd.remove_role(db, guild_id=OTHER_GUILD, role_id=ROLE_A)


async def test_parallel_writers_on_one_guild_never_deadlock(db: AsyncEngine, retries: list[int]) -> None:
    await _seed(db)
    rng = random.Random(5)  # noqa: S311
    users = list(range(2000, 2030))
    roles = [ROLE_A, ROLE_B, ADMIN_ROLE, 7100, 7101]

    def task(index: int) -> Any:
        kind = index % 7
        user = rng.choice(users)
        if kind == 0:
            members = [MemberSnapshot(u, frozenset(rng.sample(roles, 2))) for u in rng.sample(users, 20)]
            return gd.replace_guild(db, guild_id=GUILD, owner_id=rng.choice(users), administrator_role_ids=rng.sample(roles, 1), members=members)
        if kind == 1:
            return gd.upsert_member(db, guild_id=GUILD, user_id=user, role_ids=rng.sample(roles, 3))
        if kind == 2:
            return gd.remove_member(db, guild_id=GUILD, user_id=user)
        if kind == 3:
            return gd.set_role_administrator(db, guild_id=GUILD, role_id=rng.choice(roles), is_administrator=rng.random() < 0.5)
        if kind == 4:
            return gd.remove_role(db, guild_id=GUILD, role_id=rng.choice(roles))
        if kind == 5:
            return gd.upsert_guild(db, guild_id=GUILD, owner_id=user)
        return gd.record_heartbeat(db)

    await asyncio.gather(*(task(index) for index in range(140)))
    assert retries == []
    await gd.record_heartbeat(db)
    assert await _info(db).guild_owner(GUILD) in users


async def test_parallel_first_writers_of_new_guilds_succeed(db: AsyncEngine) -> None:
    guilds = [GUILD + offset for offset in range(4)]

    def task(index: int) -> Any:
        guild_id = guilds[index % len(guilds)]
        if index % 3 == 0:
            return gd.replace_guild(db, guild_id=guild_id, owner_id=OWNER, administrator_role_ids=[ROLE_A], members=[MemberSnapshot(MEMBER, frozenset({ROLE_A}))])
        if index % 3 == 1:
            return gd.upsert_guild(db, guild_id=guild_id, owner_id=OWNER)
        return gd.upsert_member(db, guild_id=guild_id, user_id=ADMIN, role_ids=[ROLE_B])

    await asyncio.gather(*(task(index) for index in range(60)))
    await gd.record_heartbeat(db)
    info = _info(db)
    assert [await info.guild_owner(guild_id) for guild_id in guilds] == [OWNER] * len(guilds)


async def test_parity_with_in_memory_guild_info(db: AsyncEngine) -> None:
    memory = InMemoryGuildInfo()
    directory = _info(db)
    await gd.record_heartbeat(db)
    users = (OWNER, ADMIN, MEMBER, NEW_OWNER, STRANGER)
    guilds = (GUILD, OTHER_GUILD)
    role_ids = (ADMIN_ROLE, ROLE_A, ROLE_B)

    async def compare(step: str) -> None:
        for guild_id in (*guilds, 9999):
            assert await directory.guild_owner(guild_id) == await memory.guild_owner(guild_id), (step, guild_id)
            for user_id in users:
                assert await directory.is_administrator(guild_id, user_id) == await memory.is_administrator(guild_id, user_id), (step, guild_id, user_id)
                for role_id in (*role_ids, guild_id):
                    assert await directory.has_role(guild_id, user_id, role_id) == await memory.has_role(guild_id, user_id, role_id), (step, guild_id, user_id, role_id)

    await compare("empty")
    await gd.replace_guild(
        db,
        guild_id=GUILD,
        owner_id=OWNER,
        administrator_role_ids=[ADMIN_ROLE],
        members=[MemberSnapshot(OWNER, frozenset({ROLE_B})), MemberSnapshot(ADMIN, frozenset({ADMIN_ROLE, ROLE_A})), MemberSnapshot(MEMBER, frozenset({ROLE_A, GUILD}))],
    )
    memory.add_guild(GUILD, OWNER)
    memory.set_role_administrator(GUILD, ADMIN_ROLE, True)
    memory.add_member(GUILD, OWNER, {ROLE_B})
    memory.add_member(GUILD, ADMIN, {ADMIN_ROLE, ROLE_A})
    memory.add_member(GUILD, MEMBER, {ROLE_A, GUILD})
    await gd.replace_guild(db, guild_id=OTHER_GUILD, owner_id=NEW_OWNER, administrator_role_ids=[], members=[MemberSnapshot(NEW_OWNER, frozenset({ROLE_A}))])
    memory.add_guild(OTHER_GUILD, NEW_OWNER)
    memory.add_member(OTHER_GUILD, NEW_OWNER, {ROLE_A})
    await compare("seeded")

    await gd.upsert_guild(db, guild_id=GUILD, owner_id=NEW_OWNER)
    memory.set_owner(GUILD, NEW_OWNER)
    await compare("owner changed")

    await gd.upsert_member(db, guild_id=GUILD, user_id=MEMBER, role_ids=[ADMIN_ROLE])
    memory.add_member(GUILD, MEMBER, {ADMIN_ROLE})
    await compare("member promoted")

    await gd.set_role_administrator(db, guild_id=GUILD, role_id=ROLE_A, is_administrator=True)
    memory.set_role_administrator(GUILD, ROLE_A, True)
    await compare("role A grants administrator")

    await gd.set_role_administrator(db, guild_id=GUILD, role_id=ADMIN_ROLE, is_administrator=False)
    memory.set_role_administrator(GUILD, ADMIN_ROLE, False)
    await compare("admin role demoted")

    await gd.set_role_administrator(db, guild_id=GUILD, role_id=GUILD, is_administrator=True)
    memory.set_role_administrator(GUILD, GUILD, True)
    await compare("@everyone grants administrator")

    await gd.set_role_administrator(db, guild_id=GUILD, role_id=GUILD, is_administrator=False)
    memory.set_role_administrator(GUILD, GUILD, False)
    await gd.remove_role(db, guild_id=GUILD, role_id=ROLE_A)
    memory.delete_role(GUILD, ROLE_A)
    await compare("role A deleted")

    await gd.remove_member(db, guild_id=GUILD, user_id=ADMIN)
    memory.remove_member(GUILD, ADMIN)
    await compare("admin left")

    await gd.upsert_member(db, guild_id=GUILD, user_id=STRANGER, role_ids=[ROLE_B])
    memory.add_member(GUILD, STRANGER, {ROLE_B})
    await compare("stranger joined")

    await gd.remove_guild(db, guild_id=OTHER_GUILD)
    memory.remove_guild(OTHER_GUILD)
    await compare("bot left the other guild")


async def test_dedicated_engine_answers_while_the_main_pool_is_exhausted(db: AsyncEngine) -> None:
    await _seed(db)
    await gd.record_heartbeat(db)
    main = create_engine(db.url, pool_size=1, max_overflow=0, pool_timeout=0.2)
    info = DbGuildInfo.open(main.url, Settings(_env_file=None, db_password="password-1234"))
    try:
        async with main.connect() as held:
            await held.execute(select(1))
            with pytest.raises(PoolTimeoutError):
                async with main.connect():
                    pass
            # Reads through the exhausted main pool would time out after its 0.2 s pool timeout and answer unknown, so correct answers show the reader uses its own engine.
            assert await info.has_role(GUILD, MEMBER, ROLE_A)
            assert await info.is_administrator(GUILD, ADMIN)
            assert await info.guild_owner(GUILD) == OWNER
    finally:
        await info.close()
        await main.dispose()


async def test_an_exhausted_reader_pool_answers_unknown(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    await _seed(db)
    await gd.record_heartbeat(db)
    monkeypatch.setattr(gd, "LOOKUP_WAIT_SECONDS", 0.3)
    info = DbGuildInfo.open(db.url, Settings(_env_file=None, db_password="password-1234"))
    try:
        assert await _answers(info) == (OWNER, True, False, True, True)
        async with AsyncExitStack() as stack:
            for _ in range(gd.READER_POOL_SIZE):
                await (await stack.enter_async_context(info.engine.connect())).execute(select(1))
            with caplog.at_level(logging.WARNING, logger="rapidwire.core.guild_directory"):
                # has_role() asks the pool once instead of waiting LOOKUP_WAIT_SECONDS, so it answers long before a lookup wait set far beyond the bound.
                monkeypatch.setattr(gd, "LOOKUP_WAIT_SECONDS", SLOW_LOOKUP_WAIT_SECONDS)
                assert await asyncio.wait_for(info.has_role(GUILD, MEMBER, ROLE_A), timeout=SLOW_LOOKUP_WAIT_SECONDS / 3) is False
                monkeypatch.setattr(gd, "LOOKUP_WAIT_SECONDS", 0.3)
                assert await info.guild_owner(GUILD) is None
                assert await info.is_administrator(GUILD, ADMIN) is False
                snapshot = await snapshot_guild(info, GUILD, OWNER)
                assert (snapshot.owner_id, snapshot.administrator) == (None, False)
                with pytest.raises(GuildUnavailable):
                    await cur.create_currency(db, info, guild_id=GUILD, user_id=OWNER, name="Gold", symbol="GLD", initial_supply=0)
            assert {record.getMessage() for record in caplog.records} == {"guild directory read answered unknown: no reader connection was free"}
            # A connection freed while guild_owner() waits is used.
            monkeypatch.setattr(gd, "LOOKUP_WAIT_SECONDS", SLOW_LOOKUP_WAIT_SECONDS)
            held = stack.pop_all()
            waiting = asyncio.ensure_future(info.guild_owner(GUILD))
            await asyncio.sleep(0.1)
            await held.aclose()
            assert await waiting == OWNER
        assert await _answers(info) == (OWNER, True, False, True, True)
    finally:
        await info.close()


async def test_database_errors_answer_unknown(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    await _seed(db)
    await gd.record_heartbeat(db)
    info = _info(db)

    async def failing(self: AsyncConnection, *args: object, **kwargs: object) -> object:
        raise OperationalError("SELECT guild_snapshot", {}, Exception(2013, "Lost connection"))

    with caplog.at_level(logging.WARNING, logger="rapidwire.core.guild_directory"), monkeypatch.context() as patch:
        patch.setattr(AsyncConnection, "execute", failing)
        assert await _answers(info) == (None, False, False, False, False)
    assert len(caplog.records) == 5
    assert {record.getMessage() for record in caplog.records} == {"guild directory read answered unknown after OperationalError"}
    assert await _answers(info) == (OWNER, True, False, True, True)


async def test_api_lifespan_opens_and_closes_its_own_directory(db: AsyncEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    closed: list[DbGuildInfo] = []
    close = DbGuildInfo.close

    async def recording_close(self: DbGuildInfo) -> None:
        closed.append(self)
        await close(self)

    monkeypatch.setattr(DbGuildInfo, "close", recording_close)
    app = create_app(Settings(_env_file=None, db_password="password-1234"), engine=db)
    async with app.router.lifespan_context(app):
        info = app.state.rapidwire.guild_info
        assert isinstance(info, DbGuildInfo)
        assert info.engine is not db
        assert info.engine.url == db.url
        await _seed(db)
        await gd.record_heartbeat(db)
        assert await info.has_role(GUILD, MEMBER, ROLE_A)
    assert closed == [info]
    async with db.connect() as conn:
        assert (await conn.execute(select(1))).scalar_one() == 1


async def test_snapshot_guild_drives_currency_management(db: AsyncEngine) -> None:
    await _seed(db)
    await gd.record_heartbeat(db)
    info = _info(db)
    snapshot = await snapshot_guild(info, GUILD, ADMIN)
    assert (snapshot.owner_id, snapshot.administrator, snapshot.is_owner) == (OWNER, True, False)
    created = await cur.create_currency(db, info, guild_id=GUILD, user_id=OWNER, name="Gold", symbol="GLD", initial_supply=0)
    assert created.currency_id == GUILD
    minted = await cur.mint(db, info, currency_id=GUILD, user_id=OWNER, amount=10)
    assert minted.amount == 10
    with pytest.raises(PermissionDenied):
        await cur.mint(db, info, currency_id=GUILD, user_id=ADMIN, amount=10)
    await cur.enable_delegation(db, info, currency_id=GUILD, user_id=OWNER)
    assert (await cur.mint(db, info, currency_id=GUILD, user_id=ADMIN, amount=5)).amount == 5
    with pytest.raises(PermissionDenied):
        await cur.mint(db, info, currency_id=GUILD, user_id=MEMBER, amount=5)
    await _heartbeat_aged(db, STALENESS * 2)
    with pytest.raises(GuildUnavailable):
        await cur.mint(db, info, currency_id=GUILD, user_id=OWNER, amount=10)
    await gd.record_heartbeat(db)
    await gd.remove_guild(db, guild_id=GUILD)
    with pytest.raises(GuildUnavailable):
        await cur.mint(db, info, currency_id=GUILD, user_id=OWNER, amount=10)


async def _expect_rejected(engine: AsyncEngine, table: Any, values: dict[str, Any], match: str) -> None:
    async with engine.connect() as conn:
        with pytest.raises(DBAPIError, match=match):
            await conn.execute(insert(table).values(**values))
        await conn.rollback()


async def test_schema_checks(db: AsyncEngine) -> None:
    async with db.connect() as conn:
        now = await database_now(conn)
    await _expect_rejected(db, guild_directory_state, {"state_id": 2, "bot_heartbeat_at": now}, "ck_guild_directory_state_single_row")
    await _expect_rejected(db, guild_directory_state, {"state_id": 1, "bot_user_id": 0}, "ck_guild_directory_state_bot_user_id_positive")
    await _expect_rejected(db, guild_snapshot, {"guild_id": 0, "owner_id": OWNER, "updated_at": now}, "ck_guild_snapshot_guild_id_positive")
    await _expect_rejected(db, guild_snapshot, {"guild_id": GUILD, "owner_id": -1, "updated_at": now}, "ck_guild_snapshot_owner_id_positive")
    await _expect_rejected(db, guild_member_snapshot, {"guild_id": GUILD, "user_id": MEMBER, "updated_at": now}, "fk_guild_member_snapshot_guild_id_guild_snapshot")
    await gd.upsert_guild(db, guild_id=GUILD, owner_id=OWNER)
    await _expect_rejected(db, guild_member_snapshot, {"guild_id": GUILD, "user_id": 0, "updated_at": now}, "ck_guild_member_snapshot_user_id_positive")
    await _expect_rejected(db, guild_administrator_role, {"guild_id": GUILD, "role_id": 0, "updated_at": now}, "ck_guild_administrator_role_role_id_positive")
    await _expect_rejected(db, guild_member_role, {"guild_id": GUILD, "user_id": MEMBER, "role_id": ROLE_A}, "fk_guild_member_role_guild_id_guild_member_snapshot")
    await gd.upsert_member(db, guild_id=GUILD, user_id=MEMBER, role_ids=[])
    await _expect_rejected(db, guild_member_role, {"guild_id": GUILD, "user_id": MEMBER, "role_id": 0}, "ck_guild_member_role_role_id_positive")


def _downgrade_on(connection: Connection, revision: str) -> None:
    command.downgrade(make_config(connection=connection), revision)


def _upgrade_on(connection: Connection, revision: str) -> None:
    command.upgrade(make_config(connection=connection), revision)


async def test_guild_directory_migration_downgrades_and_upgrades(db: AsyncEngine) -> None:
    names = {table.name for table in DIRECTORY_TABLES}

    async def tables() -> set[str]:
        async with db.connect() as conn:
            return set((await conn.execute(text("SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE()"))).scalars().all())

    async with db.begin() as conn:
        await conn.run_sync(_downgrade_on, "0004")
    present = await tables()
    assert not present & names
    assert "contract" in present
    async with db.begin() as conn:
        await conn.run_sync(_upgrade_on, "head")
    assert names <= await tables()
    async with db.connect() as conn:
        assert (await conn.execute(text("SELECT version_num FROM alembic_version"))).scalar_one() == head_revision() == "0007"
    await _seed(db)
