"""Tests for the engine service command loop.

Covers: stale command recovery on startup, _ListRenderer output capture.
"""
import pytest
import asyncio
from datetime import datetime, timezone

from ib_trader.data.models import PendingCommand, PendingCommandStatus
from ib_trader.engine.service import recover_stale_commands, _ListRenderer


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
    """Verify that RUNNING commands from a previous crash are marked FAILURE."""

    @pytest.mark.asyncio
    async def test_recovers_running_commands(self, ctx):
        # Insert commands in RUNNING state (simulating a crash)
        cmd1 = _make_cmd(text="buy AAPL 10 mid", status=PendingCommandStatus.RUNNING)
        cmd2 = _make_cmd(text="sell TSLA 5 market", status=PendingCommandStatus.RUNNING)
        ctx.pending_commands.insert(cmd1)
        ctx.pending_commands.insert(cmd2)

        # Also insert a PENDING command that should NOT be touched
        cmd3 = _make_cmd(text="status", status=PendingCommandStatus.PENDING)
        ctx.pending_commands.insert(cmd3)

        count = await recover_stale_commands(ctx)
        assert count == 2

        # Verify RUNNING commands are now FAILURE
        recovered1 = ctx.pending_commands.get(cmd1.id)
        assert recovered1.status == PendingCommandStatus.FAILURE
        assert "crashed" in recovered1.error.lower()
        assert recovered1.completed_at is not None

        recovered2 = ctx.pending_commands.get(cmd2.id)
        assert recovered2.status == PendingCommandStatus.FAILURE

        # PENDING command untouched
        still_pending = ctx.pending_commands.get(cmd3.id)
        assert still_pending.status == PendingCommandStatus.PENDING

    @pytest.mark.asyncio
    async def test_no_stale_commands(self, ctx):
        count = await recover_stale_commands(ctx)
        assert count == 0

    @pytest.mark.asyncio
    async def test_success_commands_not_touched(self, ctx):
        cmd = _make_cmd(text="done", status=PendingCommandStatus.SUCCESS)
        ctx.pending_commands.insert(cmd)

        count = await recover_stale_commands(ctx)
        assert count == 0

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
