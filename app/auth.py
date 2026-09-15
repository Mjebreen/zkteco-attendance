"""Authentication dependencies.

- API routes: `X-API-Key: <API_KEY>` header or `?api_key=` query parameter.
- Dashboard / print view: HTTP Basic Auth (DASHBOARD_USER / DASHBOARD_PASSWORD).
"""

from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import Settings, get_settings

_basic = HTTPBasic(auto_error=False, realm="Attendance")


def _eq(a: str, b: str) -> bool:
    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def require_api_key(request: Request, settings: Settings = Depends(get_settings)) -> None:
    if not settings.api_key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "API_KEY is not configured on the server")
    provided = request.headers.get("X-API-Key") or request.query_params.get("api_key") or ""
    if not _eq(provided, settings.api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


def require_dashboard_auth(
    credentials: HTTPBasicCredentials | None = Depends(_basic),
    settings: Settings = Depends(get_settings),
) -> str:
    if not settings.dashboard_password:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "DASHBOARD_PASSWORD is not set; refusing to serve the dashboard unauthenticated",
        )
    if credentials is None or not (
        _eq(credentials.username, settings.dashboard_user) and _eq(credentials.password, settings.dashboard_password)
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Authentication required",
            headers={"WWW-Authenticate": 'Basic realm="Attendance"'},
        )
    return credentials.username
