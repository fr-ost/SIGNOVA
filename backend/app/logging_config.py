"""Structured logging with secret redaction. Secrets never reach log output."""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime

_STANDARD_ATTRS = set(
    logging.LogRecord("x", logging.INFO, "x", 0, "x", None, None).__dict__.keys()
) | {"message", "asctime"}

_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9_\-\.=]{12,}"),
    re.compile(r"(?i)(x-cmc_pro_api_key[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9\-]{8,}"),
    re.compile(r"(postgres(?:ql)?(?:\+asyncpg)?://[^:/\s]+:)[^@\s]+(@)"),
]


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        self._secrets = sorted({s for s in secrets if s and len(s) >= 8}, key=len, reverse=True)

    def redact(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, "***")
        for pattern in _PATTERNS:
            if pattern.groups == 2:
                text = pattern.sub(r"\1***\2", text)
            elif pattern.groups == 1:
                text = pattern.sub(r"\1***", text)
            else:
                text = pattern.sub("***", text)
        return text


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return self._redactor.redact(json.dumps(payload, default=str))


class TextFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self._redactor = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redactor.redact(super().format(record))


def configure_logging(level: str = "INFO", *, json_logs: bool = True, secrets: Iterable[str] = ()) -> None:
    redactor = Redactor(secrets)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(redactor) if json_logs else TextFormatter(redactor))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers = [handler]
        logger.propagate = False
    for noisy in ("httpx", "httpcore", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
