from __future__ import annotations

import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from rapidwire import __version__, cli
from rapidwire.cli import COMMANDS, EXIT_CONFIG, EXIT_INVARIANT_VIOLATION, EXIT_USAGE, HELP, main
from rapidwire.core.invariants import INVARIANTS_SUMMARY

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_command_tables_are_consistent() -> None:
    assert set(COMMANDS) == set(HELP)
    assert set(HELP) == {"bot", "api", "worker", "migrate", "backup", "verify-invariants"}
    assert EXIT_CONFIG == 78


def test_exit_codes_are_the_documented_set() -> None:
    assert {name for name in vars(cli) if name.startswith("EXIT_")} == {"EXIT_OK", "EXIT_FAILURE", "EXIT_USAGE", "EXIT_INVARIANT_VIOLATION", "EXIT_CONFIG"}


def test_unknown_variable_exits_78(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "test-password")
    monkeypatch.setenv("RAPIDWIRE_DB_HOTS", "typo")
    assert main(["migrate"]) == EXIT_CONFIG
    assert "unknown settings variables: RAPIDWIRE_DB_HOTS" in capsys.readouterr().err


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == f"rapidwire {__version__}"


def test_missing_command_is_usage_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == EXIT_USAGE
    assert "usage: rapidwire" in capsys.readouterr().err


def test_missing_password_exits_78(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["migrate"]) == EXIT_CONFIG
    err = capsys.readouterr().err
    assert "Invalid configuration:" in err
    assert "RAPIDWIRE_DB_PASSWORD" in err


def test_invalid_config_does_not_leak_secret(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "leaky-password-123")
    monkeypatch.setenv("RAPIDWIRE_DISCORD_TOKEN", "leaky-token-456")
    monkeypatch.setenv("RAPIDWIRE_DB_PORT", "99999")
    monkeypatch.setenv("RAPIDWIRE_OPERATOR_ACCOUNT_IDS", "leaky-password-123")
    assert main(["migrate"]) == EXIT_CONFIG
    captured = capsys.readouterr()
    assert "RAPIDWIRE_DB_PORT" in captured.err
    assert "RAPIDWIRE_OPERATOR_ACCOUNT_IDS" in captured.err
    for text in (captured.out, captured.err):
        assert "leaky-password-123" not in text
        assert "leaky-token-456" not in text


def test_cross_field_config_error_exits_78(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "test-password")
    monkeypatch.setenv("RAPIDWIRE_DEFAULT_GAS", "500")
    monkeypatch.setenv("RAPIDWIRE_MAX_GAS", "10")
    assert main(["migrate"]) == EXIT_CONFIG
    assert "default_gas must not exceed max_gas" in capsys.readouterr().err


def test_migrate_failure_logs_without_secret(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "unreachable-pw-789")
    monkeypatch.setenv("MYSQL_ROOT_PASSWORD", "compose-root-pw")
    monkeypatch.setenv("RAPIDWIRE_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("RAPIDWIRE_DB_PORT", "1")
    assert main(["migrate"]) == 1
    captured = capsys.readouterr()
    records = [json.loads(line) for line in captured.out.splitlines()]
    assert records[-1]["msg"] == "command failed"
    assert records[-1]["command"] == "migrate"
    assert "exc_info" in records[-1]
    assert "unreachable-pw-789" not in captured.out + captured.err
    assert logging.getLogger("alembic.runtime.plugins").getEffectiveLevel() >= logging.WARNING


def test_verify_invariants_is_implemented() -> None:
    assert "verify-invariants" in COMMANDS


def test_verify_invariants_needs_config(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["verify-invariants"]) == EXIT_CONFIG
    assert "RAPIDWIRE_DB_PASSWORD" in capsys.readouterr().err


def test_verify_invariants_unreachable_database_exits_1(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "unreachable-pw-789")
    monkeypatch.setenv("RAPIDWIRE_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("RAPIDWIRE_DB_PORT", "1")
    assert main(["verify-invariants"]) == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    records = [json.loads(line) for line in captured.err.splitlines()]
    assert records[-1]["msg"] == "command failed"
    assert records[-1]["command"] == "verify-invariants"
    assert "unreachable-pw-789" not in captured.out + captured.err


def test_api_builds_the_app_and_serves_it(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import uvicorn
    from fastapi import FastAPI

    served: list[uvicorn.Config] = []

    async def fake_serve(self: uvicorn.Server, sockets: object = None) -> None:
        served.append(self.config)
        self.started = True

    monkeypatch.setattr(uvicorn.Server, "serve", fake_serve)
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "api-test-password")
    monkeypatch.setenv("RAPIDWIRE_API_HOST", "127.0.0.1")
    monkeypatch.setenv("RAPIDWIRE_API_PORT", "8123")
    assert "api" in COMMANDS
    assert main(["api"]) == 0
    [config] = served
    assert isinstance(config.app, FastAPI)
    assert (config.host, config.port, config.log_config, config.proxy_headers, config.lifespan) == ("127.0.0.1", 8123, None, True, "on")
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [record["msg"] for record in records] == ["starting API", "API stopped"]


def test_api_that_fails_to_start_exits_1(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    async def failed_serve(self: uvicorn.Server, sockets: object = None) -> None:
        self.started = False

    monkeypatch.setattr(uvicorn.Server, "serve", failed_serve)
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "api-test-password")
    assert main(["api"]) == 1


def test_api_config_error_exits_78(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["api"]) == EXIT_CONFIG
    assert "RAPIDWIRE_DB_PASSWORD" in capsys.readouterr().err
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "api-test-password")
    monkeypatch.setenv("RAPIDWIRE_API_PORT", "0")
    assert main(["api"]) == EXIT_CONFIG
    assert "RAPIDWIRE_API_PORT" in capsys.readouterr().err


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def _wait_for_port(process: subprocess.Popen[str], port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError(f"the API exited early with {process.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            time.sleep(0.1)
    raise AssertionError("the API did not start listening")


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_api_shuts_down_gracefully_on_a_signal(tmp_path: Path, sig: signal.Signals) -> None:
    port = _free_port()
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAPIDWIRE_")}
    # The app opens no database connection until a request needs one, so a closed port stands in for the database.
    env.update(RAPIDWIRE_DB_PASSWORD="api-test-password", RAPIDWIRE_DB_HOST="127.0.0.1", RAPIDWIRE_DB_PORT="1", RAPIDWIRE_API_HOST="127.0.0.1", RAPIDWIRE_API_PORT=str(port))
    process = subprocess.Popen([sys.executable, "-m", "rapidwire", "api"], cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        _wait_for_port(process, port, timeout=30)
        process.send_signal(sig)
        output, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, output
    messages = [json.loads(line)["msg"] for line in output.splitlines() if line.startswith("{")]
    assert messages[0] == "starting API"
    assert messages[-1] == "API stopped"
    assert "Application shutdown complete." in messages


def test_bot_is_implemented() -> None:
    assert "bot" in COMMANDS


def test_bot_needs_a_password_and_a_token(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["bot"]) == EXIT_CONFIG
    assert "RAPIDWIRE_DB_PASSWORD" in capsys.readouterr().err
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "bot-test-password")
    assert main(["bot"]) == EXIT_CONFIG
    assert "RAPIDWIRE_DISCORD_TOKEN is required" in capsys.readouterr().err


def test_bot_list_commands_needs_no_token_or_password(capsys: pytest.CaptureFixture[str]) -> None:
    from rapidwire.bot.app import build_bot, describe_commands
    from rapidwire.config import Settings

    assert main(["bot", "--list-commands"]) == 0
    captured = capsys.readouterr()
    expected = describe_commands(build_bot(Settings(_env_file=None, db_password="unused-password")).tree)
    assert captured.out.splitlines() == expected
    assert all(line.startswith("/") or line.startswith("[") for line in expected)
    assert all(not line.startswith("{") for line in captured.out.splitlines())


def test_list_commands_is_only_a_bot_option(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["api", "--list-commands"]) == EXIT_USAGE


_FAKE_BOT_DRIVER = """
import asyncio, pathlib, sys
from rapidwire.bot.client import RapidWireBot

async def start(self, token, *, reconnect=True):
    pathlib.Path(sys.argv[1]).write_text(token)
    while not self.is_closed():
        await asyncio.sleep(0.05)

RapidWireBot.start = start
from rapidwire.cli import main
sys.exit(main(["bot"]))
"""


@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
def test_bot_shuts_down_gracefully_on_a_signal(tmp_path: Path, sig: signal.Signals) -> None:
    started = tmp_path / "started"
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAPIDWIRE_")}
    # The Discord connection is replaced by a loop that runs until the client closes, and nothing opens the database, so a closed port stands in for it.
    env.update(RAPIDWIRE_DB_PASSWORD="bot-test-password", RAPIDWIRE_DB_HOST="127.0.0.1", RAPIDWIRE_DB_PORT="1", RAPIDWIRE_DISCORD_TOKEN="fake-discord-token")
    process = subprocess.Popen([sys.executable, "-c", _FAKE_BOT_DRIVER, str(started)], cwd=tmp_path, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        deadline = time.monotonic() + 30
        while not started.exists():
            assert process.poll() is None, f"the bot exited early with {process.returncode}"
            assert time.monotonic() < deadline, "the bot did not start"
            time.sleep(0.1)
        process.send_signal(sig)
        output, _ = process.communicate(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
    assert process.returncode == 0, output
    assert started.read_text() == "fake-discord-token"
    messages = [json.loads(line)["msg"] for line in output.splitlines() if line.startswith("{")]
    assert messages[0] == "starting bot"
    assert "stopping bot" in messages
    assert messages[-1] == "bot stopped"
    assert "fake-discord-token" not in output


def _bot_env(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    from sqlalchemy.ext.asyncio import AsyncEngine

    events: list[str] = []

    async def dispose(self: AsyncEngine, close: bool = True) -> None:
        events.append("dispose")

    monkeypatch.setattr(AsyncEngine, "dispose", dispose)
    # Shutdown hooks may reach for the database, so a closed local port stands in for it and refuses at once.
    monkeypatch.setenv("RAPIDWIRE_DB_HOST", "127.0.0.1")
    monkeypatch.setenv("RAPIDWIRE_DB_PORT", "1")
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "bot-test-password")
    monkeypatch.setenv("RAPIDWIRE_DISCORD_TOKEN", "fake-discord-token")
    return events


def test_bot_that_fails_to_start_exits_1_and_disposes_the_engine(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from rapidwire.bot.client import RapidWireBot

    events = _bot_env(monkeypatch)

    async def failing_start(self: RapidWireBot, token: str, *, reconnect: bool = True) -> None:
        events.append("start")
        raise RuntimeError("login failed")

    monkeypatch.setattr(RapidWireBot, "start", failing_start)
    assert main(["bot"]) == 1
    assert events == ["start", "dispose"]
    records = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert records[-1]["msg"] == "command failed"
    assert records[-1]["command"] == "bot"
    assert "fake-discord-token" not in json.dumps(records)


def test_run_bot_attaches_no_handler_to_the_discord_logger(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from rapidwire.bot.client import RapidWireBot

    events = _bot_env(monkeypatch)

    async def quick_start(self: RapidWireBot, token: str, *, reconnect: bool = True) -> None:
        events.append("start")

    monkeypatch.setattr(RapidWireBot, "start", quick_start)
    assert main(["bot"]) == 0
    assert events == ["start", "dispose"]
    # discord.py's own package import adds only a NullHandler, so its records reach the root JSON handler.
    for name in ("discord", "discord.client", "discord.gateway", "discord.http", "discord.app_commands.tree"):
        logger = logging.getLogger(name)
        assert all(type(handler) is logging.NullHandler for handler in logger.handlers), (name, logger.handlers)
        assert logger.propagate
    assert len(logging.getLogger().handlers) == 1
    messages = [json.loads(line)["msg"] for line in capsys.readouterr().out.splitlines()]
    assert messages[0] == "starting bot"
    assert messages[-1] == "bot stopped"


def test_invariant_violation_has_its_own_exit_code() -> None:
    assert EXIT_INVARIANT_VIOLATION == 4
    assert len({0, 1, EXIT_USAGE, EXIT_INVARIANT_VIOLATION, EXIT_CONFIG}) == 5
    assert "4 when a violation" in HELP["verify-invariants"]


def test_verify_invariants_help_states_the_verifier_summary() -> None:
    assert INVARIANTS_SUMMARY in HELP["verify-invariants"]


def test_python_dash_m_entry_point() -> None:
    result = subprocess.run([sys.executable, "-m", "rapidwire", "--version"], capture_output=True, text=True, check=False)
    assert result.returncode == 0
    assert result.stdout.strip() == f"rapidwire {__version__}"


def test_alembic_ini_offline_sql(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("RAPIDWIRE_")}
    env["RAPIDWIRE_DB_PASSWORD"] = "offline-secret-pw"
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", str(REPO_ROOT / "alembic.ini"), "upgrade", "head", "--sql"],
        capture_output=True,
        text=True,
        check=False,
        cwd=tmp_path,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert "ALTER DATABASE CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_bin" in result.stdout
    assert "INSERT INTO alembic_version (version_num) VALUES ('0001')" in result.stdout
    assert "CREATE TABLE execution" in result.stdout
    assert "INSERT INTO account (account_id, kind, currency_id, pool_id, created_at) VALUES (-1, 'stake_custody'" in result.stdout
    assert "UPDATE alembic_version SET version_num='0002'" in result.stdout
    assert "offline-secret-pw" not in result.stdout + result.stderr
