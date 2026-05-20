"""Unit tests for ``safe_commit`` — the session-poisoning + lock-retry guard.

The bug it fixes (observed 2026-05-19, BOT_RUNNER heartbeat loop): when
``session.commit()`` raises any exception (most commonly
``OperationalError("database is locked")``), the SQLAlchemy session
transitions to a "pending rollback" state. Every subsequent operation
on the same scoped_session then raises ``PendingRollbackError`` until
something calls ``rollback()``. ``safe_commit`` always rolls back on
failure so the session stays usable for the next caller, AND retries
with backoff on transient locks.
"""
from unittest.mock import MagicMock

import pytest
from sqlalchemy.exc import OperationalError

from ib_trader.data.repository import safe_commit


def _lock_error() -> OperationalError:
    """A real-shape OperationalError carrying the 'database is locked' text."""
    return OperationalError("UPDATE …", {}, Exception("database is locked"))


def _other_op_error() -> OperationalError:
    return OperationalError("UPDATE …", {}, Exception("syntax error"))


class TestHappyPath:
    def test_commit_succeeds_on_first_try(self):
        session = MagicMock()
        safe_commit(session)
        session.commit.assert_called_once()
        session.rollback.assert_not_called()


class TestLockRetry:
    def test_retries_lock_with_backoff(self, monkeypatch):
        session = MagicMock()
        # First two commits raise lock; third succeeds.
        session.commit.side_effect = [_lock_error(), _lock_error(), None]
        monkeypatch.setattr("time.sleep", lambda _s: None)

        safe_commit(session, retries=3)

        assert session.commit.call_count == 3
        # rollback called between each failed commit (not after success)
        assert session.rollback.call_count == 2

    def test_reraises_when_retries_exhausted(self, monkeypatch):
        session = MagicMock()
        session.commit.side_effect = _lock_error()
        monkeypatch.setattr("time.sleep", lambda _s: None)

        with pytest.raises(OperationalError):
            safe_commit(session, retries=3)

        assert session.commit.call_count == 3
        # rollback called every attempt — session must be left clean
        assert session.rollback.call_count == 3


class TestSessionPoisoningGuard:
    def test_rolls_back_on_non_lock_operational_error(self):
        """The original bug: any non-lock OperationalError used to leave
        the session in pending-rollback state. safe_commit must rollback."""
        session = MagicMock()
        session.commit.side_effect = _other_op_error()

        with pytest.raises(OperationalError):
            safe_commit(session, retries=3)

        # Single rollback (we don't retry non-lock errors)
        assert session.rollback.call_count == 1
        assert session.commit.call_count == 1

    def test_rolls_back_on_arbitrary_exception(self):
        """Anything that escapes commit() — even ValueError, RuntimeError,
        IntegrityError — must rollback the session."""
        session = MagicMock()
        session.commit.side_effect = ValueError("simulated bug")

        with pytest.raises(ValueError):
            safe_commit(session)

        session.rollback.assert_called_once()

    def test_rollback_failure_does_not_mask_original_error(self):
        """If rollback() ITSELF raises (rare — usually only on a torn-down
        connection), we still re-raise the original commit error."""
        session = MagicMock()
        session.commit.side_effect = ValueError("original")
        session.rollback.side_effect = RuntimeError("rollback also broken")

        with pytest.raises(ValueError, match="original"):
            safe_commit(session)
