"""JSON-lines logging to stdout with redaction of registered secrets and sensitive extra fields, and with terminal escape sequences removed from every string."""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import sys
import threading
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, quote_plus

REDACTED = "***"

# Shorter values would redact ordinary words and digits all over the log, so register_secret ignores them.
MIN_SECRET_LENGTH = 4

_SENSITIVE_KEY = re.compile(r"token|password|passwd|secret|authorization|api_key|cookie|credential|dsn|private_key", re.IGNORECASE)

# Attributes every LogRecord carries; anything else on a record came from `extra`.
_RECORD_ATTRS = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {"message", "asctime", "taskName"}

# Extras that only restate the message for a terminal: uvicorn's color_message is msg with ANSI color codes.
_DROPPED_EXTRAS = frozenset({"color_message"})

# ANSI escape sequences (CSI such as colors, OSC such as hyperlinks and window titles, and other ESC sequences such as ESC c or ESC ( B), then any ESC or C1 CSI byte left over.
_ESCAPE_SEQUENCE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?|\x1b[ -/]*[0-~]|\x9b[0-?]*[ -/]*[@-~]|[\x1b\x9b]")

# Library loggers that are too chatty at INFO for service logs.
_QUIET_LOGGERS = {"alembic.runtime.plugins": logging.WARNING}

_secrets: set[str] = set()
_secrets_lock = threading.Lock()


def register_secret(value: str) -> None:
    """Redact value, and its URL-quoted forms, from every log line emitted after this call; values shorter than MIN_SECRET_LENGTH are ignored."""
    if len(value) < MIN_SECRET_LENGTH:
        return
    forms = {value, quote(value), quote(value, safe=""), quote_plus(value)}
    with _secrets_lock:
        _secrets.update(forms)


def clear_secrets() -> None:
    """Forget all registered secrets."""
    with _secrets_lock:
        _secrets.clear()


def _sorted_secrets() -> list[str]:
    with _secrets_lock:
        return sorted(_secrets, key=len, reverse=True)


def redact(text: str) -> str:
    """Replace every registered secret in text with the redaction marker, longest secrets first."""
    for secret in _sorted_secrets():
        text = text.replace(secret, REDACTED)
    return text


def _clean(text: str) -> str:
    """Remove terminal escape sequences from text, then redact registered secrets."""
    return redact(_ESCAPE_SEQUENCE.sub("", text))


def _redact_json(line: str) -> str:
    # A rendered JSON line holds secrets in their escaped form, which differs from the raw value for quotes, backslashes and non-ASCII characters.
    for secret in _sorted_secrets():
        line = line.replace(json.dumps(secret, ensure_ascii=True)[1:-1], REDACTED)
    return line


def _jsonable(value: Any) -> Any:
    """Convert an extra value to JSON-compatible data, masking sensitive keys at any depth, and removing escape sequences and registered secrets from strings and keys."""
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _clean(value)
    if isinstance(value, Mapping):
        return {_clean(str(k)): REDACTED if _SENSITIVE_KEY.search(str(k)) else _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v) for v in value]
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return _jsonable({field.name: getattr(value, field.name) for field in dataclasses.fields(value)})
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump) and not isinstance(value, type):
        try:
            dumped = model_dump()
        except Exception:
            dumped = None
        if isinstance(dumped, Mapping):
            return _jsonable(dumped)
    return _clean(repr(value))


class JsonFormatter(logging.Formatter):
    """Render a record as one JSON object per line: ts, level, logger, msg, extra fields except _DROPPED_EXTRAS, and exc_info; no string in it carries a terminal escape sequence."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": _clean(record.levelname),
            "logger": _clean(record.name),
            "msg": _clean(record.getMessage()),
        }
        for key, value in record.__dict__.items():
            if key in _RECORD_ATTRS or key in _DROPPED_EXTRAS or key in payload:
                continue
            payload[_clean(key)] = REDACTED if _SENSITIVE_KEY.search(key) else _jsonable(value)
        if record.exc_info:
            payload["exc_info"] = _clean(self.formatException(record.exc_info))
        elif record.exc_text:
            payload["exc_info"] = _clean(record.exc_text)
        if record.stack_info:
            payload["stack_info"] = _clean(self.formatStack(record.stack_info))
        return _redact_json(json.dumps(payload, ensure_ascii=True, default=repr))


def configure_logging(level: str, secrets: Iterable[str] = ()) -> None:
    """Route all logging, including library loggers such as discord.py, uvicorn and alembic, through one stdout JsonFormatter handler on the root logger."""
    for secret in secrets:
        register_secret(secret)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(level.upper())
    for name, quiet_level in _QUIET_LOGGERS.items():
        logging.getLogger(name).setLevel(max(quiet_level, root.level))
    logging.captureWarnings(True)
