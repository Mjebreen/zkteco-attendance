"""Attendance business rules - the single place attendance is computed.

Pure functions: no database, no device. Everything is naive local time.

Shift day
---------
A shift day labelled D covers [D DAY_START_HOUR:00, D+1 DAY_START_HOUR:00).
    shift_day = (timestamp - timedelta(hours=DAY_START_HOUR)).date()

Per employee, per shift day
---------------------------
punches   = that employee's punches inside the window, sorted ascending
attended  = len(punches) >= 1
first_in  = punches[0]
last_out  = punches[-1] only if len(punches) >= 2, else None
hours     = (last_out - first_in) in hours; 0 if last_out is None
Punch type/status is ignored; breaks are not subtracted.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Protocol


class Punch(Protocol):
    user_id: str
    timestamp: datetime


@dataclass(frozen=True)
class Employee:
    user_id: str
    name: str
    department: str | None = None  # assigned in this system, never on the device
    department_id: int | None = None


@dataclass
class EmployeeDay:
    user_id: str
    name: str
    first_in: datetime | None
    last_out: datetime | None
    punches: int
    department: str | None = None
    department_id: int | None = None

    @property
    def attended(self) -> bool:
        return self.first_in is not None

    @property
    def hours_worked(self) -> float:
        if self.first_in and self.last_out and self.last_out > self.first_in:
            return (self.last_out - self.first_in).total_seconds() / 3600
        return 0.0


@dataclass
class EmployeeRangeSummary:
    user_id: str
    name: str
    days_present: int
    days_total: int
    total_hours: float
    per_day: dict[date, EmployeeDay] = field(default_factory=dict)
    department: str | None = None
    department_id: int | None = None

    @property
    def days_absent(self) -> int:
        return self.days_total - self.days_present

    @property
    def attendance_rate(self) -> float:
        return self.days_present / self.days_total if self.days_total else 0.0

    @property
    def avg_hours_per_attended_day(self) -> float:
        return self.total_hours / self.days_present if self.days_present else 0.0


@dataclass
class DailyReport:
    target_date: date
    day_start_hour: int
    employees: list[EmployeeDay]  # present (by name) then absent (by name)

    @property
    def present(self) -> list[EmployeeDay]:
        return [e for e in self.employees if e.attended]

    @property
    def absent(self) -> list[EmployeeDay]:
        return [e for e in self.employees if not e.attended]

    @property
    def total_hours(self) -> float:
        return sum(e.hours_worked for e in self.present)


@dataclass
class RangeReport:
    from_date: date
    to_date: date
    day_start_hour: int
    employees: list[EmployeeRangeSummary]  # by days_present desc, then name

    @property
    def days_total(self) -> int:
        return (self.to_date - self.from_date).days + 1

    @property
    def total_hours(self) -> float:
        return sum(e.total_hours for e in self.employees)

    @property
    def avg_present_per_day(self) -> float:
        if not self.days_total:
            return 0.0
        return sum(e.days_present for e in self.employees) / self.days_total


# --------------------------------------------------------------------------- #
# Shift-day helpers
# --------------------------------------------------------------------------- #


def shift_day(ts: datetime, day_start_hour: int) -> date:
    """Return the shift day a timestamp belongs to."""
    return (ts - timedelta(hours=day_start_hour)).date()


def shift_window(target_date: date, day_start_hour: int) -> tuple[datetime, datetime]:
    """[start, end) datetimes for the shift day labelled `target_date`."""
    start = datetime.combine(target_date, time(day_start_hour, 0))
    return start, start + timedelta(days=1)


def range_window(from_date: date, to_date: date, day_start_hour: int) -> tuple[datetime, datetime]:
    """[start, end) datetimes covering the inclusive shift-day range from_date..to_date."""
    if to_date < from_date:
        from_date, to_date = to_date, from_date
    days_total = (to_date - from_date).days + 1
    start = datetime.combine(from_date, time(day_start_hour, 0))
    return start, start + timedelta(days=days_total)


def _employee_day(e: Employee, punches: list[datetime]) -> EmployeeDay:
    sorted_p = sorted(punches)
    return EmployeeDay(
        user_id=e.user_id,
        name=e.name,
        first_in=sorted_p[0] if sorted_p else None,
        last_out=sorted_p[-1] if len(sorted_p) > 1 else None,
        punches=len(sorted_p),
        department=e.department,
        department_id=e.department_id,
    )


# --------------------------------------------------------------------------- #
# Live-day helpers (dashboard only; reports never use these)
# --------------------------------------------------------------------------- #


def is_live_day(target_date: date, day_start_hour: int, now: datetime) -> bool:
    """True when `target_date` is the shift day currently in progress."""
    return shift_day(now, day_start_hour) == target_date


def live_hours(day: EmployeeDay, now: datetime) -> float:
    """Hours so far: completed pair -> hours_worked; single punch -> now - first_in (>= 0)."""
    if day.first_in is None:
        return 0.0
    if day.last_out is not None:
        return day.hours_worked
    return max(0.0, (now - day.first_in).total_seconds() / 3600)


def _parse_hhmm(value: str) -> time | None:
    if not value:
        return None
    h, m = value.split(":")
    return time(int(h), int(m))


def _in_window(t: time, start: time, end: time) -> bool:
    """t in [start, end) on a 24h clock; the window wraps midnight when end <= start."""
    if start == end:
        return False
    if start < end:
        return start <= t < end
    return t >= start or t < end


def checkin_flag(first_in: datetime | None, late_after: str, early_before: str, turn: str) -> str | None:
    """Classify a first punch by time-of-day: "late", "early" or None.

    late  window = [late_after, turn)      e.g. 18:30 -> 01:00 (wraps midnight)
    early window = [turn, early_before)    e.g. 01:00 -> 13:00
    An empty turn means midnight. Empty late_after / early_before disables that badge.
    """
    if first_in is None:
        return None
    t = first_in.time().replace(second=0, microsecond=0)
    late = _parse_hhmm(late_after)
    early = _parse_hhmm(early_before)
    turn_t = _parse_hhmm(turn) or time(0, 0)
    if late is not None and _in_window(t, late, turn_t):
        return "late"
    if early is not None and _in_window(t, turn_t, early):
        return "early"
    return None


def target_progress(hours: float, target_hours: float) -> float:
    """0..1 completion of the daily hours target."""
    if target_hours <= 0:
        return 1.0
    return max(0.0, min(1.0, hours / target_hours))


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #


def daily_report(
    employees: Iterable[Employee],
    punches: Iterable[Punch],
    target_date: date,
    day_start_hour: int,
) -> DailyReport:
    """Build the single-day report for the shift day that STARTS on `target_date`.

    Every enrolled employee is included (present or absent). Punches outside the
    window are ignored, so callers may pass more than they need.
    """
    window_start, window_end = shift_window(target_date, day_start_hour)

    by_user: dict[str, list[datetime]] = defaultdict(list)
    for p in punches:
        if window_start <= p.timestamp < window_end:
            by_user[str(p.user_id)].append(p.timestamp)

    results = [_employee_day(e, by_user.get(e.user_id, [])) for e in employees]
    results.sort(key=lambda e: (not e.attended, e.name.lower()))
    return DailyReport(target_date=target_date, day_start_hour=day_start_hour, employees=results)


def range_report(
    employees: Iterable[Employee],
    punches: Iterable[Punch],
    from_date: date,
    to_date: date,
    day_start_hour: int,
) -> RangeReport:
    """Aggregate across the INCLUSIVE [from_date, to_date] range of shift days."""
    if to_date < from_date:
        from_date, to_date = to_date, from_date
    days_total = (to_date - from_date).days + 1
    range_start, range_end = range_window(from_date, to_date, day_start_hour)

    by_user_day: dict[str, dict[date, list[datetime]]] = defaultdict(lambda: defaultdict(list))
    for p in punches:
        if range_start <= p.timestamp < range_end:
            by_user_day[str(p.user_id)][shift_day(p.timestamp, day_start_hour)].append(p.timestamp)

    results: list[EmployeeRangeSummary] = []
    for e in employees:
        per_day: dict[date, EmployeeDay] = {}
        days_present = 0
        total_hours = 0.0
        for d, day_punches in by_user_day.get(e.user_id, {}).items():
            day = _employee_day(e, day_punches)
            per_day[d] = day
            if day.attended:
                days_present += 1
            total_hours += day.hours_worked
        results.append(
            EmployeeRangeSummary(
                user_id=e.user_id,
                name=e.name,
                days_present=days_present,
                days_total=days_total,
                total_hours=total_hours,
                per_day=per_day,
                department=e.department,
                department_id=e.department_id,
            )
        )

    results.sort(key=lambda r: (-r.days_present, r.name.lower()))
    return RangeReport(from_date=from_date, to_date=to_date, day_start_hour=day_start_hour, employees=results)
