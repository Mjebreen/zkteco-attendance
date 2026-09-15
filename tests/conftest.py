"""Test fixtures: fresh SQLite DB per test, migrated with Alembic, app wired to it."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

TEST_ENV = {
    "DAY_START_HOUR": "7",
    "API_KEY": "test-api-key",
    "DASHBOARD_USER": "admin",
    "DASHBOARD_PASSWORD": "secret",
    "COMPANY_NAME": "Test Co",
    "TZ": "UTC",
    "ZK_IP": "",  # web never talks to the device in tests
    "COLLECTOR_TARGET": "db",
    "LOG_FORMAT": "text",
    "AUTO_MIGRATE": "false",
}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the app at a temp SQLite DB and reset cached settings/engine."""
    db_path = tmp_path / "test.db"
    for k, v in TEST_ENV.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("OUTPUT_DIR", str(tmp_path / "reports"))

    from app import config, db

    config.reset_settings()
    db.reset_engine()
    yield config.get_settings()
    db.reset_engine()
    config.reset_settings()


@pytest.fixture()
def migrated(env):
    from app.migrate import run_migrations

    run_migrations(retries=1, delay=0)
    return env


@pytest.fixture()
def session_factory(migrated):
    from app.db import get_sessionmaker

    return get_sessionmaker()


@pytest.fixture()
def client(migrated):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as c:
        yield c


API_HEADERS = {"X-API-Key": "test-api-key"}
BASIC_AUTH = ("admin", "secret")
