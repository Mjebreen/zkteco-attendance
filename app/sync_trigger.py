"""Bridge between the web service and the collector for "Sync now".

Direct mode : this process has ZK_IP configured (single-box deploy) -> pull the device here.
Queued mode : no ZK_IP here (split deploy) -> set a flag in sync_state that a
              COLLECTOR_TARGET=db collector polls every few seconds.
"""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings
from app.db import session_scope
from app.service import request_sync

log = logging.getLogger("app.sync")


def trigger_sync(settings: Settings, wait: bool = True) -> dict[str, Any]:
    if settings.zk_ip:
        from collector.sync import run_once  # imported lazily so the web image works without a device

        try:
            result = run_once(settings, source="web")
        except Exception as exc:
            log.warning("manual sync failed", extra={"ctx_error": str(exc)})
            return {"ok": False, "mode": "direct", "error": str(exc)}
        return {"ok": True, "mode": "direct", **result}

    with session_scope() as session:
        request_sync(session)
    return {
        "ok": True,
        "mode": "queued",
        "message": "Sync requested; a collector with COLLECTOR_TARGET=db will run within ~10 s.",
    }
