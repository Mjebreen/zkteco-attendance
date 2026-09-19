"""JSON / PDF API. Stable contracts, designed to be called from automation tools such as n8n.

    GET  /health                       open
    GET  /report?date=|from=&to=       API key  -> PDF download
    GET  /summary?date=|from=&to=      API key  -> JSON
    POST /api/ingest                   API key  -> upsert users + records
    POST /api/sync                     API key  -> trigger an immediate device pull
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app import pdf, service
from app.auth import require_api_key
from app.config import Settings, get_settings
from app.dates import resolve_range
from app.db import get_db
from app.i18n import normalize_lang
from app.rules import format_hm
from app.sync_trigger import trigger_sync

log = logging.getLogger("app.api")
router = APIRouter()


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


@router.get("/health")
def health(db: Session = Depends(get_db), settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    try:
        state = service.get_sync_state(db, create=False)
        count = service.record_count(db)
        users = service.user_count(db)
    except Exception as exc:  # DB unreachable -> the service is unhealthy
        log.exception("health check failed")
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, f"database error: {exc}") from exc

    last_sync = state.last_sync if state else None
    stale_after = timedelta(minutes=settings.poll_interval_minutes * 3)
    if last_sync is None:
        sync_status = "never"
    elif datetime.now() - last_sync > stale_after:
        sync_status = "stale"
    else:
        sync_status = "ok"
    return {
        "status": "ok",
        "device": settings.device_label or None,
        "company": settings.company_name,
        "last_sync": last_sync.isoformat(timespec="seconds") if last_sync else None,
        "last_attempt": state.last_attempt.isoformat(timespec="seconds") if state and state.last_attempt else None,
        "last_error": state.last_error if state else None,
        "sync_status": sync_status,
        "record_count": count,
        "user_count": users,
        "day_start_hour": settings.day_start_hour,
        "tz": settings.tz,
        "time": datetime.now().isoformat(timespec="seconds"),
    }


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def _range_from_query(request: Request) -> tuple:
    q = request.query_params
    try:
        return resolve_range(q.get("date"), q.get("from"), q.get("to"), q.get("range"), strict=True)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid date: {exc}") from exc


def _department_from_query(request: Request) -> int | None:
    """Optional ?department=<id> filter (departments are defined in this system's settings)."""
    raw = request.query_params.get("department")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "department must be a numeric id") from exc


@router.get("/report", dependencies=[Depends(require_api_key)])
def report(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    """PDF download. `date=today|yesterday|YYYY-MM-DD` or `from=&to=` for a range."""
    from_date, to_date = _range_from_query(request)
    dept_id = _department_from_query(request)
    lang = normalize_lang(request.query_params.get("lang")) or settings.default_lang
    out_dir = settings.output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = (f"_dept{dept_id}" if dept_id else "") + (f"_{lang}" if lang != "en" else "")

    if from_date == to_date:
        rep = service.build_daily(db, from_date, settings.day_start_hour, dept_id)
        filename = f"attendance_{from_date:%Y-%m-%d}{suffix}.pdf"
        path = out_dir / filename
        pdf.build_daily_pdf(str(path), settings.company_name, rep, lang)
    else:
        rep = service.build_range(db, from_date, to_date, settings.day_start_hour, dept_id)
        filename = f"attendance_{from_date:%Y-%m-%d}_to_{to_date:%Y-%m-%d}{suffix}.pdf"
        path = out_dir / filename
        pdf.build_range_pdf(str(path), settings.company_name, rep, lang)

    return FileResponse(
        path,
        media_type="application/pdf",
        filename=filename,
        content_disposition_type="attachment",
    )


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


@router.get("/summary", dependencies=[Depends(require_api_key)])
def summary(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    from_date, to_date = _range_from_query(request)
    dept_id = _department_from_query(request)

    if from_date == to_date:
        rep = service.build_daily(db, from_date, settings.day_start_hour, dept_id)
        return {
            "date": from_date.isoformat(),
            "total": len(rep.employees),
            "present": len(rep.present),
            "absent": len(rep.absent),
            "off": len(rep.off),
            "online": len(rep.online),
            "total_hours": round(rep.total_hours, 2),
            "total_hours_hm": format_hm(rep.total_hours),
            "employees": [
                {
                    "id": e.user_id,
                    "name": e.name,
                    "department": e.department,
                    "first_in": _iso(e.first_in),
                    "last_out": _iso(e.last_out),
                    "hours": round(e.hours_worked, 2),
                    "hours_hm": format_hm(e.hours_worked),
                    "day_type": e.day_type,
                    "attended": e.attended,
                }
                for e in rep.employees
            ],
        }

    rep = service.build_range(db, from_date, to_date, settings.day_start_hour, dept_id)
    return {
        "from": from_date.isoformat(),
        "to": to_date.isoformat(),
        "days_total": rep.days_total,
        "total": len(rep.employees),
        "avg_present_per_day": round(rep.avg_present_per_day, 2),
        "total_hours": round(rep.total_hours, 2),
        "total_hours_hm": format_hm(rep.total_hours),
        "employees": [
            {
                "id": e.user_id,
                "name": e.name,
                "department": e.department,
                "days_present": e.days_present,
                "days_total": e.days_total,
                "days_absent": e.days_absent,
                "days_off": e.days_off,
                "days_online": e.days_online,
                "days_expected": e.days_expected,
                "total_hours": round(e.total_hours, 2),
                "total_hours_hm": format_hm(e.total_hours),
                "avg_hours_per_attended_day": round(e.avg_hours_per_attended_day, 2),
                "avg_hours_hm": format_hm(e.avg_hours_per_attended_day),
                "attendance_rate": round(e.attendance_rate, 4),
            }
            for e in rep.employees
        ],
    }


# --------------------------------------------------------------------------- #
# Ingest / sync
# --------------------------------------------------------------------------- #


class IngestUser(BaseModel):
    id: str | int
    name: str | None = None


class IngestRecord(BaseModel):
    user_id: str | int
    timestamp: datetime | str
    status: int | None = None
    punch: int | None = None


class IngestBody(BaseModel):
    users: list[IngestUser] = Field(default_factory=list)
    records: list[IngestRecord] = Field(default_factory=list)
    source: str | None = None
    deactivate_missing: bool = True


@router.post("/api/ingest", dependencies=[Depends(require_api_key)])
def ingest(body: IngestBody, db: Session = Depends(get_db)) -> dict[str, Any]:
    """Upsert a device snapshot pushed by a remote collector (COLLECTOR_TARGET=http)."""
    try:
        return service.ingest(
            db,
            [u.model_dump() for u in body.users],
            [r.model_dump() for r in body.records],
            source=(body.source or "http")[:32],
            deactivate_missing=body.deactivate_missing,
        )
    except Exception as exc:
        db.rollback()
        log.exception("ingest failed")
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, f"ingest failed: {exc}") from exc


@router.post("/api/sync", dependencies=[Depends(require_api_key)])
async def sync_now(
    wait: bool = Query(True, description="Wait for the device pull to finish (direct mode)"),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    """Trigger an immediate collector run.

    - If this web instance can reach the device (ZK_IP set), the pull runs here and the result is returned.
    - Otherwise a sync request is queued in the database; a co-located collector (COLLECTOR_TARGET=db)
      picks it up within ~10 s.
    """
    result = await run_in_threadpool(trigger_sync, settings, wait)
    if not result.get("ok", True):
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, result)
    return result
