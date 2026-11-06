from __future__ import annotations

import dataclasses
import io
import json
import logging
from datetime import datetime
from typing import Any
from urllib.parse import quote, quote_plus

import pytest
from pydantic import BaseModel

from rapidwire.logging import REDACTED, JsonFormatter, configure_logging, register_secret


def make_logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("rapidwire.test.logging")
    logger.handlers[:] = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    return logger, stream


def lines(stream: io.StringIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def test_json_shape() -> None:
    logger, stream = make_logger()
    logger.info("hello %s", "world", extra={"user_id": 42, "items": [1, "a"], "nested": {"k": None}})
    [record] = lines(stream)
    assert record["level"] == "INFO"
    assert record["logger"] == "rapidwire.test.logging"
    assert record["msg"] == "hello world"
    assert record["user_id"] == 42
    assert record["items"] == [1, "a"]
    assert record["nested"] == {"k": None}
    ts = datetime.fromisoformat(record["ts"])
    assert ts.utcoffset() is not None
    assert ts.utcoffset().total_seconds() == 0  # type: ignore[union-attr]
    assert "exc_info" not in record
    assert set(record) == {"ts", "level", "logger", "msg", "user_id", "items", "nested"}


def test_one_line_per_record() -> None:
    logger, stream = make_logger()
    logger.warning("multi\nline")
    assert len(stream.getvalue().splitlines()) == 1
    assert lines(stream)[0]["msg"] == "multi\nline"


def test_exception_rendered() -> None:
    logger, stream = make_logger()
    try:
        raise ValueError("boom")
    except ValueError:
        logger.exception("failed")
    [record] = lines(stream)
    assert record["level"] == "ERROR"
    assert "Traceback" in record["exc_info"]
    assert "ValueError: boom" in record["exc_info"]


def test_registered_secret_redacted_everywhere() -> None:
    secret = "s3cr3t-value"
    register_secret(secret)
    logger, stream = make_logger()
    logger.info("connecting with %s", secret, extra={"detail": f"dsn=mysql://u:{secret}@h", "parts": [secret]})
    try:
        raise RuntimeError(f"auth failed for {secret}")
    except RuntimeError:
        logger.exception("error %s", secret)
    output = stream.getvalue()
    assert secret not in output
    first, second = lines(stream)
    assert first["msg"] == f"connecting with {REDACTED}"
    assert first["detail"] == f"dsn=mysql://u:{REDACTED}@h"
    assert first["parts"] == [REDACTED]
    assert second["msg"] == f"error {REDACTED}"
    assert f"auth failed for {REDACTED}" in second["exc_info"]


def test_secret_with_json_escaped_characters_redacted() -> None:
    secret = 'a"b\\c' + chr(0xE9)
    register_secret(secret)
    logger, stream = make_logger()
    logger.info("value=%s", secret)
    assert lines(stream)[0]["msg"] == f"value={REDACTED}"


@pytest.mark.parametrize("key", ["token", "api_token", "db_password", "client_secret", "Authorization", "API_KEY"])
def test_sensitive_extra_keys_redacted(key: str) -> None:
    logger, stream = make_logger()
    logger.info("request", extra={key: "visible-value", "safe": "ok", "headers": {"authorization": "Bearer x", "accept": "json"}})
    [record] = lines(stream)
    assert record[key] == REDACTED
    assert record["safe"] == "ok"
    assert record["headers"] == {"authorization": REDACTED, "accept": "json"}
    assert "visible-value" not in stream.getvalue()


def test_configure_logging_writes_json_to_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("debug", secrets=["topsecret"])
    logging.getLogger("rapidwire.test.configure").debug("token is topsecret")
    out = capsys.readouterr().out
    [record] = [json.loads(line) for line in out.splitlines()]
    assert record["level"] == "DEBUG"
    assert record["msg"] == f"token is {REDACTED}"
    assert logging.getLogger().level == logging.DEBUG


def test_configure_logging_level_filters(capsys: pytest.CaptureFixture[str]) -> None:
    configure_logging("WARNING")
    logging.getLogger("rapidwire.test.configure").info("hidden")
    logging.getLogger("rapidwire.test.configure").warning("shown")
    msgs = [json.loads(line)["msg"] for line in capsys.readouterr().out.splitlines()]
    assert msgs == ["shown"]


@pytest.mark.parametrize("key", ["session_cookie", "credentials", "db_passwd", "DSN", "private_key"])
def test_extended_sensitive_keys_redacted(key: str) -> None:
    logger, stream = make_logger()
    logger.info("x", extra={key: "visible-value"})
    assert lines(stream)[0][key] == REDACTED


def test_short_secrets_are_ignored() -> None:
    register_secret("abc")
    register_secret("")
    logger, stream = make_logger()
    logger.info("abc abcd")
    assert lines(stream)[0]["msg"] == "abc abcd"


def test_url_quoted_secret_forms_redacted() -> None:
    secret = "p@ss w/rd+&"
    register_secret(secret)
    logger, stream = make_logger()
    logger.info("a=%s b=%s c=%s", quote(secret), quote(secret, safe=""), quote_plus(secret))
    assert lines(stream)[0]["msg"] == f"a={REDACTED} b={REDACTED} c={REDACTED}"


@dataclasses.dataclass
class _Creds:
    user: str
    password: str
    note: str


class _Model(BaseModel):
    api_key: str
    host: str


class _Opaque:
    def __init__(self, value: str) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"_Opaque({self.value})"


def test_structured_extras_go_through_redaction() -> None:
    register_secret("hidden-value")
    logger, stream = make_logger()
    logger.info(
        "x",
        extra={"creds": _Creds("alice", "pw-plain", "has hidden-value"), "model": _Model(api_key="k-plain", host="h"), "opaque": _Opaque("hidden-value")},
    )
    record = lines(stream)[0]
    assert record["creds"] == {"user": "alice", "password": REDACTED, "note": f"has {REDACTED}"}
    assert record["model"] == {"api_key": REDACTED, "host": "h"}
    assert record["opaque"] == f"_Opaque({REDACTED})"
    assert "pw-plain" not in stream.getvalue()
    assert "k-plain" not in stream.getvalue()


def test_uvicorn_color_message_is_dropped() -> None:
    logger, stream = make_logger()
    logger.info("Started server process [%d]", 42, extra={"color_message": "Started server process [\x1b[36m%d\x1b[0m]"})
    [record] = lines(stream)
    assert record["msg"] == "Started server process [42]"
    assert "color_message" not in record
    assert "\\u001b" not in stream.getvalue()


def test_real_uvicorn_startup_records_carry_no_escape_sequences() -> None:
    from uvicorn.config import Config
    from uvicorn.server import Server

    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    uvicorn_logger = logging.getLogger("uvicorn.error")
    saved = (uvicorn_logger.handlers[:], uvicorn_logger.level, uvicorn_logger.propagate)
    uvicorn_logger.handlers[:] = [handler]
    uvicorn_logger.setLevel(logging.INFO)
    uvicorn_logger.propagate = False
    try:
        config = Config(app=lambda scope, receive, send: None, host="127.0.0.1", port=8123, log_config=None)
        config.load()
        Server(config)._log_started_message([])
    finally:
        uvicorn_logger.handlers[:], level, uvicorn_logger.propagate = saved
        uvicorn_logger.setLevel(level)
    [record] = lines(stream)
    assert record["msg"] == "Uvicorn running on http://127.0.0.1:8123 (Press CTRL+C to quit)"
    assert set(record) == {"ts", "level", "logger", "msg"}
    assert "\\u001b" not in stream.getvalue()


@pytest.mark.parametrize(
    "text",
    [
        "\x1b[1;31mred\x1b[0m",
        "\x1b]8;;https://example.com\x1b\\red\x1b]8;;\x1b\\",
        "\x1b]0;title\x07red",
        "\x1bcred",
        "\x1b(Bred",
        "\x9b31mred",
        "r\x1b\x1b[2Je\x1b[0md",
    ],
)
def test_escape_sequences_are_removed_from_every_string(text: str) -> None:
    logger, stream = make_logger()
    logger.info(text, extra={"detail": text, "nested": {text: [text]}, "opaque": _Opaque(text)})
    try:
        raise RuntimeError(text)
    except RuntimeError:
        logger.exception("failed")
    first, second = lines(stream)
    assert first["msg"] == "red"
    assert first["detail"] == "red"
    assert first["nested"] == {"red": ["red"]}
    assert first["opaque"] == "_Opaque(red)"
    assert "RuntimeError: red" in second["exc_info"]
    assert "\\u001b" not in stream.getvalue()
    assert "\\u009b" not in stream.getvalue()


def test_escape_sequences_cannot_hide_a_secret() -> None:
    register_secret("hidden-value")
    logger, stream = make_logger()
    logger.info("hidden\x1b[0m-value")
    assert lines(stream)[0]["msg"] == REDACTED


def test_configure_logging_quiets_alembic_plugins() -> None:
    configure_logging("INFO")
    assert logging.getLogger("alembic.runtime.plugins").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("alembic.runtime.migration").getEffectiveLevel() == logging.INFO


def test_stdlib_logging_is_not_shadowed() -> None:
    import rapidwire.logging as rw_logging

    assert vars(rw_logging)["logging"] is logging
    assert logging.__name__ == "logging"
