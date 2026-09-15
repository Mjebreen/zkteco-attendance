"""Run Alembic migrations programmatically (used at startup by web + collector)."""

from __future__ import annotations

import logging
import time
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.config import get_settings

log = logging.getLogger("app.migrate")
ROOT = Path(__file__).resolve().parent.parent


def alembic_config(database_url: str | None = None) -> Config:
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url or get_settings().database_url)
    cfg.attributes["url_from_caller"] = True
    cfg.attributes["skip_logging"] = True
    return cfg


def run_migrations(retries: int = 10, delay: float = 3.0) -> None:
    """`alembic upgrade head`, retrying while the DB is starting or briefly locked."""
    cfg = alembic_config()
    from app.db import ensure_sqlite_dir

    ensure_sqlite_dir(cfg.get_main_option("sqlalchemy.url") or "")
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            command.upgrade(cfg, "head")
            log.info("database schema up to date")
            return
        except Exception as exc:  # DB not ready / locked by the sibling container
            last_exc = exc
            log.warning("migration attempt failed", extra={"ctx_attempt": attempt, "ctx_error": str(exc)})
            time.sleep(delay)
    assert last_exc is not None
    raise last_exc
