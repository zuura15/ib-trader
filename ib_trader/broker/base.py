"""Abstract broker interface with built-in throttle layer.

BrokerClientBase is the broker-agnostic evolution of IBClientBase.
All broker IDs are strings. No int con_id anywhere in the interface.

The engine imports BrokerClientBase and depends on this interface only.
IBClient and AlpacaClient implement it. Tests use MockBrokerClient.

The throttle layer enforces a minimum interval between API calls
and handles pacing violations. All methods pass through the throttle.
"""
import asyncio
import logging
import time
from abc import ABC, abstractmethod
from decimal import Decimal

from ib_trader.broker.types import (
    BrokerCapabilities, Instrument, Snapshot, OrderResult,
)
from ib_trader.broker.fill_stream import FillStream
from ib_trader.broker.market_hours import MarketHoursProvider

logger = logging.getLogger(__name__)


class BrokerClientBase(ABC):
    """Abstract interface for broker API communication.

    Subclasses must implement all abstract methods and properties.
    The throttle layer is provided by this base class.
    """

    def __init__(self, min_call_interval_ms: int = 100) -> None:
        """Initialize the throttle state.

        Args:
            min_call_interval_ms: Minimum milliseconds between API calls.
        """
        self._min_interval = min_call_interval_ms / 1000.0
        self._last_call_time: float = 0.0
        self._throttle_lock = asyncio.Lock()

    async def _throttle(self) -> None:
        """Enforce the minimum interval between API calls.

        Logs a THROTTLED event at DEBUG level if a call is delayed.
        Thread-safe via asyncio lock.
        """
        async with self._throttle_lock:
            now = time.monotonic()
            elapsed = now - self._last_call_time
            if elapsed < self._min_interval:
                delay = self._min_interval - elapsed
                logger.debug(
                    '{"event": "BROKER_THROTTLED", "broker": "%s", "delay_ms": %.1f}',
                    self.broker_name, delay * 1000,
                )
                await asyncio.sleep(delay)
            self._last_call_time = time.monotonic()

    # --- Identity ---

    @property
    @abstractmethod
    def broker_name(self) -> str:
        """Return the broker identifier ("ib" or "alpaca")."""
        ...

    @property
    @abstractmethod
    def capabilities(self) -> BrokerCapabilities:
        """Return the broker's capability set."""
        ...

    @property
    @abstractmethod
    def market_hours(self) -> MarketHoursProvider:
        """Return the broker's market hours provider."""
        ...

    # --- Connection ---

    @abstractmethod
    async def connect(self) -> None:
        """Connect to the broker."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the broker."""
        ...

    # --- Instrument Resolution ---

    @abstractmethod
    async def resolve_instrument(self, symbol: str, **kwargs) -> Instrument:
        """Resolve a symbol to a fully-qualified instrument.

        Args:
            symbol: Ticker symbol.
            **kwargs: Broker-specific options (sec_type, exchange, currency for IB).

        Returns:
            Instrument with asset_id, symbol, exchange, currency, etc.
            IB: asset_id = str(con_id). Alpaca: asset_id = UUID string.
        """
        ...

    @abstractmethod
    def has_instrument_cached(self, asset_id: str) -> bool:
        """Return True if the in-memory instrument cache has this asset."""
        ...

    # --- Market Data ---

    @abstractmethod
    async def get_snapshot(self, asset_id: str) -> Snapshot:
        """Fetch a live bid/ask/last snapshot.

        Args:
            asset_id: Broker-specific asset identifier.

        Returns:
            Snapshot with bid, ask, last as Decimal.
        """
        ...

    # --- Order Placement ---

    @abstractmethod
    async def place_limit_order(
        self,
        asset_id: str,
        symbol: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        extended_hours: bool = True,
        tif: str = "gtc",
    ) -> str:
        """Place a limit order.

        Args:
            asset_id: Broker-specific asset identifier.
            symbol: Ticker symbol (for logging/DB).
            side: "BUY" or "SELL".
            qty: Order quantity.
            price: Limit price.
            extended_hours: If True, order works in extended-hours sessions.
            tif: Time-in-force. Engine passes the value from market_hours.order_params().

        Returns:
            Broker order ID as a string. Write to SQLite immediately on return.
        """
        ...

    @abstractmethod
    async def place_market_order(
        self,
        asset_id: str,
        symbol: str,
        side: str,
        qty: Decimal,
        extended_hours: bool = True,
    ) -> str:
        """Place a market order.

        Args:
            asset_id: Broker-specific asset identifier.
            symbol: Ticker symbol (for logging/DB).
            side: "BUY" or "SELL".
            qty: Order quantity.
            extended_hours: If True, order works in extended-hours sessions.

        Returns:
            Broker order ID as a string.
        """
        ...

    # --- Order Management ---

    @abstractmethod
    async def amend_order(self, broker_order_id: str, new_price: Decimal) -> str:
        """Amend an existing order's price. Returns the (possibly new) order ID.

        For IB: native in-place amendment, returns the SAME order ID.
        For Alpaca: PATCH /v2/orders/{id}, returns a NEW order ID.

        The engine handles both transparently — if the returned ID differs
        from the input, it updates tracking and the DB accordingly.

        Args:
            broker_order_id: Current broker order ID.
            new_price: New limit price.

        Returns:
            Broker order ID (same for IB, new for Alpaca).
        """
        ...

    @abstractmethod
    async def cancel_order(self, broker_order_id: str) -> None:
        """Cancel an open order.

        Args:
            broker_order_id: Broker order ID to cancel.
        """
        ...

    @abstractmethod
    async def get_order_status(self, broker_order_id: str) -> OrderResult:
        """Get current status of an order.

        Args:
            broker_order_id: Broker order ID.

        Returns:
            OrderResult with status, qty_filled, avg_fill_price, etc.
        """
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[dict]:
        """Get all currently open orders from the broker.

        Returns:
            List of dicts with keys: broker_order_id, symbol, side, status,
                qty_filled, avg_fill_price.
        """
        ...

    def get_order_error(self, broker_order_id: str) -> str | None:
        """Return the stored rejection message for an order, or None.

        IB captures errors via errorEvent callback. Alpaca gets errors
        from the REST response. Default returns None.
        """
        return None

    def get_live_order_status(self, broker_order_id: str) -> str | None:
        """Return the live status string from in-memory cache, or None.

        No API call — uses the broker's in-memory state only.
        """
        return None

    # --- Fill Stream ---

    @abstractmethod
    def create_fill_stream(self) -> FillStream:
        """Create a fill stream for this broker.

        IB: callback-based (push). Alpaca: WebSocket (TradingStream).
        """
        ...

    # --- Legacy Compatibility ---
    # These methods exist so that existing IB-specific engine code continues
    # working during the migration. They delegate to the new generic methods.

    def register_fill_callback(self, callback, ib_order_id: str | None = None) -> None:  # noqa: B027 — optional override, default no-op
        """Legacy IB callback registration. Override in IBClient only."""

    def register_status_callback(self, callback, ib_order_id: str | None = None) -> None:  # noqa: B027 — optional override, default no-op
        """Legacy IB callback registration. Override in IBClient only."""

    def unregister_callbacks(self, ib_order_id: str) -> None:  # noqa: B027 — optional override, default no-op
        """Legacy IB callback cleanup. Override in IBClient only."""

    def has_contract_cached(self, con_id: int) -> bool:
        """Legacy IB method. Delegates to has_instrument_cached."""
        return self.has_instrument_cached(str(con_id))

    async def qualify_contract(self, symbol: str, sec_type: str = "STK",
                                exchange: str = "SMART", currency: str = "USD") -> dict:
        """Legacy IB method. Delegates to resolve_instrument and converts format."""
        instrument = await self.resolve_instrument(
            symbol, sec_type=sec_type, exchange=exchange, currency=currency,
        )
        return {
            "con_id": int(instrument.asset_id) if instrument.asset_id.isdigit() else 0,
            "exchange": instrument.exchange,
            "currency": instrument.currency,
            "multiplier": instrument.multiplier,
            "raw": instrument.raw,
        }

    async def get_market_snapshot(self, con_id: int) -> dict:
        """Legacy IB method. Delegates to get_snapshot."""
        snapshot = await self.get_snapshot(str(con_id))
        return {"bid": snapshot.bid, "ask": snapshot.ask, "last": snapshot.last}
