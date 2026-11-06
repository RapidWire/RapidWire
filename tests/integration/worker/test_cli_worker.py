"""The rapidwire worker command as a subprocess against the test database: it takes its job locks, and SIGTERM or SIGINT stops it with exit code 0, released locks and "worker stopped" as its last log line."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import URL, select
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.db.tables import job_lock
from rapidwire.worker.app import OUTBOX_JOB, STAKE_ACCRUAL_JOB
from rapidwire.worker.scheduler import SHUTDOWN_CANCEL_SECONDS, SHUTDOWN_DRAIN_SECONDS

TOKEN = "fake-worker-token-for-cli"


async def _locks(engine: AsyncEngine) -> set[str]:
    async with engine.connect() as conn:
        return set((await conn.execute(select(job_lock.c.name))).scalars())


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
async def test_worker_stops_gracefully_on_a_signal(db: AsyncEngine, migrated_url: URL, tmp_path: Path, sig: signal.Signals) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAPIDWIRE_")}
    env.update(
        RAPIDWIRE_DB_HOST=str(migrated_url.host),
        RAPIDWIRE_DB_PORT=str(migrated_url.port),
        RAPIDWIRE_DB_NAME=str(migrated_url.database),
        RAPIDWIRE_DB_USER=str(migrated_url.username),
        RAPIDWIRE_DB_PASSWORD=str(migrated_url.password),
        RAPIDWIRE_DISCORD_TOKEN=TOKEN,
        RAPIDWIRE_BACKUP_DIR=str(tmp_path / "backups"),
    )
    # Nothing is queued in the outbox, so the worker never contacts Discord.
    process: subprocess.Popen[str] = await asyncio.to_thread(
        subprocess.Popen, [sys.executable, "-m", "rapidwire", "worker"], cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    try:
        deadline = time.monotonic() + 30
        while not {OUTBOX_JOB, STAKE_ACCRUAL_JOB} <= await _locks(db):
            assert process.poll() is None, f"the worker exited early with {process.returncode}: {process.communicate()[0]}"
            assert time.monotonic() < deadline, "the worker did not take its job locks"
            await asyncio.sleep(0.1)
        process.send_signal(sig)
        began = time.monotonic()
        output, _ = await asyncio.to_thread(process.communicate, timeout=60)
        elapsed = time.monotonic() - began
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, output
    assert elapsed < SHUTDOWN_DRAIN_SECONDS + SHUTDOWN_CANCEL_SECONDS + 5
    messages = [json.loads(line)["msg"] for line in output.splitlines() if line.startswith("{")]
    assert messages[0] == "starting worker"
    assert "stopping worker" in messages
    assert messages[-1] == "worker stopped"
    assert TOKEN not in output
    assert await _locks(db) == set()
