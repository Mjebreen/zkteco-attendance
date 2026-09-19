"""SQLAlchemy ORM models.

Timestamps are stored as *naive local time* exactly as the device reports them.
The container's TZ must match the device's timezone; there is no UTC conversion.
"""

from __future__ import annotations

from datetime import datetime

from datetime import date as date_type

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Department(Base):
    """Departments exist only in this system (the device knows nothing about them)."""

    __tablename__ = "departments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class User(Base):
    """An employee enrolled on the device. Never deleted; `active` flips when they vanish.

    `name` is what the device reports; `display_name` (optional) overrides it everywhere in this
    system; `department_id` is assigned here, never on the device.
    """

    __tablename__ = "users"

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    first_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    last_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    department_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("departments.id", ondelete="SET NULL"), nullable=True
    )

    department: Mapped[Department | None] = relationship(Department, lazy="joined")

    @property
    def effective_name(self) -> str:
        return (self.display_name or "").strip() or (self.name or "").strip() or f"User {self.user_id}"


class EmployeeWeeklyDay(Base):
    """Recurring weekly schedule entry, e.g. every Friday = off, every Tuesday = online."""

    __tablename__ = "employee_weekly_days"
    __table_args__ = (UniqueConstraint("user_id", "weekday", name="uq_weekly_user_weekday"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    weekday: Mapped[int] = mapped_column(Integer, nullable=False)  # 0 = Monday ... 6 = Sunday
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # off | online


class EmployeeDayOverride(Base):
    """One-date exception that always wins over the weekly pattern (off | online | work)."""

    __tablename__ = "employee_day_overrides"
    __table_args__ = (UniqueConstraint("user_id", "day", name="uq_override_user_day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    day: Mapped[date_type] = mapped_column(Date, nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class EmployeeVacation(Base):
    """A vacation / leave period (inclusive dates). Not an absence; excluded from expected days."""

    __tablename__ = "employee_vacations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    start_day: Mapped[date_type] = mapped_column(Date, nullable=False)
    end_day: Mapped[date_type] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)

    @property
    def days(self) -> int:
        return (self.end_day - self.start_day).days + 1


class PunchCorrection(Base):
    """A manual correction layered on top of the device records (which are never modified).

    kind = "add"  : a punch HR entered by hand at `timestamp`
    kind = "void" : the device punch at `timestamp` is ignored
    """

    __tablename__ = "punch_corrections"
    __table_args__ = (UniqueConstraint("user_id", "kind", "timestamp", name="uq_correction_user_kind_ts"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    note: Mapped[str] = mapped_column(String(255), nullable=False)
    created_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class AppSetting(Base):
    """Key/value settings an admin changes from the UI (branding)."""

    __tablename__ = "app_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppAsset(Base):
    """Small binary assets kept in the database (the logo), so backups and split deploys carry them."""

    __tablename__ = "app_assets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    data: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)


class AppUser(Base):
    """A person who signs in to this system (admin / HR). Unrelated to device employees."""

    __tablename__ = "app_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="hr")  # admin | hr
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AppSession(Base):
    """Server-side login session. Only the SHA-256 of the cookie token is stored."""

    __tablename__ = "app_sessions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(255), nullable=True)


class AuditLog(Base):
    """Append-only record of who did what. Visible to admins only."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.now, index=True)
    actor_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)


class AttendanceRecord(Base):
    """A raw punch. Unique on (user_id, timestamp) so re-pulling the device is idempotent."""

    __tablename__ = "attendance_records"
    __table_args__ = (
        UniqueConstraint("user_id", "timestamp", name="uq_attendance_user_ts"),
        Index("ix_attendance_timestamp", "timestamp"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    punch: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SyncState(Base):
    """Single-row table (id=1) describing the last collector run."""

    __tablename__ = "sync_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    last_sync: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_attempt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    record_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    user_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sync_requested_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
