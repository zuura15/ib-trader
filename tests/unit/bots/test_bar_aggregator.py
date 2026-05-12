"""Tests for the bar aggregator module."""

from datetime import datetime, timezone

from ib_trader.bots.bar_aggregator import BarAggregator


def _make_bar(ts_offset: int, close: float = 100.0) -> dict:
    """Create a 5-sec bar dict with a given offset in seconds."""
    from datetime import timedelta
    base = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
    return {
        "timestamp_utc": base + timedelta(seconds=ts_offset),
        "open": close - 0.5,
        "high": close + 0.5,
        "low": close - 1.0,
        "close": close,
        "volume": 100,
    }


class TestBarAggregator:
    def test_no_bars_before_target(self):
        agg = BarAggregator(target_seconds=30, lookback_bars=5)
        bars = [_make_bar(i * 5) for i in range(5)]  # 25 sec < 30 sec
        completed = agg.add_bars(bars)
        assert completed == []
        assert agg.has_partial

    def test_completes_bar_at_target(self):
        agg = BarAggregator(target_seconds=30, lookback_bars=5)
        # Six 5s bars fill slot 0 (offsets 0,5,...,25). A 7th bar in
        # slot 30 triggers slot-roll finalize.
        bars = [_make_bar(i * 5, close=100 + i) for i in range(7)]
        completed = agg.add_bars(bars)
        assert len(completed) == 1
        assert completed[0]["open"] == 99.5  # first bar's open
        assert completed[0]["close"] == 105.0  # 6th bar's close (last in slot 0)
        assert completed[0]["volume"] == 600  # 100 * 6

    def test_ohlcv_aggregation(self):
        agg = BarAggregator(target_seconds=15, lookback_bars=10)
        bars = [
            {"timestamp_utc": datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc),
             "open": 100, "high": 105, "low": 99, "close": 102, "volume": 50},
            {"timestamp_utc": datetime(2026, 4, 9, 10, 0, 5, tzinfo=timezone.utc),
             "open": 102, "high": 110, "low": 98, "close": 108, "volume": 75},
            {"timestamp_utc": datetime(2026, 4, 9, 10, 0, 10, tzinfo=timezone.utc),
             "open": 108, "high": 112, "low": 101, "close": 106, "volume": 60},
            # Trigger slot-roll into [10:00:15, 10:00:30).
            {"timestamp_utc": datetime(2026, 4, 9, 10, 0, 15, tzinfo=timezone.utc),
             "open": 106, "high": 107, "low": 105, "close": 106, "volume": 10},
        ]
        completed = agg.add_bars(bars)
        assert len(completed) == 1
        bar = completed[0]
        assert bar["open"] == 100
        assert bar["high"] == 112
        assert bar["low"] == 98
        assert bar["close"] == 106
        assert bar["volume"] == 185

    def test_lookback_window(self):
        agg = BarAggregator(target_seconds=15, lookback_bars=3)
        # 5 full slots (3 ticks each) + 1 trigger tick in the 6th
        # slot so all 5 prior slots are finalized via slot-roll.
        all_bars = []
        for batch in range(5):
            for tick in range(3):
                ts_offset = batch * 15 + tick * 5
                all_bars.append(_make_bar(ts_offset, close=100 + batch))
        # Trigger slot-roll into the 6th slot.
        all_bars.append(_make_bar(5 * 15, close=200))
        agg.add_bars(all_bars)
        assert agg.bar_count == 5

        window = agg.get_bar_window()
        assert window is not None
        assert len(window) == 3  # lookback_bars=3

    def test_window_returns_none_before_enough_bars(self):
        agg = BarAggregator(target_seconds=15, lookback_bars=10)
        bars = [_make_bar(i * 5) for i in range(3)]
        agg.add_bars(bars)
        assert agg.get_bar_window() is None

    def test_dedup_on_restore(self):
        agg = BarAggregator(target_seconds=15, lookback_bars=5)
        bars = [_make_bar(i * 5) for i in range(3)]
        agg.add_bars(bars)

        # Simulate restart: feed same bars again — all should be skipped
        completed = agg.add_bars(bars)
        assert completed == []

    def test_state_roundtrip(self):
        agg = BarAggregator(target_seconds=15, lookback_bars=5)
        # 6 bars: offsets 0/5/10 (slot 0), 15/20/25 (slot 15). Slot 0
        # finalizes on the slot-15 roll.
        bars = [_make_bar(i * 5, close=100 + i) for i in range(6)]
        agg.add_bars(bars)

        state = agg.to_state_dict()
        restored = BarAggregator.from_state_dict(state)

        assert restored.bar_count == agg.bar_count
        assert restored.buffered_bars == agg.buffered_bars
        assert restored.target_seconds == agg.target_seconds

    def test_multiple_completed_bars(self):
        agg = BarAggregator(target_seconds=10, lookback_bars=10)
        # 5 bars: slot 0 (offsets 0, 5), slot 10 (offsets 10, 15),
        # slot 20 (offset 20). Slots 0 and 10 finalize on slot-rolls;
        # slot 20 stays in-progress.
        bars = [_make_bar(i * 5) for i in range(5)]
        completed = agg.add_bars(bars)
        assert len(completed) == 2
        assert agg.bar_count == 2

    def test_bars_aligned_to_standard_boundaries(self):
        """5s bars starting :25 past a 3-min boundary still finalize
        at the aligned slot start (the chart's grid)."""
        from datetime import timedelta
        # Slot start should be 10:00:00 (aligned). Subscription
        # started mid-slot at :25, then continued through the slot
        # roll into 10:03:00 (which finalizes the first partial slot).
        base = datetime(2026, 4, 9, 10, 0, 25, tzinfo=timezone.utc)
        agg = BarAggregator(target_seconds=180, lookback_bars=5)
        bars = []
        # 31 5s bars from :25 up to 10:02:55 (slot 10:00:00, partial).
        for i in range(31):
            bars.append({
                "timestamp_utc": base + timedelta(seconds=i * 5),
                "open": 100, "high": 101, "low": 99, "close": 100.5,
                "volume": 1,
            })
        # First bar in slot 10:03:00 triggers slot-roll → finalize.
        bars.append({
            "timestamp_utc": datetime(2026, 4, 9, 10, 3, 0, tzinfo=timezone.utc),
            "open": 100, "high": 101, "low": 99, "close": 100.5,
            "volume": 1,
        })
        completed = agg.add_bars(bars)
        assert len(completed) == 1
        # Aligned slot start, NOT the first 5s bar's timestamp.
        assert completed[0]["timestamp_utc"] == datetime(
            2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc,
        )

    def test_partial_first_slot_still_emits_at_aligned_boundary(self):
        """A subscription that lands mid-slot produces a partial
        first 3-min bar — but that bar still emits at the aligned
        slot start when the next slot arrives."""
        from datetime import timedelta
        base = datetime(2026, 4, 9, 10, 1, 30, tzinfo=timezone.utc)  # half through slot 10:00:00
        agg = BarAggregator(target_seconds=180, lookback_bars=5)
        bars = []
        # 18 ticks in the rest of slot 10:00:00 (10:01:30 → 10:02:55).
        for i in range(18):
            bars.append({
                "timestamp_utc": base + timedelta(seconds=i * 5),
                "open": 100, "high": 101, "low": 99, "close": 100.5,
                "volume": 1,
            })
        # Trigger into slot 10:03:00.
        bars.append({
            "timestamp_utc": datetime(2026, 4, 9, 10, 3, 0, tzinfo=timezone.utc),
            "open": 100, "high": 101, "low": 99, "close": 100.5,
            "volume": 1,
        })
        completed = agg.add_bars(bars)
        assert len(completed) == 1
        assert completed[0]["timestamp_utc"] == datetime(
            2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc,
        )
        # Volume from only the captured partial.
        assert completed[0]["volume"] == 18

    def test_missing_5s_bar_does_not_shift_boundaries(self):
        """A dropped 5s bar in the middle of a slot must not shift
        subsequent slot boundaries — slots are timestamp-binned, not
        count-based."""
        from datetime import timedelta
        base = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
        agg = BarAggregator(target_seconds=180, lookback_bars=5)
        bars = []
        # 35 of the 36 ticks for slot 10:00:00 (skip one in the middle).
        for i in range(36):
            if i == 17:
                continue  # dropped
            bars.append({
                "timestamp_utc": base + timedelta(seconds=i * 5),
                "open": 100, "high": 101, "low": 99, "close": 100.5,
                "volume": 1,
            })
        # First bar in slot 10:03:00 → finalize prior slot at aligned start.
        bars.append({
            "timestamp_utc": datetime(2026, 4, 9, 10, 3, 0, tzinfo=timezone.utc),
            "open": 100, "high": 101, "low": 99, "close": 100.5,
            "volume": 1,
        })
        completed = agg.add_bars(bars)
        assert len(completed) == 1
        # Despite the drop, slot start is still 10:00:00 (aligned).
        assert completed[0]["timestamp_utc"] == datetime(
            2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc,
        )

    def test_accepts_iso_string_timestamps_and_serializes(self):
        # Runtime ingests bars from the Redis bar:*:5s stream where
        # timestamp_utc is a JSON-decoded ISO-8601 string, not a datetime.
        # Must not crash on to_state_dict (regression: .isoformat() on str
        # raised AttributeError in BOT_DISPATCH_ERROR).
        base = datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
        agg = BarAggregator(target_seconds=10, lookback_bars=5)
        from datetime import timedelta
        bars = [
            {
                "timestamp_utc": (base + timedelta(seconds=i * 5)).isoformat(),
                "open": 100.0, "high": 101.0, "low": 99.0,
                "close": 100.5, "volume": 10,
            }
            for i in range(3)
        ]
        agg.add_bars(bars)
        state = agg.to_state_dict()  # would crash before the fix
        assert state["last_seen_ts"] == (base + timedelta(seconds=10)).isoformat()
        # Round-trip through from_state_dict should also work.
        restored = BarAggregator.from_state_dict(state)
        assert restored._last_seen_ts == base + timedelta(seconds=10)
