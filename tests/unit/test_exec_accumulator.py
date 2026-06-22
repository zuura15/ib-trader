"""Unit tests for the durable execution accumulator."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ib_trader.engine.exec_accumulator import (
    merge_executions, records_to_execs,
)
from ib_trader.engine.pnl_rollup import compute_pnl_rollup

UTC = timezone.utc
NOW = datetime(2026, 6, 22, 18, 30, 0, tzinfo=UTC)


def _ex(exec_id, sym, side, qty, price, ts, *, realized=None, comm="2.25",
        sec="FUT", perm=0):
    return {
        "local_symbol": sym, "symbol": sym[:2], "sec_type": sec,
        "side": side, "exec_id": exec_id, "perm_id": perm,
        "exec_time": ts,
        "realized_pnl": None if realized is None else Decimal(str(realized)),
        "commission": Decimal(str(comm)),
        "price": Decimal(str(price)), "shares": Decimal(str(qty)),
    }


class TestMerge:
    def test_insert_and_dedupe_by_exec_id(self):
        fresh = [_ex("e1", "NQU6", "SLD", 1, 30765, NOW, realized=100)]
        s = merge_executions({}, fresh, NOW)
        assert list(s.keys()) == ["e1"]
        # Same exec_id again on the next sweep doesn't duplicate.
        s2 = merge_executions(s, fresh, NOW)
        assert list(s2.keys()) == ["e1"]

    def test_survives_when_ib_drops_it(self):
        # Seen in sweep 1; sweep 2 no longer returns it (Gateway restart).
        old = NOW - timedelta(hours=3)
        s = merge_executions({}, [_ex("e1", "NQU6", "SLD", 1, 30765, old, realized=100)], NOW)
        s2 = merge_executions(s, [], NOW)  # IB returns nothing now
        assert "e1" in s2  # still in the window

    def test_realized_refreshes_when_pairing_completes(self):
        old = NOW - timedelta(minutes=5)
        # First sweep: commission report unpaired → realized None.
        s = merge_executions({}, [_ex("e1", "NQU6", "SLD", 1, 30765, old, realized=None)], NOW)
        assert s["e1"]["realized_pnl"] is None
        # Later sweep: same fill now paired with realized 100.
        s = merge_executions(s, [_ex("e1", "NQU6", "SLD", 1, 30765, old, realized=100)], NOW)
        assert s["e1"]["realized_pnl"] == "100"

    def test_known_realized_not_overwritten(self):
        old = NOW - timedelta(minutes=5)
        s = merge_executions({}, [_ex("e1", "NQU6", "SLD", 1, 30765, old, realized=100)], NOW)
        # A later snapshot somehow carrying None must not wipe the real value.
        s = merge_executions(s, [_ex("e1", "NQU6", "SLD", 1, 30765, old, realized=None)], NOW)
        assert s["e1"]["realized_pnl"] == "100"

    def test_prunes_older_than_window(self):
        stale = NOW - timedelta(hours=25)
        recent = NOW - timedelta(hours=1)
        s = merge_executions({}, [
            _ex("old", "NQU6", "SLD", 1, 30000, stale, realized=50),
            _ex("new", "NQU6", "SLD", 1, 30765, recent, realized=100),
        ], NOW)
        assert set(s.keys()) == {"new"}

    def test_skips_rows_without_exec_id(self):
        s = merge_executions({}, [_ex("", "NQU6", "SLD", 1, 30765, NOW, realized=100)], NOW)
        assert s == {}


class TestRoundTrip:
    def test_records_to_execs_types_and_rollup(self):
        old = NOW - timedelta(hours=2)
        s = merge_executions({}, [
            _ex("e1", "NQU6", "SLD", 1, 30765, old, realized=100),
            _ex("e2", "NQU6", "BOT", 1, 30700, old, realized=None),  # opening
            _ex("e3", "ESU6", "SLD", 1, 7540, old, realized=50, sec="FUT"),
        ], NOW)
        execs = records_to_execs(s)
        by = {e["exec_id"]: e for e in execs}
        assert isinstance(by["e1"]["exec_time"], datetime)
        assert by["e1"]["realized_pnl"] == Decimal("100")
        assert by["e2"]["realized_pnl"] is None
        assert isinstance(by["e1"]["shares"], Decimal)
        # Feeds compute_pnl_rollup unchanged.
        rollup = compute_pnl_rollup(execs, NOW, 15)
        assert rollup["NQU6"]["pnl_24h"] == 100.0
        assert rollup["ESU6"]["pnl_24h"] == 50.0

    def test_accumulation_beats_session_truncation(self):
        # Simulate: sweep A sees the early fills, sweep B (post-restart)
        # sees only later ones. The union must retain both.
        t_early = NOW - timedelta(hours=8)
        t_late = NOW - timedelta(hours=1)
        s = merge_executions({}, [_ex("early", "NQU6", "SLD", 1, 30000, t_early, realized=1034)], NOW)
        s = merge_executions(s, [_ex("late", "NQU6", "SLD", 1, 30765, t_late, realized=220)], NOW)
        rollup = compute_pnl_rollup(records_to_execs(s), NOW, 15)
        assert rollup["NQU6"]["pnl_24h"] == 1254.0  # 1034 + 220, not just 220
