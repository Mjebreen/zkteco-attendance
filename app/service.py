"""Database-facing service layer: queries, idempotent ingest, sync state.

The web service only ever reads through these functions; the collector (or the
/api/ingest endpoint) writes through `ingest()`.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any, Iterable, NamedTuple

from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.orm import Session

from app import rules
from app.models import (
    AttendanceRecord,
    Department,
    EmployeeDayOverride,
    EmployeeVacation,
    EmployeeWeeklyDay,
    PunchCorrection,
    SyncState,
    User,
)

log = logging.getLogger("app.service")

INSERT_CHUNK = 500


# --------------------------------------------------------------------------- #
# Reads
# --------------------------------------------------------------------------- #


def _employee_from_user(u: User) -> rules.Employee:
    return rules.Employee(
        user_id=u.user_id,
        name=u.effective_name,
        department=u.department.name if u.department else None,
        department_id=u.department_id,
    )


def active_employees(session: Session, department_id: int | None = None) -> list[rules.Employee]:
    """Enrolled (active) employees with their system-side display name and department."""
    stmt = select(User).where(User.active.is_(True))
    if department_id is not None:
        stmt = stmt.where(User.department_id == department_id)
    return [_employee_from_user(u) for u in session.scalars(stmt).unique().all()]


def get_employee(session: Session, user_id: str) -> rules.Employee | None:
    u = session.get(User, user_id)
    return _employee_from_user(u) if u else None


# --------------------------------------------------------------------------- #
# Departments & employee settings (system-side only; the device is never touched)
# --------------------------------------------------------------------------- #


class ValidationError(ValueError):
    """User-facing validation problem (message is an i18n key)."""


def list_departments(session: Session) -> list[Department]:
    return list(session.scalars(select(Department).order_by(Department.name)).all())


def department_member_counts(session: Session) -> dict[int | None, int]:
    rows = session.execute(
        select(User.department_id, func.count()).where(User.active.is_(True)).group_by(User.department_id)
    ).all()
    return {dept_id: int(n) for dept_id, n in rows}


def create_department(session: Session, name: str) -> Department:
    name = (name or "").strip()[:120]
    if not name:
        raise ValidationError("name_required")
    if session.scalar(select(Department.id).where(func.lower(Department.name) == name.lower())):
        raise ValidationError("dept_exists")
    dept = Department(name=name, created_at=datetime.now())
    session.add(dept)
    session.commit()
    return dept


def rename_department(session: Session, dept_id: int, name: str) -> Department | None:
    dept = session.get(Department, dept_id)
    if dept is None:
        return None
    name = (name or "").strip()[:120]
    if not name:
        raise ValidationError("name_required")
    clash = session.scalar(
        select(Department.id).where(func.lower(Department.name) == name.lower(), Department.id != dept_id)
    )
    if clash:
        raise ValidationError("dept_exists")
    dept.name = name
    session.commit()
    return dept


def delete_department(session: Session, dept_id: int) -> bool:
    dept = session.get(Department, dept_id)
    if dept is None:
        return False
    for u in session.scalars(select(User).where(User.department_id == dept_id)).unique().all():
        u.department_id = None
    session.delete(dept)
    session.commit()
    return True


def list_users_admin(session: Session, include_inactive: bool = False) -> list[User]:
    stmt = select(User)
    if not include_inactive:
        stmt = stmt.where(User.active.is_(True))
    users = list(session.scalars(stmt).unique().all())
    users.sort(key=lambda u: (not u.active, u.effective_name.lower()))
    return users


def update_user_settings(
    session: Session, user_id: str, display_name: str | None, department_id: int | None
) -> User | None:
    u = session.get(User, user_id)
    if u is None:
        return None
    if department_id is not None and session.get(Department, department_id) is None:
        department_id = None
    u.display_name = (display_name or "").strip()[:255] or None
    u.department_id = department_id
    session.commit()
    return u


class EffectivePunch(NamedTuple):
    user_id: str
    timestamp: datetime
    manual: bool = False


def punches_between(session: Session, start: datetime, end: datetime, user_id: str | None = None) -> list[Any]:
    """EFFECTIVE punches in [start, end): device records minus voided ones plus manual additions.

    Lightweight rows (no ORM objects) so large ranges stay fast. Every report goes through here,
    so corrections apply to the dashboard, API, CSV, print view and PDFs alike.
    """
    stmt = select(AttendanceRecord.user_id, AttendanceRecord.timestamp).where(
        AttendanceRecord.timestamp >= start, AttendanceRecord.timestamp < end
    )
    cstmt = select(PunchCorrection.user_id, PunchCorrection.kind, PunchCorrection.timestamp).where(
        PunchCorrection.timestamp >= start, PunchCorrection.timestamp < end
    )
    if user_id is not None:
        stmt = stmt.where(AttendanceRecord.user_id == user_id)
        cstmt = cstmt.where(PunchCorrection.user_id == user_id)
    corrections = session.execute(cstmt).all()
    rows: list[Any] = list(session.execute(stmt.order_by(AttendanceRecord.timestamp)))
    if not corrections:
        return rows
    voided = {(uid, ts) for uid, kind, ts in corrections if kind == "void"}
    out = [EffectivePunch(uid, ts) for uid, ts in rows if (uid, ts) not in voided]
    existing = {(p.user_id, p.timestamp) for p in out}
    out += [EffectivePunch(uid, ts, True) for uid, kind, ts in corrections if kind == "add" and (uid, ts) not in existing]
    out.sort(key=lambda p: p.timestamp)
    return out


def corrected_days(session: Session, start: datetime, end: datetime, day_start_hour: int) -> dict[str, set[date]]:
    """{user_id: {shift days that carry at least one manual correction}}."""
    out: dict[str, set[date]] = {}
    rows = session.execute(
        select(PunchCorrection.user_id, PunchCorrection.timestamp).where(
            PunchCorrection.timestamp >= start, PunchCorrection.timestamp < end
        )
    )
    for uid, ts in rows:
        out.setdefault(uid, set()).add(rules.shift_day(ts, day_start_hour))
    return out


def build_daily(
    session: Session, target_date: date, day_start_hour: int, department_id: int | None = None
) -> rules.DailyReport:
    start, end = rules.shift_window(target_date, day_start_hour)
    schedule = load_day_types(session, target_date, target_date)
    day_types = {uid: days[target_date] for uid, days in schedule.items() if target_date in days}
    corrected = {uid for uid, days in corrected_days(session, start, end, day_start_hour).items() if target_date in days}
    return rules.daily_report(
        active_employees(session, department_id),
        punches_between(session, start, end),
        target_date,
        day_start_hour,
        day_types,
        corrected,
    )


def build_range(
    session: Session, from_date: date, to_date: date, day_start_hour: int, department_id: int | None = None
) -> rules.RangeReport:
    start, end = rules.range_window(from_date, to_date, day_start_hour)
    if to_date < from_date:
        from_date, to_date = to_date, from_date
    return rules.range_report(
        active_employees(session, department_id),
        punches_between(session, start, end),
        from_date,
        to_date,
        day_start_hour,
        load_day_types(session, from_date, to_date),
        corrected_days(session, start, end, day_start_hour),
    )


def build_employee_history(
    session: Session, employee: rules.Employee, from_date: date, to_date: date, day_start_hour: int
) -> list[rules.EmployeeDay]:
    """One EmployeeDay per shift day in the inclusive range (absent days included), oldest first."""
    if to_date < from_date:
        from_date, to_date = to_date, from_date
    start, end = rules.range_window(from_date, to_date, day_start_hour)
    schedule = load_day_types(session, from_date, to_date, [employee.user_id])
    rep = rules.range_report(
        [employee],
        punches_between(session, start, end, employee.user_id),
        from_date,
        to_date,
        day_start_hour,
        schedule,
        corrected_days(session, start, end, day_start_hour),
    )
    per_day = rep.employees[0].per_day if rep.employees else {}
    mine = schedule.get(employee.user_id, {})
    days: list[rules.EmployeeDay] = []
    d = from_date
    while d <= to_date:
        days.append(
            per_day.get(d)
            or rules.EmployeeDay(employee.user_id, employee.name, None, None, 0, employee.department,
                                 employee.department_id, mine.get(d, "work"))
        )
        d = d + timedelta(days=1)
    return days


# --------------------------------------------------------------------------- #
# Manual punch corrections (device records are never modified)
# --------------------------------------------------------------------------- #


def day_punches(session: Session, user_id: str, day: date, day_start_hour: int) -> list[dict[str, Any]]:
    """Every punch of one shift day for the correction screen: device, manual and voided ones."""
    start, end = rules.shift_window(day, day_start_hour)
    device = session.execute(
        select(AttendanceRecord.timestamp).where(
            AttendanceRecord.user_id == user_id, AttendanceRecord.timestamp >= start, AttendanceRecord.timestamp < end
        )
    ).all()
    corrections = session.scalars(
        select(PunchCorrection).where(
            PunchCorrection.user_id == user_id, PunchCorrection.timestamp >= start, PunchCorrection.timestamp < end
        )
    ).all()
    voids = {c.timestamp: c for c in corrections if c.kind == "void"}
    rows: list[dict[str, Any]] = []
    for (ts,) in device:
        v = voids.get(ts)
        rows.append({"timestamp": ts, "source": "device", "voided": v is not None, "note": v.note if v else "",
                     "by": v.created_by if v else "", "correction_id": v.id if v else None})
    device_times = {ts for (ts,) in device}
    for c in corrections:
        if c.kind == "add" and c.timestamp not in device_times:
            rows.append({"timestamp": c.timestamp, "source": "manual", "voided": False, "note": c.note,
                         "by": c.created_by, "correction_id": c.id})
    rows.sort(key=lambda r: r["timestamp"])
    return rows


def add_manual_punch(session: Session, user_id: str, ts: datetime, note: str, by: str) -> PunchCorrection:
    note = (note or "").strip()[:255]
    if not note:
        raise ValidationError("note_required")
    ts = ts.replace(microsecond=0)
    if ts > datetime.now() + timedelta(minutes=5):
        raise ValidationError("punch_in_future")
    exists = session.scalar(
        select(AttendanceRecord.id).where(AttendanceRecord.user_id == user_id, AttendanceRecord.timestamp == ts)
    ) or session.scalar(
        select(PunchCorrection.id).where(
            PunchCorrection.user_id == user_id, PunchCorrection.kind == "add", PunchCorrection.timestamp == ts
        )
    )
    if exists:
        raise ValidationError("punch_exists")
    row = PunchCorrection(user_id=user_id, kind="add", timestamp=ts, note=note, created_by=by, created_at=datetime.now())
    session.add(row)
    session.commit()
    return row


def void_punch(session: Session, user_id: str, ts: datetime, note: str, by: str) -> PunchCorrection:
    note = (note or "").strip()[:255]
    if not note:
        raise ValidationError("note_required")
    if not session.scalar(
        select(AttendanceRecord.id).where(AttendanceRecord.user_id == user_id, AttendanceRecord.timestamp == ts)
    ):
        raise ValidationError("punch_not_found")
    if session.scalar(
        select(PunchCorrection.id).where(
            PunchCorrection.user_id == user_id, PunchCorrection.kind == "void", PunchCorrection.timestamp == ts
        )
    ):
        raise ValidationError("punch_already_voided")
    row = PunchCorrection(user_id=user_id, kind="void", timestamp=ts, note=note, created_by=by, created_at=datetime.now())
    session.add(row)
    session.commit()
    return row


def remove_correction(session: Session, user_id: str, correction_id: int) -> PunchCorrection | None:
    """Delete a manual punch, or restore a voided device punch. Returns the removed row."""
    row = session.get(PunchCorrection, correction_id)
    if row is None or row.user_id != user_id:
        return None
    session.delete(row)
    session.commit()
    return row


# --------------------------------------------------------------------------- #
# Schedules: weekly pattern + per-date overrides (system-side only)
# --------------------------------------------------------------------------- #

SCHEDULE_KINDS = ("off", "online")
OVERRIDE_KINDS = ("off", "online", "work", "vacation")
MAX_VACATION_DAYS = 366


def get_weekly_pattern(session: Session, user_id: str) -> dict[int, str]:
    rows = session.execute(
        select(EmployeeWeeklyDay.weekday, EmployeeWeeklyDay.kind).where(EmployeeWeeklyDay.user_id == user_id)
    ).all()
    return {int(wd): kind for wd, kind in rows}


def get_overrides(session: Session, user_id: str, from_date: date, to_date: date) -> dict[date, EmployeeDayOverride]:
    rows = session.scalars(
        select(EmployeeDayOverride).where(
            EmployeeDayOverride.user_id == user_id,
            EmployeeDayOverride.day >= from_date,
            EmployeeDayOverride.day <= to_date,
        )
    ).all()
    return {o.day: o for o in rows}


def load_day_types(
    session: Session, from_date: date, to_date: date, user_ids: list[str] | None = None
) -> dict[str, dict[date, str]]:
    """{user_id: {date: "off" | "online"}} for the inclusive range. "work" days are omitted.

    Weekly patterns are expanded over the range, then per-date overrides are applied on top
    (an override of "work" removes a recurring day off).
    """
    if to_date < from_date:
        from_date, to_date = to_date, from_date
    weekly_stmt = select(EmployeeWeeklyDay.user_id, EmployeeWeeklyDay.weekday, EmployeeWeeklyDay.kind)
    over_stmt = select(EmployeeDayOverride.user_id, EmployeeDayOverride.day, EmployeeDayOverride.kind).where(
        EmployeeDayOverride.day >= from_date, EmployeeDayOverride.day <= to_date
    )
    if user_ids is not None:
        weekly_stmt = weekly_stmt.where(EmployeeWeeklyDay.user_id.in_(user_ids))
        over_stmt = over_stmt.where(EmployeeDayOverride.user_id.in_(user_ids))

    weekly: dict[str, dict[int, str]] = {}
    for uid, wd, kind in session.execute(weekly_stmt):
        weekly.setdefault(uid, {})[int(wd)] = kind

    out: dict[str, dict[date, str]] = {}
    if weekly:
        d = from_date
        while d <= to_date:
            wd = d.weekday()
            for uid, pattern in weekly.items():
                kind = pattern.get(wd)
                if kind:
                    out.setdefault(uid, {})[d] = kind
            d += timedelta(days=1)
    # Vacations sit between the weekly pattern and single-date overrides.
    vac_stmt = select(EmployeeVacation.user_id, EmployeeVacation.start_day, EmployeeVacation.end_day).where(
        EmployeeVacation.start_day <= to_date, EmployeeVacation.end_day >= from_date
    )
    if user_ids is not None:
        vac_stmt = vac_stmt.where(EmployeeVacation.user_id.in_(user_ids))
    for uid, start_day, end_day in session.execute(vac_stmt):
        d = max(start_day, from_date)
        while d <= min(end_day, to_date):
            out.setdefault(uid, {})[d] = "vacation"
            d += timedelta(days=1)

    for uid, day, kind in session.execute(over_stmt):
        if kind == "work":
            out.get(uid, {}).pop(day, None)
        else:
            out.setdefault(uid, {})[day] = kind
    return {uid: days for uid, days in out.items() if days}


def set_weekly_pattern(session: Session, user_id: str, pattern: dict[int, str]) -> None:
    """Replace the whole weekly pattern. `pattern` maps weekday (0 = Monday) -> off | online."""
    existing = {w.weekday: w for w in session.scalars(
        select(EmployeeWeeklyDay).where(EmployeeWeeklyDay.user_id == user_id)).all()}
    for wd in range(7):
        kind = pattern.get(wd)
        row = existing.get(wd)
        if kind in SCHEDULE_KINDS:
            if row is None:
                session.add(EmployeeWeeklyDay(user_id=user_id, weekday=wd, kind=kind))
            else:
                row.kind = kind
        elif row is not None:
            session.delete(row)
    session.commit()


def set_weekly_day_bulk(session: Session, user_ids: list[str], weekday: int, kind: str) -> int:
    """Set (or clear, kind="work") one weekday for many employees. Returns how many were touched."""
    if not 0 <= weekday <= 6:
        raise ValidationError("bad_weekday")
    existing = {w.user_id: w for w in session.scalars(
        select(EmployeeWeeklyDay).where(EmployeeWeeklyDay.weekday == weekday,
                                        EmployeeWeeklyDay.user_id.in_(user_ids))).all()}
    for uid in user_ids:
        row = existing.get(uid)
        if kind in SCHEDULE_KINDS:
            if row is None:
                session.add(EmployeeWeeklyDay(user_id=uid, weekday=weekday, kind=kind))
            else:
                row.kind = kind
        elif row is not None:
            session.delete(row)
    session.commit()
    return len(user_ids)


def set_day_override(session: Session, user_id: str, day: date, kind: str | None, note: str | None = None) -> None:
    """kind: off | online | work, or None/"auto" to remove the override (fall back to the weekly pattern)."""
    row = session.scalar(
        select(EmployeeDayOverride).where(EmployeeDayOverride.user_id == user_id, EmployeeDayOverride.day == day)
    )
    if kind in OVERRIDE_KINDS:
        note = (note or "").strip()[:255] or None
        if row is None:
            session.add(EmployeeDayOverride(user_id=user_id, day=day, kind=kind, note=note))
        else:
            row.kind, row.note = kind, note
    elif row is not None:
        session.delete(row)
    session.commit()


def list_vacations(session: Session, user_id: str) -> list[EmployeeVacation]:
    return list(
        session.scalars(
            select(EmployeeVacation).where(EmployeeVacation.user_id == user_id).order_by(EmployeeVacation.start_day.desc())
        ).all()
    )


def add_vacation(session: Session, user_ids: list[str], start_day: date, end_day: date, note: str | None = None) -> int:
    """Create the same vacation period for one or many employees. Returns how many were created."""
    if end_day < start_day:
        raise ValidationError("bad_range")
    if (end_day - start_day).days + 1 > MAX_VACATION_DAYS:
        raise ValidationError("range_too_long")
    note = (note or "").strip()[:255] or None
    for uid in user_ids:
        session.add(EmployeeVacation(user_id=uid, start_day=start_day, end_day=end_day, note=note,
                                     created_at=datetime.now()))
    session.commit()
    return len(user_ids)


def delete_vacation(session: Session, user_id: str, vacation_id: int) -> bool:
    row = session.get(EmployeeVacation, vacation_id)
    if row is None or row.user_id != user_id:
        return False
    session.delete(row)
    session.commit()
    return True


def move_day_off(session: Session, user_id: str, from_day: date, to_day: date, note: str | None = None) -> None:
    """The employee works `from_day` (normally off) and takes `to_day` off instead."""
    if from_day == to_day:
        raise ValidationError("same_day")
    label = (note or "").strip() or f"moved from {from_day.isoformat()}"
    set_day_override(session, user_id, from_day, "work", f"day off moved to {to_day.isoformat()}")
    set_day_override(session, user_id, to_day, "off", label)


def record_count(session: Session) -> int:
    return int(session.scalar(select(func.count()).select_from(AttendanceRecord)) or 0)


def user_count(session: Session, active_only: bool = True) -> int:
    stmt = select(func.count()).select_from(User)
    if active_only:
        stmt = stmt.where(User.active.is_(True))
    return int(session.scalar(stmt) or 0)


# --------------------------------------------------------------------------- #
# Sync state
# --------------------------------------------------------------------------- #


def get_sync_state(session: Session, create: bool = True) -> SyncState | None:
    state = session.get(SyncState, 1)
    if state is None and create:
        state = SyncState(id=1, record_count=0, user_count=0)
        session.add(state)
        session.flush()
    return state


def record_sync_failure(session: Session, error: str, source: str) -> None:
    state = get_sync_state(session)
    assert state is not None
    state.last_attempt = datetime.now()
    state.last_error = error[:2000]
    state.source = source
    session.commit()


def request_sync(session: Session) -> None:
    state = get_sync_state(session)
    assert state is not None
    state.sync_requested_at = datetime.now()
    session.commit()


def consume_sync_request(session: Session) -> bool:
    """Return True (and clear the flag) if a sync was requested via the web UI/API."""
    state = get_sync_state(session)
    assert state is not None
    if state.sync_requested_at is None:
        return False
    state.sync_requested_at = None
    session.commit()
    return True


# --------------------------------------------------------------------------- #
# Ingest (idempotent upsert)
# --------------------------------------------------------------------------- #


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        ts = value
    elif isinstance(value, str):
        ts = datetime.fromisoformat(value.strip())
    else:
        raise ValueError(f"unsupported timestamp: {value!r}")
    if ts.tzinfo is not None:
        # Device times are naive local; strip any offset a client attached.
        ts = ts.replace(tzinfo=None)
    return ts.replace(microsecond=0)


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_users(users: Iterable[Any]) -> dict[str, str]:
    """Accept dicts ({id|user_id, name}) or pyzk User objects -> {user_id: name}."""
    out: dict[str, str] = {}
    for u in users:
        if isinstance(u, dict):
            uid = u.get("id", u.get("user_id"))
            name = u.get("name")
        else:
            uid = getattr(u, "user_id", None)
            name = getattr(u, "name", None)
        if uid is None or str(uid).strip() == "":
            continue
        uid = str(uid).strip()
        name = (name or "").strip() or f"User {uid}"
        out[uid] = name
    return out


def normalize_records(records: Iterable[Any]) -> list[dict[str, Any]]:
    """Accept dicts or pyzk Attendance objects -> deduped rows on (user_id, timestamp)."""
    seen: set[tuple[str, datetime]] = set()
    rows: list[dict[str, Any]] = []
    for r in records:
        if isinstance(r, dict):
            uid, ts, status, punch = r.get("user_id", r.get("id")), r.get("timestamp"), r.get("status"), r.get("punch")
        else:
            uid, ts = getattr(r, "user_id", None), getattr(r, "timestamp", None)
            status, punch = getattr(r, "status", None), getattr(r, "punch", None)
        if uid is None or ts is None:
            continue
        uid = str(uid).strip()
        try:
            ts = _parse_timestamp(ts)
        except ValueError:
            log.warning("skipping record with bad timestamp", extra={"ctx_user_id": uid, "ctx_ts": str(ts)})
            continue
        key = (uid, ts)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"user_id": uid, "timestamp": ts, "status": _to_int(status), "punch": _to_int(punch)})
    return rows


def _insert_ignore(session: Session, rows: list[dict[str, Any]]) -> int:
    """INSERT ... ON CONFLICT DO NOTHING, chunked. Returns number of new rows."""
    if not rows:
        return 0
    dialect = session.get_bind().dialect.name
    inserted = 0
    for i in range(0, len(rows), INSERT_CHUNK):
        chunk = rows[i : i + INSERT_CHUNK]
        # RETURNING gives an exact count of new rows on both dialects (rowcount is unreliable
        # for multi-row inserts on Postgres/psycopg).
        if dialect == "postgresql":
            stmt = (
                postgresql.insert(AttendanceRecord)
                .values(chunk)
                .on_conflict_do_nothing(index_elements=["user_id", "timestamp"])
                .returning(AttendanceRecord.id)
            )
        elif dialect == "sqlite":
            stmt = (
                sqlite.insert(AttendanceRecord)
                .values(chunk)
                .on_conflict_do_nothing(index_elements=["user_id", "timestamp"])
                .returning(AttendanceRecord.id)
            )
        else:  # pragma: no cover - other dialects: fall back to row-by-row existence checks
            for row in chunk:
                exists = session.scalar(
                    select(AttendanceRecord.id).where(
                        AttendanceRecord.user_id == row["user_id"], AttendanceRecord.timestamp == row["timestamp"]
                    )
                )
                if not exists:
                    session.add(AttendanceRecord(**row))
                    inserted += 1
            continue
        inserted += len(session.execute(stmt).all())
    return inserted


def upsert_users(session: Session, users: dict[str, str], deactivate_missing: bool = True) -> dict[str, int]:
    """Insert/update users; mark users that vanished from the device inactive (never delete)."""
    now = datetime.now()
    existing = {u.user_id: u for u in session.scalars(select(User)).all()}
    created = updated = deactivated = reactivated = 0
    for uid, name in users.items():
        u = existing.get(uid)
        if u is None:
            session.add(User(user_id=uid, name=name, active=True, first_seen=now, last_seen=now))
            created += 1
            continue
        changed = False
        if u.name != name:
            u.name = name
            changed = True
        if not u.active:
            u.active = True
            reactivated += 1
            changed = True
        u.last_seen = now
        if changed:
            updated += 1
    if deactivate_missing and users:
        for uid, u in existing.items():
            if uid not in users and u.active:
                u.active = False
                deactivated += 1
    session.flush()
    return {"created": created, "updated": updated, "deactivated": deactivated, "reactivated": reactivated}


def ingest(
    session: Session,
    users: Iterable[Any],
    records: Iterable[Any],
    source: str,
    deactivate_missing: bool = True,
) -> dict[str, Any]:
    """Idempotent upsert of a full device snapshot. Safe to call repeatedly."""
    user_map = normalize_users(users)
    rows = normalize_records(records)

    user_stats = upsert_users(session, user_map, deactivate_missing=deactivate_missing)
    inserted = _insert_ignore(session, rows)

    state = get_sync_state(session)
    assert state is not None
    now = datetime.now()
    state.last_sync = now
    state.last_attempt = now
    state.last_error = None
    state.source = source
    state.record_count = record_count(session)
    state.user_count = user_count(session)
    session.commit()

    result = {
        "users_received": len(user_map),
        "users_created": user_stats["created"],
        "users_updated": user_stats["updated"],
        "users_deactivated": user_stats["deactivated"],
        "users_reactivated": user_stats["reactivated"],
        "records_received": len(rows),
        "records_inserted": inserted,
        "records_skipped": len(rows) - inserted,
        "record_count": state.record_count,
        "last_sync": now.isoformat(timespec="seconds"),
    }
    log.info("ingest complete", extra={f"ctx_{k}": v for k, v in result.items()} | {"ctx_source": source})
    return result
