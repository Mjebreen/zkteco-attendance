"""Configuration from environment variables.

See .env.example for every variable.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# Load a .env next to the repo root when present (Docker passes real env vars).
load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)

_TZ_NAME = os.getenv("TZ") or "UTC"
if sys.platform == "win32" and "TZ" in os.environ:
    # The Windows C runtime cannot parse IANA names like "Europe/Berlin" and silently falls back to
    # UTC for every localtime() call. TZ only matters inside the Linux container, so drop it here
    # (before the first clock call) and rely on the machine's own zone.
    os.environ.pop("TZ")


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _int(value: str | None, default: int) -> int:
    try:
        return int(value) if value not in (None, "") else default
    except ValueError:
        return default


def _float(value: str | None, default: float) -> float:
    try:
        return float(value) if value not in (None, "") else default
    except ValueError:
        return default


def _hhmm(value: str | None, default: str = "") -> str:
    """Validate an HH:MM string. Unset -> default; explicitly empty or malformed -> '' (off)."""
    if value is None:
        return default
    v = value.strip()
    if not v:
        return ""
    try:
        h, m = v.split(":")
        if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
            return f"{int(h):02d}:{int(m):02d}"
    except ValueError:
        pass
    return ""


def _lang(value: str | None) -> str:
    v = (value or "en").strip().lower()[:2]
    return v if v in {"en", "ar"} else "en"


def normalize_database_url(url: str) -> str:
    """Map common Postgres URL spellings onto the psycopg3 driver."""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://") :]
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg://" + url[len("postgresql://") :]
    return url


@dataclass(frozen=True)
class Settings:
    # Device
    zk_ip: str
    zk_port: int
    zk_password: int
    zk_timeout: int
    zk_force_udp: bool
    zk_omit_ping: bool
    # Business
    company_name: str
    day_start_hour: int
    tz: str
    target_hours: float  # daily hours target shown as a progress bar
    late_after: str  # first punch at/after this time-of-day is "Late" ("" = off)
    early_before: str  # first punch before this time-of-day is "Early" ("" = off)
    late_early_turn: str  # time-of-day where the late window ends and the early window begins ("" = midnight)
    default_lang: str  # "en" | "ar"
    week_start: int  # first column of calendars: 0 = Monday ... 5 = Saturday, 6 = Sunday
    # Storage / API
    database_url: str
    api_key: str
    output_dir: Path
    # Collector
    poll_interval_minutes: int
    collector_target: str  # "db" | "http"
    ingest_url: str
    # Web
    server_host: str
    server_port: int
    dashboard_user: str
    dashboard_password: str
    log_format: str  # "json" | "text"
    log_level: str

    @property
    def shift_label(self) -> str:
        """Human label for the shift window, e.g. 'Shift window: 07:00 -> next day 06:59'."""
        if self.day_start_hour:
            end_hour = (self.day_start_hour - 1) % 24
            return f"Shift window: {self.day_start_hour:02d}:00 → next day {end_hour:02d}:59"
        return ""

    @property
    def device_label(self) -> str:
        return f"{self.zk_ip}:{self.zk_port}" if self.zk_ip else ""


def load_settings() -> Settings:
    day_start = _int(os.getenv("DAY_START_HOUR"), 7)
    if not 0 <= day_start <= 23:
        raise ValueError("DAY_START_HOUR must be between 0 and 23")
    target = (os.getenv("COLLECTOR_TARGET") or "db").strip().lower()
    if target not in {"db", "http"}:
        raise ValueError("COLLECTOR_TARGET must be 'db' or 'http'")
    return Settings(
        zk_ip=(os.getenv("ZK_IP") or "").strip(),
        zk_port=_int(os.getenv("ZK_PORT"), 4370),
        zk_password=_int(os.getenv("ZK_PASSWORD"), 0),
        zk_timeout=_int(os.getenv("ZK_TIMEOUT"), 10),
        zk_force_udp=_bool(os.getenv("ZK_FORCE_UDP"), False),
        zk_omit_ping=_bool(os.getenv("ZK_OMIT_PING"), False),
        company_name=os.getenv("COMPANY_NAME") or "Attendance",
        day_start_hour=day_start,
        tz=_TZ_NAME,
        target_hours=_float(os.getenv("TARGET_HOURS"), 6.0),
        late_after=_hhmm(os.getenv("LATE_AFTER_TIME"), "18:30"),
        early_before=_hhmm(os.getenv("EARLY_BEFORE_TIME"), "13:00"),
        late_early_turn=_hhmm(os.getenv("LATE_EARLY_TURN_TIME"), "01:00"),
        default_lang=_lang(os.getenv("DEFAULT_LANG")),
        week_start={"monday": 0, "saturday": 5, "sunday": 6}.get((os.getenv("WEEK_START") or "monday").strip().lower(), 0),
        database_url=normalize_database_url(os.getenv("DATABASE_URL") or "sqlite:////data/attendance.db"),
        api_key=(os.getenv("API_KEY") or "").strip(),
        output_dir=Path(os.getenv("OUTPUT_DIR") or "/data/reports"),
        poll_interval_minutes=max(1, _int(os.getenv("POLL_INTERVAL_MINUTES"), 5)),
        collector_target=target,
        ingest_url=(os.getenv("INGEST_URL") or "").strip().rstrip("/"),
        server_host=os.getenv("SERVER_HOST") or "0.0.0.0",
        server_port=_int(os.getenv("SERVER_PORT"), 5000),
        dashboard_user=os.getenv("DASHBOARD_USER") or "admin",
        dashboard_password=os.getenv("DASHBOARD_PASSWORD") or "",
        log_format=(os.getenv("LOG_FORMAT") or "json").strip().lower(),
        log_level=(os.getenv("LOG_LEVEL") or "INFO").strip().upper(),
    )


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = load_settings()
    return _settings


def reset_settings() -> None:
    """Forget the cached settings (tests change env vars between runs)."""
    global _settings
    _settings = None
