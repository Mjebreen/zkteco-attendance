"""Idempotent ingest: re-pulling the device never duplicates; vanished users go inactive."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select

from app.models import AttendanceRecord, User
from app.service import get_sync_state, ingest, record_sync_failure


class FakeZkUser:
    def __init__(self, user_id, name):
        self.user_id, self.name = user_id, name


class FakeZkAttendance:
    def __init__(self, user_id, timestamp, status=1, punch=0):
        self.user_id, self.timestamp, self.status, self.punch = user_id, timestamp, status, punch


def test_ingest_is_idempotent_and_dedupes_within_batch(session_factory):
    users = [{"id": "1", "name": "Alice"}, {"id": 2, "name": "Bob"}]
    records = [
        {"user_id": "1", "timestamp": "2026-05-23T08:00:00"},
        {"user_id": "1", "timestamp": "2026-05-23T08:00:00"},  # duplicate in the same batch
        {"user_id": 2, "timestamp": datetime(2026, 5, 23, 8, 5, 0)},
    ]
    with session_factory() as s:
        first = ingest(s, users, records, source="test")
    assert first["records_received"] == 2 and first["records_inserted"] == 2 and first["records_skipped"] == 0
    assert first["users_created"] == 2

    with session_factory() as s:
        second = ingest(s, users, records, source="test")  # "collector restart"
    assert second["records_inserted"] == 0 and second["records_skipped"] == 2
    assert second["users_created"] == 0 and second["record_count"] == 2

    with session_factory() as s:
        assert s.scalar(select(AttendanceRecord.id).where(AttendanceRecord.user_id == "1")) is not None
        assert len(s.scalars(select(AttendanceRecord)).all()) == 2


def test_ingest_accepts_pyzk_objects_and_strips_microseconds_and_tz(session_factory):
    users = [FakeZkUser(7, "  Zed  "), FakeZkUser(8, None)]
    records = [
        FakeZkAttendance(7, datetime(2026, 5, 23, 8, 0, 0, 123456)),
        {"user_id": "8", "timestamp": "2026-05-23T09:00:00+03:00"},  # offset stripped, kept as naive local
    ]
    with session_factory() as s:
        out = ingest(s, users, records, source="test")
        assert out["records_inserted"] == 2
        names = dict(s.execute(select(User.user_id, User.name)).all())
        assert names == {"7": "Zed", "8": "User 8"}
        stamps = {r.user_id: r.timestamp for r in s.scalars(select(AttendanceRecord))}
        assert stamps["7"] == datetime(2026, 5, 23, 8, 0, 0)
        assert stamps["8"] == datetime(2026, 5, 23, 9, 0, 0)


def test_vanished_user_marked_inactive_never_deleted_and_reactivated(session_factory):
    with session_factory() as s:
        ingest(s, [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bob"}],
               [{"user_id": "2", "timestamp": "2026-05-23T08:00:00"}], source="test")
    with session_factory() as s:
        out = ingest(s, [{"id": "1", "name": "Alice"}], [], source="test")  # Bob removed from device
        assert out["users_deactivated"] == 1
        bob = s.get(User, "2")
        assert bob is not None and bob.active is False
        assert len(s.scalars(select(AttendanceRecord).where(AttendanceRecord.user_id == "2")).all()) == 1
    with session_factory() as s:
        out = ingest(s, [{"id": "1", "name": "Alice"}, {"id": "2", "name": "Bobby"}], [], source="test")
        assert out["users_reactivated"] == 1
        bob = s.get(User, "2")
        assert bob.active is True and bob.name == "Bobby"


def test_empty_user_list_does_not_deactivate_everyone(session_factory):
    with session_factory() as s:
        ingest(s, [{"id": "1", "name": "Alice"}], [], source="test")
    with session_factory() as s:
        out = ingest(s, [], [{"user_id": "1", "timestamp": "2026-05-23T08:00:00"}], source="test")
        assert out["users_deactivated"] == 0
        assert s.get(User, "1").active is True


def test_sync_failure_is_recorded_and_previous_data_survives(session_factory):
    with session_factory() as s:
        ingest(s, [{"id": "1", "name": "Alice"}], [{"user_id": "1", "timestamp": "2026-05-23T08:00:00"}], source="test")
        good_sync = get_sync_state(s).last_sync
    with session_factory() as s:
        record_sync_failure(s, "timed out connecting to 192.0.2.10:4370", source="collector")
    with session_factory() as s:
        state = get_sync_state(s)
        assert state.last_sync == good_sync  # unchanged -> becomes "stale" in /health over time
        assert "timed out" in state.last_error
        assert state.record_count == 1
