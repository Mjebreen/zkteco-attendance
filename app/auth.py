"""Authentication dependencies.

- API routes: `X-API-Key: <API_KEY>` header or `?api_key=` query parameter (unchanged).
- Web UI: login accounts with a server-side session cookie (see app/accounts.py).
  Browsers that are not signed in are redirected to /login; other clients get 401.
- Legacy: HTTP Basic with DASHBOARD_USER / DASHBOARD_PASSWORD still works *only while
  DASHBOARD_PASSWORD is set* (scripts, old bookmarks). It gets the "hr" role and can never see
  the audit log or manage users. Leave DASHBOARD_PASSWORD empty to disable it.
"""

from __future__ import annotations

import base64
import secrets
from urllib.parse import urlsplit

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.accounts import CurrentUser, user_for_token
from app.config import Settings, get_settings
from app.db import get_db

SESSION_COOKIE = "att_session"


class LoginRequired(Exception):
    """Raised when a web route needs a signed-in user; turned into a redirect or a 401 in main.py."""


class AdminRequired(Exception):
    """A signed-in non-admin opened an admin-only page; browsers are sent back to the dashboard."""


def _eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def require_api_key(request: Request, settings: Settings = Depends(get_settings)) -> None:
    if not settings.api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API_KEY is not configured on the server")
    provided = request.headers.get("X-API-Key") or request.query_params.get("api_key") or ""
    if not _eq(provided, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


def client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def check_same_origin(request: Request) -> None:
    """CSRF guard for state-changing requests: a browser-sent Origin must match this host."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    origin = request.headers.get("origin")
    if not origin or origin == "null":
        return  # non-browser clients (curl, tests) send no Origin; SameSite=Lax covers browsers
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    if urlsplit(origin).netloc.lower() != host.split(",")[0].strip().lower():
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Cross-site request blocked")


def _legacy_basic(request: Request, settings: Settings) -> CurrentUser | None:
    if not settings.dashboard_password:
        return None
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("basic "):
        return None
    try:
        username, _, password = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except (ValueError, UnicodeDecodeError):
        return None
    if _eq(username, settings.dashboard_user) and _eq(password, settings.dashboard_password):
        return CurrentUser(id=None, email=f"basic:{username}", name=username, role="hr", legacy=True)
    return None


def require_dashboard_auth(
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CurrentUser:
    """Signed-in user (session cookie), or the legacy Basic pair. Sets request.state.user."""
    check_same_origin(request)
    user = user_for_token(db, request.cookies.get(SESSION_COOKIE)) or _legacy_basic(request, settings)
    if user is None:
        raise LoginRequired()
    request.state.user = user
    return user


def require_admin(user: CurrentUser = Depends(require_dashboard_auth)) -> CurrentUser:
    if not user.is_admin:
        raise AdminRequired()
    return user


def current_user(request: Request) -> CurrentUser | None:
    return getattr(request.state, "user", None)
