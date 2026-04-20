"""Tests for the engine service command execution helpers.

Covers: _ListRenderer output capture and crash-recovery of RUNNING
pending_commands audit rows.
"""
from datetime import datetime, timezone


from ib_trader.data.models import PendingCommand, PendingCommandStatus
from ib_trader.engine.service import _ListRenderer, recover_stale_commands


def _now():
    return datetime.now(timezone.utc)


def _make_cmd(source="repl", text="status", status=PendingCommandStatus.PENDING):
    return PendingCommand(
        source=source,
        broker="ib",
        command_text=text,
        status=status,
        submitted_at=_now(),
    )


class TestStaleCommandRecovery:
    """Verify that RUNNING audit rows from a previous crash are marked FAILURE.

    execute_single_command writes these rows; without cleanup after a crash,
    /api/commands/{id} would report them permanently in-flight.
    """

    def test_recovers_running_commands(self, ctx):
        cmd1 = _make_cmd(text="buy AAPL 10 mid", status=PendingCommandStatus.RUNNING)
        cmd2 = _make_cmd(text="sell TSLA 5 market", status=PendingCommandStatus.RUNNING)
        ctx.pending_commands.insert(cmd1)
        ctx.pending_commands.insert(cmd2)

        cmd3 = _make_cmd(text="status", status=PendingCommandStatus.PENDING)
        ctx.pending_commands.insert(cmd3)

        count = recover_stale_commands(ctx)
        assert count == 2

        recovered1 = ctx.pending_commands.get(cmd1.id)
        assert recovered1.status == PendingCommandStatus.FAILURE
        assert "crashed" in recovered1.error.lower()
        assert recovered1.completed_at is not None

        recovered2 = ctx.pending_commands.get(cmd2.id)
        assert recovered2.status == PendingCommandStatus.FAILURE

        still_pending = ctx.pending_commands.get(cmd3.id)
        assert still_pending.status == PendingCommandStatus.PENDING

    def test_no_stale_commands(self, ctx):
        assert recover_stale_commands(ctx) == 0

    def test_success_commands_not_touched(self, ctx):
        cmd = _make_cmd(text="done", status=PendingCommandStatus.SUCCESS)
        ctx.pending_commands.insert(cmd)

        assert recover_stale_commands(ctx) == 0

        fetched = ctx.pending_commands.get(cmd.id)
        assert fetched.status == PendingCommandStatus.SUCCESS


class TestListRenderer:
    """Verify _ListRenderer captures output correctly."""

    def test_captures_log_messages(self):
        r = _ListRenderer()
        r.write_log("first message")
        r.write_log("second message")
        assert len(r.messages) == 2
        assert r.messages[0] == "first message"

    def test_captures_command_output(self):
        r = _ListRenderer()
        r.write_command_output("order placed")
        assert r.messages == ["order placed"]

    def test_update_methods_are_noop(self):
        r = _ListRenderer()
        r.update_order_row(1, {})
        r.update_header(ib_connected=True)
        assert r.messages == []
