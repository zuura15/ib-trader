"""Unit tests for the chart-pane realized-P&L rollup math."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ib_trader.engine.pnl_rollup import compute_pnl_rollup, session_start

# Fixed tz-aware "now": 2026-06-11 18:00 at UTC-7 (Pacific). The futures
# session boundary at hour 15 (3 PM) is therefore 3 h ago today.
PT = timezone(timedelta(hours=-7))
NOW = datetime(2026, 6, 11, 18, 0, 0, tzinfo=PT)
SESSION_HOUR = 15


def _ex(local_symbol, sec_type, pnl, ts, exec_id="e"):
    return {
        "local_symbol": local_symbol,
        "sec_type": sec_type,
        "realized_pnl": None if pnl is None else Decimal(str(pnl)),
        "exec_time": ts,
        "exec_id": exec_id,
        "side": "SLD",
    }


class TestSessionStart:
    def test_after_boundary_anchors_today(self):
        # now 18:00 PT, boundary 15:00 → today 15:00.
        ss = session_start(NOW, SESSION_HOUR)
        assert ss == datetime(2026, 6, 11, 15, 0, 0, tzinfo=PT)

    def test_before_boundary_anchors_yesterday(self):
        # now 09:00 PT, boundary 15:00 → yesterday 15:00.
        before = datetime(2026, 6, 11, 9, 0, 0, tzinfo=PT)
        ss = session_start(before, SESSION_HOUR)
        assert ss == datetime(2026, 6, 10, 15, 0, 0, tzinfo=PT)


class TestRollup:
    def test_24h_sums_per_contract(self):
        execs = [
            _ex("NQM6", "FUT", 100, NOW - timedelta(hours=1)),
            _ex("NQM6", "FUT", -30, NOW - timedelta(hours=2)),
            _ex("GCQ6", "FUT", 50, NOW - timedelta(hours=5)),
        ]
        r = compute_pnl_rollup(execs, NOW, SESSION_HOUR)
        assert r["NQM6"]["pnl_24h"] == 70.0
        assert r["GCQ6"]["pnl_24h"] == 50.0

    def test_session_is_futures_only_and_from_boundary(self):
        execs = [
            # 1h ago (after 15:00 boundary) → counts in session
            _ex("NQM6", "FUT", 100, NOW - timedelta(hours=1)),
            # 6h ago (before 15:00 boundary, still <24h) → 24h only
            _ex("NQM6", "FUT", 40, NOW - timedelta(hours=6)),
            # a stock close, 1h ago → 24h yes, session must stay None
            _ex("AAPL", "STK", 25, NOW - timedelta(hours=1)),
        ]
        r = compute_pnl_rollup(execs, NOW, SESSION_HOUR)
        assert r["NQM6"]["pnl_24h"] == 140.0
        assert r["NQM6"]["pnl_session"] == 100.0   # only the post-boundary fill
        assert r["AAPL"]["pnl_24h"] == 25.0
        assert r["AAPL"]["pnl_session"] is None     # stocks: no session figure

    def test_older_than_24h_excluded(self):
        execs = [
            _ex("NQM6", "FUT", 100, NOW - timedelta(hours=1)),
            _ex("NQM6", "FUT", 999, NOW - timedelta(hours=30)),  # outside 24h
        ]
        r = compute_pnl_rollup(execs, NOW, SESSION_HOUR)
        assert r["NQM6"]["pnl_24h"] == 100.0

    def test_opening_fills_skipped(self):
        # realized_pnl None = opening fill / sentinel-filtered upstream.
        execs = [
            _ex("NQM6", "FUT", None, NOW - timedelta(hours=1)),
            _ex("NQM6", "FUT", 60, NOW - timedelta(minutes=30)),
        ]
        r = compute_pnl_rollup(execs, NOW, SESSION_HOUR)
        assert r["NQM6"]["pnl_24h"] == 60.0
        assert r["NQM6"]["pnl_session"] == 60.0

    def test_empty_and_all_stale_drop_out(self):
        assert compute_pnl_rollup([], NOW, SESSION_HOUR) == {}
        stale = [_ex("NQM6", "FUT", 100, NOW - timedelta(hours=40))]
        # outside 24h and (being >24h) outside session → contract dropped
        assert compute_pnl_rollup(stale, NOW, SESSION_HOUR) == {}

    def test_manual_tws_fill_included_like_any_other(self):
        # The whole point: a fill with empty/foreign orderRef (manual TWS)
        # is just another execution row here — no special handling needed.
        execs = [_ex("MNQM6", "FUT", 212.50, NOW - timedelta(minutes=10))]
        r = compute_pnl_rollup(execs, NOW, SESSION_HOUR)
        assert r["MNQM6"]["pnl_24h"] == 212.5
        assert r["MNQM6"]["pnl_session"] == 212.5
