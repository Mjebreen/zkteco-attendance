#!/usr/bin/env python3
"""Create a login account, reset a password, or list accounts from the command line.

    docker compose exec web python scripts/create_user.py --list
    docker compose exec web python scripts/create_user.py --email hr@example.com --role hr
    docker compose exec web python scripts/create_user.py --email me@example.com --reset

The password is asked for interactively (never pass it on the command line: it would end up in
your shell history). Every action is written to the audit log.
"""

from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import accounts  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.logging_config import setup_audit_file, setup_logging  # noqa: E402


def _ask_password() -> str:
    first = getpass.getpass("Password (8+ characters): ")
    if first != getpass.getpass("Repeat password: "):
        print("Passwords do not match.", file=sys.stderr)
        sys.exit(2)
    return first


def main() -> int:
    parser = argparse.ArgumentParser(description="Manage login accounts")
    parser.add_argument("--list", action="store_true", help="list accounts")
    parser.add_argument("--email")
    parser.add_argument("--name", default="")
    parser.add_argument("--role", choices=accounts.ROLES, default="hr")
    parser.add_argument("--reset", action="store_true", help="reset the password of an existing account")
    args = parser.parse_args()

    settings = get_settings()
    setup_logging("text", "WARNING")
    setup_audit_file(settings.audit_log_file)

    with session_scope() as session:
        if args.list:
            for u in accounts.list_users(session):
                last = u.last_login_at.strftime("%Y-%m-%d %H:%M") if u.last_login_at else "never"
                print(f"{u.email:<40} {u.role:<6} {'active' if u.active else 'disabled':<9} last sign-in: {last}")
            return 0
        if not args.email:
            parser.error("--email is required (or use --list)")
        try:
            if args.reset:
                user = accounts.get_user_by_email(session, args.email)
                if user is None:
                    print("No such account.", file=sys.stderr)
                    return 1
                accounts.set_password(session, user.id, _ask_password())
                accounts.record_audit(session, None, "user.password_reset", f"Reset the password of {user.email} (CLI)",
                                      target=f"user:{user.email}", actor_label="cli")
                print(f"Password reset for {user.email}; all their sessions were signed out.")
            else:
                user = accounts.create_user(session, args.email, _ask_password(), role=args.role, name=args.name)
                accounts.record_audit(session, None, "user.create", f"Created {user.role} account {user.email} (CLI)",
                                      target=f"user:{user.email}", actor_label="cli")
                print(f"Created {user.role} account {user.email}.")
        except accounts.AccountError as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
