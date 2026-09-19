"""Structured logging to stdout (JSON by default, plain text with LOG_FORMAT=text)."""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),  # local, with offset
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key.startswith("ctx_"):
                payload[key[4:]] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_audit_file(path: str) -> None:
    """Mirror every audit entry into a plain-text file (tail -f friendly). Idempotent."""
    audit = logging.getLogger("audit")
    audit.setLevel(logging.INFO)
    if not path:
        return
    target = os.path.abspath(path)
    for h in audit.handlers:
        if isinstance(h, logging.FileHandler) and os.path.abspath(h.baseFilename) == target:
            return
    try:
        os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
        handler = logging.FileHandler(target, encoding="utf-8")
    except OSError as exc:
        logging.getLogger("app").warning("audit log file unavailable", extra={"ctx_path": path, "ctx_error": str(exc)})
        return
    handler.setFormatter(logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    audit.addHandler(handler)


def setup_logging(fmt: str = "json", level: str = "INFO") -> None:
    root = logging.getLogger()
    root.handlers.clear()
    handler = logging.StreamHandler(sys.stdout)
    if fmt == "text":
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    else:
        handler.setFormatter(JsonFormatter())
    root.addHandler(handler)
    root.setLevel(level)
    # Uvicorn installs its own handlers; route them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
