from __future__ import annotations

import pytest

from rapidwire.db.engine import INIT_COMMAND, LOCK_WAIT_TIMEOUT_SECONDS, create_engine, create_sessionmaker, init_command


def test_init_command_sets_lock_wait_timeout() -> None:
    assert f"SESSION innodb_lock_wait_timeout = {LOCK_WAIT_TIMEOUT_SECONDS}" in INIT_COMMAND
    assert 1 <= LOCK_WAIT_TIMEOUT_SECONDS <= 10
    assert init_command(2).endswith("SESSION innodb_lock_wait_timeout = 2")
    for bad in (0, -1, True):
        with pytest.raises(ValueError, match="lock_wait_timeout"):
            init_command(bad)


async def test_engine_pool_defaults_and_override() -> None:
    engine = create_engine("mysql+asyncmy://u:p@127.0.0.1:1/db?charset=utf8mb4")
    overridden = create_engine("mysql+asyncmy://u:p@127.0.0.1:1/db", pool_recycle=10)
    try:
        assert engine.pool._pre_ping is True
        assert engine.pool._recycle == 3600
        assert overridden.pool._recycle == 10
        assert create_sessionmaker(engine).kw["expire_on_commit"] is False
    finally:
        await engine.dispose()
        await overridden.dispose()
