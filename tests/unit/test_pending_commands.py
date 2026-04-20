"""Tests for PendingCommand model and PendingCommandRepository.

Covers: CRUD operations, status transitions, crash recovery of stale commands.
"""
from datetime import datetime, timezone

from ib_trader.data.models import PendingCommand, PendingCommandStatus
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository


def _now():
    return datetime.now(timezone.utc)


def _make_cmd(source="repl", broker="ib", text="status", status=PendingCommandStatus.PENDING):
    return PendingCommand(
        source=source,
        broker=broker,
        command_text=text,
        status=status,
        submitted_at=_now(),
    )


class TestPendingCommandRepository:
    """Repository CRUD and query tests."""

    def test_insert_and_get(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        cmd = _make_cmd(text="buy AAPL 10 mid")
        repo.insert(cmd)

        fetched = repo.get(cmd.id)
        assert fetched is not None
        assert fetched.command_text == "buy AAPL 10 mid"
        assert fetched.source == "repl"
        assert fetched.broker == "ib"
        assert fetched.status == PendingCommandStatus.PENDING

    def test_get_pending_returns_ordered(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        cmd1 = _make_cmd(text="first")
        cmd2 = _make_cmd(text="second")
        repo.insert(cmd1)
        repo.insert(cmd2)

        pending = repo.get_pending()
        assert len(pending) == 2
        assert pending[0].command_text == "first"
        assert pending[1].command_text == "second"

    def test_get_pending_excludes_non_pending(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        cmd1 = _make_cmd(text="pending_one")
        cmd2 = _make_cmd(text="running_one", status=PendingCommandStatus.RUNNING)
        cmd3 = _make_cmd(text="done_one", status=PendingCommandStatus.SUCCESS)
        repo.insert(cmd1)
        repo.insert(cmd2)
        repo.insert(cmd3)

        pending = repo.get_pending()
        assert len(pending) == 1
        assert pending[0].command_text == "pending_one"

    def test_update_status_sets_started_at(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        cmd = _make_cmd()
        repo.insert(cmd)

        repo.update_status(cmd.id, PendingCommandStatus.RUNNING)
        fetched = repo.get(cmd.id)
        assert fetched.status == PendingCommandStatus.RUNNING
        assert fetched.started_at is not None

    def test_complete_success(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        cmd = _make_cmd()
        repo.insert(cmd)

        repo.complete(cmd.id, PendingCommandStatus.SUCCESS, output="Order placed")
        fetched = repo.get(cmd.id)
        assert fetched.status == PendingCommandStatus.SUCCESS
        assert fetched.output == "Order placed"
        assert fetched.error is None
        assert fetched.completed_at is not None

    def test_complete_failure(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        cmd = _make_cmd()
        repo.insert(cmd)

        repo.complete(cmd.id, PendingCommandStatus.FAILURE, error="Symbol not found")
        fetched = repo.get(cmd.id)
        assert fetched.status == PendingCommandStatus.FAILURE
        assert fetched.error == "Symbol not found"
        assert fetched.completed_at is not None

    def test_get_by_status(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        repo.insert(_make_cmd(text="a", status=PendingCommandStatus.RUNNING))
        repo.insert(_make_cmd(text="b", status=PendingCommandStatus.RUNNING))
        repo.insert(_make_cmd(text="c", status=PendingCommandStatus.PENDING))

        running = repo.get_by_status(PendingCommandStatus.RUNNING)
        assert len(running) == 2

    def test_get_by_source(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        repo.insert(_make_cmd(source="api", text="cmd1"))
        repo.insert(_make_cmd(source="repl", text="cmd2"))
        repo.insert(_make_cmd(source="api", text="cmd3"))

        api_cmds = repo.get_by_source("api")
        assert len(api_cmds) == 2
        # Newest first
        assert api_cmds[0].command_text == "cmd3"

    def test_get_nonexistent_returns_none(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        assert repo.get("nonexistent-id") is None

    def test_bot_source_format(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        cmd = _make_cmd(source="bot:abc-123", text="buy SPY 100 mid")
        repo.insert(cmd)

        fetched = repo.get(cmd.id)
        assert fetched.source == "bot:abc-123"

    def test_broker_field_default(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        cmd = PendingCommand(
            source="api",
            command_text="status",
            submitted_at=_now(),
        )
        repo.insert(cmd)

        fetched = repo.get(cmd.id)
        assert fetched.broker == "ib"

    def test_broker_field_alpaca(self, session_factory):
        repo = PendingCommandRepository(session_factory)
        cmd = _make_cmd(broker="alpaca", text="buy AAPL 10 mid")
        repo.insert(cmd)

        fetched = repo.get(cmd.id)
        assert fetched.broker == "alpaca"
