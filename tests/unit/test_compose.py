"""compose.yaml deployment rules: data lives only in named volumes, only MySQL publishes a port and only on loopback, and the backups volume is read-only for the API and read-write for the processes that write backups."""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Any, cast

import pytest
import yaml  # type: ignore[import-untyped]

COMPOSE = Path(__file__).resolve().parents[2] / "compose.yaml"
BACKUP_DIR = "/var/lib/rapidwire/backups"


@cache
def compose() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(COMPOSE.read_text(encoding="utf-8")))


def mounts(service: dict[str, Any]) -> list[tuple[str, str, bool]]:
    """Every volume entry of a service as (source, target, read_only), from the short or the long syntax."""
    found: list[tuple[str, str, bool]] = []
    for entry in service.get("volumes", []):
        if isinstance(entry, str):
            parts = entry.split(":")
            assert len(parts) in {2, 3}, f"volume entry {entry!r} needs a source and a target"
            found.append((parts[0], parts[1], len(parts) == 3 and "ro" in parts[2].split(",")))
        else:
            assert entry.get("type", "volume") == "volume", f"volume entry {entry!r} is not a named volume"
            found.append((entry["source"], entry["target"], bool(entry.get("read_only", False))))
    return found


def backups_mount(name: str) -> tuple[str, str, bool]:
    [mount] = [mount for mount in mounts(compose()["services"][name]) if mount[0] == "backups"]
    return mount


def test_every_mount_is_a_declared_named_volume() -> None:
    declared = set(compose()["volumes"])
    assert declared
    used: set[str] = set()
    for name, service in compose()["services"].items():
        for source, target, _ in mounts(service):
            assert not source.startswith((".", "/", "~", "$")), f"{name} bind-mounts the host path {source!r}"
            assert source in declared, f"{name} mounts {source!r}, which is not a top-level named volume"
            assert target.startswith("/"), f"{name} mounts {source!r} at a relative target"
            used.add(source)
    assert used == declared


def test_only_mysql_publishes_ports_and_only_on_loopback() -> None:
    services = compose()["services"]
    assert {name for name, service in services.items() if service.get("ports")} == {"mysql"}
    for port in services["mysql"]["ports"]:
        assert isinstance(port, str) and port.startswith("127.0.0.1:"), port
    for name, service in services.items():
        assert service.get("network_mode") != "host", name


def test_api_mounts_backups_read_only() -> None:
    assert backups_mount("api") == ("backups", BACKUP_DIR, True)


@pytest.mark.parametrize("name", ["worker", "backup"])
def test_backup_writers_mount_backups_read_write(name: str) -> None:
    assert backups_mount(name) == ("backups", BACKUP_DIR, False)


def test_mysql_data_is_only_on_mysql() -> None:
    holders = {name for name, service in compose()["services"].items() if any(source == "mysql-data" for source, _, _ in mounts(service))}
    assert holders == {"mysql"}
