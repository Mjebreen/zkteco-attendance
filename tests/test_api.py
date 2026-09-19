"""HTTP contract tests (n8n depends on these), acceptance checks, and auth."""

from __future__ import annotations

import time
from datetime import datetime, timedelta

from tests.conftest import API_HEADERS, BASIC_AUTH


def seed(client, users, records):
    resp = client.post("/api/ingest", json={"users": users, "records": records}, headers=API_HEADERS)
    assert resp.status_code == 200, resp.text
    return resp.json()


USERS = [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}, {"id": "3", "name": ""}]


def test_health_open_and_reports_never_synced_then_synced(client):
    r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["company"] == "Test Co"
    assert body["last_sync"] is None and body["sync_status"] == "never"
    assert body["record_count"] == 0

    seed(client, USERS, [{"user_id": "1", "timestamp": "2026-05-23T09:00:00"}])
    body = client.get("/health").json()
    assert body["last_sync"] is not None and body["sync_status"] == "ok"
    assert body["record_count"] == 1 and body["user_count"] == 3


def test_api_key_required_on_protected_routes(client):
    assert client.get("/summary?date=2026-05-23").status_code == 401
    assert client.get("/report?date=2026-05-23").status_code == 401
    assert client.post("/api/ingest", json={}).status_code == 401
    assert client.post("/api/sync").status_code == 401
    assert client.get("/summary?date=2026-05-23", headers={"X-API-Key": "wrong"}).status_code == 401
    # query-string form also accepted
    assert client.get("/summary?date=2026-05-23&api_key=test-api-key").status_code == 200


def test_acceptance_shift_day_boundary_via_summary(client):
    """DAY_START_HOUR=7: /summary?date=2026-05-23 includes 2026-05-24 03:00, excludes 2026-05-24 07:00."""
    seed(
        client,
        USERS,
        [
            {"user_id": "1", "timestamp": "2026-05-23T08:00:00", "status": 1, "punch": 0},
            {"user_id": "1", "timestamp": "2026-05-24T03:00:00", "status": 1, "punch": 1},
            {"user_id": "2", "timestamp": "2026-05-24T07:00:00", "status": 1, "punch": 0},
        ],
    )
    body = client.get("/summary?date=2026-05-23", headers=API_HEADERS).json()
    assert body["date"] == "2026-05-23"
    assert body["total"] == 3 and body["present"] == 1 and body["absent"] == 2
    by_id = {e["id"]: e for e in body["employees"]}
    assert by_id["1"]["first_in"] == "2026-05-23T08:00:00"
    assert by_id["1"]["last_out"] == "2026-05-24T03:00:00"
    assert by_id["1"]["hours"] == 19.0 and by_id["1"]["attended"] is True
    assert by_id["2"]["attended"] is False and by_id["2"]["first_in"] is None
    assert by_id["3"]["name"] == "User 3"  # name fallback
    assert body["total_hours"] == 19.0
    # present first (Alice), then absent by name (Bob, User 3)
    assert [e["id"] for e in body["employees"]] == ["1", "2", "3"]

    # ...and the 07:00 punch belongs to the next shift day
    nxt = client.get("/summary?date=2026-05-24", headers=API_HEADERS).json()
    assert nxt["present"] == 1 and {e["id"] for e in nxt["employees"] if e["attended"]} == {"2"}


def test_summary_range_shape(client):
    seed(
        client,
        USERS,
        [
            {"user_id": "1", "timestamp": "2026-05-20T08:00:00"},
            {"user_id": "1", "timestamp": "2026-05-20T16:00:00"},
            {"user_id": "1", "timestamp": "2026-05-21T08:00:00"},
            {"user_id": "2", "timestamp": "2026-05-22T08:00:00"},
            {"user_id": "2", "timestamp": "2026-05-22T12:30:00"},
        ],
    )
    body = client.get("/summary?from=2026-05-20&to=2026-05-22", headers=API_HEADERS).json()
    assert body["from"] == "2026-05-20" and body["to"] == "2026-05-22"
    assert body["days_total"] == 3 and body["total"] == 3
    assert body["total_hours"] == 12.5
    assert body["avg_present_per_day"] == 1.0  # 3 employee-days / 3 days
    alice, bob, user3 = body["employees"]
    assert alice["id"] == "1" and alice["days_present"] == 2 and alice["days_absent"] == 1
    assert alice["total_hours"] == 8.0 and alice["avg_hours_per_attended_day"] == 4.0
    assert alice["attendance_rate"] == round(2 / 3, 4)
    assert bob["days_present"] == 1 and bob["total_hours"] == 4.5
    assert user3["days_present"] == 0 and user3["attendance_rate"] == 0


def test_report_pdf_download_daily_and_range(client, migrated):
    seed(client, USERS, [{"user_id": "1", "timestamp": "2026-05-23T08:00:00"}, {"user_id": "1", "timestamp": "2026-05-23T17:00:00"}])
    r = client.get("/report?date=2026-05-23", headers=API_HEADERS)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert 'attachment; filename="attendance_2026-05-23.pdf"' in r.headers["content-disposition"]
    assert r.content.startswith(b"%PDF")
    assert (migrated.output_dir / "attendance_2026-05-23.pdf").exists()

    r = client.get("/report?from=2026-05-20&to=2026-05-23", headers=API_HEADERS)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    assert "attendance_2026-05-20_to_2026-05-23.pdf" in r.headers["content-disposition"]

    # today/yesterday keywords resolve (n8n uses date=yesterday)
    r = client.get("/report?date=yesterday", headers=API_HEADERS)
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    y = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    assert f"attendance_{y}.pdf" in r.headers["content-disposition"]

    assert client.get("/report?date=not-a-date", headers=API_HEADERS).status_code == 400


def test_dashboard_and_print_require_basic_auth(client):
    seed(client, USERS, [{"user_id": "1", "timestamp": "2026-05-23T08:00:00"}, {"user_id": "1", "timestamp": "2026-05-23T17:30:00"}])
    assert client.get("/").status_code == 401
    assert client.get("/print").status_code == 401
    assert client.get("/", auth=("admin", "nope")).status_code == 401

    r = client.get("/?date=2026-05-23", auth=BASIC_AUTH)
    assert r.status_code == 200
    html = r.text
    assert "Alice" in html and "08:00:00" in html and "17:30:00" in html and "9:30" in html
    assert "Absent" in html and "Bob" in html
    assert "Sync now" in html and "Last synced" in html

    r = client.get("/print?from=2026-05-23&to=2026-05-23", auth=BASIC_AUTH)
    assert r.status_code == 200
    assert "window.print()" in r.text and "Attendance Report" in r.text
    assert "signature" not in r.text.lower() and "ATT-" not in r.text  # no signature block / reference numbers

    r = client.get("/?range=7", auth=BASIC_AUTH)
    assert r.status_code == 200 and "attendance summary" in r.text.lower() and "avg present / day" in r.text.lower()


def test_sync_queues_request_when_web_has_no_device(client, session_factory):
    from app.service import consume_sync_request

    r = client.post("/api/sync", headers=API_HEADERS)
    assert r.status_code == 200 and r.json()["mode"] == "queued"
    with session_factory() as s:
        assert consume_sync_request(s) is True
        assert consume_sync_request(s) is False

    # dashboard button does the same and redirects back
    r = client.post("/sync", data={"from": "2026-05-23", "to": "2026-05-23"}, auth=BASIC_AUTH, follow_redirects=False)
    assert r.status_code == 303 and "synced=queued" in r.headers["location"]


def test_dashboard_renders_fast_for_40_users_x_90_days(client):
    users = [{"id": str(i), "name": f"Employee {i:02d}"} for i in range(1, 41)]
    records = []
    start = datetime(2026, 3, 1, 8, 0, 0)
    for day in range(90):
        base = start + timedelta(days=day)
        for i in range(1, 41):
            for k, offset in enumerate((0, 4, 5, 9)):  # 4 punches a day
                ts = base + timedelta(hours=offset, minutes=i, seconds=k)
                records.append({"user_id": str(i), "timestamp": ts.isoformat(timespec="seconds")})
    out = seed(client, users, records)
    assert out["records_inserted"] == 40 * 90 * 4

    client.get("/?from=2026-03-01&to=2026-05-29", auth=BASIC_AUTH)  # warm-up
    t0 = time.perf_counter()
    r = client.get("/?from=2026-03-01&to=2026-05-29", auth=BASIC_AUTH)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200 and "Employee 40" in r.text
    assert elapsed < 1.0, f"range dashboard took {elapsed:.3f}s"  # generous CI bound; typically < 0.2 s

    t0 = time.perf_counter()
    r = client.get("/?date=2026-04-15", auth=BASIC_AUTH)
    elapsed = time.perf_counter() - t0
    assert r.status_code == 200 and elapsed < 0.5
