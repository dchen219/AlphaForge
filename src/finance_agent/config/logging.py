"""Structured logging with credential redaction."""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime

_SECRET_PATTERNS = [
    re.compile(r"(api_key=)[^&\s\"']+", re.IGNORECASE),
    re.compile(r"(x-api-key[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", re.IGNORECASE),
    re.compile(r"(sk-ant-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]+"),
    re.compile(r"(authorization[\"']?\s*[:=]\s*[\"']?bearer\s+)[^\s\"',}]+", re.IGNORECASE),
    re.compile(r"(apca-api-(?:key-id|secret-key)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+", re.IGNORECASE),
]


def redact(text: str) -> str:
    """Remove credential material from arbitrary text (URLs, messages, tracebacks)."""
    for pat in _SECRET_PATTERNS:
        text = pat.sub(r"\1***REDACTED***", text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # Render first (so %d etc. still work), then redact the final text.
        try:
            message = record.getMessage()
        except Exception:
            message = f"{record.msg} {record.args}"
        record.msg = redact(message)
        record.args = None
        return True


_RESERVED = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return redact(json.dumps(payload, default=str))


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging(level: str = "INFO", json_output: bool = True) -> None:
    root = logging.getLogger()
    for h in list(root.handlers):
        if getattr(h, "_finance_agent", False):
            root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler._finance_agent = True  # type: ignore[attr-defined]
    handler.addFilter(RedactingFilter())
    handler.setFormatter(
        JsonFormatter() if json_output else TextFormatter("%(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)
    root.setLevel(level)
    # httpx logs full request URLs (FRED keys are query params) at INFO — silence it.
    for noisy in ("httpx", "httpx2", "httpcore", "anthropic"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
