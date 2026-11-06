"""Runtime settings read from RAPIDWIRE_* environment variables (and .env), validated at construction."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal

from dotenv import dotenv_values
from pydantic import Field, SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict
from sqlalchemy import URL

from rapidwire.db.types import AMOUNT_MAX

LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

ENV_PREFIX = "RAPIDWIRE_"

# Variables under this prefix configure the test suite, not the application.
TEST_ENV_PREFIX = "RAPIDWIRE_TEST_"

# Account and currency ids are Discord snowflakes stored in signed BIGINT columns.
ID_MAX = 2**63 - 1

SECRET_MIN_LENGTH = 8

# A database name is 1 to 64 ASCII letters, digits and "_", not all digits, so MySQL accepts it unquoted, docker compose does not interpolate it, and the backup tools can pass it to the MySQL client as is.
DB_NAME_PATTERN = r"^(?![0-9]+$)[A-Za-z0-9_]{1,64}$"

# A contract binary may be at most this many bytes uncompressed; contract_max_size may lower the limit but not raise it.
CONTRACT_SIZE_LIMIT = 8000

# Largest max_gas: every random() costs at least 2 gas, so a call records at most max_gas // 2 drawn values (each at most 78 digits), which at this bound keeps a failed call's output under half of MySQL's default 64 MiB max_allowed_packet.
MAX_GAS_LIMIT = 500_000

# Seconds between the bot's guild directory heartbeats; guild_directory_staleness_seconds must be at least twice this, so one late heartbeat does not make guild information unavailable.
GUILD_HEARTBEAT_INTERVAL_SECONDS = 30

# Infrastructure secrets that are redacted from logs whenever they are present in the process environment.
FOREIGN_SECRET_ENV_VARS = ("MYSQL_ROOT_PASSWORD", "TUNNEL_TOKEN")

_UNSET: Any = object()


class ConfigError(Exception):
    """A setting is missing or invalid for the role being started."""


def _env_file_names(env_file: object) -> list[str]:
    if env_file is None:
        return []
    files: Iterable[object] = [env_file] if isinstance(env_file, str | os.PathLike) else env_file  # type: ignore[assignment]
    names: list[str] = []
    for path in files:
        if isinstance(path, str | os.PathLike) and Path(path).is_file():
            names.extend(dotenv_values(path).keys())
    return names


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix=ENV_PREFIX,
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        validate_default=True,
        hide_input_in_errors=True,
    )

    db_host: str = Field(default="mysql", min_length=1)
    db_port: int = Field(default=3306, ge=1, le=65535)
    db_name: str = "rapidwire"
    db_user: str = Field(default="rapidwire", min_length=1)
    db_password: SecretStr = Field(min_length=SECRET_MIN_LENGTH)

    discord_token: SecretStr | None = Field(default=None, min_length=SECRET_MIN_LENGTH)

    operator_account_ids: Annotated[list[Annotated[int, Field(ge=1, le=ID_MAX)]], NoDecode] = Field(default_factory=list)

    gas_currency_id: int = Field(default=1269970084965912747, ge=1, le=ID_MAX)
    gas_price: int = Field(default=1, ge=1)
    default_gas: int = Field(default=128, ge=1)
    # Largest gas limit a call may request; the default lets a call that spends all of it on the VM's slowest gas-per-second workload finish within about half of the wall-clock limit.
    max_gas: int = Field(default=200000, ge=1, le=MAX_GAS_LIMIT)

    contract_max_size: int = Field(default=CONTRACT_SIZE_LIMIT, ge=1, le=CONTRACT_SIZE_LIMIT)

    swap_chunk_count: int = Field(default=100, ge=1)
    swap_max_routes: int = Field(default=32, ge=1)

    stake_accrual_interval_seconds: int = Field(default=1800, ge=1)
    stake_accrual_staleness_seconds: int = Field(default=10800, ge=1)

    outbox_poll_interval_seconds: float = Field(default=2.0, gt=0)

    # Age of the bot's last guild directory heartbeat after which processes without a gateway treat guild information as unavailable.
    guild_directory_staleness_seconds: int = Field(default=120, ge=2 * GUILD_HEARTBEAT_INTERVAL_SECONDS)

    backup_dir: Path = Path("/var/lib/rapidwire/backups")
    backup_retention: int = Field(default=14, ge=1)
    backup_interval_seconds: int = Field(default=86400, ge=60)

    api_host: str = "0.0.0.0"  # noqa: S104 - the API listens on all interfaces inside its container.
    api_port: int = Field(default=8000, ge=1, le=65535)

    log_level: LogLevel = "INFO"

    def __init__(self, _env_file: Any = _UNSET, **values: Any) -> None:
        """Reject unknown keyword arguments and unknown RAPIDWIRE_* names in the environment or the env file with ConfigError, then load and validate."""
        unknown_kwargs = sorted(name for name in values if not name.startswith("_") and name not in type(self).model_fields)
        if unknown_kwargs:
            raise ConfigError(f"unknown settings: {', '.join(unknown_kwargs)}")
        env_file = self.model_config.get("env_file") if _env_file is _UNSET else _env_file
        self._reject_unknown_variables([*os.environ, *_env_file_names(env_file)])
        if _env_file is _UNSET:
            super().__init__(**values)
        else:
            super().__init__(_env_file=_env_file, **values)

    @classmethod
    def known_variables(cls) -> frozenset[str]:
        return frozenset(f"{ENV_PREFIX}{name}".upper() for name in cls.model_fields)

    @classmethod
    def _reject_unknown_variables(cls, names: Iterable[str]) -> None:
        known = cls.known_variables()
        unknown = sorted({name.upper() for name in names if name.upper().startswith(ENV_PREFIX) and not name.upper().startswith(TEST_ENV_PREFIX)} - known)
        if unknown:
            raise ConfigError(f"unknown settings variables: {', '.join(unknown)}")

    @field_validator("db_password")
    @classmethod
    def _password_not_blank(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().strip():
            raise ValueError("must not be blank")
        return value

    @field_validator("db_name")
    @classmethod
    def _db_name_is_plain(cls, value: str) -> str:
        if re.fullmatch(DB_NAME_PATTERN, value) is None:
            raise ValueError('must be 1 to 64 ASCII letters, digits or "_", and not only digits')
        return value

    @field_validator("discord_token", mode="before")
    @classmethod
    def _blank_token_is_none(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @field_validator("operator_account_ids", mode="before")
    @classmethod
    def _parse_operator_ids(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        ids: list[int] = []
        for part in value.split(","):
            item = part.strip()
            if not item:
                continue
            if not item.isascii() or not item.isdigit():
                raise ValueError("must be a comma-separated list of positive integer account ids")
            ids.append(int(item))
        return ids

    @field_validator("operator_account_ids")
    @classmethod
    def _operator_ids_unique(cls, value: list[int]) -> list[int]:
        if len(set(value)) != len(value):
            raise ValueError("account ids must not repeat")
        return value

    @field_validator("backup_dir")
    @classmethod
    def _backup_dir_absolute(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("must be an absolute path")
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_log_level(cls, value: object) -> object:
        return value.strip().upper() if isinstance(value, str) else value

    @model_validator(mode="after")
    def _cross_field_bounds(self) -> Settings:
        if self.default_gas > self.max_gas:
            raise ValueError("default_gas must not exceed max_gas")
        if self.stake_accrual_staleness_seconds < self.stake_accrual_interval_seconds:
            raise ValueError("stake_accrual_staleness_seconds must not be less than stake_accrual_interval_seconds")
        return self

    @model_validator(mode="after")
    def _gas_cost_fits_an_amount(self) -> Settings:
        if self.gas_price * self.max_gas > AMOUNT_MAX:
            raise ValueError("gas_price * max_gas must not exceed the largest amount")
        return self

    @property
    def database_url(self) -> URL:
        """SQLAlchemy URL for the asyncmy driver; its repr and str() mask the password."""
        return URL.create(
            "mysql+asyncmy",
            username=self.db_user,
            password=self.db_password.get_secret_value(),
            host=self.db_host,
            port=self.db_port,
            database=self.db_name,
            query={"charset": "utf8mb4"},
        )

    def require_discord_token(self) -> str:
        """Return the Discord bot token, or raise ConfigError when RAPIDWIRE_DISCORD_TOKEN is unset."""
        if self.discord_token is None:
            raise ConfigError("RAPIDWIRE_DISCORD_TOKEN is required for this command but is not set")
        return self.discord_token.get_secret_value()

    def secret_values(self) -> list[str]:
        """Plain values of every secret setting, the password as escaped inside a rendered URL, and compose-only secrets present in the environment, for log redaction."""
        password = self.db_password.get_secret_value()
        secrets = [password, url_escaped_password(password)]
        if self.discord_token is not None:
            secrets.append(self.discord_token.get_secret_value())
        secrets.extend(value for name in FOREIGN_SECRET_ENV_VARS if (value := os.environ.get(name)))
        return list(dict.fromkeys(secrets))


def url_escaped_password(password: str) -> str:
    """The password segment exactly as URL.render_as_string(hide_password=False) writes it."""
    rendered = URL.create("x", username="u", password=password, host="h").render_as_string(hide_password=False)
    return rendered.removeprefix("x://u:").removesuffix("@h")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load and validate settings once per process; raises pydantic.ValidationError or ConfigError on invalid input."""
    return Settings()


def reset_settings_cache() -> None:
    """Forget the cached Settings so the next get_settings() reloads the environment."""
    get_settings.cache_clear()


def format_validation_error(exc: ValidationError) -> str:
    """Describe failing settings by environment variable name and reason, never including the offending values."""
    lines = ["Invalid configuration:"]
    for error in exc.errors(include_input=False, include_url=False):
        loc = error["loc"]
        name = f"{ENV_PREFIX}{str(loc[0]).upper()}" if loc else "(settings)"
        lines.append(f"  {name}: {error['msg']}")
    return "\n".join(lines)
