"""document/operations.md pinned to the code: its rapidwire command lines parse with the real CLI parser, its docker compose commands name real services, its settings and defaults are real, and its numbers come from compose.yaml and the worker and backup modules."""

from __future__ import annotations

import asyncio
import inspect
import re
import shlex
from datetime import timedelta
from functools import cache
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml  # type: ignore[import-untyped]
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncEngine

from rapidwire.backup.cli import OPERATOR_COMMANDS
from rapidwire.backup.jobs import BACKUP_JOB_NAME, BACKUP_LEASE_TTL
from rapidwire.backup.naming import BackupKind, parse_backup_name
from rapidwire.bot.client import bot_intents
from rapidwire.cli import EXIT_CONFIG, EXIT_FAILURE, EXIT_INVARIANT_VIOLATION, EXIT_OK, EXIT_USAGE, HELP, build_parser
from rapidwire.config import Settings
from rapidwire.core.joblock import LeaseLost, LeaseUnavailable
from rapidwire.worker import dispatcher as dispatcher_module
from rapidwire.worker.app import BACKUP_CHECK_JOB, OUTBOX_JOB, STAKE_ACCRUAL_JOB, build_jobs
from rapidwire.worker.dispatcher import AUTH_FAILURE_PAUSE_SECONDS, MAX_DELIVERY_ATTEMPTS, RETRY_BASE_SECONDS, RETRY_MAX_SECONDS, Dispatcher
from rapidwire.worker.jobs import BACKUP_CHECK_INTERVAL_SECONDS, BACKUP_LEASE_RENEW_INTERVAL, BACKUP_RETRY_DELAY, default_backup_functions
from rapidwire.worker.scheduler import SHUTDOWN_CANCEL_SECONDS, SHUTDOWN_DRAIN_SECONDS
from tests.unit.backup.test_doc_backups import backup_command_exit
from tests.unit.doc_text import character_violations, heading_anchors

REPO = Path(__file__).resolve().parents[2]
DOCS = REPO / "document"
DOC = DOCS / "operations.md"
COMPOSE = REPO / "compose.yaml"
APP_IMAGE = "rapidwire:local"
# The database name rule as both operations.md and backups.md state it; the Settings tests below pin its meaning.
DB_NAME_RULE = "ASCII 英字、数字、`_` からなる 1 から 64 文字で、数字だけではない"

# Compose subcommands whose first non-option argument is one service, and those whose non-option arguments are all services.
ONE_SERVICE = frozenset({"run", "exec"})
SERVICES = frozenset({"up", "down", "logs", "stop", "start", "restart", "build", "ps", "rm", "pull"})
SHELL_SEPARATORS = frozenset({"|", "||", "&&", ";"})


@cache
def doc() -> str:
    return DOC.read_text(encoding="utf-8")


@cache
def compose() -> dict[str, Any]:
    return cast(dict[str, Any], yaml.safe_load(COMPOSE.read_text(encoding="utf-8")))


def code_lines(text: str) -> list[str]:
    """Every line of a shell or plain fenced block and every inline code span outside fenced blocks."""
    found: list[str] = []
    fence: str | None = None
    for line in text.splitlines():
        if line.startswith("```"):
            fence = line[3:].strip() if fence is None else None
            continue
        if fence is not None:
            if fence not in {"mermaid", "python"}:
                found.append(line)
            continue
        found.extend(re.findall(r"`([^`\n]+)`", line))
    return found


def shell_words(line: str) -> list[str]:
    words: list[str] = []
    for word in shlex.split(line):
        if word in SHELL_SEPARATORS or word.startswith("#"):
            break
        words.append(word)
    return words


def rapidwire_invocations() -> list[tuple[str, list[str]]]:
    """The arguments after every rapidwire command in the document: a bare rapidwire word, or the image name of a docker run line."""
    invocations: list[tuple[str, list[str]]] = []
    for line in code_lines(doc()):
        if not re.search(rf"(?:^|\s)(?:rapidwire|{re.escape(APP_IMAGE)})(?:\s|$)", line):
            continue
        words = shell_words(line)
        if words[:2] == ["docker", "run"] and APP_IMAGE in words:
            args = words[words.index(APP_IMAGE) + 1 :]
        elif "rapidwire" in words:
            args = words[words.index("rapidwire") + 1 :]
        else:
            continue
        if args:
            invocations.append((line, args))
    return invocations


def compose_invocations() -> list[tuple[str, str, list[str]]]:
    """Every docker compose command in the document as (line, subcommand, the services it names)."""
    invocations: list[tuple[str, str, list[str]]] = []
    for line in code_lines(doc()):
        if "docker compose" not in line:
            continue
        words = shell_words(line)
        start = next(index for index in range(len(words) - 1) if words[index : index + 2] == ["docker", "compose"])
        subcommand, rest = words[start + 2], words[start + 3 :]
        positional = [word for word in rest if not word.startswith("-")]
        if subcommand in ONE_SERVICE:
            services = positional[:1]
        elif subcommand in SERVICES:
            services = positional
        else:
            pytest.fail(f"{line!r} uses docker compose {subcommand}, which this test does not know")
        invocations.append((line, subcommand, services))
    return invocations


def section(heading: str) -> str:
    text = doc()
    start = text.index(f"\n{heading}\n")
    end = text.find("\n## ", start + len(heading) + 2)
    return text[start : end if end != -1 else len(text)]


def table_rows(text: str) -> list[list[str]]:
    rows = [[cell.strip() for cell in line.strip().strip("|").split("|")] for line in text.splitlines() if line.startswith("| ")]
    return [row for row in rows if not all(set(cell) <= {"-"} for cell in row)][1:]


def tables(text: str) -> list[list[list[str]]]:
    """Every Markdown table in text as its body rows, header and separator left out."""
    found: list[list[list[str]]] = []
    current: list[str] = []
    for line in [*text.splitlines(), ""]:
        if line.startswith("| "):
            current.append(line)
        elif current:
            found.append(table_rows("\n".join(current)))
            current = []
    return found


def seconds(value: str) -> float:
    match = re.fullmatch(r"([0-9]+)(s|m)", value)
    assert match is not None, f"unexpected duration {value!r}"
    return float(match[1]) * (60 if match[2] == "m" else 1)


def lease_words(ttl: timedelta) -> str:
    total = int(ttl.total_seconds())
    return f"{total // 60} 分" if total % 60 == 0 and total >= 300 else f"{total} 秒"


def test_the_document_is_ascii_outside_japanese_text() -> None:
    assert character_violations(DOC) == []


def is_command_name(args: list[str]) -> bool:
    """Whether args only name a command or a backup subcommand, as prose does when it refers to one, rather than invoking it."""
    return (len(args) == 1 and args[0] in HELP) or (len(args) == 2 and args[0] == "backup" and args[1] in OPERATOR_COMMANDS)


def test_every_rapidwire_command_line_parses() -> None:
    invocations = rapidwire_invocations()
    assert invocations
    for line, args in invocations:
        if is_command_name(args):
            continue
        try:
            build_parser().parse_args(args)
        except SystemExit as exc:
            assert exc.code == 0, f"{line!r} does not parse with the rapidwire CLI"


def test_every_command_and_backup_subcommand_is_documented() -> None:
    invocations = [args for _, args in rapidwire_invocations()]
    assert {args[0] for args in invocations} >= set(HELP)
    assert {args[1] for args in invocations if args[0] == "backup" and len(args) > 1} >= OPERATOR_COMMANDS


def test_compose_service_commands_parse() -> None:
    services = compose()["services"]
    app_services = [name for name, service in services.items() if service.get("image") == APP_IMAGE]
    assert app_services
    for name in app_services:
        build_parser().parse_args(services[name]["command"])


def test_every_compose_command_names_real_services() -> None:
    known = set(compose()["services"])
    invocations = compose_invocations()
    assert invocations
    for line, subcommand, services in invocations:
        if subcommand in ONE_SERVICE:
            assert services, f"{line!r} names no service"
        assert set(services) <= known, f"{line!r} names a service that compose.yaml does not define"


def test_every_service_and_volume_is_mentioned() -> None:
    text = doc()
    for name in compose()["services"]:
        assert f"`{name}`" in text
    for volume in compose()["volumes"]:
        assert f"`{volume}`" in text
    assert set(compose()["volumes"]) == {"mysql-data", "backups"}


def test_every_setting_name_is_real() -> None:
    names = set(re.findall(r"RAPIDWIRE_[A-Z0-9_]+", doc()))
    assert names
    assert names <= Settings.known_variables()


def test_settings_table_defaults_equal_the_settings() -> None:
    rows = table_rows(section("## 設定"))
    documented: dict[str, str] = {}
    for row in rows:
        match = re.fullmatch(r"`RAPIDWIRE_([A-Z0-9_]+)`", row[0])
        assert match is not None, f"unexpected settings row {row}"
        documented[match[1].lower()] = row[1]
    assert {"db_password", "discord_token", "operator_account_ids", "backup_interval_seconds", "backup_retention", "backup_dir"} <= set(documented)
    for name, cell in documented.items():
        field = Settings.model_fields[name]
        if field.is_required():
            assert cell == "必須", name
            continue
        default = field.get_default(call_default_factory=True)
        if default is None:
            assert cell.startswith("必須"), name
        elif default == []:
            assert cell.startswith("空"), name
        else:
            assert cell == f"`{default}`", name


def test_stop_grace_periods_and_shutdown_waits() -> None:
    services = compose()["services"]
    grace = services["worker"]["stop_grace_period"]
    text = doc()
    assert f"`worker` サービスの `stop_grace_period: {grace}` は両方の待ち時間の合計より長い" in text
    assert services["bot"]["stop_grace_period"] == grace
    assert seconds(grace) > SHUTDOWN_DRAIN_SECONDS + SHUTDOWN_CANCEL_SECONDS
    assert f"実行中のジョブに終了まで {SHUTDOWN_DRAIN_SECONDS:g} 秒を与え" in text
    assert f"ロックを解放するためにさらに {SHUTDOWN_CANCEL_SECONDS:g} 秒を与え" in text


def test_log_rotation_equals_compose() -> None:
    options = compose()["x-logging"]["options"]
    assert compose()["x-logging"]["driver"] == "json-file"
    assert f"`max-size` {options['max-size']}、`max-file` {options['max-file']}" in doc()


def test_mysql_is_published_on_loopback_only() -> None:
    ports = compose()["services"]["mysql"]["ports"]
    assert len(ports) == 1
    match = re.fullmatch(r"127\.0\.0\.1:\$\{MYSQL_HOST_PORT:-([0-9]+)\}:3306", ports[0])
    assert match is not None, ports
    assert f"MySQL はサーバーの `127.0.0.1` の `MYSQL_HOST_PORT` (既定値 {match[1]}、compose の変数展開のため `.env` で設定する) にのみ公開される" in doc()


def test_worker_job_table_matches_the_jobs() -> None:
    settings = Settings(db_password="unused-password", outbox_poll_interval_seconds=3.5, stake_accrual_interval_seconds=1234, stake_accrual_staleness_seconds=5000)
    engine = cast(AsyncEngine, object())
    dispatcher = Dispatcher(engine, cast(httpx.AsyncClient, object()))
    jobs = {job.name: job for job in build_jobs(engine, settings, dispatcher, asyncio.Event(), holder="test", backup=default_backup_functions())}
    assert jobs[OUTBOX_JOB].interval_seconds == settings.outbox_poll_interval_seconds
    assert jobs[STAKE_ACCRUAL_JOB].interval_seconds == settings.stake_accrual_interval_seconds
    assert jobs[BACKUP_CHECK_JOB].interval_seconds == BACKUP_CHECK_INTERVAL_SECONDS
    intervals = {
        OUTBOX_JOB: "`RAPIDWIRE_OUTBOX_POLL_INTERVAL_SECONDS`",
        STAKE_ACCRUAL_JOB: "`RAPIDWIRE_STAKE_ACCRUAL_INTERVAL_SECONDS`",
        BACKUP_CHECK_JOB: f"{BACKUP_CHECK_INTERVAL_SECONDS:g} 秒",
    }
    rows = {row[0].strip("`"): row for row in table_rows(section("## worker のジョブ").split("\n### ")[0])}
    assert set(rows) == set(jobs)
    assert timedelta(minutes=1) == BACKUP_LEASE_RENEW_INTERVAL
    for name, job in jobs.items():
        assert rows[name][1] == intervals[name], name
        lock = rows[name][2]
        if job.lease_ttl is None:
            assert lock == f"なし (期限が来たバックアップは、1 分ごとに更新する {lease_words(BACKUP_LEASE_TTL)}の `{BACKUP_JOB_NAME}` リースを取る)", name
        else:
            assert lock == f"`{name}` ({lease_words(job.lease_ttl)}のリース)", name


def test_documented_worker_numbers_come_from_the_code() -> None:
    text = doc()
    assert f"min({RETRY_MAX_SECONDS:g}, {RETRY_BASE_SECONDS:g} x 2^(n-1)) 秒" in text
    assert f"{MAX_DELIVERY_ATTEMPTS} 回目の試行が失敗すると行は `failed` になり" in text
    assert f"dispatcher は 1 回あたり {AUTH_FAILURE_PAUSE_SECONDS:g} 秒停止し" in text
    assert f"worker は {BACKUP_CHECK_INTERVAL_SECONDS:g} 秒ごとにバックアップの期限が来たかを確認する" in text
    assert BACKUP_RETRY_DELAY.total_seconds() % 60 == 0
    assert f"worker は再試行まで {BACKUP_RETRY_DELAY.total_seconds() / 60:g} 分待つ" in text
    backups = (DOCS / "backups.md").read_text(encoding="utf-8")
    assert f"worker は {BACKUP_CHECK_INTERVAL_SECONDS:g} 秒ごとに実行の期限が来たかを確認する" in backups
    assert f"worker は次の試行まで {BACKUP_RETRY_DELAY.total_seconds() / 60:g} 分待ち" in backups


def test_documented_exit_codes_come_from_the_cli() -> None:
    text = doc()
    assert f"終了コード {EXIT_CONFIG} でコマンドを停止し" in text
    assert f"その他の終了コードは、成功が {EXIT_OK}、失敗が {EXIT_FAILURE}、使い方の誤りが {EXIT_USAGE}、不変条件違反またはバックアップの不一致が {EXIT_INVARIANT_VIOLATION} である。" in text


def test_on_demand_backup_exit_codes_come_from_the_command(monkeypatch: pytest.MonkeyPatch) -> None:
    assert backup_command_exit(monkeypatch, violations=0) == EXIT_OK
    assert backup_command_exit(monkeypatch, violations=1) == EXIT_INVARIANT_VIOLATION
    assert backup_command_exit(monkeypatch, failed=True) == EXIT_FAILURE
    assert backup_command_exit(monkeypatch, lease_error=LeaseLost) == EXIT_FAILURE
    assert backup_command_exit(monkeypatch, lease_error=LeaseUnavailable) == EXIT_FAILURE
    assert (
        f"両方のファイルを書き込めたときは {EXIT_OK}、書き込めたがスナップショットが台帳の不変条件に違反するときは {EXIT_INVARIANT_VIOLATION}、"
        f"実行が失敗した、リースを失った、または別のバックアップが実行中のときは {EXIT_FAILURE} で終了する。" in doc()
    )


def test_delivery_failure_reasons_exist_in_the_dispatcher() -> None:
    source = inspect.getsource(dispatcher_module)
    reasons = set(re.findall(r"理由 `([a-z_]+)`", doc())) | set(re.findall(r"例えば `([a-z_]+)` や `([a-z_]+)`", doc())[0])
    assert {"retries_exhausted", "internal_error", "message_rejected", "role_change_rejected"} <= reasons
    for reason in reasons:
        assert f'"{reason}"' in source, reason


@pytest.mark.parametrize("name", ["a$b", "123", "a" * 65, "", "rapid-wire", "rapidwire" + chr(0xE9)])
def test_settings_refuse_database_names_outside_the_documented_rule(name: str) -> None:
    with pytest.raises(ValidationError):
        Settings(db_password="unused-password", db_name=name)


@pytest.mark.parametrize("name", ["rapidwire", "rw_check", "a" * 64, "_1", "2026_backup"])
def test_settings_accept_database_names_inside_the_documented_rule(name: str) -> None:
    assert Settings(db_password="unused-password", db_name=name).db_name == name


def test_both_documents_state_the_database_name_rule() -> None:
    backups = (DOCS / "backups.md").read_text(encoding="utf-8")
    assert doc().count(DB_NAME_RULE) == 2
    assert backups.count(DB_NAME_RULE) == 2


def test_services_table_matches_compose() -> None:
    services = compose()["services"]
    rows = {row[0].strip("`"): row for row in tables(section("## サービス"))[0]}
    assert set(rows) == set(services)
    for name, service in services.items():
        _, command, role = rows[name]
        if service["image"] == APP_IMAGE:
            assert command == f"`rapidwire {' '.join(service['command'])}`", name
        else:
            assert command in {f"`{service['image']}` イメージ", f"`{service['image'].rsplit(':', 1)[0]}` イメージ"}, name
        assert ("`build` セクション" in role) == ("build" in service), name
        for profile in service.get("profiles", []):
            assert f"`{profile}` プロファイルに属する" in role, name
        for volume in service.get("volumes", []):
            source = volume.split(":")[0]
            assert f"`{source}` ボリューム" in role, name
            if source == "backups":
                assert ("読み取り専用" in role) == volume.endswith(":ro"), name
                assert ("読み書き可能" in role) == (not volume.endswith(":ro")), name


def test_env_file_table_matches_compose() -> None:
    readers: dict[str, set[str]] = {}
    for name, service in compose()["services"].items():
        entries = service.get("env_file", [])
        for entry in [entries] if isinstance(entries, str) else entries:
            readers.setdefault(entry if isinstance(entry, str) else entry["path"], set()).add(name)
    rows = next(table for table in tables(section("## 配備")) if table[0][0] == "`.env`")
    documented = {row[0].strip("`"): set(re.findall(r"`([a-z]+)`", row[1])) for row in rows}
    assert documented == readers


def test_restart_and_start_order_match_compose() -> None:
    services = compose()["services"]
    text = doc()
    always = [name for name, service in services.items() if service["restart"] == "unless-stopped"]
    once = [name for name, service in services.items() if service["restart"] == "no"]
    assert f"{'、'.join(f'`{name}`' for name in always)} は自動的に再起動する (`restart: unless-stopped`)" in text
    assert f"`{once[0]}` と `{once[1]}` は 1 回だけ実行される" in text and len(once) == 2
    assert services["migrate"]["depends_on"] == {"mysql": {"condition": "service_healthy"}}
    for name in ("api", "bot", "worker"):
        assert services[name]["depends_on"] == {"migrate": {"condition": "service_completed_successfully"}}
    assert services["cloudflared"]["depends_on"] == ["api"]
    assert (
        "`docker compose up -d` は `mysql` を起動して healthy になるまで待ち、`migrate` を完了まで実行してから初めて `api`、`bot`、`worker` を起動し、`cloudflared` は `api` の後に起動する。" in text
    )


def test_log_table_levels_match_the_log_calls() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in (REPO / "rapidwire").rglob("*.py"))
    levels = {"debug": "DEBUG", "info": "INFO", "warning": "WARNING", "error": "ERROR", "exception": "ERROR", "critical": "CRITICAL"}
    for row in table_rows(section("## ログ")):
        for message in re.findall(r"`([^`]+)`", row[0]):
            calls = set(re.findall(rf'\.(debug|info|warning|error|exception|critical)\(\s*"{re.escape(message)}"', source))
            if calls:
                assert {levels[call] for call in calls} == {row[1]}, message
            else:
                assert re.search(rf'\.log\(\s*\w+,\s*"{re.escape(message)}"', source), message
                assert row[1] == "INFO (一部が失敗したときは WARNING)", message


def test_backup_file_names_follow_the_naming() -> None:
    names = [name for name in re.findall(r"rapidwire-(?:dump|export)-[^\s`\"'/,)\]、。]+", doc()) if "<" not in name and "*" not in name]
    assert names
    parsed = [parse_backup_name(name) for name in names]
    assert all(item is not None for item in parsed), names
    assert {item.kind for item in parsed if item is not None} == set(BackupKind)


def test_required_intents_and_the_volume_prohibition_are_stated() -> None:
    text = doc()
    intents = bot_intents()
    enabled = [name for name, value in intents if value]
    assert set(enabled) == {"guilds", "members", "guild_messages", "dm_messages"}
    for name in enabled:
        assert f"`{name}`" in text
    assert not intents.message_content
    assert "`message_content` は不要" in text
    assert "Server Members Intent を有効にすること" in text
    assert (
        "サーバー上では `docker compose down -v`、`docker compose down --volumes`、`docker volume rm`、`docker volume prune` を決して実行せず、`docker system prune --volumes` も決して実行しない。"
        in text
    )


def test_logged_messages_exist_in_the_code() -> None:
    source = "\n".join(path.read_text(encoding="utf-8") for path in (REPO / "rapidwire").rglob("*.py"))
    rows = table_rows(section("## ログ"))
    messages = [message for row in rows for message in re.findall(r"`([^`]+)`", row[0])]
    assert len(messages) >= len(rows)
    for message in messages:
        assert f'"{message}"' in source, f"no log call uses the message {message!r}"


@pytest.mark.parametrize("name", ["operations.md", "backups.md"])
def test_links_reach_existing_files_and_headings(name: str) -> None:
    path = DOCS / name
    links = re.findall(r"\]\(([^)\s]+)\)", path.read_text(encoding="utf-8"))
    assert links
    for link in links:
        target, _, anchor = link.partition("#")
        target_path = (path.parent / target).resolve() if target else path
        assert target_path.is_file(), link
        if anchor:
            assert anchor in heading_anchors(target_path), link


def test_backups_and_operations_link_to_each_other() -> None:
    assert "(backups.md" in doc()
    assert "(operations.md" in (DOCS / "backups.md").read_text(encoding="utf-8")
