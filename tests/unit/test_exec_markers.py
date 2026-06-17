"""Unit tests for chart execution-marker aggregation."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ib_trader.engine.exec_markers import compute_exec_markers

UTC = timezone.utc
T0 = datetime(2026, 6, 15, 18, 0, 0, tzinfo=UTC)


def _ex(sym, side, qty, price, ts, *, perm_id=0, exec_id="e"):
    return {
        "local_symbol": sym,
        "side": side,
        "shares": Decimal(str(qty)),
        "price": Decimal(str(price)),
        "exec_time": ts,
        "perm_id": perm_id,
        "exec_id": exec_id,
    }


class TestComputeExecMarkers:
    def test_partial_fills_same_order_aggregate(self):
        execs = [
            _ex("NQU6", "BOT", 2, 21000, T0, perm_id=7, exec_id="a"),
            _ex("NQU6", "BOT", 3, 21010, T0 + timedelta(seconds=5), perm_id=7, exec_id="b"),
        ]
        out = compute_exec_markers(execs)
        assert list(out.keys()) == ["NQU6"]
        m = out["NQU6"]
        assert len(m) == 1
        assert m[0]["side"] == "B"
        assert m[0]["qty"] == 5.0
        # qty-weighted avg = (2*21000 + 3*21010)/5 = 21006
        assert m[0]["price"] == 21006.0
        # marker time = latest partial fill
        assert m[0]["time"] == (T0 + timedelta(seconds=5)).isoformat()

    def test_distinct_perm_ids_separate_markers(self):
        execs = [
            _ex("NQU6", "BOT", 1, 21000, T0, perm_id=1),
            _ex("NQU6", "SLD", 1, 21100, T0 + timedelta(minutes=1), perm_id=2),
        ]
        out = compute_exec_markers(execs)
        assert len(out["NQU6"]) == 2
        assert [m["side"] for m in out["NQU6"]] == ["B", "S"]

    def test_perm_id_zero_falls_back_to_exec_id(self):
        # Two manual TWS fills (permId 0) must NOT collapse together.
        execs = [
            _ex("GCQ6", "SLD", 1, 3300, T0, perm_id=0, exec_id="x"),
            _ex("GCQ6", "SLD", 1, 3305, T0 + timedelta(seconds=2), perm_id=0, exec_id="y"),
        ]
        out = compute_exec_markers(execs)
        assert len(out["GCQ6"]) == 2

    def test_side_mapping_and_grouping_by_symbol(self):
        execs = [
            _ex("NQU6", "BOT", 1, 21000, T0, perm_id=1),
            _ex("GCQ6", "SLD", 2, 3300, T0, perm_id=2),
        ]
        out = compute_exec_markers(execs)
        assert out["NQU6"][0]["side"] == "B"
        assert out["GCQ6"][0]["side"] == "S"
        assert out["GCQ6"][0]["qty"] == 2.0

    def test_skips_rows_missing_price_or_shares(self):
        execs = [
            {"local_symbol": "NQU6", "side": "BOT", "exec_time": T0, "perm_id": 1},
            _ex("NQU6", "BOT", 1, 21000, T0 + timedelta(minutes=1), perm_id=2),
        ]
        out = compute_exec_markers(execs)
        assert len(out["NQU6"]) == 1

    def test_markers_sorted_chronologically(self):
        execs = [
            _ex("NQU6", "BOT", 1, 21000, T0 + timedelta(minutes=5), perm_id=1),
            _ex("NQU6", "BOT", 1, 21010, T0, perm_id=2),
        ]
        out = compute_exec_markers(execs)
        times = [m["time"] for m in out["NQU6"]]
        assert times == sorted(times)

    def test_empty(self):
        assert compute_exec_markers([]) == {}
