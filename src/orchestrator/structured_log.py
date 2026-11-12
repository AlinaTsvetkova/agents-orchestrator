"""JSON structured logging — stderr sink, env-driven level/format."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone

LOG_LEVEL_ENV = "ORCHESTRATOR_LOG_LEVEL"
LOG_FORMAT_ENV = "ORCHESTRATOR_LOG_FORMAT"
_NAMESPACE = "orchestrator"

_RESERVED = frozenset(vars(logging.LogRecord("", 0, "", 0, None, None, None))) | {
    "message",
    "asctime",
}


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z"),
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED or key.startswith("_"):
                continue
            payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, sort_keys=True)


_configured = False


def _configure_once() -> None:
    global _configured
    if _configured:
        return
    root = logging.getLogger(_NAMESPACE)
    level_name = os.environ.get(LOG_LEVEL_ENV, "INFO").strip().upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))
    root.propagate = False
    if not root.handlers:
        handler = logging.StreamHandler(sys.stderr)
        if os.environ.get(LOG_FORMAT_ENV, "json").strip().lower() == "text":
            handler.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
        else:
            handler.setFormatter(_JsonFormatter())
        root.addHandler(handler)
    _configured = True


def get_logger(name: str) -> logging.Logger:
    _configure_once()
    short = name.rsplit(".", 1)[-1] if name else "root"
    return logging.getLogger(f"{_NAMESPACE}.{short}")


def reset_for_tests() -> None:
    global _configured
    root = logging.getLogger(_NAMESPACE)
    for handler in list(root.handlers):
        root.removeHandler(handler)
    _configured = False
