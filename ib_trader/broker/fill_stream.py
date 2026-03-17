"""FillStream abstraction over push (IB callbacks) and streaming (Alpaca WebSocket)
fill detection.

Each broker implements its own FillStream subclass. The engine uses the abstract
interface so the same reprice loop and fill-wait logic works for both IB and Alpaca.
"""
from abc import ABC, abstractmethod

from ib_trader.broker.types import FillResult


class FillStream(ABC):
    """Abstract fill stream. Implementations deliver fill events to waiters."""

    def __init__(self):
        self._results: dict[str, FillResult] = {}

    def check_filled(self, broker_order_id: str) -> FillResult | None:
        """Non-blocking check. Returns FillResult if already filled, None otherwise."""
        return self._results.get(broker_order_id)

    @abstractmethod
    async def wait_for_fill(
        self, broker_order_id: str, timeout: float = 30.0
    ) -> FillResult | None:
        """Block until fill or timeout. Returns FillResult or None on timeout."""
        ...

    @abstractmethod
    async def start(self) -> None:
        """Start the fill stream (connect, subscribe, etc.)."""
        ...

    @abstractmethod
    async def stop(self) -> None:
        """Stop the fill stream and clean up resources."""
        ...

    def clear(self, broker_order_id: str) -> None:
        """Remove a tracked fill result (cleanup after terminal state)."""
        self._results.pop(broker_order_id, None)
