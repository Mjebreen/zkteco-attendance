"""Login accounts, sessions, roles, user management and the audit log."""

from __future__ import annotations

from pathlib import Path

import pytest

from app import accounts, security
from tests.conftest import API_HEADERS, BASIC_AUTH

ADMIN = ("boss@example.com", "correct-horse-1")
HR = ("hr@example.com", "hr-password-22")


@pytest.fixture()
def admin_client(client, session_factory):
    """A TestClient signed in as an admin through the real login form."""
    from app.web_admin import reset_throttle

    reset_throttle()
    with session_factory() as s:
        accounts.create_user(s, ADMIN[0], ADMIN[1], role="admin", name="The Boss")
    r = client.post("/login", data={"email": ADMIN[0], "password": ADMIN[1], "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return client


def login(client, email, password, next_url="/"):
    return client.post("/login", data={"email": email, "password": password, "next": next_url}, follow_redirects=False)


# ---- primitives ------------------------------------------------------------- #


def test_password_hashing_and_tokens():
    h = security.hash_password("s3cret-password")
    assert h.startswith("scrypt$") and "s3cret" not in h
    assert security.verify_password("s3cret-password", h)
    assert not security.verify_password("wrong", h)
    assert not security.verify_password("x", "garbage") and not security.verify_password("x", "")
    assert security.hash_password("s3cret-password") != h  # salted
    t1, t2 = security.new_session_token(), security.new_session_token()
    assert t1 != t2 and len(security.token_hash(t1)) == 64


def test_account_rules(session_factory):
    with session_factory() as s:
        a = accounts.create_user(s, "  Boss@Example.COM ", "long-enough-pw", role="admin")
        assert a.email == "boss@example.com"
        for bad, key in ((("x", "long-enough-pw", "hr"), "bad_email"), (("a@b.c", "short", "hr"), "password_too_short"),
                         (("a@b.c", "long-enough-pw", "root"), "bad_role"), (("boss@example.com", "long-enough-pw", "hr"), "email_exists")):
            with pytest.raises(accounts.AccountError, match=key):
                accounts.create_user(s, bad[0], bad[1], role=bad[2])
        # the last admin can be neither demoted nor disabled
        with pytest.raises(accounts.AccountError, match="last_admin"):
            accounts.update_user(s, a.id, name="", role="hr", active=True, acting_user_id=None)
        with pytest.raises(accounts.AccountError, match="last_admin"):
            accounts.update_user(s, a.id, name="", role="admin", active=False, acting_user_id=None)
        assert accounts.authenticate(s, "BOSS@example.com", "long-enough-pw") is not None
        assert accounts.authenticate(s, "boss@example.com", "nope") is None
        assert accounts.authenticate(s, "ghost@example.com", "long-enough-pw") is None
        # bootstrap never overwrites an existing account
        assert accounts.bootstrap_admin(s, "boss@example.com", "another-password") is None
        assert accounts.authenticate(s, "boss@example.com", "long-enough-pw") is not None
        assert accounts.bootstrap_admin(s, "second@example.com", "another-password").role == "admin"


# ---- login flow ------------------------------------------------------------- #


def test_browser_is_redirected_to_login_and_api_clients_get_401(client):
    r = client.get("/", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login?next=%2F"
    r = client.get("/employee/5?month=2026-05", headers={"accept": "text/html"}, follow_redirects=False)
    assert r.headers["location"] == "/login?next=%2Femployee%2F5%3Fmonth%3D2026-05"
    assert client.get("/").status_code == 401  # non-browser
    assert client.post("/settings/departments", data={"name": "X"}, headers={"accept": "text/html"}).status_code == 401
    page = client.get("/login")
    assert page.status_code == 200 and 'name="password"' in page.text and "No accounts exist yet" in page.text
    assert client.get("/health").status_code == 200  # still open
    assert client.get("/summary?date=2026-05-23", headers=API_HEADERS).status_code == 200  # API key unaffected


def test_login_logout_session_cookie_and_open_redirect(client, session_factory):
    from app.web_admin import reset_throttle

    reset_throttle()
    with session_factory() as s:
        accounts.create_user(s, ADMIN[0], ADMIN[1], role="admin")

    r = login(client, ADMIN[0], "wrong-password")
    assert r.status_code == 401 and "Wrong email or password" in r.text and "att_session" not in r.headers.get("set-cookie", "")

    r = login(client, ADMIN[0], ADMIN[1], next_url="https://evil.example/steal")
    assert r.status_code == 303 and r.headers["location"] == "/"  # open redirect refused
    cookie = r.headers["set-cookie"]
    assert "att_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie

    home = client.get("/?date=2026-05-23")
    assert home.status_code == 200 and "Sign out" in home.text and "Audit log" in home.text and ADMIN[0] in home.text
    assert client.get("/login", follow_redirects=False).status_code == 303  # already signed in

    with session_factory() as s:  # only a hash of the token is stored
        from app.models import AppSession

        row = s.query(AppSession).one()
        assert row.token_hash != client.cookies.get("att_session") and len(row.token_hash) == 64

    r = client.post("/logout", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/login"
    assert client.get("/").status_code == 401
    with session_factory() as s:
        assert s.query(AppSession).count() == 0


def test_cross_site_post_is_blocked(admin_client):
    r = admin_client.post("/settings/departments", data={"name": "Evil"}, headers={"origin": "https://evil.example"})
    assert r.status_code == 403
    r = admin_client.post("/settings/departments", data={"name": "Fine"}, headers={"origin": "http://testserver"}, follow_redirects=False)
    assert r.status_code == 303


def test_lockout_after_repeated_failures(client, session_factory):
    from app.web_admin import MAX_FAILURES, reset_throttle

    reset_throttle()
    with session_factory() as s:
        accounts.create_user(s, ADMIN[0], ADMIN[1], role="admin")
    for _ in range(MAX_FAILURES):
        assert login(client, ADMIN[0], "bad-password-x").status_code == 401
    r = login(client, ADMIN[0], ADMIN[1])  # even the right password is refused while locked
    assert r.status_code == 429 and "Too many failed attempts" in r.text
    reset_throttle()
    assert login(client, ADMIN[0], ADMIN[1]).status_code == 303


# ---- roles, users, passwords ------------------------------------------------ #


def test_roles_and_user_management(admin_client, session_factory):
    c = admin_client
    assert c.get("/users").status_code == 200 and c.get("/audit").status_code == 200

    r = c.post("/users", data={"email": HR[0], "name": "Hana HR", "role": "hr", "password": HR[1]}, follow_redirects=False)
    assert "msg=saved" in r.headers["location"]
    assert "password_too_short" in c.post("/users", data={"email": "x@example.com", "role": "hr", "password": "short"},
                                          follow_redirects=False).headers["location"]
    assert "email_exists" in c.post("/users", data={"email": HR[0], "role": "hr", "password": HR[1]},
                                    follow_redirects=False).headers["location"]
    with session_factory() as s:
        hr_id, admin_id = accounts.get_user_by_email(s, HR[0]).id, accounts.get_user_by_email(s, ADMIN[0]).id

    # an admin cannot lock themselves out
    r = c.post(f"/users/{admin_id}", data={"name": "", "role": "hr", "active": "1"}, follow_redirects=False)
    assert "last_admin" in r.headers["location"] or "cannot_demote_self" in r.headers["location"]

    # HR signs in on a second client: dashboard + settings yes, users + audit no
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as hr:
        assert login(hr, HR[0], HR[1]).status_code == 303
        assert hr.get("/").status_code == 200 and hr.get("/settings").status_code == 200
        assert hr.get("/users").status_code == 403 and hr.get("/audit").status_code == 403 and hr.get("/audit.csv").status_code == 403
        assert hr.post("/users", data={"email": "z@example.com", "role": "admin", "password": "whatever-123"}).status_code == 403
        assert "Audit log" not in hr.get("/").text
        hr.post("/settings/departments", data={"name": "Support"})  # HR change, attributed to HR in the log

        # own password: wrong current -> refused; mismatch -> refused; ok -> other sessions die
        assert "wrong_password" in hr.post("/account/password", data={"current_password": "nope", "new_password": "brand-new-pass-1",
                                           "confirm_password": "brand-new-pass-1"}, follow_redirects=False).headers["location"]
        assert "password_mismatch" in hr.post("/account/password", data={"current_password": HR[1], "new_password": "brand-new-pass-1",
                                              "confirm_password": "different-pass-1"}, follow_redirects=False).headers["location"]
        assert "msg=saved" in hr.post("/account/password", data={"current_password": HR[1], "new_password": "brand-new-pass-1",
                                      "confirm_password": "brand-new-pass-1"}, follow_redirects=False).headers["location"]
        assert hr.get("/").status_code == 200  # this session survives

        # admin disables the HR account -> the HR session stops working immediately
        r = c.post(f"/users/{hr_id}", data={"name": "Hana HR", "role": "hr", "active": "0"}, follow_redirects=False)
        assert "msg=saved" in r.headers["location"]
        assert hr.get("/").status_code == 401
        assert login(hr, HR[0], "brand-new-pass-1").status_code == 401

    # admin resets the password and re-enables the account
    c.post(f"/users/{hr_id}", data={"name": "Hana HR", "role": "hr", "active": "1"})
    assert "msg=saved" in c.post(f"/users/{hr_id}/password", data={"password": "reset-by-admin-9"}, follow_redirects=False).headers["location"]
    with session_factory() as s:
        assert accounts.authenticate(s, HR[0], "reset-by-admin-9") is not None

    # legacy HTTP Basic still works while DASHBOARD_PASSWORD is set, as HR only
    from fastapi.testclient import TestClient as TC

    with TC(create_app()) as legacy:
        assert legacy.get("/", auth=BASIC_AUTH).status_code == 200
        assert legacy.get("/audit", auth=BASIC_AUTH).status_code == 403 and legacy.get("/users", auth=BASIC_AUTH).status_code == 403


# ---- audit log -------------------------------------------------------------- #


def test_everything_is_audited_and_written_to_the_file(admin_client, migrated):
    c = admin_client
    c.post("/api/ingest", json={"users": [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}], "records": []}, headers=API_HEADERS)

    c.post("/settings/departments", data={"name": "Engineering"})
    c.post("/settings/departments/1", data={"action": "rename", "name": "R&D"})
    c.post("/settings/employees/1", data={"display_name": "Alice Smith", "department_id": "1"})
    c.post("/employee/1/weekly", data={f"wd{i}": "work" for i in range(7)} | {"wd4": "off"})
    c.post("/employee/1/day", data={"day": "2026-05-25", "kind": "online", "note": "WFH"})
    c.post("/employee/1/day", data={"day": "2026-05-22", "kind": "move", "move_to": "2026-05-24"})
    c.post("/employee/1/vacations", data={"start": "2026-06-01", "end": "2026-06-07", "note": "Annual"})
    c.post("/employee/1/vacations/1/delete")
    c.post("/settings/weekly-bulk", data={"scope": "all", "weekday": "5", "kind": "off"})
    c.post("/settings/vacation-bulk", data={"scope": "all", "start": "2026-06-20", "end": "2026-06-20", "note": "Holiday"})
    c.post("/settings/departments/1", data={"action": "delete"})
    c.get("/export.csv?date=2026-05-23")
    c.post("/sync", data={"from": "2026-05-23", "to": "2026-05-23"})
    c.post("/users", data={"email": HR[0], "role": "hr", "password": HR[1]})
    # a no-op save must NOT create noise
    c.post("/employee/1/weekly", data={f"wd{i}": "work" for i in range(7)} | {"wd4": "off", "wd5": "off"})

    page = c.get("/audit").text
    for needle in ("login.success", "department.create", "Renamed department Engineering", "employee.update", "Alice Smith",
                   "schedule.weekly", "Friday=day off", "schedule.day", "set to online", "schedule.move",
                   "Moved day off for Alice Smith (1) from 2026-05-22 to 2026-05-24", "vacation.add", "vacation.delete",
                   "schedule.weekly_bulk", "Every Saturday = day off for all employees (2 employees)", "vacation.bulk",
                   "department.delete", "export.csv", "sync.manual", "user.create", ADMIN[0]):
        assert needle in page, needle
    assert HR[1] not in page and ADMIN[1] not in page  # passwords never reach the log

    # filters + CSV
    assert "department.create" not in c.get("/audit?action=vacation").text
    assert "vacation.add" in c.get("/audit?q=Annual").text
    assert "No" in c.get("/audit?q=zzzz-nothing").text or "Nothing recorded" in c.get("/audit?q=zzzz-nothing").text
    csv_text = c.get("/audit.csv?action=schedule").text
    assert csv_text.splitlines()[0].lstrip("﻿") == "time,who,ip,action,target,summary,details" and "schedule.move" in csv_text

    # the plain-text file an admin can tail on the server
    text = Path(migrated.audit_log_file).read_text(encoding="utf-8")
    assert "vacation.add" in text and ADMIN[0] in text and "Moved day off for Alice Smith" in text and ADMIN[1] not in text

    # failed logins are recorded too (from a separate client)
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as other:
        login(other, ADMIN[0], "totally-wrong")
    assert "login.failed" in c.get("/audit?action=login").text


# ---- branding --------------------------------------------------------------- #

def _png(width: int = 8, height: int = 4) -> bytes:
    import io

    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (width, height), (124, 58, 237, 255)).save(buf, format="PNG")
    return buf.getvalue()


PNG_1PX = _png()


def test_branding_colors_and_validation():
    from app import branding

    assert branding.normalize_hex("1A2B3C") == "#1a2b3c" and branding.normalize_hex("#abc") == "#aabbcc"
    assert branding.normalize_hex("") is None
    with pytest.raises(branding.BrandingError):
        branding.normalize_hex("red")
    assert branding.on_color("#ffffff") == "#0b1220" and branding.on_color("#0f766e") == "#ffffff"
    assert branding.mix("#000000", "#ffffff", 0.5) == "#808080"
    assert branding.sniff_image(PNG_1PX) == "image/png"
    assert branding.sniff_image(b"<svg xmlns='http://www.w3.org/2000/svg'><script>alert(1)</script></svg>") is None


def test_admin_can_rebrand_and_hr_cannot(admin_client, session_factory):
    c = admin_client
    assert c.get("/branding").status_code == 200
    assert c.get("/branding/logo").status_code == 404  # nothing uploaded yet

    r = c.post("/branding", data={"company_name": "Acme Corp", "tagline": "People first", "accent": "#7c3aed",
                                  "accent_dark": "", "default_theme": "dark"},
               files={"logo": ("logo.png", PNG_1PX, "image/png")}, follow_redirects=False)
    assert "msg=saved" in r.headers["location"]

    home = c.get("/?date=2026-05-23").text
    assert "Acme Corp" in home and "--accent:#7c3aed" in home and "/branding/logo?v=" in home and 'rel="icon"' in home
    assert '"dark"' in home  # default theme handed to the theme bootstrap script

    logo = c.get("/branding/logo")
    assert logo.status_code == 200 and logo.headers["content-type"] == "image/png" and logo.content == PNG_1PX
    assert logo.headers["x-content-type-options"] == "nosniff" and "immutable" in logo.headers["cache-control"]

    # the login page (signed out) and the PDF / API pick it up too
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as anon:
        page = anon.get("/login").text
        assert "Acme Corp" in page and "People first" in page and "/branding/logo?v=" in page
        assert anon.get("/branding/logo").status_code == 200  # public on purpose
        assert anon.get("/branding").status_code == 401
        assert anon.get("/health").json()["company"] == "Acme Corp"
    assert c.get("/report?date=2026-05-23", headers=API_HEADERS).content.startswith(b"%PDF")
    assert "Acme Corp" in c.get("/print?from=2026-05-23&to=2026-05-23").text

    # validation: bad colour, SVG / non-image upload, oversize
    assert "bad_color" in c.post("/branding", data={"accent": "blue", "default_theme": "auto"}, follow_redirects=False).headers["location"]
    assert "logo_bad_type" in c.post("/branding", data={"default_theme": "auto"},
                                     files={"logo": ("x.svg", b"<svg></svg>", "image/svg+xml")}, follow_redirects=False).headers["location"]
    assert "logo_bad_type" in c.post("/branding", data={"default_theme": "auto"},
                                     files={"logo": ("cut.png", PNG_1PX[:40], "image/png")}, follow_redirects=False).headers["location"]
    assert "logo_too_big" in c.post("/branding", data={"default_theme": "auto"},
                                    files={"logo": ("big.png", PNG_1PX + b"0" * (1024 * 1024 + 10), "image/png")},
                                    follow_redirects=False).headers["location"]

    # back to defaults + remove logo
    c.post("/branding", data={"company_name": "", "tagline": "", "use_default_accent": "1", "default_theme": "auto", "remove_logo": "1"})
    home = c.get("/?date=2026-05-23").text
    assert "Test Co" in home and "--accent:#7c3aed" not in home and c.get("/branding/logo").status_code == 404

    audit = c.get("/audit?action=branding").text
    assert "branding.update" in audit and "Acme Corp" in audit and "uploaded logo.png" in audit

    # HR cannot open or change it
    with session_factory() as s:
        accounts.create_user(s, HR[0], HR[1], role="hr")
    with TestClient(create_app()) as hr:
        assert login(hr, HR[0], HR[1]).status_code == 303
        assert hr.get("/branding").status_code == 403
        assert hr.post("/branding", data={"company_name": "Hacked", "default_theme": "auto"}).status_code == 403
        assert "Branding" not in hr.get("/").text


def test_hr_in_a_browser_is_sent_back_to_the_dashboard_not_shown_json(client, session_factory):
    from app.web_admin import reset_throttle

    reset_throttle()
    with session_factory() as s:
        accounts.create_user(s, HR[0], HR[1], role="hr")
    # logging in with a saved "next" that points at an admin page lands on the dashboard instead
    r = login(client, HR[0], HR[1], next_url="/audit?page=2")
    assert r.status_code == 303 and r.headers["location"] == "/"
    html = {"accept": "text/html,application/xhtml+xml"}
    for path in ("/users", "/audit", "/branding"):
        r = client.get(path, headers=html, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/?denied=1", path
    page = client.get("/?denied=1", headers=html)
    assert page.status_code == 200 and "administrators only" in page.text and "Administrator access required" not in page.text
    # scripts / API clients still get a proper 403
    assert client.get("/audit").status_code == 403
    assert client.post("/users", data={"email": "z@example.com", "role": "admin", "password": "whatever-123"}, headers=html).status_code == 403
