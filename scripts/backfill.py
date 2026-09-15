#!/usr/bin/env python3
"""One full pull from the device into the database (first run / recovery).

Usage (inside the container or a venv with the repo on PYTHONPATH):

    python scripts/backfill.py                # honours COLLECTOR_TARGET (db | http)
    python scripts/backfill.py --target db    # force direct DB write
    python scripts/backfill.py --dry-run      # pull + print counts, write nothing

The pull is idempotent: existing (user_id, timestamp) rows are skipped.
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import get_settings  # noqa: E402
from app.logging_config import setup_logging  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill attendance from the ZKTeco device")
    parser.add_argument("--target", choices=["db", "http"], help="override COLLECTOR_TARGET")
    parser.add_argument("--dry-run", action="store_true", help="pull from the device but do not store anything")
    parser.add_argument("--no-migrate", action="store_true", help="skip running Alembic migrations first")
    args = parser.parse_args()

    settings = get_settings()
    if args.target:
        settings = dataclasses.replace(settings, collector_target=args.target)
    setup_logging("text", settings.log_level)

    if not settings.zk_ip:
        print("ERROR: ZK_IP is not set", file=sys.stderr)
        return 2

    from collector.device import fetch_snapshot

    print(f"Pulling from {settings.device_label} (timeout {settings.zk_timeout}s, "
          f"force_udp={settings.zk_force_udp}, omit_ping={settings.zk_omit_ping}) ...")
    try:
        users, records = fetch_snapshot(settings)
    except Exception as exc:
        print(f"ERROR: device pull failed: {exc}", file=sys.stderr)
        return 1
    print(f"  users on device   : {len(users)}")
    print(f"  attendance records: {len(records)}")
    if records:
        stamps = sorted(r["timestamp"] for r in records)
        print(f"  oldest / newest   : {stamps[0]} / {stamps[-1]}")

    if args.dry_run:
        print("Dry run - nothing written.")
        return 0

    if settings.collector_target == "http":
        from collector.sync import push_http

        print(f"Pushing to {settings.ingest_url} ...")
        result = push_http(settings, users, records, source="backfill")
    else:
        if not args.no_migrate and os.getenv("AUTO_MIGRATE", "true").lower() in {"1", "true", "yes", "on"}:
            from app.migrate import run_migrations

            run_migrations(retries=3, delay=2)
        from app.db import session_scope
        from app.service import ingest

        with session_scope() as session:
            result = ingest(session, users, records, source="backfill")

    print("Done:")
    for key in ("users_received", "users_created", "users_updated", "users_deactivated",
                "records_received", "records_inserted", "records_skipped", "record_count"):
        if key in result:
            print(f"  {key:<20}: {result[key]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
