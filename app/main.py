"""FastAPI application factory + `python -m app.main` entry point."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse

from app.api import router as api_router
from app.auth import AdminRequired, LoginRequired
from app.config import get_settings
from app.logging_config import setup_audit_file, setup_logging
from app.web import router as web_router
from app.web_admin import admin_router, public_router

log = logging.getLogger("app")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_format, settings.log_level)
    if os.getenv("AUTO_MIGRATE", "true").strip().lower() in {"1", "true", "yes", "on"}:
        from app.migrate import run_migrations

        run_migrations()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
    setup_audit_file(settings.audit_log_file)
    if settings.admin_email and settings.admin_password:
        from app.accounts import bootstrap_admin
        from app.db import session_scope

        try:
            with session_scope() as session:
                if bootstrap_admin(session, settings.admin_email, settings.admin_password):
                    log.info("created admin account from ADMIN_EMAIL", extra={"ctx_email": settings.admin_email})
        except Exception as exc:  # e.g. password too short
            log.error("could not create the admin account", extra={"ctx_error": str(exc)})
    log.info(
        "web service ready",
        extra={
            "ctx_company": settings.company_name,
            "ctx_day_start_hour": settings.day_start_hour,
            "ctx_tz": settings.tz,
            "ctx_database": settings.database_url.split("@")[-1],
            "ctx_direct_sync": bool(settings.zk_ip),
        },
    )
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="ZKTeco Attendance", docs_url=None, redoc_url=None, lifespan=_lifespan)
    app.include_router(api_router)
    app.include_router(public_router)
    app.include_router(web_router)
    app.include_router(admin_router)

    @app.exception_handler(LoginRequired)
    async def _login_required(request: Request, _exc: LoginRequired):
        wants_html = "text/html" in request.headers.get("accept", "")
        if request.method == "GET" and wants_html:
            target = request.url.path + (("?" + request.url.query) if request.url.query else "")
            return RedirectResponse(url="/login?next=" + quote(target, safe=""), status_code=303)
        return JSONResponse({"detail": "Authentication required"}, status_code=401)

    @app.exception_handler(AdminRequired)
    async def _admin_required(request: Request, _exc: AdminRequired):
        if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(url="/?denied=1", status_code=303)
        return JSONResponse({"detail": "Administrator access required"}, status_code=403)

    return app


app = create_app()


def main() -> None:
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.server_host,
        port=settings.server_port,
        log_config=None,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
