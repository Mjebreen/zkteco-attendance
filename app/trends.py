"""Trend statistics for a date range, derived from the same RangeReport every other view uses.

Arrival times are measured as minutes since the shift day starts, so a 01:30 arrival on a
07:00-based shift day sorts after 23:00 instead of before 08:00.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

from app import rules


def _minutes_since_day_start(ts: datetime, day_start_hour: int) -> int:
    return ((ts.hour - day_start_hour) % 24) * 60 + ts.minute


def clock(minutes: float | None, day_start_hour: int) -> str:
    """Minutes-since-shift-start back to a wall clock 'HH:MM'."""
    if minutes is None:
        return "—"
    total = int(round(minutes)) + day_start_hour * 60
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


@dataclass
class DayStat:
    day: date
    present: int = 0
    expected: int = 0
    online: int = 0
    late: int = 0
    early: int = 0
    hours: float = 0.0
    completed: int = 0  # present with a check-out

    @property
    def rate(self) -> float:
        return min(1.0, (self.present + self.online) / self.expected) if self.expected else 0.0

    @property
    def avg_hours(self) -> float:
        return self.hours / self.completed if self.completed else 0.0


@dataclass
class PersonStat:
    user_id: str
    name: str
    department: str | None
    days_present: int = 0
    days_expected: int = 0
    rate: float = 0.0
    total_hours: float = 0.0
    avg_hours: float = 0.0
    late: int = 0
    early: int = 0
    missing_out: int = 0
    target_met: int = 0
    arrivals: list[int] = field(default_factory=list)

    @property
    def avg_arrival(self) -> float | None:
        return sum(self.arrivals) / len(self.arrivals) if self.arrivals else None


def build(
    rep: rules.RangeReport,
    day_types: dict[str, dict[date, str]],
    *,
    late_after: str,
    early_before: str,
    turn: str,
    target_hours: float,
    now: datetime,
) -> dict[str, Any]:
    h = rep.day_start_hour
    live_day = rules.shift_day(now, h)
    days: dict[date, DayStat] = {}
    d = rep.from_date
    while d <= rep.to_date:
        days[d] = DayStat(day=d)
        d += timedelta(days=1)

    people: list[PersonStat] = []
    histogram = [0] * 24  # arrivals by hour since the shift day starts
    weekday_present = [0] * 7
    weekday_days = [0] * 7
    for d in days:
        weekday_days[d.weekday()] += 1

    for e in rep.employees:
        schedule = day_types.get(e.user_id, {})
        p = PersonStat(e.user_id, e.name, e.department, days_present=e.days_present, days_expected=e.days_expected,
                       rate=e.attendance_rate, total_hours=e.total_hours, avg_hours=e.avg_hours_per_attended_day)
        for d, stat in days.items():
            day = e.per_day.get(d)
            kind = schedule.get(d, "work")
            attended = bool(day and day.attended)
            if attended or kind not in ("off", "vacation"):
                stat.expected += 1
            if not attended:
                if kind == "online":
                    stat.online += 1
                continue
            stat.present += 1
            weekday_present[d.weekday()] += 1
            mins = _minutes_since_day_start(day.first_in, h)
            p.arrivals.append(mins)
            histogram[mins // 60] += 1
            flag = rules.checkin_flag(day.first_in, late_after, early_before, turn)
            if flag == "late":
                p.late += 1
                stat.late += 1
            elif flag == "early":
                p.early += 1
                stat.early += 1
            if day.last_out is None:
                if d != live_day:  # today's open sessions are not "forgotten" yet
                    p.missing_out += 1
            else:
                stat.hours += day.hours_worked
                stat.completed += 1
                if target_hours > 0 and day.hours_worked >= target_hours:
                    p.target_met += 1
        people.append(p)

    series = list(days.values())
    past = [s for s in series if s.day <= live_day and s.expected]
    total_present = sum(s.present for s in series)
    completed = sum(s.completed for s in series)
    hist_labels = [f"{(h + i) % 24:02d}" for i in range(24)]
    busiest = max(range(24), key=lambda i: histogram[i]) if any(histogram) else None
    return {
        "days": series,
        "people": people,
        "histogram": list(zip(hist_labels, histogram)),
        "histogram_max": max(histogram) or 1,
        "busiest_hour": hist_labels[busiest] + ":00" if busiest is not None else "—",
        "weekdays": [
            (wd, weekday_present[wd] / weekday_days[wd] if weekday_days[wd] else 0.0) for wd in range(7)
        ],
        "kpi": {
            "avg_rate": sum(s.rate for s in past) / len(past) if past else 0.0,
            "avg_present": total_present / len(past) if past else 0.0,
            "avg_hours": sum(s.hours for s in series) / completed if completed else 0.0,
            "late": sum(p.late for p in people),
            "early": sum(p.early for p in people),
            "missing_out": sum(p.missing_out for p in people),
            "employees": len(people),
        },
        "max_present": max((s.expected for s in series), default=0) or 1,
        "max_avg_hours": max((s.avg_hours for s in series), default=0.0) or 1.0,
        "most_late": sorted((p for p in people if p.late), key=lambda p: (-p.late, p.name.lower()))[:5],
        "most_missing_out": sorted((p for p in people if p.missing_out), key=lambda p: (-p.missing_out, p.name.lower()))[:5],
    }
