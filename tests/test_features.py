"""Departments, display names, live status, i18n/RTL, employee profile, settings, CSV, Arabic PDF."""

from __future__ import annotations

from datetime import datetime, timedelta

from app import i18n, rules
from tests.conftest import API_HEADERS, BASIC_AUTH


def seed(client, users, records):
    r = client.post("/api/ingest", json={"users": users, "records": records}, headers=API_HEADERS)
    assert r.status_code == 200, r.text
    return r.json()


USERS = [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}, {"id": "3", "name": "محمد"}]


# ---- pure helpers --------------------------------------------------------- #


def test_live_hours_and_target_progress():
    now = datetime(2026, 5, 23, 12, 30)
    open_day = rules.EmployeeDay("1", "A", datetime(2026, 5, 23, 9, 0), None, 1)
    closed = rules.EmployeeDay("1", "A", datetime(2026, 5, 23, 9, 0), datetime(2026, 5, 23, 11, 0), 2)
    absent = rules.EmployeeDay("1", "A", None, None, 0)
    assert rules.live_hours(open_day, now) == 3.5
    assert rules.live_hours(closed, now) == 2.0  # completed pair never ticks
    assert rules.live_hours(absent, now) == 0
    assert open_day.hours_worked == 0  # the reporting rule is untouched
    assert rules.target_progress(3, 6) == 0.5 and rules.target_progress(9, 6) == 1.0 and rules.target_progress(1, 0) == 1.0
    assert rules.is_live_day(datetime(2026, 5, 23).date(), 7, datetime(2026, 5, 24, 3, 0))
    assert not rules.is_live_day(datetime(2026, 5, 23).date(), 7, datetime(2026, 5, 24, 7, 0))


def test_i18n_resolution_and_dates():
    assert i18n.resolve_lang("ar", None, None, "en") == "ar"
    assert i18n.resolve_lang(None, "ar", "en-US", "en") == "ar"
    assert i18n.resolve_lang(None, None, "ar-SA,ar;q=0.9", "en") == "ar"
    assert i18n.resolve_lang(None, None, "fr", "en") == "en"
    assert i18n.resolve_lang("xx", "yy", None, "ar") == "ar"
    d = datetime(2026, 9, 10).date()
    assert i18n.fmt_date(d, "en", "long") == "Thursday, September 10, 2026"
    assert i18n.fmt_date(d, "ar", "long") == "الخميس، 10 سبتمبر 2026"
    assert i18n.t("ar", "present") == "حاضر"
    assert i18n.t("en", "min_ago", n=5) == "5 min ago"
    assert i18n.t("en", "missing_key") == "missing_key"


# ---- departments & display names ----------------------------------------- #


def test_departments_crud_and_assignment_via_settings(client, session_factory):
    from app.models import User
    from app.service import list_departments

    seed(client, USERS, [])
    r = client.get("/settings", auth=BASIC_AUTH)
    assert r.status_code == 200 and "Alice" in r.text and "No departments yet" in r.text

    r = client.post("/settings/departments", data={"name": "Engineering"}, auth=BASIC_AUTH, follow_redirects=False)
    assert r.status_code == 303 and "msg=saved" in r.headers["location"]
    r = client.post("/settings/departments", data={"name": "engineering"}, auth=BASIC_AUTH, follow_redirects=False)
    assert "msg=err%3Adept_exists" in r.headers["location"]
    r = client.post("/settings/departments", data={"name": "  "}, auth=BASIC_AUTH, follow_redirects=False)
    assert "name_required" in r.headers["location"]
    client.post("/settings/departments", data={"name": "Sales"}, auth=BASIC_AUTH)

    with session_factory() as s:
        depts = {d.name: d.id for d in list_departments(s)}
    assert set(depts) == {"Engineering", "Sales"}

    # assign Alice + display name override, Bob to Sales
    client.post("/settings/employees/1", data={"display_name": "Alice Smith", "department_id": str(depts["Engineering"])}, auth=BASIC_AUTH)
    client.post("/settings/employees/2", data={"display_name": "", "department_id": str(depts["Sales"])}, auth=BASIC_AUTH)
    assert client.post("/settings/employees/999", data={"display_name": "x", "department_id": ""}, auth=BASIC_AUTH).status_code == 404

    with session_factory() as s:
        alice = s.get(User, "1")
        assert alice.display_name == "Alice Smith" and alice.department.name == "Engineering"
        assert alice.name == "Alice"  # device name untouched

    # rename + settings page shows counts
    client.post(f"/settings/departments/{depts['Sales']}", data={"action": "rename", "name": "Sales & Marketing"}, auth=BASIC_AUTH)
    r = client.get("/settings", auth=BASIC_AUTH)
    assert "Sales &amp; Marketing" in r.text and "Alice Smith" in r.text

    # reports use the display name + department; the device name is gone from output
    body = client.get("/summary?date=2026-05-23", headers=API_HEADERS).json()
    by_id = {e["id"]: e for e in body["employees"]}
    assert by_id["1"]["name"] == "Alice Smith" and by_id["1"]["department"] == "Engineering"
    assert by_id["2"]["department"] == "Sales & Marketing" and by_id["3"]["department"] is None

    # department filter on API + dashboard
    body = client.get(f"/summary?date=2026-05-23&department={depts['Engineering']}", headers=API_HEADERS).json()
    assert body["total"] == 1 and body["employees"][0]["id"] == "1"
    assert client.get("/summary?date=2026-05-23&department=abc", headers=API_HEADERS).status_code == 400
    r = client.get(f"/?date=2026-05-23&department={depts['Engineering']}", auth=BASIC_AUTH)
    assert r.status_code == 200 and "Alice Smith" in r.text and "Bob" not in r.text.split("<select")[0].split("</select>")[-1]

    # re-ingest from the device does not clobber system-side fields
    seed(client, USERS, [])
    with session_factory() as s:
        alice = s.get(User, "1")
        assert alice.display_name == "Alice Smith" and alice.department_id == depts["Engineering"]

    # delete department -> employees kept, unassigned
    r = client.post(f"/settings/departments/{depts['Engineering']}", data={"action": "delete"}, auth=BASIC_AUTH, follow_redirects=False)
    assert "msg=deleted" in r.headers["location"]
    with session_factory() as s:
        alice = s.get(User, "1")
        assert alice is not None and alice.department_id is None and alice.display_name == "Alice Smith"


# ---- live dashboard ------------------------------------------------------- #


def test_live_day_shows_checked_in_status_and_progress(client, migrated):
    now = datetime.now()
    today_shift = rules.shift_day(now, migrated.day_start_hour)
    start = datetime.combine(today_shift, datetime.min.time()).replace(hour=migrated.day_start_hour)
    first = max(start, now - timedelta(hours=3))  # 3 h ago, but inside the current shift window
    if first >= now:  # shift started less than a moment ago; nudge inside
        first = now - timedelta(minutes=1)
    seed(
        client,
        USERS,
        [
            {"user_id": "1", "timestamp": first.isoformat(timespec="seconds")},  # open session
            {"user_id": "2", "timestamp": first.isoformat(timespec="seconds")},
            {"user_id": "2", "timestamp": (first + timedelta(hours=1)).isoformat(timespec="seconds")},  # closed
        ],
    )
    r = client.get(f"/?date={today_shift.isoformat()}", auth=BASIC_AUTH)
    html = r.text
    assert r.status_code == 200
    assert "setAttribute('data-live','1')" in html  # marks the page as live (auto-refresh + ticking bars)
    assert "checked out yet" in html  # apostrophe is HTML-escaped
    assert "Checked out" in html
    assert "Currently in" in html
    assert 'data-open="1"' in html and 'data-open="0"' in html
    # the API rule is unchanged: Alice's single punch is 0 hours
    body = client.get(f"/summary?date={today_shift.isoformat()}", headers=API_HEADERS).json()
    assert {e["id"]: e["hours"] for e in body["employees"]}["1"] == 0.0

    # a past day is not live
    r = client.get("/?date=2026-01-05", auth=BASIC_AUTH)
    assert "setAttribute('data-live','1')" not in r.text and "checked out yet" not in r.text


# ---- i18n in pages, search, dark mode hooks ------------------------------- #


def test_arabic_ui_rtl_cookie_and_search_box(client):
    seed(client, USERS, [{"user_id": "3", "timestamp": "2026-05-23T08:00:00"}, {"user_id": "3", "timestamp": "2026-05-23T15:00:00"}])
    r = client.get("/?date=2026-05-23&lang=ar", auth=BASIC_AUTH)
    assert r.status_code == 200
    assert 'dir="rtl"' in r.text and 'lang="ar"' in r.text
    assert "حاضر" in r.text and "محمد" in r.text and "مزامنة الآن" in r.text
    assert r.cookies.get("lang") == "ar"
    # cookie persists the choice
    r = client.get("/?date=2026-05-23", auth=BASIC_AUTH, cookies={"lang": "ar"})
    assert 'dir="rtl"' in r.text
    r = client.get("/?date=2026-05-23&lang=en", auth=BASIC_AUTH)
    assert 'dir="ltr"' in r.text and 'id="search"' in r.text and "data-search=" in r.text
    assert 'id="themebtn"' in r.text and "data-theme" in r.text
    # print view in Arabic
    r = client.get("/print?from=2026-05-23&to=2026-05-23&lang=ar", auth=BASIC_AUTH)
    assert 'dir="rtl"' in r.text and "تقرير الحضور" in r.text


# ---- employee profile, CSV, PDF ------------------------------------------ #


def test_employee_profile_page(client):
    seed(
        client,
        USERS,
        [
            {"user_id": "1", "timestamp": "2026-05-20T08:00:00"}, {"user_id": "1", "timestamp": "2026-05-20T16:00:00"},
            {"user_id": "1", "timestamp": "2026-05-22T09:00:00"},
        ],
    )
    r = client.get("/employee/1?from=2026-05-20&to=2026-05-23", auth=BASIC_AUTH)
    assert r.status_code == 200
    html = r.text
    assert "Alice" in html and "2026-05-20" in html and "2026-05-21" in html and "2026-05-23" in html
    assert html.count("Absent") >= 2  # 21st and 23rd
    assert "No check-out recorded" in html  # 22nd single punch
    assert "8:00" in html
    assert client.get("/employee/nope", auth=BASIC_AUTH).status_code == 404
    assert client.get("/employee/1").status_code == 401


def test_csv_export_and_arabic_pdf(client):
    seed(client, USERS, [{"user_id": "3", "timestamp": "2026-05-23T08:00:00"}, {"user_id": "3", "timestamp": "2026-05-23T15:30:00"}])
    r = client.get("/export.csv?date=2026-05-23", auth=BASIC_AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert 'attendance_2026-05-23.csv' in r.headers["content-disposition"]
    text = r.content.decode("utf-8-sig")
    assert text.splitlines()[0].startswith("date,employee,id,department,attended")
    assert "محمد,3,,yes,2026-05-23T08:00:00,2026-05-23T15:30:00,7.50,7:30,2" in text
    assert "Alice,1,,no," in text

    r = client.get("/export.csv?from=2026-05-20&to=2026-05-23", auth=BASIC_AUTH)
    assert "days_present" in r.text and "attendance_2026-05-20_to_2026-05-23.csv" in r.headers["content-disposition"]

    r = client.get("/report?date=2026-05-23&lang=ar", headers=API_HEADERS)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert "attendance_2026-05-23_ar.pdf" in r.headers["content-disposition"]
    r = client.get("/report?date=2026-05-23", headers=API_HEADERS)
    assert "attendance_2026-05-23.pdf" in r.headers["content-disposition"]  # n8n contract unchanged


# ---- late / early check-in windows --------------------------------------- #


def test_checkin_flag_windows():
    from datetime import datetime as dt

    f = lambda hhmm: rules.checkin_flag(dt.strptime(f"2026-05-23 {hhmm}", "%Y-%m-%d %H:%M"), "18:30", "13:00", "01:00")
    assert f("18:30") == "late" and f("23:59") == "late" and f("00:30") == "late"  # late wraps midnight
    assert f("01:00") == "early" and f("02:00") == "early" and f("12:59") == "early"  # turn point starts "early"
    assert f("13:00") is None and f("15:45") is None and f("18:29") is None  # normal window
    assert rules.checkin_flag(None, "18:30", "13:00", "01:00") is None
    # disabling one side
    g = lambda hhmm: rules.checkin_flag(dt.strptime(f"2026-05-23 {hhmm}", "%Y-%m-%d %H:%M"), "18:30", "", "01:00")
    assert g("02:00") is None and g("23:00") == "late"
    h = lambda hhmm: rules.checkin_flag(dt.strptime(f"2026-05-23 {hhmm}", "%Y-%m-%d %H:%M"), "", "13:00", "")
    assert h("00:10") == "early" and h("23:00") is None


def test_late_and_early_badges_render(client):
    seed(
        client,
        USERS,
        [
            {"user_id": "1", "timestamp": "2026-05-23T19:05:00"},  # late (after 18:30)
            {"user_id": "2", "timestamp": "2026-05-23T09:00:00"},  # early (before 13:00)
            {"user_id": "3", "timestamp": "2026-05-23T14:00:00"},  # normal
        ],
    )
    r = client.get("/?date=2026-05-23", auth=BASIC_AUTH)
    html = r.text
    assert html.count('class="chip bad">Late<') == 1
    assert html.count('class="chip info">Early<') == 1
    assert "late after 18:30" in html and "early before 13:00" in html
    r = client.get("/export.csv?date=2026-05-23", auth=BASIC_AUTH)
    assert ",1,,yes,2026-05-23T19:05:00,,0.00,0:00,1,late" in r.text and ",2,,yes,2026-05-23T09:00:00,,0.00,0:00,1,early" in r.text
