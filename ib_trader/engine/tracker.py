"""In-flight order state tracker.

Maps ib_order_id → asyncio.Event for fill notification.
Allows reprice_loop and fill callbacks to coordinate without shared memory.

This is coordination state only — not trade state.
Trade state always lives in SQLite. This tracker is rebuilt from SQLite on startup.
"""
import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class OrderTrack:
    """Tracking state for a single in-flight order."""

    order_id: str           # Internal UUID
    ib_order_id: str        # IB-assigned order ID
    symbol: str
    fill_event: asyncio.Event = field(default_factory=asyncio.Event)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    is_filled: bool = False
    is_canceled: bool = False


class OrderTracker:
    """Thread-safe registry mapping ib_order_id to OrderTrack.

    Used by the reprice loop and fill callbacks to coordinate order completion.
    State here is ephemeral — rebuild from SQLite on restart.
    """

    def __init__(self) -> None:
        """Initialize empty tracker."""
        self._tracks: dict[str, OrderTrack] = {}

    def register(self, order_id: str, ib_order_id: str, symbol: str) -> OrderTrack:
        """Register a new order for tracking.

        Args:
            order_id: Internal UUID of the order.
            ib_order_id: IB-assigned order ID.
            symbol: Ticker symbol.

        Returns:
            The OrderTrack for this order.
        """
        track = OrderTrack(order_id=order_id, ib_order_id=ib_order_id, symbol=symbol)
        self._tracks[ib_order_id] = track
        logger.debug('{"event": "TRACKER_REGISTERED", "ib_order_id": "%s"}', ib_order_id)
        return track

    def get(self, ib_order_id: str) -> OrderTrack | None:
        """Return the OrderTrack for an IB order ID, or None."""
        return self._tracks.get(ib_order_id)

    def notify_filled(self, ib_order_id: str) -> None:
        """Signal the fill event for an order, unblocking the reprice loop."""
        track = self._tracks.get(ib_order_id)
        if track:
            track.is_filled = True
            track.fill_event.set()
            logger.debug('{"event": "TRACKER_FILL_NOTIFIED", "ib_order_id": "%s"}', ib_order_id)

    def notify_canceled(self, ib_order_id: str) -> None:
        """Signal the cancel event for an order."""
        track = self._tracks.get(ib_order_id)
        if track:
            track.is_canceled = True
            track.cancel_event.set()
            track.fill_event.set()  # Also unblock fill waiters
            logger.debug(
                '{"event": "TRACKER_CANCEL_NOTIFIED", "ib_order_id": "%s"}', ib_order_id
            )

    def unregister(self, ib_order_id: str) -> None:
        """Remove an order from the tracker after it reaches a terminal state."""
        self._tracks.pop(ib_order_id, None)
