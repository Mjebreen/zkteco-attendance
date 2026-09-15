"""Collector entry point: APScheduler loop that polls the device every POLL_INTERVAL_MINUTES.

- Runs immediately on start, then on the interval (coalesced, one instance at a time).
- Never crashes on device/network errors: logs, records last_error, and retries next tick.
- With COLLECTOR_TARGET=db it also honours "Sync now" requests queued by the web UI.
- Touches a heartbeat file every loop for the Docker healthcheck.
"""

from __future__ import annotations

import logging
import os
import signal
import sys
from datetime import datetime
from pathlib import Path

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import get_settings
from app.logging_config import setup_logging
from collector.sync import safe_run_once

log = logging.getLogger("collector")

HEARTBEAT = Path(os.getenv("HEARTBEAT_FILE", "/tmp/collector-heartbeat"))
SYNC_REQUEST_POLL_SECONDS = int(os.getenv("SYNC_REQUEST_POLL_SECONDS", "10"))


def _heartbeat() -> None:
    try:
        HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
        HEARTBEAT.write_text(datetime.now().isoformat(timespec="seconds"))
    except OSError:
        pass


def poll_job() -> None:
    settings = get_settings()
    log.info("poll start", extra={"ctx_device": settings.device_label, "ctx_target": settings.collector_target})
    result = safe_run_once(settings, source="collector")
    if result is not None:
        log.info(
            "poll ok",
            extra={
                "ctx_records_inserted": result.get("records_inserted"),
                "ctx_records_received": result.get("records_received"),
                "ctx_users_received": result.get("users_received"),
            },
        )
    _heartbeat()


def sync_request_job() -> None:
    """Honour a "Sync now" flag set by the web UI (db target only)."""
    settings = get_settings()
    _heartbeat()
    if settings.collector_target != "db":
        return
    try:
        from app.db import session_scope
        from app.service import consume_sync_request

        with session_scope() as session:
            requested = consume_sync_request(session)
    except Exception as exc:
        log.warning("sync-request check failed", extra={"ctx_error": str(exc)})
        return
    if requested:
        log.info("manual sync requested via web UI")
        safe_run_once(settings, source="web")


def main() -> int:
    settings = get_settings()
    setup_logging(settings.log_format, settings.log_level)
    # APScheduler logs every job start/finish at INFO; our own poll_job logs are enough.
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
    if not settings.zk_ip:
        log.error("ZK_IP is not set; collector cannot start")
        return 2
    if settings.collector_target == "http" and not settings.ingest_url:
        log.error("COLLECTOR_TARGET=http but INGEST_URL is empty")
        return 2

    if settings.collector_target == "db":
        if os.getenv("AUTO_MIGRATE", "true").strip().lower() in {"1", "true", "yes", "on"}:
            from app.migrate import run_migrations

            run_migrations()

    log.info(
        "collector starting",
        extra={
            "ctx_device": settings.device_label,
            "ctx_target": settings.collector_target,
            "ctx_interval_minutes": settings.poll_interval_minutes,
            "ctx_force_udp": settings.zk_force_udp,
            "ctx_omit_ping": settings.zk_omit_ping,
            "ctx_tz": settings.tz,
        },
    )
    _heartbeat()

    scheduler = BlockingScheduler(timezone=settings.tz, job_defaults={"coalesce": True, "max_instances": 1})
    scheduler.add_job(
        poll_job,
        IntervalTrigger(minutes=settings.poll_interval_minutes),
        id="poll",
        next_run_time=datetime.now(scheduler.timezone),
        misfire_grace_time=60,
    )
    scheduler.add_job(
        sync_request_job,
        IntervalTrigger(seconds=SYNC_REQUEST_POLL_SECONDS),
        id="sync-request",
        misfire_grace_time=30,
    )

    def _stop(*_args) -> None:
        log.info("collector stopping")
        scheduler.shutdown(wait=False)

    signal.signal(signal.SIGTERM, _stop)
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
