"""Unit tests for daemon/integrity.py."""
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import Base
from ib_trader.daemon.integrity import run_integrity_check


@pytest.fixture
def clean_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return engine


@pytest.fixture
def clean_session_factory(clean_engine):
    factory = sessionmaker(bind=clean_engine)
    return scoped_session(factory)


class TestIntegrityCheck:
    def test_passes_on_clean_db(self, clean_session_factory, ctx):
        """PRAGMA integrity_check returns 'ok' on a freshly created DB."""
        result = run_integrity_check(clean_session_factory, ctx)
        assert result is True

    def test_no_catastrophic_alert_on_pass(self, clean_session_factory, ctx):
        """No alert is created when integrity check passes."""
        run_integrity_check(clean_session_factory, ctx)
        alerts = ctx.alerts.get_open()
        catastrophic = [a for a in alerts if a.trigger == "DB_INTEGRITY_FAILED"]
        assert len(catastrophic) == 0

    def test_handles_session_error_gracefully(self, ctx):
        """If session factory raises, returns False without crashing."""
        from unittest.mock import MagicMock
        bad_factory = MagicMock()
        bad_factory.return_value.execute.side_effect = Exception("DB error")
        result = run_integrity_check(bad_factory, ctx)
        assert result is False
