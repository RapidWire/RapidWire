from __future__ import annotations

import io
import json
import logging
import re
from pathlib import Path

import pytest
from pydantic import ValidationError
from sqlalchemy.engine import make_url

from rapidwire.config import (
    DB_NAME_PATTERN,
    GUILD_HEARTBEAT_INTERVAL_SECONDS,
    SECRET_MIN_LENGTH,
    ConfigError,
    Settings,
    format_validation_error,
    get_settings,
    reset_settings_cache,
    url_escaped_password,
)
from rapidwire.logging import MIN_SECRET_LENGTH, REDACTED, JsonFormatter, register_secret

PASSWORD = "test-password"


def load(monkeypatch: pytest.MonkeyPatch, **env: str) -> Settings:
    env.setdefault("RAPIDWIRE_DB_PASSWORD", PASSWORD)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return Settings(_env_file=None)


def error_fields(exc: ValidationError) -> set[str]:
    return {str(err["loc"][0]) for err in exc.errors() if err["loc"]}


def test_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = load(monkeypatch)
    assert settings.db_host == "mysql"
    assert settings.db_port == 3306
    assert settings.db_name == "rapidwire"
    assert settings.db_user == "rapidwire"
    assert settings.discord_token is None
    assert settings.operator_account_ids == []
    assert settings.gas_currency_id == 1269970084965912747
    assert (settings.gas_price, settings.default_gas, settings.max_gas) == (1, 128, 200000)
    assert settings.contract_max_size == 8000
    assert (settings.swap_chunk_count, settings.swap_max_routes) == (100, 32)
    assert (settings.stake_accrual_interval_seconds, settings.stake_accrual_staleness_seconds) == (1800, 10800)
    assert settings.outbox_poll_interval_seconds == 2.0
    assert settings.guild_directory_staleness_seconds == 120
    assert settings.backup_dir == Path("/var/lib/rapidwire/backups")
    assert (settings.backup_retention, settings.backup_interval_seconds) == (14, 86400)
    assert (settings.api_host, settings.api_port) == ("0.0.0.0", 8000)
    assert settings.log_level == "INFO"


def test_password_required(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as info:
        Settings(_env_file=None)
    assert error_fields(info.value) == {"db_password"}


@pytest.mark.parametrize("raw", ["", "   ", "\t", "        ", "1234567"])
def test_password_must_be_long_and_not_blank(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, RAPIDWIRE_DB_PASSWORD=raw)
    assert error_fields(info.value) == {"db_password"}


def test_short_discord_token_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, RAPIDWIRE_DISCORD_TOKEN="1234567")
    assert error_fields(info.value) == {"discord_token"}


def test_secret_minimum_covers_log_redaction_minimum() -> None:
    # Every accepted secret must be long enough for the log redactor to register it.
    assert SECRET_MIN_LENGTH >= MIN_SECRET_LENGTH


@pytest.mark.parametrize("name", ["RAPIDWIRE_DB_HOST", "RAPIDWIRE_DB_NAME", "RAPIDWIRE_DB_USER"])
def test_db_identity_fields_not_empty(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, **{name: ""})
    assert error_fields(info.value) == {name.removeprefix("RAPIDWIRE_").lower()}


@pytest.mark.parametrize("value", ["rapid-wire", "rapid wire", "db.name", "x" * 65, chr(0x540D) + chr(0x524D), "`x`", "a$b", "123", "rapidwire\n"])
def test_db_name_must_be_a_plain_identifier(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, RAPIDWIRE_DB_NAME=value)
    assert error_fields(info.value) == {"db_name"}


@pytest.mark.parametrize("value", ["rapidwire", "rapidwire_test", "R_1", "1a", "x" * 64])
def test_db_name_accepts_plain_identifiers(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    assert re.fullmatch(DB_NAME_PATTERN, value)
    assert load(monkeypatch, RAPIDWIRE_DB_NAME=value).db_name == value


def test_backup_dir_must_be_absolute(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, RAPIDWIRE_BACKUP_DIR="backups")
    assert error_fields(info.value) == {"backup_dir"}
    assert load(monkeypatch, RAPIDWIRE_BACKUP_DIR="/srv/backups").backup_dir == Path("/srv/backups")


def test_unknown_environment_variable_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAPIDWIRE_DB_PASWORD", "typo-value")
    monkeypatch.setenv("rapidwire_gas_prise", "1")
    with pytest.raises(ConfigError, match="unknown settings variables: RAPIDWIRE_DB_PASWORD, RAPIDWIRE_GAS_PRISE") as info:
        load(monkeypatch)
    assert "typo-value" not in str(info.value)


def test_unknown_env_file_variable_rejected(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"RAPIDWIRE_DB_PASSWORD={PASSWORD}\nRAPIDWIRE_OPERATOR_IDS=1\nOTHER_TOOL_SETTING=x\n")
    reset_settings_cache()
    with pytest.raises(ConfigError, match="RAPIDWIRE_OPERATOR_IDS"):
        get_settings()


def test_foreign_env_file_keys_ignored(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text(f"RAPIDWIRE_DB_PASSWORD={PASSWORD}\nOTHER_TOOL_SETTING=x\nMYSQL_ROOT_PASSWORD=root-password\n")
    reset_settings_cache()
    assert get_settings().db_password.get_secret_value() == PASSWORD


def test_unknown_keyword_argument_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="unknown settings: db_pasword, gas_prise"):
        Settings(_env_file=None, db_password=PASSWORD, db_pasword="x", gas_prise=1)
    settings = Settings(_env_file=None, db_password=PASSWORD, db_port=3307)
    assert settings.db_port == 3307


def test_test_prefix_and_foreign_variables_allowed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAPIDWIRE_TEST_DB_URL", "mysql+asyncmy://u:p@h/x_test")
    monkeypatch.setenv("MYSQL_ROOT_PASSWORD", "root-password")
    assert load(monkeypatch).db_name == "rapidwire"


def test_validation_error_text_hides_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RAPIDWIRE_DISCORD_TOKEN", "tok-should-not-appear")
    monkeypatch.setenv("RAPIDWIRE_DB_PORT", "port-should-not-appear")
    with pytest.raises(ValidationError) as info:
        Settings(_env_file=None)
    assert error_fields(info.value) == {"db_password", "db_port"}
    rendered = str(info.value) + repr(info.value)
    assert "tok-should-not-appear" not in rendered
    assert "port-should-not-appear" not in rendered


def test_env_file_is_read(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("RAPIDWIRE_DB_PASSWORD=from-file-pw\nRAPIDWIRE_DB_PORT=3307\n")
    reset_settings_cache()
    settings = get_settings()
    assert settings.db_password.get_secret_value() == "from-file-pw"
    assert settings.db_port == 3307
    assert get_settings() is settings


def test_environment_overrides_env_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("RAPIDWIRE_DB_PASSWORD=from-file-pw\nRAPIDWIRE_DB_NAME=file_db\n")
    monkeypatch.setenv("RAPIDWIRE_DB_PASSWORD", "from-env-pw")
    reset_settings_cache()
    settings = get_settings()
    assert settings.db_password.get_secret_value() == "from-env-pw"
    assert settings.db_name == "file_db"


def test_secret_values_include_compose_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MYSQL_ROOT_PASSWORD", "root-pw-value")
    monkeypatch.setenv("TUNNEL_TOKEN", "tunnel-token-value")
    settings = load(monkeypatch, RAPIDWIRE_DISCORD_TOKEN="discord-token-value")
    assert set(settings.secret_values()) == {PASSWORD, "discord-token-value", "root-pw-value", "tunnel-token-value"}


def test_secret_values_without_compose_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load(monkeypatch).secret_values() == [PASSWORD]


def test_url_escaped_password_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    password = "p@ss#w rd%"
    settings = load(monkeypatch, RAPIDWIRE_DB_PASSWORD=password)
    rendered = settings.database_url.render_as_string(hide_password=False)
    escaped = url_escaped_password(password)
    assert escaped != password
    assert f":{escaped}@" in rendered
    assert escaped in settings.secret_values()
    for secret in settings.secret_values():
        register_secret(secret)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("rapidwire.test.config.url")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.warning("connect failed: %s", rendered)
    line = stream.getvalue()
    assert escaped not in line
    assert password not in line
    assert json.loads(line)["msg"] == f"connect failed: mysql+asyncmy://rapidwire:{REDACTED}@mysql:3306/rapidwire?charset=utf8mb4"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("", []),
        ("123", [123]),
        ("123,456", [123, 456]),
        (" 123 , 456 ,", [123, 456]),
        ("1269970084965912747", [1269970084965912747]),
    ],
)
def test_operator_ids_parsing(monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[int]) -> None:
    assert load(monkeypatch, RAPIDWIRE_OPERATOR_ACCOUNT_IDS=raw).operator_account_ids == expected


@pytest.mark.parametrize("raw", ["abc", "1,x", "0", "-5", "1.5", "[1,2]", "1;2", "123,123", "9223372036854775808"])
def test_operator_ids_invalid(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, RAPIDWIRE_OPERATOR_ACCOUNT_IDS=raw)
    assert error_fields(info.value) == {"operator_account_ids"}


def test_operator_id_upper_bound(monkeypatch: pytest.MonkeyPatch) -> None:
    assert load(monkeypatch, RAPIDWIRE_OPERATOR_ACCOUNT_IDS="9223372036854775807").operator_account_ids == [2**63 - 1]


@pytest.mark.parametrize("raw", ["0", "9223372036854775808"])
def test_gas_currency_id_bounds(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, RAPIDWIRE_GAS_CURRENCY_ID=raw)
    assert error_fields(info.value) == {"gas_currency_id"}


def test_default_gas_above_max_gas_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="default_gas must not exceed max_gas"):
        load(monkeypatch, RAPIDWIRE_DEFAULT_GAS="200", RAPIDWIRE_MAX_GAS="100")
    assert load(monkeypatch, RAPIDWIRE_DEFAULT_GAS="100", RAPIDWIRE_MAX_GAS="100").default_gas == 100


def test_contract_max_size_capped_at_the_spec_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, RAPIDWIRE_CONTRACT_MAX_SIZE="8001")
    assert error_fields(info.value) == {"contract_max_size"}
    assert load(monkeypatch, RAPIDWIRE_CONTRACT_MAX_SIZE="8000").contract_max_size == 8000
    assert load(monkeypatch, RAPIDWIRE_CONTRACT_MAX_SIZE="4000").contract_max_size == 4000


def test_gas_cost_must_fit_an_amount(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="gas_price \\* max_gas must not exceed"):
        load(monkeypatch, RAPIDWIRE_GAS_PRICE=str(10**38), RAPIDWIRE_MAX_GAS="1", RAPIDWIRE_DEFAULT_GAS="1")
    assert load(monkeypatch, RAPIDWIRE_GAS_PRICE=str(10**38 - 1), RAPIDWIRE_MAX_GAS="1", RAPIDWIRE_DEFAULT_GAS="1").gas_price == 10**38 - 1


def test_staleness_below_interval_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError, match="stake_accrual_staleness_seconds must not be less than"):
        load(monkeypatch, RAPIDWIRE_STAKE_ACCRUAL_INTERVAL_SECONDS="600", RAPIDWIRE_STAKE_ACCRUAL_STALENESS_SECONDS="599")
    settings = load(monkeypatch, RAPIDWIRE_STAKE_ACCRUAL_INTERVAL_SECONDS="600", RAPIDWIRE_STAKE_ACCRUAL_STALENESS_SECONDS="600")
    assert settings.stake_accrual_staleness_seconds == 600


def test_guild_directory_staleness_covers_two_heartbeats(monkeypatch: pytest.MonkeyPatch) -> None:
    minimum = 2 * GUILD_HEARTBEAT_INTERVAL_SECONDS
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, RAPIDWIRE_GUILD_DIRECTORY_STALENESS_SECONDS=str(minimum - 1))
    assert error_fields(info.value) == {"guild_directory_staleness_seconds"}
    assert load(monkeypatch, RAPIDWIRE_GUILD_DIRECTORY_STALENESS_SECONDS=str(minimum)).guild_directory_staleness_seconds == minimum


def test_env_example_documents_every_setting() -> None:
    example = Path(__file__).resolve().parents[2] / ".env.example"
    names = {line.split("=", 1)[0] for line in example.read_text(encoding="utf-8").splitlines() if line.startswith("RAPIDWIRE_")}
    assert names == Settings.known_variables()


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("RAPIDWIRE_DB_PORT", "0"),
        ("RAPIDWIRE_DB_PORT", "65536"),
        ("RAPIDWIRE_GAS_PRICE", "0"),
        ("RAPIDWIRE_DEFAULT_GAS", "0"),
        ("RAPIDWIRE_CONTRACT_MAX_SIZE", "0"),
        ("RAPIDWIRE_SWAP_CHUNK_COUNT", "0"),
        ("RAPIDWIRE_SWAP_MAX_ROUTES", "0"),
        ("RAPIDWIRE_STAKE_ACCRUAL_INTERVAL_SECONDS", "0"),
        ("RAPIDWIRE_OUTBOX_POLL_INTERVAL_SECONDS", "0"),
        ("RAPIDWIRE_BACKUP_RETENTION", "0"),
        ("RAPIDWIRE_BACKUP_INTERVAL_SECONDS", "59"),
        ("RAPIDWIRE_LOG_LEVEL", "verbose"),
    ],
)
def test_out_of_range_rejected(monkeypatch: pytest.MonkeyPatch, name: str, value: str) -> None:
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, **{name: value})
    assert error_fields(info.value) == {name.removeprefix("RAPIDWIRE_").lower()}


@pytest.mark.parametrize("raw", ["debug", "Warning", " error ", "CRITICAL"])
def test_log_level_case_insensitive(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    assert load(monkeypatch, RAPIDWIRE_LOG_LEVEL=raw).log_level == raw.strip().upper()


def test_database_url_escapes_password_and_hides_it(monkeypatch: pytest.MonkeyPatch) -> None:
    password = "p@ss:w/rd#?%&= x"
    settings = load(monkeypatch, RAPIDWIRE_DB_PASSWORD=password, RAPIDWIRE_DB_HOST="db.local", RAPIDWIRE_DB_PORT="3307")
    url = settings.database_url
    assert url.drivername == "mysql+asyncmy"
    assert url.query == {"charset": "utf8mb4"}
    rendered = url.render_as_string(hide_password=False)
    parsed = make_url(rendered)
    assert parsed.password == password
    assert (parsed.host, parsed.port, parsed.database, parsed.username) == ("db.local", 3307, "rapidwire", "rapidwire")
    for text in (repr(url), str(url), repr(settings), str(settings)):
        assert password not in text
        assert "p@ss" not in text


def test_require_discord_token(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigError, match="RAPIDWIRE_DISCORD_TOKEN"):
        load(monkeypatch).require_discord_token()
    with pytest.raises(ConfigError):
        load(monkeypatch, RAPIDWIRE_DISCORD_TOKEN="  ").require_discord_token()
    settings = load(monkeypatch, RAPIDWIRE_DISCORD_TOKEN="tok-12345")
    assert settings.require_discord_token() == "tok-12345"
    assert "tok-12345" not in repr(settings)
    assert set(settings.secret_values()) == {PASSWORD, "tok-12345"}


def test_format_validation_error_omits_values(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError) as info:
        load(monkeypatch, RAPIDWIRE_DB_PASSWORD="hunter2-secret", RAPIDWIRE_DB_PORT="notaport", RAPIDWIRE_OPERATOR_ACCOUNT_IDS="hunter2-secret")
    message = format_validation_error(info.value)
    assert "RAPIDWIRE_DB_PORT" in message
    assert "RAPIDWIRE_OPERATOR_ACCOUNT_IDS" in message
    assert "hunter2" not in message
    assert "notaport" not in message
