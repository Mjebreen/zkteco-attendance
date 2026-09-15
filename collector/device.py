"""pyzk device access. The only module in the repo that talks to the terminal."""

from __future__ import annotations

import logging
from typing import Any

from app.config import Settings

log = logging.getLogger("collector.device")


class DeviceError(RuntimeError):
    pass


def fetch_snapshot(settings: Settings) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Connect once and return (users, records) as plain dicts.

    users   : [{"id": "12", "name": "Jane"}]
    records : [{"user_id": "12", "timestamp": datetime (naive local), "status": int, "punch": int}]
    """
    if not settings.zk_ip:
        raise DeviceError("ZK_IP is not configured")
    try:
        from zk import ZK
    except ImportError as exc:  # pragma: no cover
        raise DeviceError("pyzk is not installed") from exc

    zk = ZK(
        settings.zk_ip,
        port=settings.zk_port,
        timeout=settings.zk_timeout,
        password=settings.zk_password,
        force_udp=settings.zk_force_udp,
        ommit_ping=settings.zk_omit_ping,
    )
    conn = None
    try:
        conn = zk.connect()
        conn.disable_device()
        users = [
            {"id": str(u.user_id), "name": (u.name or "").strip() or f"User {u.user_id}"}
            for u in conn.get_users()
        ]
        records = [
            {
                "user_id": str(r.user_id),
                "timestamp": r.timestamp,
                "status": getattr(r, "status", None),
                "punch": getattr(r, "punch", None),
            }
            for r in conn.get_attendance()
        ]
        return users, records
    except Exception as exc:
        raise DeviceError(f"{settings.zk_ip}:{settings.zk_port}: {exc}") from exc
    finally:
        if conn is not None:
            try:
                conn.enable_device()
            except Exception:  # never leave the device disabled, but don't mask the real error
                log.warning("enable_device failed", extra={"ctx_device": settings.device_label})
            try:
                conn.disconnect()
            except Exception:
                pass
