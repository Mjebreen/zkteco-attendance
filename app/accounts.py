"""Login accounts, server-side sessions and the audit log.

Accounts here are the people who operate this system (admin / HR). They are unrelated to the
employees enrolled on the fingerprint device.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import AppSession, AppUser, AuditLog
from app.security import MIN_PASSWORD_LENGTH, hash_password, new_session_token, token_hash, verify_password

log = logging.getLogger("app.accounts")
audit_logger = logging.getLogger("audit")

ROLES = ("admin", "hr")


class AccountError(ValueError):
    """User-facing validation problem (message is an i18n key)."""


@dataclass(frozen=True)
class CurrentUser:
    id: int | None
    email: str
    name: str
    role: str
    legacy: bool = False  # authenticated with the deprecated DASHBOARD_USER/PASSWORD pair

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def label(self) -> str:
        return self.email


def normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


# --------------------------------------------------------------------------- #
# Users
# --------------------------------------------------------------------------- #


def get_user_by_email(session: Session, email: str) -> AppUser | None:
    return session.scalar(select(AppUser).where(AppUser.email == normalize_email(email)))


def list_users(session: Session) -> list[AppUser]:
    return list(session.scalars(select(AppUser).order_by(AppUser.role, AppUser.email)).all())


def count_active_admins(session: Session) -> int:
    return int(
        session.scalar(select(func.count()).select_from(AppUser).where(AppUser.role == "admin", AppUser.active.is_(True)))
        or 0
    )


def _check_password(password: str) -> None:
    if len(password or "") < MIN_PASSWORD_LENGTH:
        raise AccountError("password_too_short")


def create_user(session: Session, email: str, password: str, role: str = "hr", name: str | None = None) -> AppUser:
    email = normalize_email(email)
    if "@" not in email or len(email) > 255:
        raise AccountError("bad_email")
    if role not in ROLES:
        raise AccountError("bad_role")
    _check_password(password)
    if get_user_by_email(session, email):
        raise AccountError("email_exists")
    user = AppUser(
        email=email,
        name=(name or "").strip()[:255] or None,
        password_hash=hash_password(password),
        role=role,
        active=True,
        created_at=datetime.now(),
    )
    session.add(user)
    session.commit()
    return user


def update_user(session: Session, user_id: int, *, name: str | None, role: str, active: bool, acting_user_id: int | None) -> AppUser:
    user = session.get(AppUser, user_id)
    if user is None:
        raise AccountError("user_not_found")
    if role not in ROLES:
        raise AccountError("bad_role")
    losing_admin = user.role == "admin" and user.active and (role != "admin" or not active)
    if losing_admin and count_active_admins(session) <= 1:
        raise AccountError("last_admin")
    if user.id == acting_user_id and (not active or role != "admin"):
        raise AccountError("cannot_demote_self")
    user.name = (name or "").strip()[:255] or None
    user.role = role
    user.active = active
    if not active:
        revoke_sessions(session, user.id, commit=False)
    session.commit()
    return user


def set_password(session: Session, user_id: int, password: str, keep_token: str | None = None) -> AppUser:
    user = session.get(AppUser, user_id)
    if user is None:
        raise AccountError("user_not_found")
    _check_password(password)
    user.password_hash = hash_password(password)
    revoke_sessions(session, user.id, keep_token=keep_token, commit=False)  # log out everywhere else
    session.commit()
    return user


def bootstrap_admin(session: Session, email: str, password: str) -> AppUser | None:
    """Create the first admin from ADMIN_EMAIL / ADMIN_PASSWORD. Never overwrites an existing account."""
    email = normalize_email(email)
    if not email or not password:
        return None
    if get_user_by_email(session, email):
        return None
    user = create_user(session, email, password, role="admin")
    record_audit(session, None, "user.bootstrap", f"Created admin account {email} from ADMIN_EMAIL", target=f"user:{email}",
                 actor_label="system")
    return user


# --------------------------------------------------------------------------- #
# Authentication + sessions
# --------------------------------------------------------------------------- #


def authenticate(session: Session, email: str, password: str) -> AppUser | None:
    user = get_user_by_email(session, email)
    if user is None:
        verify_password(password, "scrypt$16384$8$1$AAAAAAAAAAAAAAAAAAAAAA$" + "A" * 43)  # equalize timing
        return None
    if not user.active or not verify_password(password, user.password_hash):
        return None
    return user


def create_session(session: Session, user: AppUser, days: int, ip: str | None, user_agent: str | None) -> str:
    now = datetime.now()
    session.query(AppSession).filter(AppSession.expires_at < now).delete()  # housekeeping
    token = new_session_token()
    session.add(
        AppSession(
            token_hash=token_hash(token),
            user_id=user.id,
            created_at=now,
            expires_at=now + timedelta(days=max(1, days)),
            ip=(ip or "")[:64] or None,
            user_agent=(user_agent or "")[:255] or None,
        )
    )
    user.last_login_at = now
    session.commit()
    return token


def user_for_token(session: Session, token: str | None) -> CurrentUser | None:
    if not token:
        return None
    row = session.scalar(select(AppSession).where(AppSession.token_hash == token_hash(token)))
    if row is None or row.expires_at < datetime.now():
        return None
    user = session.get(AppUser, row.user_id)
    if user is None or not user.active:
        return None
    return CurrentUser(id=user.id, email=user.email, name=user.name or user.email, role=user.role)


def delete_session(session: Session, token: str | None) -> None:
    if not token:
        return
    session.query(AppSession).filter(AppSession.token_hash == token_hash(token)).delete()
    session.commit()


def revoke_sessions(session: Session, user_id: int, keep_token: str | None = None, commit: bool = True) -> None:
    q = session.query(AppSession).filter(AppSession.user_id == user_id)
    if keep_token:
        q = q.filter(AppSession.token_hash != token_hash(keep_token))
    q.delete(synchronize_session=False)
    if commit:
        session.commit()


# --------------------------------------------------------------------------- #
# Audit log
# --------------------------------------------------------------------------- #


def record_audit(
    session: Session,
    user: CurrentUser | None,
    action: str,
    summary: str,
    *,
    target: str | None = None,
    details: dict[str, Any] | None = None,
    ip: str | None = None,
    actor_label: str | None = None,
) -> None:
    """Append one audit entry to the database AND to the `audit` logger (stdout + audit file).

    Never raises: an audit failure must not break the action being audited, but it is logged loudly.
    """
    actor = actor_label or (user.label if user else "anonymous")
    now = datetime.now()
    payload = json.dumps(details, ensure_ascii=False, default=str) if details else None
    try:
        session.add(
            AuditLog(
                ts=now,
                actor_id=user.id if user else None,
                actor=actor[:255],
                action=action[:64],
                target=(target or "")[:255] or None,
                summary=summary,
                details=payload,
                ip=(ip or "")[:64] or None,
            )
        )
        session.commit()
    except Exception:  # pragma: no cover - DB down
        session.rollback()
        log.exception("could not write audit entry to the database")
    audit_logger.info(
        "%s | %s | %s | %s%s%s",
        actor,
        ip or "-",
        action,
        summary,
        f" | target={target}" if target else "",
        f" | {payload}" if payload else "",
        extra={"ctx_actor": actor, "ctx_action": action, "ctx_target": target, "ctx_ip": ip},
    )


def query_audit(
    session: Session,
    *,
    q: str = "",
    actor: str = "",
    action: str = "",
    date_from: datetime | None = None,
    date_to: datetime | None = None,
    limit: int = 100,
    offset: int = 0,
) -> tuple[list[AuditLog], int]:
    stmt = select(AuditLog)
    count_stmt = select(func.count()).select_from(AuditLog)
    conds = []
    if q:
        like = f"%{q.strip()}%"
        conds.append(or_(AuditLog.summary.ilike(like), AuditLog.target.ilike(like), AuditLog.details.ilike(like)))
    if actor:
        conds.append(AuditLog.actor == actor)
    if action:
        conds.append(AuditLog.action.like(f"{action}%"))
    if date_from:
        conds.append(AuditLog.ts >= date_from)
    if date_to:
        conds.append(AuditLog.ts < date_to)
    for c in conds:
        stmt, count_stmt = stmt.where(c), count_stmt.where(c)
    total = int(session.scalar(count_stmt) or 0)
    rows = list(session.scalars(stmt.order_by(AuditLog.ts.desc(), AuditLog.id.desc()).limit(limit).offset(offset)).all())
    return rows, total


def audit_actors(session: Session) -> list[str]:
    return [a for (a,) in session.execute(select(AuditLog.actor).distinct().order_by(AuditLog.actor)).all()]
