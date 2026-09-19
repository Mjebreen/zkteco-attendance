"""Employee schedules: weekly pattern, per-date overrides, moving a day off, and their effect on reports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app import rules
from tests.conftest import API_HEADERS, BASIC_AUTH


@dataclass
class P:
    user_id: str
    timestamp: datetime


def dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M")


def seed(client, users, records):
    r = client.post("/api/ingest", json={"users": users, "records": records}, headers=API_HEADERS)
    assert r.status_code == 200, r.text


USERS = [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}, {"id": "3", "name": "Carol"}]
FRI, SAT, SUN, MON, TUE = date(2026, 5, 22), date(2026, 5, 23), date(2026, 5, 24), date(2026, 5, 25), date(2026, 5, 26)


# ---- pure rules ------------------------------------------------------------ #


def test_resolve_day_type_override_beats_weekly():
    weekly = {4: "off", 1: "online"}  # Friday off, Tuesday online
    assert rules.resolve_day_type(FRI, weekly, {}) == "off"
    assert rules.resolve_day_type(TUE, weekly, {}) == "online"
    assert rules.resolve_day_type(MON, weekly, {}) == "work"
    assert rules.resolve_day_type(FRI, weekly, {FRI: "work"}) == "work"  # worked this Friday
    assert rules.resolve_day_type(SUN, weekly, {SUN: "off"}) == "off"  # day off moved to Sunday
    assert rules.resolve_day_type(MON, None, None) == "work"


def test_daily_report_day_off_and_online_are_not_absences():
    emps = [rules.Employee("1", "Alice"), rules.Employee("2", "Bob"), rules.Employee("3", "Carol"), rules.Employee("4", "Dan")]
    punches = [P("4", dt("2026-05-22 09:00")), P("4", dt("2026-05-22 15:00"))]  # Dan works on his day off
    rep = rules.daily_report(emps, punches, FRI, 7, {"1": "off", "2": "online", "4": "off"})
    assert [e.user_id for e in rep.present] == ["4"] and rep.present[0].day_type == "off"
    assert [e.user_id for e in rep.off] == ["1"]
    assert [e.user_id for e in rep.online] == ["2"]
    assert [e.user_id for e in rep.absent] == ["3"]  # only Carol is really absent


def test_range_report_expected_days_and_rate():
    emp = [rules.Employee("1", "Alice")]
    # 5-day range Fri..Tue. Friday off (not worked), Tuesday online (no punch), present Sat + Sun, absent Mon.
    punches = [P("1", dt("2026-05-23 09:00")), P("1", dt("2026-05-23 17:00")), P("1", dt("2026-05-24 09:00"))]
    rep = rules.range_report(emp, punches, FRI, TUE, 7, {"1": {FRI: "off", TUE: "online"}})
    a = rep.employees[0]
    assert (a.days_total, a.days_off, a.days_online, a.days_expected) == (5, 1, 1, 4)
    assert a.days_present == 2 and a.days_absent == 1
    assert a.attendance_rate == 0.75  # (2 present + 1 online) / 4 expected

    # working the day off: it no longer counts as "off", expected goes back to 5
    punches.append(P("1", dt("2026-05-22 10:00")))
    a = rules.range_report(emp, punches, FRI, TUE, 7, {"1": {FRI: "off", TUE: "online"}}).employees[0]
    assert (a.days_off, a.days_expected, a.days_present, a.days_absent) == (0, 5, 3, 1)
    assert a.per_day[FRI].day_type == "off"

    # no schedule at all -> original behaviour
    a = rules.range_report(emp, punches, FRI, TUE, 7).employees[0]
    assert (a.days_off, a.days_online, a.days_expected, a.days_absent) == (0, 0, 5, 2)
    assert a.attendance_rate == 3 / 5


# ---- service --------------------------------------------------------------- #


def test_weekly_pattern_overrides_and_move(session_factory):
    from app import service

    with session_factory() as s:
        service.ingest(s, USERS, [], source="test")
        service.set_weekly_pattern(s, "1", {4: "off", 1: "online", 0: "work"})
        assert service.get_weekly_pattern(s, "1") == {4: "off", 1: "online"}
        types = service.load_day_types(s, date(2026, 5, 18), date(2026, 5, 31))
        assert types == {"1": {date(2026, 5, 19): "online", FRI: "off", TUE: "online", date(2026, 5, 29): "off"}}

        # Alice works Friday 22nd, takes Sunday 24th instead
        service.move_day_off(s, "1", FRI, SUN)
        types = service.load_day_types(s, FRI, TUE)["1"]
        assert FRI not in types and types[SUN] == "off" and types[TUE] == "online"
        # the following Friday is untouched
        assert service.load_day_types(s, date(2026, 5, 29), date(2026, 5, 29))["1"] == {date(2026, 5, 29): "off"}

        # "auto" removes the override again
        service.set_day_override(s, "1", FRI, None)
        service.set_day_override(s, "1", SUN, "auto")
        assert service.load_day_types(s, FRI, SUN)["1"] == {FRI: "off"}

        # replacing the pattern clears days that are no longer listed
        service.set_weekly_pattern(s, "1", {5: "off"})
        assert service.get_weekly_pattern(s, "1") == {5: "off"}

        # bulk: every Friday off for everyone, then back to working for Bob only
        assert service.set_weekly_day_bulk(s, ["1", "2", "3"], 4, "off") == 3
        service.set_weekly_day_bulk(s, ["2"], 4, "work")
        assert service.get_weekly_pattern(s, "2") == {} and service.get_weekly_pattern(s, "3") == {4: "off"}


# ---- web + API ------------------------------------------------------------- #


def test_schedule_flow_through_ui_and_reports(client):
    seed(
        client,
        USERS,
        [
            {"user_id": "2", "timestamp": "2026-05-22T09:00:00"}, {"user_id": "2", "timestamp": "2026-05-22T16:00:00"},
            {"user_id": "1", "timestamp": "2026-05-23T09:00:00"}, {"user_id": "1", "timestamp": "2026-05-23T15:00:00"},
        ],
    )
    # weekly pattern through the employee page: Alice off every Friday, online every Tuesday
    form = {f"wd{i}": "work" for i in range(7)} | {"wd4": "off", "wd1": "online", "month": "2026-05"}
    r = client.post("/employee/1/weekly", data=form, auth=BASIC_AUTH, follow_redirects=False)
    assert r.status_code == 303 and "msg=saved" in r.headers["location"] and "#schedule" in r.headers["location"]

    # bulk: every Friday off for everyone (Bob worked that Friday)
    r = client.post("/settings/weekly-bulk", data={"scope": "all", "weekday": "4", "kind": "off"}, auth=BASIC_AUTH, follow_redirects=False)
    assert "msg=saved" in r.headers["location"]

    body = client.get("/summary?date=2026-05-22", headers=API_HEADERS).json()
    by_id = {e["id"]: e for e in body["employees"]}
    assert body["present"] == 1 and body["absent"] == 0 and body["off"] == 2 and body["online"] == 0
    assert by_id["1"]["day_type"] == "off" and by_id["2"]["day_type"] == "off" and by_id["2"]["attended"] is True

    html = client.get("/?date=2026-05-22", auth=BASIC_AUTH).text
    assert "Worked on day off" in html and "Day off" in html
    # online Tuesday, nobody punched
    body = client.get("/summary?date=2026-05-26", headers=API_HEADERS).json()
    assert body["online"] == 1 and body["absent"] == 2

    # Bob worked Friday -> move his day off to Sunday the 24th via the calendar dialog
    r = client.post("/employee/2/day", data={"day": "2026-05-22", "kind": "move", "move_to": "2026-05-24", "month": "2026-05"},
                    auth=BASIC_AUTH, follow_redirects=False)
    assert "msg=saved" in r.headers["location"]
    fri = {e["id"]: e for e in client.get("/summary?date=2026-05-22", headers=API_HEADERS).json()["employees"]}
    sun = client.get("/summary?date=2026-05-24", headers=API_HEADERS).json()
    assert fri["2"]["day_type"] == "work"
    assert {e["id"]: e["day_type"] for e in sun["employees"]}["2"] == "off" and sun["off"] == 1

    # bad input is rejected politely
    r = client.post("/employee/2/day", data={"day": "2026-05-22", "kind": "move", "move_to": "2026-05-22"}, auth=BASIC_AUTH, follow_redirects=False)
    assert "same_day" in r.headers["location"]
    r = client.post("/employee/2/day", data={"day": "nope", "kind": "off"}, auth=BASIC_AUTH, follow_redirects=False)
    assert "bad_date" in r.headers["location"]
    assert client.post("/employee/999/day", data={"day": "2026-05-22", "kind": "off"}, auth=BASIC_AUTH).status_code == 404
    assert client.post("/employee/1/weekly", data=form).status_code == 401

    # range: Alice 22..26 = Fri off, Sat present, Sun/Mon absent, Tue online -> expected 4, rate 2/4
    rng = client.get("/summary?from=2026-05-22&to=2026-05-26", headers=API_HEADERS).json()
    alice = {e["id"]: e for e in rng["employees"]}["1"]
    assert (alice["days_off"], alice["days_online"], alice["days_expected"], alice["days_present"], alice["days_absent"]) == (1, 1, 4, 1, 2)
    assert alice["attendance_rate"] == 0.5

    # employee page: calendar for May 2026 renders states, the weekly editor and the dialog
    page = client.get("/employee/1?month=2026-05&from=2026-05-22&to=2026-05-26", auth=BASIC_AUTH).text
    assert 'data-day="2026-05-22"' in page and 'data-type="off"' in page and 'data-type="online"' in page
    assert "s-present" in page and "s-off" in page and "s-online" in page and "s-absent" in page
    assert 'id="daydlg"' in page and 'name="wd4"' in page and "May 2026" in page
    assert "Expected days" in page

    # single-date override to online, then back to auto
    client.post("/employee/3/day", data={"day": "2026-05-25", "kind": "online", "note": "WFH"}, auth=BASIC_AUTH)
    assert {e["id"]: e["day_type"] for e in client.get("/summary?date=2026-05-25", headers=API_HEADERS).json()["employees"]}["3"] == "online"
    client.post("/employee/3/day", data={"day": "2026-05-25", "kind": "auto"}, auth=BASIC_AUTH)
    assert {e["id"]: e["day_type"] for e in client.get("/summary?date=2026-05-25", headers=API_HEADERS).json()["employees"]}["3"] == "work"

    # settings page lists the weekly chips and the bulk tool; PDF + print + CSV still work
    settings_html = client.get("/settings", auth=BASIC_AUTH).text
    assert "Recurring day for a group" in settings_html and "weekly-bulk" in settings_html and ">Fri<" in settings_html
    assert client.get("/report?date=2026-05-22", headers=API_HEADERS).content.startswith(b"%PDF")
    assert "Day off" in client.get("/print?from=2026-05-22&to=2026-05-22", auth=BASIC_AUTH).text
    csv_text = client.get("/export.csv?date=2026-05-22", auth=BASIC_AUTH).text
    assert csv_text.splitlines()[0].endswith("checkin,day_type") and ",off" in csv_text
