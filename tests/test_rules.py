"""Unit tests for the pure business rules (no DB, no device)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app import rules


@dataclass
class P:
    user_id: str
    timestamp: datetime


EMP = [rules.Employee("1", "Alice"), rules.Employee("2", "Bob"), rules.Employee("3", "Carol")]


def dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


# ---- shift-day mapping ---------------------------------------------------- #


def test_shift_day_maps_across_midnight():
    # 03:00 the next morning still belongs to the previous shift day when the day starts at 07:00
    assert rules.shift_day(dt("2026-05-24 03:00"), 7) == date(2026, 5, 23)
    assert rules.shift_day(dt("2026-05-24 06:59"), 7) == date(2026, 5, 23)
    assert rules.shift_day(dt("2026-05-24 07:00"), 7) == date(2026, 5, 24)
    assert rules.shift_day(dt("2026-05-23 07:00"), 7) == date(2026, 5, 23)
    assert rules.shift_day(dt("2026-05-23 06:59"), 7) == date(2026, 5, 22)


def test_shift_day_day_start_zero_is_calendar_day():
    assert rules.shift_day(dt("2026-05-24 00:00"), 0) == date(2026, 5, 24)
    assert rules.shift_day(dt("2026-05-24 23:59"), 0) == date(2026, 5, 24)
    assert rules.shift_day(dt("2026-05-24 03:00"), 0) == date(2026, 5, 24)


def test_shift_window_bounds():
    start, end = rules.shift_window(date(2026, 5, 23), 7)
    assert start == dt("2026-05-23 07:00")
    assert end == dt("2026-05-24 07:00")
    start, end = rules.shift_window(date(2026, 5, 23), 0)
    assert start == dt("2026-05-23 00:00")
    assert end == dt("2026-05-24 00:00")


def test_daily_report_includes_0300_next_day_and_excludes_0700_next_day():
    punches = [
        P("1", dt("2026-05-23 09:00")),
        P("1", dt("2026-05-24 03:00")),  # inside window
        P("2", dt("2026-05-24 07:00")),  # exactly window end -> excluded
        P("3", dt("2026-05-23 06:59")),  # before window start -> excluded
    ]
    rep = rules.daily_report(EMP, punches, date(2026, 5, 23), 7)
    by_id = {e.user_id: e for e in rep.employees}
    assert by_id["1"].attended and by_id["1"].first_in == dt("2026-05-23 09:00")
    assert by_id["1"].last_out == dt("2026-05-24 03:00")
    assert by_id["1"].hours_worked == 18.0
    assert not by_id["2"].attended
    assert not by_id["3"].attended


# ---- per-day rules -------------------------------------------------------- #


def test_single_punch_is_present_with_zero_hours():
    rep = rules.daily_report(EMP, [P("1", dt("2026-05-23 08:30"))], date(2026, 5, 23), 7)
    alice = rep.present[0]
    assert alice.user_id == "1"
    assert alice.attended
    assert alice.first_in == dt("2026-05-23 08:30")
    assert alice.last_out is None
    assert alice.hours_worked == 0
    assert rep.total_hours == 0


def test_first_and_last_punch_only_middle_ignored_and_unsorted_input():
    punches = [
        P("1", dt("2026-05-23 12:00")),  # middle (lunch out)
        P("1", dt("2026-05-23 17:15")),  # last
        P("1", dt("2026-05-23 08:00")),  # first
        P("1", dt("2026-05-23 13:00")),  # middle (lunch in)
    ]
    rep = rules.daily_report(EMP, punches, date(2026, 5, 23), 7)
    alice = rep.present[0]
    assert alice.first_in == dt("2026-05-23 08:00")
    assert alice.last_out == dt("2026-05-23 17:15")
    assert alice.punches == 4
    assert round(alice.hours_worked, 2) == 9.25  # breaks are NOT subtracted


def test_daily_report_lists_every_employee_and_sorts_present_then_absent_by_name():
    emps = [rules.Employee("9", "zed"), rules.Employee("2", "Bob"), rules.Employee("5", "alice"), rules.Employee("7", "Mia")]
    punches = [P("9", dt("2026-05-23 08:00")), P("5", dt("2026-05-23 08:05"))]
    rep = rules.daily_report(emps, punches, date(2026, 5, 23), 7)
    assert [e.user_id for e in rep.employees] == ["5", "9", "2", "7"]  # alice, zed | Bob, Mia
    assert len(rep.employees) == 4
    assert len(rep.present) == 2 and len(rep.absent) == 2


def test_day_start_hour_zero_fallback_uses_calendar_day():
    punches = [P("1", dt("2026-05-23 00:10")), P("1", dt("2026-05-23 23:50")), P("2", dt("2026-05-24 00:00"))]
    rep = rules.daily_report(EMP, punches, date(2026, 5, 23), 0)
    by_id = {e.user_id: e for e in rep.employees}
    assert by_id["1"].attended and round(by_id["1"].hours_worked, 2) == 23.67
    assert not by_id["2"].attended  # 2026-05-24 00:00 is the next calendar day


# ---- range aggregation ---------------------------------------------------- #


def test_range_report_aggregates_and_sorts():
    punches = [
        # Alice: 3 days, 8h + 0h (single punch) + 9h
        P("1", dt("2026-05-20 08:00")), P("1", dt("2026-05-20 16:00")),
        P("1", dt("2026-05-21 08:00")),
        P("1", dt("2026-05-22 08:00")), P("1", dt("2026-05-22 17:00")),
        # Bob: 1 day, 4h, punched at 02:00 next morning (still shift day 21)
        P("2", dt("2026-05-21 22:00")), P("2", dt("2026-05-22 02:00")),
        # Carol: outside the range
        P("3", dt("2026-05-23 08:00")),
    ]
    rep = rules.range_report(EMP, punches, date(2026, 5, 20), date(2026, 5, 22), 7)
    assert rep.days_total == 3
    assert [e.user_id for e in rep.employees] == ["1", "2", "3"]

    alice, bob, carol = rep.employees
    assert alice.days_present == 3 and alice.days_absent == 0
    assert alice.total_hours == 17.0
    assert round(alice.avg_hours_per_attended_day, 4) == round(17 / 3, 4)
    assert alice.attendance_rate == 1.0

    assert bob.days_present == 1 and bob.days_absent == 2
    assert bob.total_hours == 4.0
    assert bob.avg_hours_per_attended_day == 4.0
    assert round(bob.attendance_rate, 4) == round(1 / 3, 4)
    assert set(bob.per_day) == {date(2026, 5, 21)}

    assert carol.days_present == 0 and carol.days_absent == 3
    assert carol.total_hours == 0 and carol.avg_hours_per_attended_day == 0 and carol.attendance_rate == 0

    assert rep.total_hours == 21.0
    assert round(rep.avg_present_per_day, 4) == round(4 / 3, 4)


def test_range_report_swaps_reversed_dates_and_ties_sorted_by_name():
    punches = [P("2", dt("2026-05-20 08:00")), P("1", dt("2026-05-21 08:00"))]
    rep = rules.range_report(EMP, punches, date(2026, 5, 21), date(2026, 5, 20), 7)
    assert rep.from_date == date(2026, 5, 20) and rep.to_date == date(2026, 5, 21)
    assert [e.user_id for e in rep.employees] == ["1", "2", "3"]  # Alice, Bob tie on 1 day -> by name


def test_format_hm():
    assert rules.format_hm(5.99) == "5:59"  # never rounds up to a premature 6:00
    assert rules.format_hm(6.0) == "6:00"
    assert rules.format_hm(9.25) == "9:15"
    assert rules.format_hm(0.5) == "0:30"
    assert rules.format_hm(0) == "0:00" and rules.format_hm(None) == "0:00"
    assert rules.format_hm(341.2) == "341:12"  # totals can exceed 24 h
    assert rules.format_hm(19.0) == "19:00"
