"""FastAPI application factory + `python -m app.main` entry point."""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api import router as api_router
from app.config import get_settings
from app.logging_config import setup_logging
from app.web import router as web_router

log = logging.getLogger("app")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    settings = get_settings()
    setup_logging(settings.log_format, settings.log_level)
    if os.getenv("AUTO_MIGRATE", "true").strip().lower() in {"1", "true", "yes", "on"}:
        from app.migrate import run_migrations

        run_migrations()
    settings.output_dir.mkdir(parents=True, exist_ok=True)
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
    app.include_router(web_router)
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
