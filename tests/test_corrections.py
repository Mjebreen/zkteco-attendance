"""Manual punch corrections, the trends page and the downloadable backup."""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import date, datetime

import pytest

from app import accounts, rules
from tests.conftest import API_HEADERS, BASIC_AUTH

ADMIN = ("boss@example.com", "correct-horse-1")
USERS = [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}, {"id": "3", "name": "Carol"}]
DAY = "2026-05-23"


def seed(client, records):
    r = client.post("/api/ingest", json={"users": USERS, "records": records}, headers=API_HEADERS)
    assert r.status_code == 200, r.text


def summary(client, day=DAY):
    return {e["id"]: e for e in client.get(f"/summary?date={day}", headers=API_HEADERS).json()["employees"]}


@pytest.fixture()
def admin_client(client, session_factory):
    from app.web_admin import reset_throttle

    reset_throttle()
    with session_factory() as s:
        accounts.create_user(s, ADMIN[0], ADMIN[1], role="admin")
    assert client.post("/login", data={"email": ADMIN[0], "password": ADMIN[1]}, follow_redirects=False).status_code == 303
    return client


# ---- corrections ------------------------------------------------------------ #


def test_forgotten_checkout_is_fixed_with_a_manual_punch(admin_client):
    c = admin_client
    seed(c, [{"user_id": "1", "timestamp": f"{DAY}T15:00:00"}])  # Alice forgot to check out
    assert summary(c)["1"]["hours"] == 0.0 and summary(c)["1"]["corrected"] is False

    page = c.get(f"/employee/1/punches?day={DAY}")
    assert page.status_code == 200 and "15:00:00" in page.text and "Add punch" in page.text

    # a reason is mandatory
    r = c.post("/employee/1/punches", data={"day": DAY, "ts": f"{DAY}T23:30", "note": "  "}, follow_redirects=False)
    assert "note_required" in r.headers["location"]
    # the time must belong to this shift day (07:00 the next morning is already the next day)
    r = c.post("/employee/1/punches", data={"day": DAY, "ts": "2026-05-24T07:00", "note": "x"}, follow_redirects=False)
    assert "outside_shift_day" in r.headers["location"]
    r = c.post("/employee/1/punches", data={"day": DAY, "ts": "2099-01-01T10:00", "note": "x"}, follow_redirects=False)
    assert "outside_shift_day" in r.headers["location"] or "punch_in_future" in r.headers["location"]

    # 01:30 after midnight still belongs to the 23rd
    r = c.post("/employee/1/punches", data={"day": DAY, "ts": "2026-05-24T01:30", "note": "Forgot to check out, confirmed by manager"},
               follow_redirects=False)
    assert "msg=saved" in r.headers["location"]
    alice = summary(c)["1"]
    assert alice["last_out"] == "2026-05-24T01:30:00" and alice["hours"] == 10.5 and alice["corrected"] is True
    # same time twice is refused
    r = c.post("/employee/1/punches", data={"day": DAY, "ts": "2026-05-24T01:30", "note": "again"}, follow_redirects=False)
    assert "punch_exists" in r.headers["location"]

    # badges + everything downstream uses the corrected value
    assert "Edited" in c.get(f"/?date={DAY}").text and "10:30" in c.get(f"/?date={DAY}").text
    assert "Edited" in c.get(f"/employee/1?from={DAY}&to={DAY}").text
    assert "10.50,10:30" in c.get(f"/export.csv?date={DAY}").text
    assert c.get(f"/report?date={DAY}", headers=API_HEADERS).content.startswith(b"%PDF")
    page = c.get(f"/employee/1/punches?day={DAY}").text
    assert "Manual" in page and "Forgot to check out" in page and ADMIN[0] in page

    # re-syncing the device never removes or duplicates the correction
    seed(c, [{"user_id": "1", "timestamp": f"{DAY}T15:00:00"}])
    assert summary(c)["1"]["hours"] == 10.5

    # delete it again -> back to the raw device data
    from app.models import PunchCorrection
    from app.db import get_sessionmaker

    with get_sessionmaker()() as s:
        cid = s.query(PunchCorrection).one().id
    assert c.post(f"/employee/2/punches/{cid}/remove", data={"day": DAY}).status_code == 404  # wrong employee
    r = c.post(f"/employee/1/punches/{cid}/remove", data={"day": DAY}, follow_redirects=False)
    assert "msg=deleted" in r.headers["location"]
    alice = summary(c)["1"]
    assert alice["hours"] == 0.0 and alice["corrected"] is False


def test_wrong_device_punch_can_be_ignored_and_restored(admin_client):
    c = admin_client
    seed(c, [
        {"user_id": "2", "timestamp": f"{DAY}T09:00:00"},
        {"user_id": "2", "timestamp": f"{DAY}T17:00:00"},
        {"user_id": "2", "timestamp": "2026-05-24T05:55:00"},  # someone else used Bob's finger slot by mistake
    ])
    assert summary(c)["2"]["hours"] == 20.92

    r = c.post("/employee/2/punches/void", data={"day": DAY, "ts": "2026-05-24T05:55:00", "note": "Wrong person"}, follow_redirects=False)
    assert "msg=saved" in r.headers["location"]
    bob = summary(c)["2"]
    assert bob["hours"] == 8.0 and bob["last_out"] == f"{DAY}T17:00:00" and bob["corrected"] is True
    assert "punch_already_voided" in c.post("/employee/2/punches/void", data={"day": DAY, "ts": "2026-05-24T05:55:00", "note": "x"},
                                            follow_redirects=False).headers["location"]
    assert "punch_not_found" in c.post("/employee/2/punches/void", data={"day": DAY, "ts": f"{DAY}T10:10:10", "note": "x"},
                                       follow_redirects=False).headers["location"]
    assert "note_required" in c.post("/employee/2/punches/void", data={"day": DAY, "ts": f"{DAY}T09:00:00", "note": ""},
                                     follow_redirects=False).headers["location"]
    page = c.get(f"/employee/2/punches?day={DAY}").text
    assert "Ignored" in page and "Restore" in page and "Wrong person" in page

    # the raw device row is still in the database
    from app.db import get_sessionmaker
    from app.models import AttendanceRecord, PunchCorrection

    with get_sessionmaker()() as s:
        assert s.query(AttendanceRecord).filter_by(user_id="2").count() == 3
        cid = s.query(PunchCorrection).one().id
    c.post(f"/employee/2/punches/{cid}/remove", data={"day": DAY})
    assert summary(c)["2"]["hours"] == 20.92 and summary(c)["2"]["corrected"] is False

    # absent employee made present by hand (device was offline)
    assert summary(c)["3"]["attended"] is False
    c.post("/employee/3/punches", data={"day": DAY, "ts": f"{DAY}T10:00", "note": "Device offline"})
    c.post("/employee/3/punches", data={"day": DAY, "ts": f"{DAY}T18:15", "note": "Device offline"})
    carol = summary(c)["3"]
    assert carol["attended"] is True and carol["hours"] == 8.25

    # the audit log has the whole story, with reasons
    audit = c.get("/audit?action=punch").text
    for needle in ("punch.void", "Wrong person", "punch.restore", "punch.add", "Device offline", "Carol (3)"):
        assert needle in audit, needle

    # HR (legacy basic = hr role here) may correct punches too, signed-out visitors may not
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as anon:
        assert anon.post("/employee/3/punches", data={"day": DAY, "ts": f"{DAY}T11:00", "note": "x"}).status_code == 401
        assert anon.post("/employee/3/punches", data={"day": DAY, "ts": f"{DAY}T11:00", "note": "HR fix"}, auth=BASIC_AUTH,
                         follow_redirects=False).status_code == 303


def test_effective_punches_unit(session_factory):
    from app import service

    with session_factory() as s:
        service.ingest(s, USERS, [{"user_id": "1", "timestamp": f"{DAY}T09:00:00"}, {"user_id": "1", "timestamp": f"{DAY}T12:00:00"}], "test")
        start, end = rules.shift_window(date(2026, 5, 23), 7)
        assert [p.timestamp.hour for p in service.punches_between(s, start, end)] == [9, 12]
        service.void_punch(s, "1", datetime(2026, 5, 23, 12, 0), "wrong", "tester")
        service.add_manual_punch(s, "1", datetime(2026, 5, 23, 18, 0, 0, 999), "late exit", "tester")
        eff = service.punches_between(s, start, end)
        assert [(p.timestamp.hour, p.manual) for p in eff] == [(9, False), (18, True)]
        assert service.punches_between(s, start, end, user_id="2") == []
        assert service.corrected_days(s, start, end, 7) == {"1": {date(2026, 5, 23)}}
        rows = service.day_punches(s, "1", date(2026, 5, 23), 7)
        assert [(r["source"], r["voided"]) for r in rows] == [("device", False), ("device", True), ("manual", False)]


# ---- trends ----------------------------------------------------------------- #


def test_trends_page_numbers(admin_client):
    c = admin_client
    seed(c, [
        {"user_id": "1", "timestamp": "2026-05-20T14:00:00"}, {"user_id": "1", "timestamp": "2026-05-20T21:00:00"},  # 7 h, normal
        {"user_id": "1", "timestamp": "2026-05-21T19:00:00"}, {"user_id": "1", "timestamp": "2026-05-22T01:00:00"},  # late, 6 h
        {"user_id": "2", "timestamp": "2026-05-20T09:00:00"},  # early, forgot to check out
        {"user_id": "2", "timestamp": "2026-05-21T14:30:00"}, {"user_id": "2", "timestamp": "2026-05-21T18:30:00"},  # 4 h
    ])
    r = c.get("/trends?from=2026-05-20&to=2026-05-21")
    assert r.status_code == 200
    html = r.text
    for needle in ("Trends", "Attendance rate per day", "Arrival times", "By weekday", "Missing check-outs", "Per employee",
                   "<polyline", "Alice", "Bob", "Carol"):
        assert needle in html, needle

    from app import service, trends
    from app.db import get_sessionmaker

    with get_sessionmaker()() as s:
        rep = service.build_range(s, date(2026, 5, 20), date(2026, 5, 21), 7)
        data = trends.build(rep, {}, late_after="18:30", early_before="13:00", turn="01:00", target_hours=6,
                            now=datetime(2026, 6, 1, 12, 0))
    kpi = data["kpi"]
    assert kpi["late"] == 1 and kpi["early"] == 1 and kpi["missing_out"] == 1 and kpi["employees"] == 3
    assert round(kpi["avg_rate"], 3) == round(((2 / 3) + (2 / 3)) / 2, 3)
    assert round(kpi["avg_hours"], 2) == round((7 + 6 + 4) / 3, 2)
    by_id = {p.user_id: p for p in data["people"]}
    assert by_id["1"].target_met == 2 and by_id["1"].late == 1 and by_id["2"].missing_out == 1 and by_id["3"].days_present == 0
    # average arrival: Alice 14:00 and 19:00 -> 16:30 on a 07:00-based day
    assert trends.clock(by_id["1"].avg_arrival, 7) == "16:30"
    assert trends.clock(None, 7) == "—"
    assert dict(data["histogram"])["14"] == 2 and dict(data["histogram"])["19"] == 1
    assert data["most_late"][0].user_id == "1" and data["most_missing_out"][0].user_id == "2"
    assert c.get("/trends").status_code == 200 and c.get("/trends?range=7").status_code == 200  # defaults


# ---- backup ----------------------------------------------------------------- #


def test_backup_download_is_admin_only_and_restorable(admin_client, session_factory, tmp_path):
    c = admin_client
    seed(c, [{"user_id": "1", "timestamp": f"{DAY}T09:00:00"}, {"user_id": "1", "timestamp": f"{DAY}T17:00:00"}])
    c.post("/settings/departments", data={"name": "Engineering"})

    r = c.get("/backup")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    assert "attachment" in r.headers["content-disposition"] and "attendance-backup-" in r.headers["content-disposition"]
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = set(zf.namelist())
    assert {"attendance.db", "RESTORE.txt", "backup-info.json", "audit.log"} <= names
    info = json.loads(zf.read("backup-info.json"))
    assert info["kind"] == "sqlite" and info["database_bytes"] > 0

    # the snapshot is a real, complete SQLite database
    snap = tmp_path / "restored.db"
    snap.write_bytes(zf.read("attendance.db"))
    con = sqlite3.connect(snap)
    try:
        assert con.execute("select count(*) from attendance_records").fetchone()[0] == 2
        assert con.execute("select name from departments").fetchone()[0] == "Engineering"
        assert con.execute("select email from app_users").fetchone()[0] == ADMIN[0]
        assert con.execute("select version_num from alembic_version").fetchone()[0].startswith("0007")
        assert con.execute("pragma integrity_check").fetchone()[0] == "ok"
    finally:
        con.close()

    assert "backup.download" in c.get("/audit?action=backup").text
    assert "Download backup" in c.get("/settings").text

    # HR and signed-out visitors cannot download it, and HR does not even see the button
    from fastapi.testclient import TestClient

    from app.main import create_app

    with session_factory() as s:
        accounts.create_user(s, "hr@example.com", "hr-password-22", role="hr")
    with TestClient(create_app()) as hr:
        assert hr.get("/backup").status_code == 401
        assert hr.post("/login", data={"email": "hr@example.com", "password": "hr-password-22"}, follow_redirects=False).status_code == 303
        assert hr.get("/backup").status_code == 403
        assert "Download backup" not in hr.get("/settings").text


def test_json_export_for_non_sqlite_databases(session_factory, migrated):
    from app import service
    from app.backup import _export_json
    from app.db import get_engine

    with session_factory() as s:
        service.ingest(s, USERS, [{"user_id": "1", "timestamp": f"{DAY}T09:00:00"}], "test")
    data = json.loads(_export_json(get_engine()))
    assert len(data["users"]) == 3 and data["attendance_records"][0]["timestamp"].startswith(DAY)
    assert data["alembic_version"][0]["version_num"].startswith("0007")
