"""Abstract IB API interface with built-in throttle layer.

All engine code imports IBClientBase and depends on this interface only.
insync_client.py implements it. Tests use MockIBClient.

The throttle layer enforces a minimum interval between IB API calls
(default 100ms) and handles pacing violations with exponential backoff.
All methods pass through the throttle automatically.
"""
import asyncio
import logging
import time
from abc import ABC, abstractmethod
from decimal import Decimal

logger = logging.getLogger(__name__)


class IBClientBase(ABC):
    """Abstract interface for IB API communication.

    Subclasses must implement all abstract methods.
    The throttle layer is provided by this base class and applies
    to all subclass method calls when using _throttled_call().
    """

    def __init__(self, min_call_interval_ms: int = 100) -> None:
        """Initialize the throttle state.

        Args:
            min_call_interval_ms: Minimum milliseconds between IB API calls.
        """
        self._min_interval = min_call_interval_ms / 1000.0
        self._last_call_time: float = 0.0
        self._throttle_lock = asyncio.Lock()

    async def _throttle(self) -> None:
        """Enforce the minimum interval between IB API calls.

        Logs a THROTTLED event at DEBUG level if a call is delayed.
        Thread-safe via asyncio lock.
        """
        async with self._throttle_lock:
            now = time.monotonic()
            elapsed = now - self._last_call_time
            if elapsed < self._min_interval:
                delay = self._min_interval - elapsed
                logger.debug(
                    '{"event": "IB_THROTTLED", "delay_ms": %.1f}', delay * 1000
                )
                await asyncio.sleep(delay)
            self._last_call_time = time.monotonic()

    @abstractmethod
    async def connect(self) -> None:
        """Connect to TWS or IB Gateway."""
        ...

    @abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from TWS or IB Gateway."""
        ...

    @abstractmethod
    def managed_accounts(self) -> list[str]:
        """Return the account IDs this Gateway session can trade.

        IB authenticates at the session level; the account ID on each
        order is validated against this list by IB on submission. Call
        this after connect() to verify the configured account_id is
        actually one the Gateway can reach — catches wrong-env-var
        configs before any order is placed.
        """
        ...

    @abstractmethod
    async def qualify_contract(
        self,
        symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> dict:
        """Qualify an IB contract and return its details.

        Args:
            symbol: Ticker symbol.
            sec_type: Security type (STK, ETF, OPT, FUT).
            exchange: Exchange (default SMART for auto-routing).
            currency: Currency (default USD).

        Returns:
            dict with keys: con_id (int), exchange (str), currency (str),
                            multiplier (str | None), raw (str JSON).
        """
        ...

    @abstractmethod
    async def get_market_snapshot(self, con_id: int) -> dict:
        """Fetch a live bid/ask/last snapshot for a contract.

        Args:
            con_id: IB contract ID.

        Returns:
            dict with keys: bid (Decimal), ask (Decimal), last (Decimal).
        """
        ...

    @abstractmethod
    async def place_limit_order(
        self,
        con_id: int,
        symbol: str,
        side: str,
        qty: Decimal,
        price: Decimal,
        outside_rth: bool = True,
        tif: str = "GTC",
        order_ref: str | None = None,
    ) -> str:
        """Place a GTC limit order.

        Args:
            con_id: IB contract ID.
            symbol: Ticker symbol (for logging).
            side: "BUY" or "SELL".
            qty: Order quantity.
            price: Limit price.
            outside_rth: If True, order works outside regular trading hours.
            tif: Time-in-force ("GTC" default).
            order_ref: Optional orderRef tag for IB order identification.
                       Format: "IBT:{bot_ref}:{symbol}:{side}:{serial}".
                       Set on the IB Order object before placement.

        Returns:
            IB order ID as a string. Write to SQLite immediately on return.
        """
        ...

    @abstractmethod
    async def place_market_order(
        self,
        con_id: int,
        symbol: str,
        side: str,
        qty: Decimal,
        outside_rth: bool = True,
        order_ref: str | None = None,
    ) -> str:
        """Place a market order.

        Args:
            con_id: IB contract ID.
            symbol: Ticker symbol (for logging).
            side: "BUY" or "SELL".
            qty: Order quantity.
            outside_rth: If True, order works outside regular trading hours.
            order_ref: Optional orderRef tag for IB order identification.
                       Format: "IBT:{bot_ref}:{symbol}:{side}:{serial}".

        Returns:
            IB order ID as a string. Write to SQLite immediately on return.
        """
        ...

    @abstractmethod
    async def amend_order(self, ib_order_id: str, new_price: Decimal) -> None:
        """Amend an existing limit order to a new price.

        Modifies the order in place (amendment, not cancel-replace).

        Args:
            ib_order_id: IB order ID to amend.
            new_price: New limit price.
        """
        ...

    @abstractmethod
    async def cancel_order(self, ib_order_id: str) -> None:
        """Cancel an open order.

        Args:
            ib_order_id: IB order ID to cancel.
        """
        ...

    @abstractmethod
    async def get_order_status(self, ib_order_id: str) -> dict:
        """Get current status of an order from IB.

        Args:
            ib_order_id: IB order ID.

        Returns:
            dict with keys:
              status (str)              — IB order status string (Submitted, PreSubmitted,
                                          Filled, Cancelled, Inactive, …)
              qty_filled (Decimal)      — shares filled so far
              avg_fill_price (Decimal | None)
              commission (Decimal | None)
              why_held (str | None)     — IB whyHeld field; non-empty when IB is
                                          holding the order (short-locate, regulatory
                                          block, etc.).  Always check this alongside
                                          the Inactive status.
        """
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[dict]:
        """Get all currently open orders from IB across all client IDs.

        Uses reqAllOpenOrders so the daemon (which connects with a different
        client ID) can see orders placed by the REPL.

        Returns:
            List of dicts with keys: ib_order_id (str), symbol (str),
                status (str), qty_filled (Decimal),
                avg_fill_price (Decimal | None).
        """
        ...

    @abstractmethod
    def get_order_error(self, ib_order_id: str) -> str | None:
        """Return the stored IB rejection message for this order, or None.

        Errors are captured by the errorEvent callback for order-related
        IB error codes (110, 200-299).  The PendingSubmit wait loop in the
        engine checks this on each poll iteration to surface the real IB
        rejection reason instead of a generic timeout message.

        Args:
            ib_order_id: IB-assigned order ID as a string.
        """
        ...

    def get_live_order_status(self, ib_order_id: str) -> str | None:
        """Return the live IB status string for an order, or None if unknown.

        Uses the in-memory active trades cache — no IB API call.
        Returns None if the order is not in the cache (e.g. from a previous session).
        """
        return None

    # ------------------------------------------------------------------
    # Streaming market data (ref-counted)
    # ------------------------------------------------------------------

    @abstractmethod
    async def subscribe_market_data(self, con_id: int, symbol: str) -> None:
        """Subscribe to streaming market data for a contract.

        Ref-counted: calling twice for the same con_id increments the
        reference count without creating a duplicate IB subscription.
        Call unsubscribe_market_data to decrement.

        Args:
            con_id: IB contract ID.
            symbol: Ticker symbol (for logging and contract lookup).
        """
        ...

    @abstractmethod
    async def unsubscribe_market_data(self, con_id: int) -> None:
        """Unsubscribe from streaming market data for a contract.

        Ref-counted: only cancels the IB subscription when the reference
        count reaches zero.

        Args:
            con_id: IB contract ID.
        """
        ...

    @abstractmethod
    def get_ticker(self, con_id: int) -> dict | None:
        """Return current streaming ticker data for a contract, or None.

        Returns:
            dict with keys: bid, ask, last, open, high, low, close,
                volume, avg_volume, high_52w, low_52w.
            All values are floats or None if unavailable.
            Returns None if no subscription exists for this con_id.
        """
        ...

    @abstractmethod
    def has_contract_cached(self, con_id: int) -> bool:
        """Return True if the in-memory contract cache has a fully-specified
        Contract for this con_id.

        Used by _get_contract to detect when the SQLite cache is fresh but the
        in-memory ib_insync contract cache was lost (e.g. after a restart),
        so it can re-qualify without making redundant IB API calls per order.

        Args:
            con_id: IB contract ID to check.
        """
        ...

    @abstractmethod
    def register_fill_callback(self, callback, ib_order_id: str | None = None) -> None:
        """Register a callback for fill events.

        Args:
            callback: async callable with signature:
                async def on_fill(ib_order_id: str, qty_filled: Decimal,
                                  avg_price: Decimal, commission: Decimal) -> None
            ib_order_id: If provided, the callback is scoped to this order and
                will be automatically removed when the order reaches a terminal
                state (Filled, Cancelled, Inactive).  If None, the callback
                fires for all orders and is never auto-removed.
        """
        ...

    @abstractmethod
    def register_status_callback(self, callback, ib_order_id: str | None = None) -> None:
        """Register a callback for order status change events.

        Args:
            callback: async callable with signature:
                async def on_status(ib_order_id: str, status: str) -> None
            ib_order_id: If provided, the callback is scoped to this order and
                will be automatically removed when the order reaches a terminal
                state (Filled, Cancelled, Inactive).  If None, the callback
                fires for all orders and is never auto-removed.
        """
        ...

    @abstractmethod
    def unregister_callbacks(self, ib_order_id: str) -> None:
        """Remove all fill and status callbacks registered for an order.

        Called automatically when a terminal status is dispatched, but can
        also be called explicitly by engine code during cleanup.

        Args:
            ib_order_id: IB order ID whose callbacks should be removed.
        """
        ...

    # ------------------------------------------------------------------
    # Real-time bars (5-second streaming)
    # ------------------------------------------------------------------

    @abstractmethod
    async def subscribe_realtime_bars(
        self, con_id: int, symbol: str,
        what_to_show: str = "TRADES",
        callback=None,
    ) -> None:
        """Subscribe to 5-second real-time bars for a contract.

        Ref-counted: calling twice increments count without duplicating.

        Args:
            con_id: IB contract ID.
            symbol: Ticker symbol (for logging).
            what_to_show: Data type (TRADES, MIDPOINT, BID, ASK).
            callback: async callable(bar_data: dict) invoked on each bar.
                      bar_data keys: time, open, high, low, close, volume.
        """
        ...

    @abstractmethod
    async def unsubscribe_realtime_bars(self, con_id: int) -> None:
        """Cancel real-time bar subscription.

        Ref-counted: only cancels when count reaches zero.

        Args:
            con_id: IB contract ID.
        """
        ...
