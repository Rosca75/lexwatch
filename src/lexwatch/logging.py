"""JSON structured logging to stdout (stdlib only).

Usage::

    log = logging.getLogger(__name__)
    log.info("ingest.done", extra={"added": 3, "run_id": 12})

Anything passed in ``extra`` ends up as top-level JSON keys. Never log personal data beyond
user id and email (spec section 8).
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

_RESERVED = set(
    logging.LogRecord("", 0, "", 0, "", None, None).__dict__.keys()
) | {"message", "asctime", "taskName"}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO") -> None:
    """Install the JSON stdout handler on the root logger (idempotent).

    Other handlers (e.g. pytest's capture handler) are left alone; uvicorn's own handlers are
    removed so its records propagate to root and come out as JSON too.
    """
    root = logging.getLogger()
    if not any(getattr(h, "_lexwatch", False) for h in root.handlers):
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(JsonFormatter())
        handler._lexwatch = True  # type: ignore[attr-defined]
        root.addHandler(handler)
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
