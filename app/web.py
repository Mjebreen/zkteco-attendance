"""Server-rendered web UI (Jinja2, no framework): dashboard, print view, employee profile,
settings (departments + employee assignment), CSV export, manual sync.

Everything here sits behind HTTP Basic Auth (DASHBOARD_USER / DASHBOARD_PASSWORD).
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import branding, i18n, rules, service
from app.accounts import record_audit
from app.auth import client_ip, current_user, require_dashboard_auth
from app.config import Settings, get_settings
from app.dates import resolve_range, today
from app.db import get_db
from app.sync_trigger import trigger_sync

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(dependencies=[Depends(require_dashboard_auth)])

LANG_COOKIE = "lang"
KIND_LABEL = {"work": "working day", "off": "day off", "online": "online", "vacation": "vacation", "auto": "auto"}
WEEKDAY_EN = i18n.WEEKDAYS["en"]


def _audit(request: Request, db: Session, action: str, summary: str, target: str | None = None, **details: Any) -> None:
    record_audit(db, current_user(request), action, summary, target=target, details=details or None, ip=client_ip(request))


def _emp_label(db: Session, user_id: str) -> str:
    emp = service.get_employee(db, user_id)
    return f"{emp.name} ({user_id})" if emp else f"employee {user_id}"


def _pattern_text(pattern: dict[int, str]) -> str:
    return ", ".join(f"{WEEKDAY_EN[wd]}={KIND_LABEL.get(k, k)}" for wd, k in sorted(pattern.items())) or "all working days"


def _scope_text(db: Session, scope: str) -> str:
    if scope == "none":
        return "employees without a department"
    if scope.isdigit():
        dept = next((d for d in service.list_departments(db) if d.id == int(scope)), None)
        return f"department {dept.name}" if dept else f"department #{scope}"
    return "all employees"


# --------------------------------------------------------------------------- #
# Template helpers
# --------------------------------------------------------------------------- #


def _fmt_hours(v: float | None) -> str:
    """H:MM, or a dash when there is no completed duration."""
    return rules.format_hm(v) if v else "—"


def _fmt_duration(v: float | None) -> str:
    """H:MM, always (0:00 for nothing) - used for totals."""
    return rules.format_hm(v)


def _fmt_time(dt: datetime | None) -> str:
    return dt.strftime("%H:%M:%S") if dt else "—"


def _fmt_hm(dt: datetime | None) -> str:
    return dt.strftime("%H:%M") if dt else "—"


def _pct(rate: float) -> int:
    return int(rate * 100)


templates.env.filters["hours"] = _fmt_hours
templates.env.filters["hm"] = _fmt_duration
templates.env.filters["hhmmss"] = _fmt_time
templates.env.filters["hhmm"] = _fmt_hm
templates.env.filters["pct"] = _pct


def _lang(request: Request, settings: Settings) -> str:
    return i18n.resolve_lang(
        request.query_params.get("lang"),
        request.cookies.get(LANG_COOKIE),
        request.headers.get("accept-language"),
        settings.default_lang,
    )


def _url_with(request: Request, **changes: Any) -> str:
    params = dict(request.query_params)
    for k, v in changes.items():
        if v is None or v == "":
            params.pop(k, None)
        else:
            params[k] = str(v)
    qs = urlencode(params)
    return request.url.path + ("?" + qs if qs else "")


def _render(request: Request, template: str, ctx: dict[str, Any], status_code: int = 200):
    lang = ctx["lang"]
    resp = templates.TemplateResponse(request, template, ctx, status_code=status_code)
    if request.query_params.get("lang"):
        resp.set_cookie(LANG_COOKIE, lang, max_age=365 * 24 * 3600, samesite="lax")
    return resp


def _sync_info(db: Session, settings: Settings) -> dict[str, Any]:
    state = service.get_sync_state(db, create=False)
    last = state.last_sync if state else None
    minutes = int((datetime.now() - last).total_seconds() // 60) if last else None
    stale = minutes is None or minutes > settings.poll_interval_minutes * 3
    return {
        "last_sync": last,
        "last_sync_minutes": minutes,
        "last_error": state.last_error if state else None,
        "sync_stale": stale,
    }


def _shift_label(settings: Settings, lang: str) -> str:
    h = settings.day_start_hour
    if not h:
        return ""
    return f"{i18n.t(lang, 'shift_window')}: {h:02d}:00 → {i18n.t(lang, 'next_day')} {(h - 1) % 24:02d}:59"


def _dept_id(request: Request) -> int | None:
    raw = request.query_params.get("department")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _base_context(request: Request, db: Session, settings: Settings, page: str) -> dict[str, Any]:
    lang = _lang(request, settings)
    other = "ar" if lang == "en" else "en"
    departments = service.list_departments(db)
    dept_id = _dept_id(request)
    dept = next((d for d in departments if d.id == dept_id), None)
    brand = branding.load(db, settings)
    return {
        "brand": brand,
        "request": request,
        "page": page,
        "lang": lang,
        "rtl": i18n.is_rtl(lang),
        "t": lambda key, **kw: i18n.t(lang, key, **kw),
        "fmt_date": lambda d, style="long": i18n.fmt_date(d, lang, style),
        "lang_switch_url": _url_with(request, lang=other),
        "url_with": lambda **kw: _url_with(request, **kw),
        "company": brand.company_name,
        "device": settings.device_label,
        "shift_label": _shift_label(settings, lang),
        "target_hours": settings.target_hours,
        "late_after": settings.late_after,
        "early_before": settings.early_before,
        "late_early_turn": settings.late_early_turn,
        "poll_interval": settings.poll_interval_minutes,
        "now": datetime.now(),
        "departments": departments,
        "department_id": dept.id if dept else None,
        "department_name": dept.name if dept else None,
        "user": current_user(request),
        "msg": request.query_params.get("msg"),
        "synced": request.query_params.get("synced"),
        "sync_error": request.query_params.get("sync_error"),
        **_sync_info(db, settings),
    }


# --------------------------------------------------------------------------- #
# Row decoration (live status, target progress, late badge)
# --------------------------------------------------------------------------- #


def _flag(e: rules.EmployeeDay, settings: Settings) -> str | None:
    """'late' / 'early' / None for the first punch (see rules.checkin_flag)."""
    return rules.checkin_flag(e.first_in, settings.late_after, settings.early_before, settings.late_early_turn)


def _decorate_day_rows(rep: rules.DailyReport, settings: Settings, live: bool, now: datetime) -> list[dict[str, Any]]:
    rows = []
    for e in rep.present:
        hours = rules.live_hours(e, now) if live else e.hours_worked
        open_session = live and e.last_out is None
        rows.append(
            {
                "e": e,
                "hours": hours,
                "progress": rules.target_progress(hours, settings.target_hours),
                "met": hours >= settings.target_hours > 0,
                "open": open_session,
                "flag": _flag(e, settings),
                "search": f"{e.name} {e.user_id} {e.department or ''}".lower(),
            }
        )
    return rows


def _dept_breakdown_day(rep: rules.DailyReport, rows: list[dict[str, Any]], no_dept_label: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    open_by_id = {r["e"].user_id for r in rows if r["open"]}
    for e in rep.employees:
        key = e.department or no_dept_label
        g = groups.setdefault(
            key, {"name": key, "total": 0, "present": 0, "absent": 0, "off": 0, "online": 0, "vacation": 0, "hours": 0.0,
                  "open": 0}
        )
        g["total"] += 1
        if e.attended:
            g["present"] += 1
            g["hours"] += e.hours_worked
            if e.user_id in open_by_id:
                g["open"] += 1
        elif e.day_type == "off":
            g["off"] += 1
        elif e.day_type == "online":
            g["online"] += 1
        elif e.day_type == "vacation":
            g["vacation"] += 1
        else:
            g["absent"] += 1
    out = sorted(groups.values(), key=lambda g: (g["name"] == no_dept_label, g["name"].lower()))
    for g in out:
        expected = g["total"] - g["off"] - g["vacation"]
        g["rate"] = min(1.0, (g["present"] + g["online"]) / expected) if expected else 0
    return out


def _dept_breakdown_range(rep: rules.RangeReport, no_dept_label: str) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for e in rep.employees:
        key = e.department or no_dept_label
        g = groups.setdefault(key, {"name": key, "employees": 0, "present_days": 0, "hours": 0.0})
        g["employees"] += 1
        g["present_days"] += e.days_present
        g["hours"] += e.total_hours
    out = sorted(groups.values(), key=lambda g: (g["name"] == no_dept_label, g["name"].lower()))
    for g in out:
        possible = g["employees"] * rep.days_total
        g["rate"] = g["present_days"] / possible if possible else 0
        g["avg_present"] = g["present_days"] / rep.days_total if rep.days_total else 0
    return out


def _presence_per_day(rep: rules.RangeReport) -> list[dict[str, Any]]:
    counts: dict[date, int] = {}
    for e in rep.employees:
        for d, day in e.per_day.items():
            if day.attended:
                counts[d] = counts.get(d, 0) + 1
    total = len(rep.employees) or 1
    out = []
    d = rep.from_date
    while d <= rep.to_date:
        n = counts.get(d, 0)
        out.append({"date": d, "count": n, "pct": int(n / total * 100)})
        d += timedelta(days=1)
    return out


# --------------------------------------------------------------------------- #
# Dashboard / print
# --------------------------------------------------------------------------- #


def _report_context(request: Request, db: Session, settings: Settings, page: str) -> dict[str, Any]:
    ctx = _base_context(request, db, settings, page)
    q = request.query_params
    from_date, to_date = resolve_range(q.get("date"), q.get("from"), q.get("to"), q.get("range"))
    is_range = from_date != to_date
    lang, t = ctx["lang"], ctx["t"]
    now = ctx["now"]
    ctx.update(
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        is_range=is_range,
        today=today().isoformat(),
        auto_print=q.get("auto") == "1",
        month_start=today().replace(day=1).isoformat(),
    )
    dept_id = ctx["department_id"]
    if is_range:
        rep = service.build_range(db, from_date, to_date, settings.day_start_hour, dept_id)
        h = settings.day_start_hour
        if h:
            each = t("shift_days_each", a=f"{h:02d}:00", b=f"{(h - 1) % 24:02d}:59")
        else:
            each = t("days")
        ctx.update(
            window_label=f"{i18n.fmt_date(from_date, lang, 'short')} → {i18n.fmt_date(to_date, lang, 'long')} · {rep.days_total} {each}",
            from_pretty=i18n.fmt_date(from_date, lang, "medium"),
            to_pretty=i18n.fmt_date(to_date, lang, "medium"),
            days_total=rep.days_total,
            total=len(rep.employees),
            avg_present=f"{rep.avg_present_per_day:.1f}",
            total_hours=rules.format_hm(rep.total_hours),
            range_list=rep.employees,
            range_rows=[
                {"e": e, "search": f"{e.name} {e.user_id} {e.department or ''}".lower(),
                 "met": e.days_present > 0 and e.avg_hours_per_attended_day >= settings.target_hours > 0}
                for e in rep.employees
            ],
            dept_breakdown=_dept_breakdown_range(rep, t("no_department")),
            presence=_presence_per_day(rep),
            has_schedule=any(e.days_off or e.days_online or e.days_vacation for e in rep.employees),
            live=False,
        )
    else:
        rep = service.build_daily(db, from_date, settings.day_start_hour, dept_id)
        live = rules.is_live_day(from_date, settings.day_start_hour, now)
        rows = _decorate_day_rows(rep, settings, live, now)
        h = settings.day_start_hour
        label = i18n.fmt_date(from_date, lang, "long")
        if h:
            label += f" · {h:02d}:00 → {t('next_day')} {(h - 1) % 24:02d}:59"
        ctx.update(
            window_label=label,
            from_pretty=i18n.fmt_date(from_date, lang, "long"),
            to_pretty=i18n.fmt_date(to_date, lang, "long"),
            days_total=1,
            total=len(rep.employees),
            present=len(rep.present),
            absent=len(rep.absent),
            currently_in=sum(1 for r in rows if r["open"]),
            late_count=sum(1 for r in rows if r["flag"] == "late"),
            early_count=sum(1 for r in rows if r["flag"] == "early"),
            total_hours=rules.format_hm(rep.total_hours),
            present_list=rep.present,
            absent_list=rep.absent,
            off_list=rep.off,
            online_list=rep.online,
            off_count=len(rep.off),
            vacation_list=rep.vacation,
            vacation_count=len(rep.vacation),
            vacation_rows=[{"e": e, "search": f"{e.name} {e.user_id} {e.department or ''}".lower()} for e in rep.vacation],
            online_count=len(rep.online),
            present_rows=rows,
            absent_rows=[{"e": e, "search": f"{e.name} {e.user_id} {e.department or ''}".lower()} for e in rep.absent],
            off_rows=[{"e": e, "search": f"{e.name} {e.user_id} {e.department or ''}".lower()} for e in rep.off],
            online_rows=[{"e": e, "search": f"{e.name} {e.user_id} {e.department or ''}".lower()} for e in rep.online],
            dept_breakdown=_dept_breakdown_day(rep, rows, t("no_department")),
            live=live,
        )
    return ctx


@router.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return _render(request, "dashboard.html", _report_context(request, db, settings, "dashboard"))


@router.get("/print", response_class=HTMLResponse)
def print_view(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    return _render(request, "print.html", _report_context(request, db, settings, "print"))


# --------------------------------------------------------------------------- #
# CSV export
# --------------------------------------------------------------------------- #


@router.get("/export.csv")
def export_csv(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    q = request.query_params
    from_date, to_date = resolve_range(q.get("date"), q.get("from"), q.get("to"), q.get("range"))
    dept_id = _dept_id(request)
    buf = io.StringIO()
    w = csv.writer(buf)
    if from_date == to_date:
        rep = service.build_daily(db, from_date, settings.day_start_hour, dept_id)
        w.writerow(["date", "employee", "id", "department", "attended", "first_in", "last_out", "hours", "hours_hm", "punches", "checkin", "day_type"])
        for e in rep.employees:
            w.writerow([
                from_date.isoformat(), e.name, e.user_id, e.department or "", "yes" if e.attended else "no",
                e.first_in.isoformat(timespec="seconds") if e.first_in else "",
                e.last_out.isoformat(timespec="seconds") if e.last_out else "",
                f"{e.hours_worked:.2f}", rules.format_hm(e.hours_worked), e.punches, _flag(e, settings) or "", e.day_type,
            ])
        filename = f"attendance_{from_date:%Y-%m-%d}.csv"
    else:
        rep = service.build_range(db, from_date, to_date, settings.day_start_hour, dept_id)
        w.writerow(["from", "to", "employee", "id", "department", "days_present", "days_total", "days_absent",
                    "attendance_rate", "total_hours", "total_hours_hm", "avg_hours_per_attended_day",
                    "avg_hours_hm", "days_off", "days_online", "days_vacation", "days_expected"])
        for e in rep.employees:
            w.writerow([
                from_date.isoformat(), to_date.isoformat(), e.name, e.user_id, e.department or "",
                e.days_present, e.days_total, e.days_absent, f"{e.attendance_rate:.4f}",
                f"{e.total_hours:.2f}", rules.format_hm(e.total_hours),
                f"{e.avg_hours_per_attended_day:.2f}", rules.format_hm(e.avg_hours_per_attended_day),
                e.days_off, e.days_online, e.days_vacation, e.days_expected,
            ])
        filename = f"attendance_{from_date:%Y-%m-%d}_to_{to_date:%Y-%m-%d}.csv"
    data = "﻿" + buf.getvalue()  # BOM so Excel opens UTF-8 (Arabic names) correctly
    _audit(request, db, "export.csv", f"Exported CSV {filename}", department=dept_id)
    return Response(
        content=data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# --------------------------------------------------------------------------- #
# Employee profile
# --------------------------------------------------------------------------- #


@router.get("/employee/{user_id}", response_class=HTMLResponse)
def employee_profile(
    user_id: str, request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    ctx = _base_context(request, db, settings, "employee")
    emp = service.get_employee(db, user_id)
    if emp is None:
        ctx.update(employee=None)
        return _render(request, "employee.html", ctx, status_code=404)
    q = request.query_params
    if q.get("from") or q.get("to") or q.get("range") or q.get("date"):
        from_date, to_date = resolve_range(q.get("date"), q.get("from"), q.get("to"), q.get("range"))
    else:
        to_date = today()
        from_date = to_date - timedelta(days=29)
    days = service.build_employee_history(db, emp, from_date, to_date, settings.day_start_hour)
    now = ctx["now"]
    rows = []
    for d, day in zip(_daterange(from_date, to_date), days):
        live = rules.is_live_day(d, settings.day_start_hour, now)
        hours = rules.live_hours(day, now) if live else day.hours_worked
        rows.append(
            {
                "date": d,
                "e": day,
                "hours": hours,
                "progress": rules.target_progress(hours, settings.target_hours),
                "met": hours >= settings.target_hours > 0,
                "open": live and day.attended and day.last_out is None,
                "live": live,
                "flag": _flag(day, settings),
            }
        )
    present_days = sum(1 for r in rows if r["e"].attended)
    off_days = sum(1 for r in rows if not r["e"].attended and r["e"].day_type == "off")
    online_days = sum(1 for r in rows if not r["e"].attended and r["e"].day_type == "online")
    vacation_days = sum(1 for r in rows if not r["e"].attended and r["e"].day_type == "vacation")
    expected_days = len(rows) - off_days - vacation_days
    total_hours = sum(r["e"].hours_worked for r in rows)
    ctx.update(_calendar_context(request, db, settings, emp, ctx["lang"]))
    ctx.update(
        employee=emp,
        from_date=from_date.isoformat(),
        to_date=to_date.isoformat(),
        rows=list(reversed(rows)),  # newest first
        days_total=len(rows),
        days_expected=expected_days,
        days_present=present_days,
        days_off=off_days,
        days_online=online_days,
        days_vacation=vacation_days,
        vacations=service.list_vacations(db, emp.user_id),
        days_absent=max(0, expected_days - present_days - online_days),
        rate=min(1.0, (present_days + online_days) / expected_days) if expected_days else 0,
        total_hours=total_hours,
        avg_hours=total_hours / present_days if present_days else 0,
        target_met_days=sum(1 for r in rows if r["met"] and not r["open"]),
    )
    return _render(request, "employee.html", ctx)


def _daterange(a: date, b: date):
    d = a
    while d <= b:
        yield d
        d += timedelta(days=1)


# --------------------------------------------------------------------------- #
# Employee schedule: weekly pattern + month calendar with per-date overrides
# --------------------------------------------------------------------------- #


def _parse_month(raw: str | None) -> date:
    try:
        y, m = (raw or "").split("-")
        return date(int(y), int(m), 1)
    except (ValueError, TypeError):
        return today().replace(day=1)


def _calendar_context(request: Request, db: Session, settings: Settings, emp: rules.Employee, lang: str) -> dict[str, Any]:
    first = _parse_month(request.query_params.get("month"))
    next_first = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    last = next_first - timedelta(days=1)
    prev_first = (first - timedelta(days=1)).replace(day=1)
    ws = settings.week_start
    grid_start = first - timedelta(days=(first.weekday() - ws) % 7)
    grid_end = last + timedelta(days=6 - (last.weekday() - ws) % 7)

    days = service.build_employee_history(db, emp, grid_start, grid_end, settings.day_start_hour)
    weekly = service.get_weekly_pattern(db, emp.user_id)
    overrides = service.get_overrides(db, emp.user_id, grid_start, grid_end)
    current = rules.shift_day(datetime.now(), settings.day_start_hour)

    cells = []
    for d, day in zip(_daterange(grid_start, grid_end), days):
        ov = overrides.get(d)
        if day.attended:
            state = "worked_off" if day.day_type in ("off", "vacation") else "present"
        elif day.day_type in ("off", "online", "vacation"):
            state = day.day_type
        elif d > current:
            state = "future"
        elif d == current:
            state = "today"
        else:
            state = "absent"
        cells.append(
            {
                "date": d,
                "in_month": d.month == first.month,
                "day_type": day.day_type,
                "weekly_kind": weekly.get(d.weekday(), "work"),
                "override": ov.kind if ov else "",
                "note": (ov.note or "") if ov else "",
                "state": state,
                "attended": day.attended,
                "hours": rules.format_hm(day.hours_worked) if day.hours_worked else "",
                "first_in": day.first_in.strftime("%H:%M") if day.first_in else "",
                "last_out": day.last_out.strftime("%H:%M") if day.last_out else "",
                "is_today": d == current,
            }
        )
    order = [(ws + i) % 7 for i in range(7)]
    return {
        "cal_weeks": [cells[i : i + 7] for i in range(0, len(cells), 7)],
        "cal_weekdays": [i18n.WEEKDAYS_SHORT[lang][wd] for wd in order],
        "cal_month": first.strftime("%Y-%m"),
        "cal_month_label": f"{i18n.MONTHS[lang][first.month - 1]} {first.year}",
        "cal_prev": prev_first.strftime("%Y-%m"),
        "cal_next": next_first.strftime("%Y-%m"),
        "cal_this": today().strftime("%Y-%m"),
        "weekly_pattern": weekly,
        "weekday_order": [(wd, i18n.WEEKDAYS[lang][wd]) for wd in order],
    }


def _employee_redirect(user_id: str, month: str, msg: str) -> RedirectResponse:
    params = {"msg": msg}
    if month:
        params["month"] = month
    return RedirectResponse(url=f"/employee/{user_id}?{urlencode(params)}#schedule", status_code=303)


@router.post("/employee/{user_id}/weekly")
async def save_weekly_pattern(user_id: str, request: Request, db: Session = Depends(get_db)):
    if service.get_employee(db, user_id) is None:
        raise HTTPException(404, "employee not found")
    form = await request.form()
    pattern = {wd: str(form.get(f"wd{wd}") or "work") for wd in range(7)}
    before = service.get_weekly_pattern(db, user_id)
    service.set_weekly_pattern(db, user_id, pattern)
    after = service.get_weekly_pattern(db, user_id)
    if after != before:
        _audit(request, db, "schedule.weekly", f"Weekly pattern for {_emp_label(db, user_id)}: {_pattern_text(after)}",
               target=f"employee:{user_id}", before=_pattern_text(before), after=_pattern_text(after))
    return _employee_redirect(user_id, str(form.get("month") or ""), "saved")


@router.post("/employee/{user_id}/day")
async def save_day_override(user_id: str, request: Request, db: Session = Depends(get_db)):
    """kind = auto | work | off | online | move (move needs move_to)."""
    if service.get_employee(db, user_id) is None:
        raise HTTPException(404, "employee not found")
    form = await request.form()
    month = str(form.get("month") or "")
    try:
        day = datetime.strptime(str(form.get("day") or ""), "%Y-%m-%d").date()
    except ValueError:
        return _employee_redirect(user_id, month, "err:bad_date")
    kind = str(form.get("kind") or "auto")
    note = str(form.get("note") or "")
    if kind == "move":
        try:
            target = datetime.strptime(str(form.get("move_to") or ""), "%Y-%m-%d").date()
        except ValueError:
            return _employee_redirect(user_id, month, "err:bad_date")
        try:
            service.move_day_off(db, user_id, day, target, note)
        except service.ValidationError as exc:
            return _employee_redirect(user_id, month, f"err:{exc}")
        _audit(request, db, "schedule.move", f"Moved day off for {_emp_label(db, user_id)} from {day} to {target}",
               target=f"employee:{user_id}", from_day=str(day), to_day=str(target), note=note or None)
    else:
        before = service.load_day_types(db, day, day, [user_id]).get(user_id, {}).get(day, "work")
        chosen = kind if kind in service.OVERRIDE_KINDS else None
        service.set_day_override(db, user_id, day, chosen, note)
        after = service.load_day_types(db, day, day, [user_id]).get(user_id, {}).get(day, "work")
        verb = f"set to {KIND_LABEL.get(chosen, chosen)}" if chosen else "reset to the weekly pattern"
        _audit(request, db, "schedule.day", f"{day} for {_emp_label(db, user_id)} {verb}",
               target=f"employee:{user_id}", day=str(day), before=before, after=after, note=note or None)
    return _employee_redirect(user_id, month, "saved")


def _form_date(value: Any) -> date | None:
    try:
        return datetime.strptime(str(value or ""), "%Y-%m-%d").date()
    except ValueError:
        return None


@router.post("/employee/{user_id}/vacations")
async def add_employee_vacation(user_id: str, request: Request, db: Session = Depends(get_db)):
    if service.get_employee(db, user_id) is None:
        raise HTTPException(404, "employee not found")
    form = await request.form()
    month = str(form.get("month") or "")
    start, end = _form_date(form.get("start")), _form_date(form.get("end") or form.get("start"))
    if start is None or end is None:
        return _employee_redirect(user_id, month, "err:bad_date")
    note = str(form.get("note") or "")
    try:
        service.add_vacation(db, [user_id], start, end, note)
    except service.ValidationError as exc:
        return _employee_redirect(user_id, month, f"err:{exc}")
    _audit(request, db, "vacation.add", f"Added vacation {start} \u2192 {end} for {_emp_label(db, user_id)}",
           target=f"employee:{user_id}", start=str(start), end=str(end), days=(end - start).days + 1, note=note or None)
    return _employee_redirect(user_id, month or start.strftime("%Y-%m"), "saved")


@router.post("/employee/{user_id}/vacations/{vacation_id}/delete")
async def delete_employee_vacation(user_id: str, vacation_id: int, request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    vac = next((v for v in service.list_vacations(db, user_id) if v.id == vacation_id), None)
    info = {"start": str(vac.start_day), "end": str(vac.end_day), "note": vac.note} if vac else {}
    if not service.delete_vacation(db, user_id, vacation_id):
        raise HTTPException(404, "vacation not found")
    _audit(request, db, "vacation.delete",
           f"Deleted vacation {info.get('start')} \u2192 {info.get('end')} for {_emp_label(db, user_id)}",
           target=f"employee:{user_id}", **info)
    return _employee_redirect(user_id, str(form.get("month") or ""), "deleted")


@router.post("/settings/vacation-bulk")
def vacation_bulk(
    request: Request,
    scope: str = Form("all"),
    start: str = Form(""),
    end: str = Form(""),
    note: str = Form(""),
    db: Session = Depends(get_db),
):
    """Same vacation / public holiday for everyone or one department."""
    users = service.list_users_admin(db, include_inactive=False)
    if scope == "none":
        users = [u for u in users if u.department_id is None]
    elif scope.isdigit():
        users = [u for u in users if u.department_id == int(scope)]
    start_day, end_day = _form_date(start), _form_date(end or start)
    if start_day is None or end_day is None:
        return _settings_redirect(request, "err:bad_date")
    try:
        service.add_vacation(db, [u.user_id for u in users], start_day, end_day, note)
    except service.ValidationError as exc:
        return _settings_redirect(request, f"err:{exc}")
    _audit(request, db, "vacation.bulk",
           f"Added vacation {start_day} \u2192 {end_day} for {_scope_text(db, scope)} ({len(users)} employees)",
           target=f"scope:{scope}", start=str(start_day), end=str(end_day), note=note or None,
           employees=[u.user_id for u in users])
    return _settings_redirect(request, "saved")


@router.post("/settings/weekly-bulk")
def weekly_bulk(
    request: Request,
    scope: str = Form("all"),
    weekday: int = Form(...),
    kind: str = Form("off"),
    db: Session = Depends(get_db),
):
    """Set one recurring weekday (off / online / back to working) for everyone or one department."""
    users = service.list_users_admin(db, include_inactive=False)
    if scope == "none":
        users = [u for u in users if u.department_id is None]
    elif scope.isdigit():
        users = [u for u in users if u.department_id == int(scope)]
    try:
        service.set_weekly_day_bulk(db, [u.user_id for u in users], weekday, kind)
    except service.ValidationError as exc:
        return _settings_redirect(request, f"err:{exc}")
    _audit(request, db, "schedule.weekly_bulk",
           f"Every {WEEKDAY_EN[weekday]} = {KIND_LABEL.get(kind, kind)} for {_scope_text(db, scope)} ({len(users)} employees)",
           target=f"scope:{scope}", weekday=WEEKDAY_EN[weekday], kind=kind, employees=[u.user_id for u in users])
    return _settings_redirect(request, "saved")


# --------------------------------------------------------------------------- #
# Settings: departments + employee assignment
# --------------------------------------------------------------------------- #


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    ctx = _base_context(request, db, settings, "settings")
    show_inactive = request.query_params.get("show_inactive") == "1"
    users = service.list_users_admin(db, include_inactive=show_inactive)
    lang = ctx["lang"]
    weekly_labels: dict[str, list[dict[str, str]]] = {}
    for u in users:
        pattern = service.get_weekly_pattern(db, u.user_id)
        weekly_labels[u.user_id] = [
            {"day": i18n.WEEKDAYS_SHORT[lang][wd], "kind": kind} for wd, kind in sorted(pattern.items())
        ]
    order = [(settings.week_start + i) % 7 for i in range(7)]
    ctx.update(
        users=users,
        member_counts=service.department_member_counts(db),
        show_inactive=show_inactive,
        weekly_labels=weekly_labels,
        weekday_order=[(wd, i18n.WEEKDAYS[lang][wd]) for wd in order],
    )
    return _render(request, "settings.html", ctx)


def _settings_redirect(request: Request, msg: str) -> RedirectResponse:
    params = {"msg": msg}
    if request.query_params.get("show_inactive") == "1":
        params["show_inactive"] = "1"
    return RedirectResponse(url="/settings?" + urlencode(params), status_code=303)


@router.post("/settings/departments")
def add_department(request: Request, name: str = Form(""), db: Session = Depends(get_db)):
    try:
        dept = service.create_department(db, name)
    except service.ValidationError as exc:
        return _settings_redirect(request, f"err:{exc}")
    _audit(request, db, "department.create", f"Created department {dept.name}", target=f"department:{dept.id}")
    return _settings_redirect(request, "saved")


@router.post("/settings/departments/{dept_id}")
def edit_department(
    dept_id: int, request: Request, action: str = Form("rename"), name: str = Form(""), db: Session = Depends(get_db)
):
    old = next((d.name for d in service.list_departments(db) if d.id == dept_id), None)
    if action == "delete":
        if service.delete_department(db, dept_id):
            _audit(request, db, "department.delete", f"Deleted department {old}", target=f"department:{dept_id}")
        return _settings_redirect(request, "deleted")
    try:
        dept = service.rename_department(db, dept_id, name)
        if dept is None:
            raise HTTPException(404, "department not found")
    except service.ValidationError as exc:
        return _settings_redirect(request, f"err:{exc}")
    if dept.name != old:
        _audit(request, db, "department.rename", f"Renamed department {old} \u2192 {dept.name}",
               target=f"department:{dept_id}", before=old, after=dept.name)
    return _settings_redirect(request, "saved")


@router.post("/settings/employees/{user_id}")
def edit_employee(
    user_id: str,
    request: Request,
    display_name: str = Form(""),
    department_id: str = Form(""),
    db: Session = Depends(get_db),
):
    dept = int(department_id) if department_id.strip().isdigit() else None
    prev = next((u for u in service.list_users_admin(db, include_inactive=True) if u.user_id == user_id), None)
    before = {"display_name": prev.display_name, "department": prev.department.name if prev.department else None} if prev else {}
    updated = service.update_user_settings(db, user_id, display_name, dept)
    if updated is None:
        raise HTTPException(404, "employee not found")
    after = {"display_name": updated.display_name, "department": updated.department.name if updated.department else None}
    if after != before:
        _audit(request, db, "employee.update", f"Updated {updated.effective_name} ({user_id}): "
               f"display name {before.get('display_name')!r} \u2192 {after['display_name']!r}, "
               f"department {before.get('department')!r} \u2192 {after['department']!r}",
               target=f"employee:{user_id}", before=before, after=after)
    return _settings_redirect(request, "saved")


# --------------------------------------------------------------------------- #
# Manual sync
# --------------------------------------------------------------------------- #


@router.post("/sync")
async def sync_from_dashboard(
    request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)
):
    """"Sync now" button: run/queue a collector pull. JSON for fetch(), redirect for plain forms."""
    form = await request.form()
    result = await run_in_threadpool(trigger_sync, settings, True)
    _audit(request, db, "sync.manual",
           "Manual sync " + ("succeeded" if result.get("ok") else f"failed: {result.get('error')}"),
           mode=result.get("mode"), records_inserted=result.get("records_inserted"))
    wants_json = "application/json" in request.headers.get("accept", "") or form.get("format") == "json"
    if wants_json:
        return JSONResponse(result, status_code=200 if result.get("ok") else 502)
    params: dict[str, str] = {}
    for key in ("from", "to", "date", "range", "department"):
        value = form.get(key)
        if value:
            params[key] = str(value)
    if result.get("ok"):
        params["synced"] = result.get("mode", "1")
    else:
        params["sync_error"] = str(result.get("error", "sync failed"))[:200]
    return RedirectResponse(url="/?" + urlencode(params), status_code=303)
