"""Alpaca broker client implementation.

Uses alpaca-py SDK for REST API communication. No persistent connection.
Rate limit: 200 req/min (333ms between calls).

This implementation is standalone — not connected to the engine yet.
Used for integration testing and validating the broker abstraction.

Alpaca quirks handled:
  - Market orders rejected during extended hours → engine must convert to limit
  - GTC orders don't fill in extended hours → must use tif=day + extended_hours=True
  - PATCH /v2/orders/{id} returns a NEW order ID (not in-place amend)
  - Commission is always zero
  - Fractional shares only during RTH
"""
import json
import logging
from decimal import Decimal

from ib_trader.broker.base import BrokerClientBase
from ib_trader.broker.types import (
    BrokerCapabilities, Instrument, Snapshot, OrderResult,
)
from ib_trader.broker.fill_stream import FillStream
from ib_trader.broker.market_hours import MarketHoursProvider
from ib_trader.broker.alpaca.hours import AlpacaMarketHours

logger = logging.getLogger(__name__)

# Alpaca status → our generic status mapping
_STATUS_MAP = {
    "new": "Submitted",
    "accepted": "Submitted",
    "partially_filled": "PartialFill",
    "filled": "Filled",
    "canceled": "Cancelled",
    "expired": "Cancelled",
    "replaced": "Amending",
    "pending_cancel": "Submitted",
    "pending_replace": "Amending",
    "done_for_day": "Cancelled",
    "stopped": "Submitted",
}


class AlpacaFillStream(FillStream):
    """Alpaca fill stream using TradingStream WebSocket.

    Uses wss://paper-api.alpaca.markets/stream (paper) or
    wss://api.alpaca.markets/stream (live) for real-time trade_updates.

    Zero additional REST API calls. No rate limit concerns.
    Handles 'replaced' status correctly (does NOT fire cancel).
    """

    def __init__(self, api_key: str, secret_key: str, paper: bool = True):
        super().__init__()
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._replace_map: dict[str, str] = {}  # old_order_id → new_order_id
        self._stream = None
        self._task = None

        import asyncio
        self._watches: dict[str, asyncio.Event] = {}

    async def start(self) -> None:
        """Start the WebSocket connection for trade updates.

        Note: Requires alpaca-py SDK with TradingStream support.
        If alpaca-py is not installed, logs a warning and falls back to no-op.
        """
        try:
            from alpaca.trading.stream import TradingStream
            self._stream = TradingStream(
                self._api_key, self._secret_key,
                paper=self._paper,
            )
            self._stream.subscribe_trade_updates(self._on_trade_update)

            import asyncio
            self._task = asyncio.create_task(self._run_stream())
            logger.info(json.dumps({
                "event": "ALPACA_FILL_STREAM_STARTED",
                "paper": self._paper,
            }))
        except ImportError:
            logger.warning(json.dumps({
                "event": "ALPACA_SDK_NOT_INSTALLED",
                "message": "alpaca-py not installed. Fill stream disabled.",
            }))

    async def _run_stream(self):
        """Run the WebSocket stream. Uses alpaca-py public API."""
        try:
            await self._stream.run()
        except Exception:
            logger.exception(json.dumps({"event": "ALPACA_STREAM_ERROR"}))

    async def stop(self) -> None:
        if self._stream:
            try:
                await self._stream.close()
            except Exception as e:
                logger.debug("alpaca stream close failed", exc_info=e)
        if self._task:
            self._task.cancel()

    async def _on_trade_update(self, data) -> None:
        """Handle trade_updates events from Alpaca WebSocket."""
        event_type = data.event
        order = data.order
        order_id = str(order.id)

        if event_type in ("fill", "partial_fill"):
            # Resolve through replace map if this is a replaced order
            watched_id = order_id
            for old_id, new_id in self._replace_map.items():
                if new_id == order_id and old_id in self._watches:
                    watched_id = old_id
                    break

            from ib_trader.broker.types import FillResult
            self._results[watched_id] = FillResult(
                broker_order_id=order_id,
                qty_filled=Decimal(str(order.filled_qty)),
                avg_fill_price=Decimal(str(order.filled_avg_price)),
                commission=Decimal("0"),
            )
            event = self._watches.get(watched_id)
            if event:
                event.set()

        elif event_type == "replaced":
            # The old order has been replaced. The new order ID is tracked
            # via register_replace() called by amend_order() — we don't
            # rely on the event payload for the new ID since it's unreliable.
            # Just ensure we don't fire a cancel event for this order.
            pass  # _replace_map is populated by register_replace()

        elif event_type in ("canceled", "expired", "done_for_day"):
            # Only fire cancel if not a replace-triggered cancel
            if order_id not in self._replace_map:
                event = self._watches.get(order_id)
                if event:
                    event.set()

    def register_replace(self, old_id: str, new_id: str) -> None:
        """Register an old→new order ID mapping for replace tracking.

        Called by AlpacaClient.amend_order() after PATCH returns the new ID.
        This is more reliable than parsing the WebSocket event payload.
        """
        self._replace_map[old_id] = new_id

    async def wait_for_fill(
        self, broker_order_id: str, timeout: float = 30.0,
    ):
        import asyncio
        event = asyncio.Event()
        self._watches[broker_order_id] = event
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
            return self._results.get(broker_order_id)
        except asyncio.TimeoutError:
            return None
        finally:
            self._watches.pop(broker_order_id, None)
            self._replace_map.pop(broker_order_id, None)


class AlpacaClient(BrokerClientBase):
    """Alpaca Markets REST API broker implementation.

    Uses alpaca-py SDK. No persistent connection — all REST.
    Rate limit: 200 req/min (333ms between calls).
    """

    _CAPABILITIES = BrokerCapabilities(
        supports_in_place_amend=False,
        supports_extended_hours=True,
        supports_overnight=False,
        supports_fractional_shares=True,
        supports_stop_orders=True,
        commission_free=True,
        fill_delivery="websocket",
        rate_limit_interval_ms=333,
        max_concurrent_connections=999,
    )

    def __init__(self, api_key: str, secret_key: str, paper: bool = True,
                 rate_limit_ms: int = 333):
        super().__init__(min_call_interval_ms=rate_limit_ms)
        self._api_key = api_key
        self._secret_key = secret_key
        self._paper = paper
        self._trading_client = None
        self._data_client = None
        self._market_hours_provider = AlpacaMarketHours()

    @property
    def broker_name(self) -> str:
        return "alpaca"

    @property
    def capabilities(self) -> BrokerCapabilities:
        return self._CAPABILITIES

    @property
    def market_hours(self) -> MarketHoursProvider:
        return self._market_hours_provider

    async def connect(self) -> None:
        """Initialize Alpaca SDK clients."""
        try:
            from alpaca.trading.client import TradingClient

            self._trading_client = TradingClient(
                self._api_key, self._secret_key,
                paper=self._paper,
            )
            logger.info(json.dumps({
                "event": "ALPACA_CONNECTED",
                "paper": self._paper,
            }))
        except ImportError as e:
            raise RuntimeError(
                "alpaca-py SDK not installed. Install with: pip install alpaca-py"
            ) from e

    async def disconnect(self) -> None:
        self._trading_client = None
        self._data_client = None
        logger.info(json.dumps({"event": "ALPACA_DISCONNECTED"}))

    def _ensure_connected(self):
        if self._trading_client is None:
            raise RuntimeError("AlpacaClient not connected. Call connect() first.")

    async def resolve_instrument(self, symbol: str, **kwargs) -> Instrument:
        """GET /v2/assets/{symbol}"""
        await self._throttle()
        self._ensure_connected()
        asset = self._trading_client.get_asset(symbol)
        return Instrument(
            asset_id=str(asset.id),
            symbol=asset.symbol,
            exchange=str(asset.exchange),
            currency="USD",  # Alpaca only supports USD
            multiplier=None,
            broker="alpaca",
            raw=json.dumps({
                "id": str(asset.id),
                "class": str(asset.asset_class),
                "exchange": str(asset.exchange),
                "symbol": asset.symbol,
                "name": asset.name,
                "tradable": asset.tradable,
                "fractionable": asset.fractionable,
            }),
        )

    def has_instrument_cached(self, asset_id: str) -> bool:
        # Alpaca doesn't need in-memory caching like IB
        return False

    async def get_snapshot(self, asset_id: str, symbol: str | None = None) -> Snapshot:
        """Get latest quote and trade for a symbol.

        Args:
            asset_id: Alpaca asset UUID (used for cache lookup).
            symbol: Ticker symbol for the data API. Required because Alpaca's
                    market data API uses symbols, not asset UUIDs.
        """
        await self._throttle()
        self._ensure_connected()

        if symbol is None:
            # Reverse-lookup symbol from asset_id via the trading client
            asset = self._trading_client.get_asset(asset_id)
            symbol = asset.symbol

        from alpaca.data.requests import (
            StockLatestQuoteRequest, StockLatestTradeRequest,
        )
        from alpaca.data.historical import StockHistoricalDataClient

        if self._data_client is None:
            self._data_client = StockHistoricalDataClient(
                self._api_key, self._secret_key,
            )

        # Get latest quote (bid/ask)
        quotes = self._data_client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=symbol)
        )
        bid = Decimal("0")
        ask = Decimal("0")
        if symbol in quotes:
            q = quotes[symbol]
            bid = Decimal(str(q.bid_price)) if q.bid_price else Decimal("0")
            ask = Decimal(str(q.ask_price)) if q.ask_price else Decimal("0")

        # Get latest trade (last price)
        trades = self._data_client.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=symbol)
        )
        last = Decimal("0")
        if symbol in trades:
            t = trades[symbol]
            last = Decimal(str(t.price)) if t.price else Decimal("0")

        return Snapshot(bid=bid, ask=ask, last=last)

    async def place_limit_order(self, asset_id, symbol, side, qty, price,
                                 extended_hours=True, tif="gtc") -> str:
        """POST /v2/orders"""
        await self._throttle()
        self._ensure_connected()

        from alpaca.trading.requests import LimitOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        alpaca_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL
        alpaca_tif = TimeInForce.GTC if tif.lower() == "gtc" else TimeInForce.DAY

        request = LimitOrderRequest(
            symbol=symbol,
            qty=str(qty),
            side=alpaca_side,
            time_in_force=alpaca_tif,
            limit_price=str(price),
            extended_hours=extended_hours,
        )
        order = self._trading_client.submit_order(request)
        logger.info(json.dumps({
            "event": "ALPACA_ORDER_PLACED",
            "order_id": str(order.id),
            "symbol": symbol,
            "side": side,
            "qty": str(qty),
            "price": str(price),
            "tif": tif,
            "extended_hours": extended_hours,
        }))
        return str(order.id)

    async def place_market_order(self, asset_id, symbol, side, qty,
                                  extended_hours=True) -> str:
        """POST /v2/orders with type=market.

        Note: Alpaca rejects market orders during extended hours.
        The engine should check market_hours.supports_market_orders()
        before calling this.
        """
        await self._throttle()
        self._ensure_connected()

        from alpaca.trading.requests import MarketOrderRequest
        from alpaca.trading.enums import OrderSide, TimeInForce

        alpaca_side = OrderSide.BUY if side.upper() == "BUY" else OrderSide.SELL

        request = MarketOrderRequest(
            symbol=symbol,
            qty=str(qty),
            side=alpaca_side,
            time_in_force=TimeInForce.DAY,
        )
        order = self._trading_client.submit_order(request)
        logger.info(json.dumps({
            "event": "ALPACA_MARKET_ORDER_PLACED",
            "order_id": str(order.id),
            "symbol": symbol,
            "side": side,
            "qty": str(qty),
        }))
        return str(order.id)

    async def amend_order(self, broker_order_id: str, new_price: Decimal) -> str:
        """PATCH /v2/orders/{id}. Returns NEW order ID.

        Alpaca's replace endpoint returns a new order object with a new ID.
        The old order transitions to status='replaced'.
        Registers the old→new mapping with the fill stream for tracking.
        """
        await self._throttle()
        self._ensure_connected()

        from alpaca.trading.requests import ReplaceOrderRequest

        request = ReplaceOrderRequest(limit_price=str(new_price))
        new_order = self._trading_client.replace_order_by_id(
            broker_order_id, request,
        )
        new_id = str(new_order.id)

        # Register the replace mapping with any active fill stream
        # so it can track fills on the new order ID
        if hasattr(self, '_active_fill_stream') and self._active_fill_stream:
            self._active_fill_stream.register_replace(broker_order_id, new_id)

        logger.info(json.dumps({
            "event": "ALPACA_ORDER_REPLACED",
            "old_order_id": broker_order_id,
            "new_order_id": new_id,
            "new_price": str(new_price),
        }))
        return new_id

    async def cancel_order(self, broker_order_id: str) -> None:
        """DELETE /v2/orders/{id}"""
        await self._throttle()
        self._ensure_connected()
        self._trading_client.cancel_order_by_id(broker_order_id)
        logger.info(json.dumps({
            "event": "ALPACA_ORDER_CANCELLED",
            "order_id": broker_order_id,
        }))

    async def get_order_status(self, broker_order_id: str) -> OrderResult:
        """GET /v2/orders/{id}"""
        await self._throttle()
        self._ensure_connected()

        order = self._trading_client.get_order_by_id(broker_order_id)
        alpaca_status = str(order.status).lower() if order.status else "unknown"
        mapped_status = _STATUS_MAP.get(alpaca_status, alpaca_status)

        return OrderResult(
            status=mapped_status,
            qty_filled=Decimal(str(order.filled_qty)) if order.filled_qty else Decimal("0"),
            avg_fill_price=Decimal(str(order.filled_avg_price)) if order.filled_avg_price else None,
            commission=Decimal("0"),  # Alpaca is commission-free
        )

    async def get_open_orders(self) -> list[dict]:
        """GET /v2/orders?status=open"""
        await self._throttle()
        self._ensure_connected()

        from alpaca.trading.requests import GetOrdersRequest
        from alpaca.trading.enums import QueryOrderStatus

        request = GetOrdersRequest(status=QueryOrderStatus.OPEN)
        orders = self._trading_client.get_orders(request)
        return [
            {
                "broker_order_id": str(o.id),
                "symbol": o.symbol,
                "side": str(o.side),
                "qty": Decimal(str(o.qty)) if o.qty else Decimal("0"),
                "status": _STATUS_MAP.get(str(o.status).lower(), str(o.status)),
                "qty_filled": Decimal(str(o.filled_qty)) if o.filled_qty else Decimal("0"),
                "avg_fill_price": Decimal(str(o.filled_avg_price)) if o.filled_avg_price else None,
            }
            for o in orders
        ]

    def create_fill_stream(self) -> FillStream:
        return AlpacaFillStream(
            self._api_key, self._secret_key, self._paper,
        )
