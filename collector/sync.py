"""One collector run: pull the device, then write to the DB or push to /api/ingest."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config import Settings
from app.db import session_scope
from app.service import ingest, record_sync_failure
from collector.device import fetch_snapshot

log = logging.getLogger("collector.sync")


def _serialize(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "user_id": r["user_id"],
            "timestamp": r["timestamp"].isoformat(timespec="seconds"),
            "status": r.get("status"),
            "punch": r.get("punch"),
        }
        for r in records
    ]


def push_http(settings: Settings, users: list[dict], records: list[dict], source: str) -> dict[str, Any]:
    if not settings.ingest_url:
        raise RuntimeError("INGEST_URL is not configured (COLLECTOR_TARGET=http)")
    if not settings.api_key:
        raise RuntimeError("API_KEY is required to push to INGEST_URL")
    url = settings.ingest_url
    if not url.endswith("/api/ingest"):
        url = url + "/api/ingest"
    payload = {"users": users, "records": _serialize(records), "source": source}
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=15.0)) as client:
        resp = client.post(url, json=payload, headers={"X-API-Key": settings.api_key})
    if resp.status_code >= 400:
        raise RuntimeError(f"ingest endpoint returned {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def run_once(settings: Settings, source: str = "collector") -> dict[str, Any]:
    """Pull + store. Raises on failure (callers decide whether to log or propagate)."""
    users, records = fetch_snapshot(settings)
    log.info(
        "device snapshot fetched",
        extra={"ctx_device": settings.device_label, "ctx_users": len(users), "ctx_records": len(records)},
    )
    if settings.collector_target == "http":
        return push_http(settings, users, records, source)
    with session_scope() as session:
        return ingest(session, users, records, source=source)


def safe_run_once(settings: Settings, source: str = "collector") -> dict[str, Any] | None:
    """Like run_once but never raises: logs, records the failure, returns None."""
    try:
        return run_once(settings, source=source)
    except Exception as exc:
        log.error("collector run failed", extra={"ctx_device": settings.device_label, "ctx_error": str(exc)})
        if settings.collector_target == "db":
            try:
                with session_scope() as session:
                    record_sync_failure(session, str(exc), source)
            except Exception as db_exc:  # DB down too: nothing more we can do this round
                log.error("could not record sync failure", extra={"ctx_error": str(db_exc)})
        return None
