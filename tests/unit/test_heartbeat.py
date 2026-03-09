"""Unit tests for heartbeat and alert repositories."""
from datetime import datetime, timezone

from ib_trader.data.models import AlertSeverity, SystemAlert
from ib_trader.data.repository import HeartbeatRepository, AlertRepository


def _now():
    return datetime.now(timezone.utc)


class TestHeartbeatRepository:
    def test_upsert_and_get(self, session_factory):
        repo = HeartbeatRepository(session_factory)
        repo.upsert("REPL", 12345)
        hb = repo.get("REPL")
        assert hb is not None
        assert hb.pid == 12345
        assert hb.process == "REPL"

    def test_upsert_updates_existing(self, session_factory):
        repo = HeartbeatRepository(session_factory)
        repo.upsert("REPL", 1111)
        repo.upsert("REPL", 2222)
        hb = repo.get("REPL")
        assert hb.pid == 2222

    def test_get_missing_returns_none(self, session_factory):
        repo = HeartbeatRepository(session_factory)
        assert repo.get("DAEMON") is None

    def test_delete_removes_record(self, session_factory):
        repo = HeartbeatRepository(session_factory)
        repo.upsert("REPL", 9999)
        repo.delete("REPL")
        assert repo.get("REPL") is None

    def test_delete_nonexistent_no_error(self, session_factory):
        repo = HeartbeatRepository(session_factory)
        repo.delete("NONEXISTENT")  # Should not raise

    def test_daemon_and_repl_independent(self, session_factory):
        repo = HeartbeatRepository(session_factory)
        repo.upsert("REPL", 100)
        repo.upsert("DAEMON", 200)
        assert repo.get("REPL").pid == 100
        assert repo.get("DAEMON").pid == 200


class TestAlertRepository:
    def test_create_and_get_open(self, session_factory):
        repo = AlertRepository(session_factory)
        alert = SystemAlert(
            severity=AlertSeverity.CATASTROPHIC,
            trigger="TEST",
            message="test alert",
            created_at=_now(),
        )
        repo.create(alert)
        open_alerts = repo.get_open()
        assert len(open_alerts) == 1
        assert open_alerts[0].severity == AlertSeverity.CATASTROPHIC

    def test_resolve_marks_resolved(self, session_factory):
        repo = AlertRepository(session_factory)
        alert = repo.create(SystemAlert(
            severity=AlertSeverity.WARNING,
            trigger="TEST_WARN",
            message="warning",
            created_at=_now(),
        ))
        repo.resolve(alert.id)
        open_alerts = repo.get_open()
        assert len(open_alerts) == 0

    def test_resolved_alert_not_in_open(self, session_factory):
        repo = AlertRepository(session_factory)
        alert1 = repo.create(SystemAlert(
            severity=AlertSeverity.WARNING,
            trigger="A",
            message="a",
            created_at=_now(),
        ))
        repo.create(SystemAlert(
            severity=AlertSeverity.CATASTROPHIC,
            trigger="B",
            message="b",
            created_at=_now(),
        ))
        repo.resolve(alert1.id)
        open_alerts = repo.get_open()
        assert len(open_alerts) == 1
        assert open_alerts[0].trigger == "B"

    def test_get_open_ordered_by_created_at(self, session_factory):
        repo = AlertRepository(session_factory)
        t1 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        repo.create(SystemAlert(severity=AlertSeverity.WARNING, trigger="LATER", message="b", created_at=t2))
        repo.create(SystemAlert(severity=AlertSeverity.WARNING, trigger="EARLIER", message="a", created_at=t1))
        open_alerts = repo.get_open()
        assert open_alerts[0].trigger == "EARLIER"
