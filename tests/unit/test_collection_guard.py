from __future__ import annotations

import os
import socket
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from tests import conftest


class _Item(SimpleNamespace):
    def add_marker(self, marker: Any) -> None:
        self.markers.append(marker)


def _item(path: Path, *fixtures: str) -> Any:
    return _Item(path=path, fixturenames=list(fixtures), nodeid=str(path), markers=[])


def test_unit_item_requesting_db_fixture_is_rejected() -> None:
    item = _item(Path(__file__), "tmp_path", "engine")
    with pytest.raises(pytest.UsageError, match=r"uses database fixtures \['engine'\]"):
        conftest.pytest_collection_modifyitems(cast(pytest.Config, None), [item])


@pytest.mark.parametrize("raw", ["mysql+asyncmy://u:p@h/rapidwire", "mysql+asyncmy://u:p@h/", "mysql+asyncmy://u:p@h/test_db"])
def test_external_url_must_name_test_database(raw: str) -> None:
    with pytest.raises(pytest.UsageError, match="ending in '_test'"):
        conftest.external_test_url(raw)


def test_external_url_accepts_test_database() -> None:
    assert conftest.external_test_url("mysql+asyncmy://u:p@h:3306/rapidwire_test").database == "rapidwire_test"


def test_container_removed_when_readiness_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str, check: bool = True, timeout: float = 0.0) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        stdout = "127.0.0.1:40000\n" if args[0] == "port" else ""
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout=stdout, stderr="")

    def never_ready(name: str, url: Any) -> None:
        pytest.fail(f"{name} was not ready")

    monkeypatch.setattr(conftest, "_docker", fake_docker)
    monkeypatch.setattr(conftest, "_wait_for_mysql", never_ready)
    with pytest.raises(pytest.fail.Exception, match="was not ready"), conftest.mysql_container():
        pass
    run = next(c for c in calls if c[0] == "run")
    name = run[run.index("--name") + 1]
    assert ("rm", "--force", "--volumes", name) in calls
    assert name not in conftest._started_containers
    assert run[-len(conftest.HOSTILE_SERVER_ARGS) :] == conftest.HOSTILE_SERVER_ARGS
    assert f"{conftest.PID_LABEL}={os.getpid()}" in run
    assert f"{conftest.HOST_LABEL}={socket.gethostname()}" in run


def test_sweep_removes_only_dead_local_owners(monkeypatch: pytest.MonkeyPatch) -> None:
    host = socket.gethostname()
    listing = "\n".join(
        [
            f"dead-local\t111\t{host}",
            f"live-local\t222\t{host}",
            "dead-remote\t111\tother-host",
            f"no-pid\t\t{host}",
        ]
    )
    calls: list[tuple[str, ...]] = []

    def fake_docker(*args: str, check: bool = True, timeout: float = 0.0) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        stdout = listing if args[0] == "ps" else ""
        return subprocess.CompletedProcess(["docker", *args], returncode=0, stdout=stdout, stderr="")

    monkeypatch.setattr(conftest, "_docker", fake_docker)
    monkeypatch.setattr(conftest, "_pid_alive", lambda pid: pid == 222)
    assert conftest.sweep_orphan_containers() == ["dead-local"]
    assert [c for c in calls if c[0] == "rm"] == [("rm", "--force", "--volumes", "dead-local")]
    ps = next(c for c in calls if c[0] == "ps")
    assert f"label={conftest.CONTAINER_LABEL}" in ps


def test_pid_alive() -> None:
    assert conftest._pid_alive(os.getpid())
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert not conftest._pid_alive(child.pid)


def test_integration_items_are_marked() -> None:
    item = _item(conftest.INTEGRATION_DIR / "test_x.py", "db")
    plain = _item(Path(__file__), "tmp_path")
    conftest.pytest_collection_modifyitems(cast(pytest.Config, None), [item, plain])
    assert [m.name for m in item.markers] == ["integration"]
    assert plain.markers == []
