"""Unit tests for engine/tracker.py."""

from ib_trader.engine.tracker import OrderTracker, OrderTrack


class TestOrderTracker:
    def test_register_returns_track(self):
        tracker = OrderTracker()
        track = tracker.register("order-uuid", "IB1000", "MSFT")
        assert isinstance(track, OrderTrack)
        assert track.ib_order_id == "IB1000"
        assert track.symbol == "MSFT"
        assert track.is_filled is False
        assert track.is_canceled is False

    def test_get_registered_order(self):
        tracker = OrderTracker()
        tracker.register("order-uuid", "IB1000", "MSFT")
        track = tracker.get("IB1000")
        assert track is not None
        assert track.order_id == "order-uuid"

    def test_get_unknown_returns_none(self):
        tracker = OrderTracker()
        assert tracker.get("NONEXISTENT") is None

    def test_notify_filled_sets_event(self):
        tracker = OrderTracker()
        track = tracker.register("order-uuid", "IB1000", "MSFT")
        assert not track.fill_event.is_set()
        tracker.notify_filled("IB1000")
        assert track.is_filled
        assert track.fill_event.is_set()

    def test_notify_canceled_sets_both_events(self):
        tracker = OrderTracker()
        track = tracker.register("order-uuid", "IB2000", "AAPL")
        tracker.notify_canceled("IB2000")
        assert track.is_canceled
        assert track.cancel_event.is_set()
        assert track.fill_event.is_set()  # Also unblocks fill waiters

    def test_notify_unknown_id_no_error(self):
        tracker = OrderTracker()
        # Should not raise
        tracker.notify_filled("NONEXISTENT")
        tracker.notify_canceled("NONEXISTENT")

    def test_unregister_removes_track(self):
        tracker = OrderTracker()
        tracker.register("order-uuid", "IB3000", "TSLA")
        tracker.unregister("IB3000")
        assert tracker.get("IB3000") is None

    def test_unregister_unknown_id_no_error(self):
        tracker = OrderTracker()
        tracker.unregister("NONEXISTENT")  # Should not raise

    def test_multiple_orders_tracked(self):
        tracker = OrderTracker()
        tracker.register("uuid1", "IB100", "MSFT")
        tracker.register("uuid2", "IB200", "AAPL")
        tracker.notify_filled("IB100")
        assert tracker.get("IB100").is_filled
        assert not tracker.get("IB200").is_filled
