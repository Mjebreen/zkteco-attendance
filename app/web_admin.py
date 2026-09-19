"""Sign-in / sign-out, own password, user management and the audit log viewer.

public_router : /login, /logout                       (no session needed)
admin_router  : /account (any signed-in account), /users and /audit (admins only)
"""

from __future__ import annotations

import csv
import io
import time
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy.orm import Session

from app import accounts, branding, i18n
from app.accounts import AccountError, CurrentUser
from app.auth import SESSION_COOKIE, check_same_origin, client_ip, require_admin, require_dashboard_auth
from app.config import Settings, get_settings
from app.db import get_db
from app.web import LANG_COOKIE, _audit, _base_context, _render, templates

public_router = APIRouter()
admin_router = APIRouter()

# ---- brute-force throttle (per IP + e-mail, in memory) ---------------------- #
MAX_FAILURES = 8
WINDOW_SECONDS = 10 * 60
_failures: dict[str, list[float]] = {}


def _throttle_key(request: Request, email: str) -> str:
    return f"{client_ip(request) or '-'}|{accounts.normalize_email(email)}"


def _is_locked(key: str) -> bool:
    now = time.time()
    recent = [t for t in _failures.get(key, []) if now - t < WINDOW_SECONDS]
    _failures[key] = recent
    return len(recent) >= MAX_FAILURES


def _note_failure(key: str) -> None:
    _failures.setdefault(key, []).append(time.time())


def reset_throttle() -> None:
    _failures.clear()


def _safe_next(target: str | None) -> str:
    target = (target or "/").strip()
    if not target.startswith("/") or target.startswith("//") or "\\" in target or target.startswith("/login"):
        return "/"
    return target


def _login_page(request: Request, settings: Settings, *, error: str = "", email: str = "", next_url: str = "/",
                status_code: int = 200, db: Session | None = None):
    lang = i18n.resolve_lang(
        request.query_params.get("lang"), request.cookies.get(LANG_COOKIE), request.headers.get("accept-language"),
        settings.default_lang,
    )
    other = "ar" if lang == "en" else "en"
    brand = branding.load(db, settings) if db is not None else None
    ctx = {
        "request": request,
        "lang": lang,
        "rtl": i18n.is_rtl(lang),
        "t": lambda key, **kw: i18n.t(lang, key, **kw),
        "brand": brand,
        "company": brand.company_name if brand else settings.company_name,
        "error": error,
        "email": email,
        "next_url": next_url,
        "lang_switch_url": "/login?" + urlencode({"lang": other, "next": next_url}),
        "setup_needed": False,
    }
    return ctx, status_code


@public_router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, db: Session = Depends(get_db), settings: Settings = Depends(get_settings)):
    next_url = _safe_next(request.query_params.get("next"))
    if accounts.user_for_token(db, request.cookies.get(SESSION_COOKIE)):
        return RedirectResponse(url=next_url, status_code=303)
    ctx, code = _login_page(request, settings, next_url=next_url, db=db)
    ctx["setup_needed"] = not accounts.list_users(db)
    resp = templates.TemplateResponse(request, "login.html", ctx, status_code=code)
    if request.query_params.get("lang"):
        resp.set_cookie(LANG_COOKIE, ctx["lang"], max_age=365 * 24 * 3600, samesite="lax")
    return resp


@public_router.post("/login")
def login_submit(
    request: Request,
    email: str = Form(""),
    password: str = Form(""),
    next: str = Form("/"),  # noqa: A002 - form field name
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    check_same_origin(request)
    next_url = _safe_next(next)
    key = _throttle_key(request, email)
    ip = client_ip(request)
    if _is_locked(key):
        accounts.record_audit(db, None, "login.locked", f"Login blocked (too many failures) for {email!r}",
                              ip=ip, actor_label=accounts.normalize_email(email) or "anonymous")
        ctx, code = _login_page(request, settings, error="too_many_attempts", email=email, next_url=next_url,
                                status_code=429, db=db)
        return templates.TemplateResponse(request, "login.html", ctx, status_code=code)

    user = accounts.authenticate(db, email, password)
    if user is None:
        _note_failure(key)
        accounts.record_audit(db, None, "login.failed", f"Failed login for {accounts.normalize_email(email)!r}",
                              ip=ip, actor_label=accounts.normalize_email(email) or "anonymous",
                              details={"user_agent": (request.headers.get("user-agent") or "")[:200]})
        ctx, code = _login_page(request, settings, error="bad_credentials", email=email, next_url=next_url,
                                status_code=401, db=db)
        return templates.TemplateResponse(request, "login.html", ctx, status_code=code)

    _failures.pop(key, None)
    token = accounts.create_session(db, user, settings.session_days, ip, request.headers.get("user-agent"))
    current = CurrentUser(id=user.id, email=user.email, name=user.name or user.email, role=user.role)
    accounts.record_audit(db, current, "login.success", f"{user.email} signed in", ip=ip,
                          details={"user_agent": (request.headers.get("user-agent") or "")[:200]})
    resp = RedirectResponse(url=next_url, status_code=303)
    resp.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_days * 24 * 3600,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure or request.url.scheme == "https",
        path="/",
    )
    return resp


@public_router.post("/logout")
def logout(request: Request, db: Session = Depends(get_db)):
    check_same_origin(request)
    token = request.cookies.get(SESSION_COOKIE)
    user = accounts.user_for_token(db, token)
    accounts.delete_session(db, token)
    if user:
        accounts.record_audit(db, user, "logout", f"{user.email} signed out", ip=client_ip(request))
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


# --------------------------------------------------------------------------- #
# Own account
# --------------------------------------------------------------------------- #


def _redirect(path: str, msg: str) -> RedirectResponse:
    return RedirectResponse(url=f"{path}?{urlencode({'msg': msg})}", status_code=303)


@admin_router.get("/account", response_class=HTMLResponse)
def account_page(
    request: Request,
    user: CurrentUser = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    ctx = _base_context(request, db, settings, "account")
    return _render(request, "account.html", ctx)


@admin_router.post("/account/password")
def change_own_password(
    request: Request,
    current_password: str = Form(""),
    new_password: str = Form(""),
    confirm_password: str = Form(""),
    user: CurrentUser = Depends(require_dashboard_auth),
    db: Session = Depends(get_db),
):
    if user.legacy or user.id is None:
        raise HTTPException(403, "This sign-in has no account")
    if accounts.authenticate(db, user.email, current_password) is None:
        _audit(request, db, "password.change_failed", f"{user.email} entered a wrong current password")
        return _redirect("/account", "err:wrong_password")
    if new_password != confirm_password:
        return _redirect("/account", "err:password_mismatch")
    try:
        accounts.set_password(db, user.id, new_password, keep_token=request.cookies.get(SESSION_COOKIE))
    except AccountError as exc:
        return _redirect("/account", f"err:{exc}")
    _audit(request, db, "password.change", f"{user.email} changed their password", target=f"user:{user.email}")
    return _redirect("/account", "saved")


# --------------------------------------------------------------------------- #
# Users (admin only)
# --------------------------------------------------------------------------- #


@admin_router.get("/users", response_class=HTMLResponse)
def users_page(
    request: Request,
    _admin: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    ctx = _base_context(request, db, settings, "users")
    ctx.update(app_users=accounts.list_users(db), roles=accounts.ROLES)
    return _render(request, "users.html", ctx)


@admin_router.post("/users")
def create_user(
    request: Request,
    email: str = Form(""),
    name: str = Form(""),
    role: str = Form("hr"),
    password: str = Form(""),
    _admin: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    try:
        user = accounts.create_user(db, email, password, role=role, name=name)
    except AccountError as exc:
        return _redirect("/users", f"err:{exc}")
    _audit(request, db, "user.create", f"Created {user.role} account {user.email}", target=f"user:{user.email}",
           role=user.role, name=user.name)
    return _redirect("/users", "saved")


@admin_router.post("/users/{user_id}")
def update_user(
    user_id: int,
    request: Request,
    name: str = Form(""),
    role: str = Form("hr"),
    active: str = Form(""),
    admin: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    before = next((u for u in accounts.list_users(db) if u.id == user_id), None)
    if before is None:
        raise HTTPException(404, "user not found")
    old = {"name": before.name, "role": before.role, "active": before.active}
    try:
        user = accounts.update_user(db, user_id, name=name, role=role, active=active == "1", acting_user_id=admin.id)
    except AccountError as exc:
        return _redirect("/users", f"err:{exc}")
    new = {"name": user.name, "role": user.role, "active": user.active}
    if new != old:
        _audit(request, db, "user.update", f"Updated account {user.email}: role {old['role']} → {new['role']}, "
               f"active {old['active']} → {new['active']}", target=f"user:{user.email}", before=old, after=new)
    return _redirect("/users", "saved")


@admin_router.post("/users/{user_id}/password")
def reset_user_password(
    user_id: int,
    request: Request,
    password: str = Form(""),
    _admin: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
):
    keep = request.cookies.get(SESSION_COOKIE) if _admin.id == user_id else None
    try:
        user = accounts.set_password(db, user_id, password, keep_token=keep)
    except AccountError as exc:
        return _redirect("/users", f"err:{exc}")
    _audit(request, db, "user.password_reset", f"Reset the password of {user.email}", target=f"user:{user.email}")
    return _redirect("/users", "saved")


# --------------------------------------------------------------------------- #
# Branding (admin only) + the public logo
# --------------------------------------------------------------------------- #


@public_router.get("/branding/logo")
def logo_image(db: Session = Depends(get_db)):
    """Public on purpose: the login page shows it. Images only (no SVG), never sniffed, long-cached by ?v=."""
    logo = branding.get_logo(db)
    if logo is None:
        raise HTTPException(404, "no logo")
    data, content_type = logo
    return Response(
        content=data,
        media_type=content_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'",
        },
    )


@admin_router.get("/branding", response_class=HTMLResponse)
def branding_page(
    request: Request,
    _admin: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    ctx = _base_context(request, db, settings, "branding")
    ctx.update(default_accent=branding.DEFAULT_ACCENT, env_company=settings.company_name, themes=branding.THEMES,
               max_logo_kb=branding.MAX_LOGO_BYTES // 1024)
    return _render(request, "branding.html", ctx)


@admin_router.post("/branding")
async def save_branding(
    request: Request,
    company_name: str = Form(""),
    tagline: str = Form(""),
    accent: str = Form(""),
    accent_dark: str = Form(""),
    use_default_accent: str = Form(""),
    default_theme: str = Form("auto"),
    remove_logo: str = Form(""),
    logo: UploadFile | None = File(None),
    _admin: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    before = branding.load(db, settings)
    if use_default_accent == "1":
        accent, accent_dark = "", ""
    try:
        branding.save(db, company_name=company_name, tagline=tagline, accent=accent, accent_dark=accent_dark,
                      default_theme=default_theme)
        logo_change = ""
        if remove_logo == "1":
            if branding.remove_logo(db):
                logo_change = "removed"
        elif logo is not None and logo.filename:
            data = await logo.read(branding.MAX_LOGO_BYTES + 1)
            content_type = branding.set_logo(db, data)
            logo_change = f"uploaded {logo.filename} ({content_type}, {len(data) // 1024} KB)"
    except branding.BrandingError as exc:
        return _redirect("/branding", f"err:{exc}")
    after = branding.load(db, settings)
    changes = {
        k: {"before": getattr(before, k), "after": getattr(after, k)}
        for k in ("company_name", "tagline", "accent", "accent_dark", "default_theme")
        if getattr(before, k) != getattr(after, k)
    }
    if logo_change:
        changes["logo"] = logo_change
    if changes:
        _audit(request, db, "branding.update", "Updated branding: " + ", ".join(changes), **changes)
    return _redirect("/branding", "saved")


# --------------------------------------------------------------------------- #
# Audit log (admin only)
# --------------------------------------------------------------------------- #

PAGE_SIZE = 100
ACTION_GROUPS = ["login", "logout", "schedule", "vacation", "employee", "department", "user", "password", "branding",
                 "sync", "export"]


def _audit_filters(request: Request) -> dict[str, Any]:
    q = request.query_params

    def _d(value: str | None) -> datetime | None:
        try:
            return datetime.strptime(value or "", "%Y-%m-%d")
        except ValueError:
            return None

    date_to = _d(q.get("to"))
    return {
        "q": (q.get("q") or "").strip(),
        "actor": (q.get("actor") or "").strip(),
        "action": (q.get("action") or "").strip(),
        "date_from": _d(q.get("from")),
        "date_to": date_to + timedelta(days=1) if date_to else None,
    }


@admin_router.get("/audit", response_class=HTMLResponse)
def audit_page(
    request: Request,
    _admin: CurrentUser = Depends(require_admin),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    ctx = _base_context(request, db, settings, "audit")
    filters = _audit_filters(request)
    try:
        page = max(1, int(request.query_params.get("page") or 1))
    except ValueError:
        page = 1
    rows, total = accounts.query_audit(db, **filters, limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE)
    params = {k: v for k, v in request.query_params.items() if k not in ("page", "msg") and v}
    ctx.update(
        entries=rows,
        total=total,
        page=page,
        pages=max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE),
        actors=accounts.audit_actors(db),
        action_groups=ACTION_GROUPS,
        f=dict(request.query_params),
        base_qs=urlencode(params),
        audit_file=settings.audit_log_file,
    )
    return _render(request, "audit.html", ctx)


@admin_router.get("/audit.csv")
def audit_csv(request: Request, _admin: CurrentUser = Depends(require_admin), db: Session = Depends(get_db)):
    rows, _total = accounts.query_audit(db, **_audit_filters(request), limit=50000, offset=0)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time", "who", "ip", "action", "target", "summary", "details"])
    for r in rows:
        w.writerow([r.ts.isoformat(timespec="seconds"), r.actor, r.ip or "", r.action, r.target or "", r.summary, r.details or ""])
    _audit(request, db, "export.audit", f"Exported the audit log ({len(rows)} entries)")
    return Response(
        content="﻿" + buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="audit_{datetime.now():%Y-%m-%d}.csv"'},
    )
