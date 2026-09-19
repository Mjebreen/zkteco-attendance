"""Downloadable backups (admin only).

SQLite   -> a consistent snapshot taken with SQLite's online backup API (safe while the app runs).
Postgres -> a portable JSON export of every table.
Both are zipped together with the audit log file and restore instructions.
"""

from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
import zipfile
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.engine import Engine

from app.config import Settings
from app.models import Base

RESTORE_SQLITE = """HOW TO RESTORE (SQLite)

1. Stop the stack:            docker compose stop
2. Copy attendance.db over the live database (default: /data/attendance.db on the `data` volume):
     docker run --rm -v <project>_data:/data -v "$PWD":/src alpine \\
       sh -c 'rm -f /data/attendance.db-wal /data/attendance.db-shm && cp /src/attendance.db /data/attendance.db'
   (find the volume name with: docker volume ls)
   Without Docker, replace the file that DATABASE_URL points to.
3. Start again:               docker compose start

The snapshot contains everything: punches, employees, departments, schedules, vacations,
corrections, accounts (hashed passwords), branding and the audit log.
"""

RESTORE_JSON = """HOW TO RESTORE (JSON export)

data.json holds every table as {"table": [rows...]}. Datetimes are ISO strings and binary columns
are base64 with a "__b64__:" prefix. Load it with a short script against an empty, migrated
database (alembic upgrade head), inserting the tables in the order they appear.
For routine Postgres backups prefer pg_dump.
"""


def _sqlite_path(database_url: str) -> str | None:
    if not database_url.startswith("sqlite"):
        return None
    path = database_url.split("sqlite:///", 1)[-1]
    return path if path and path != ":memory:" else None


def _json_default(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "__b64__:" + base64.b64encode(bytes(value)).decode("ascii")
    return str(value)


def _export_json(engine: Engine) -> bytes:
    out: dict[str, list[dict[str, Any]]] = {}
    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            out[table.name] = [dict(row._mapping) for row in conn.execute(select(table))]
        try:
            out["alembic_version"] = [dict(r._mapping) for r in conn.execute(text("select version_num from alembic_version"))]
        except Exception:  # pragma: no cover
            pass
    return json.dumps(out, default=_json_default, ensure_ascii=False, indent=1).encode("utf-8")


def create_backup(settings: Settings, engine: Engine) -> tuple[str, str, dict[str, Any]]:
    """Build the zip in a temp file. Returns (path, download filename, info). Caller deletes the file."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    filename = f"attendance-backup-{stamp}.zip"
    fd, zip_path = tempfile.mkstemp(prefix="attendance-backup-", suffix=".zip")
    os.close(fd)
    info: dict[str, Any] = {"created_at": datetime.now().isoformat(timespec="seconds"), "company": settings.company_name}
    sqlite_file = _sqlite_path(settings.database_url)
    snapshot: str | None = None
    try:
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            if sqlite_file:
                fd2, snapshot = tempfile.mkstemp(prefix="attendance-snap-", suffix=".db")
                os.close(fd2)
                src = sqlite3.connect(sqlite_file, timeout=30)
                dst = sqlite3.connect(snapshot)
                try:
                    src.backup(dst)  # consistent even while the collector is writing (includes WAL pages)
                finally:
                    dst.close()
                    src.close()
                zf.write(snapshot, "attendance.db")
                zf.writestr("RESTORE.txt", RESTORE_SQLITE)
                info.update(kind="sqlite", database_bytes=os.path.getsize(snapshot))
            else:
                data = _export_json(engine)
                zf.writestr("data.json", data)
                zf.writestr("RESTORE.txt", RESTORE_JSON)
                info.update(kind="json", database_bytes=len(data))
            audit = settings.audit_log_file
            if audit and Path(audit).is_file():
                zf.write(audit, "audit.log")
            zf.writestr("backup-info.json", json.dumps(info, indent=1))
    except Exception:
        if os.path.exists(zip_path):
            os.remove(zip_path)
        raise
    finally:
        if snapshot and os.path.exists(snapshot):
            os.remove(snapshot)
    info["zip_bytes"] = os.path.getsize(zip_path)
    return zip_path, filename, info
