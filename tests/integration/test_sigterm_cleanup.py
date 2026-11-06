from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROBE_MARKER_ENV = "RAPIDWIRE_TEST_SIGTERM_PROBE_MARKER"
LABEL_FILTER = "label=rapidwire-test=1"


def test_sigterm_probe(request: pytest.FixtureRequest) -> None:
    """Child-process half of test_sigterm_removes_container: starts the MySQL fixture, signals readiness, then waits to be terminated."""
    marker = os.environ.get(PROBE_MARKER_ENV)
    if not marker:
        pytest.skip("runs only as the child process of test_sigterm_removes_container")
    request.getfixturevalue("mysql_url")
    Path(marker).write_text("ready")
    time.sleep(300)


def _labelled_containers() -> set[str]:
    result = subprocess.run(["docker", "ps", "-aq", "--filter", LABEL_FILTER], capture_output=True, text=True, check=True, timeout=60)
    return set(result.stdout.split())


@pytest.mark.skipif(bool(os.environ.get("RAPIDWIRE_TEST_DB_URL")) or shutil.which("docker") is None, reason="needs the docker-managed MySQL fixture")
def test_sigterm_removes_container(tmp_path: Path) -> None:
    marker = tmp_path / "ready"
    before = _labelled_containers()
    env = {**os.environ, PROBE_MARKER_ENV: str(marker)}
    child = subprocess.Popen(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"{Path(__file__).relative_to(REPO_ROOT)}::test_sigterm_probe"],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.monotonic() + 240
        while not marker.exists():
            assert child.poll() is None, f"child pytest exited early:\n{child.communicate()[0]}"
            assert time.monotonic() < deadline, "child pytest did not reach the probe test in time"
            time.sleep(0.5)
        started = _labelled_containers() - before
        assert len(started) == 1
        child.send_signal(signal.SIGTERM)
        output, _ = child.communicate(timeout=120)
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate()
    assert child.returncode != 0, output
    assert not (started & _labelled_containers()), f"container left behind after SIGTERM:\n{output}"
