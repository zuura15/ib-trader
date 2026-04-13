"""ib_async concrete implementation of IBClientBase.

This is the ONLY file in the project that imports ib_async.
All IB API interaction goes through this class.
outsideRth = True is enforced here — engine code never sets IB order fields directly.
"""
import asyncio
import json
import logging
import time
from decimal import Decimal
from ib_async import IB, Contract, LimitOrder, MarketOrder, Trade, Fill

from ib_trader.engine.market_hours import is_overnight_session
from ib_trader.ib.base import IBClientBase
from ib_trader.ib.overnight_patch import apply as _apply_overnight_patch

# Patch ib_async to support includeOvernight (server version 189).
# Must run before any IB connection is established.
_apply_overnight_patch()

logger = logging.getLogger(__name__)


class InsyncClient(IBClientBase):
    """IB API client implemented via ib_async.

    Connects to TWS or IB Gateway over TCP.
    Manages a single IB() connection and registers event callbacks.
    All orders enforce outsideRth=True for extended-hours trading.
    """

    def __init__(
        self,
        host: str,
        port: int,
        client_id: int,
        account_id: str,
        min_call_interval_ms: int = 100,
        connect_timeout: int = 10,
        market_data_type: int = 3,
    ) -> None:
        """Initialize with connection parameters.

        Args:
            host: TWS/Gateway hostname or IP.
            port: TWS/Gateway port (7497 live, 7496 paper, 4001 GW live, 4002 GW paper).
            client_id: Unique client ID for this connection.
            account_id: IB account ID (e.g. U1234567 or DU1234567 for paper).
                Set on every order to satisfy IB error 435 "You must specify an
                account" when the client is connected to multiple accounts.
            min_call_interval_ms: Minimum ms between IB API calls.
            connect_timeout: Seconds to wait for connection.
            market_data_type: IB market data type (1=live, 2=frozen, 3=delayed,
                4=delayed-frozen).  Paper accounts require 3 to avoid error 10197
                (competing live session); live accounts with real-time subscriptions
                should use 1.
        """
        super().__init__(min_call_interval_ms=min_call_interval_ms)
        self._host = host
        self._port = port
        self._client_id = client_id
        self._account_id = account_id
        self._connect_timeout = connect_timeout
        self._market_data_type = market_data_type
        self._ib = IB()
        # Callbacks keyed by ib_order_id.  Each value is a list (usually one
        # entry) of async callables.  A special key _GLOBAL holds callbacks that
        # fire for every order and are never auto-removed.
        self._fill_callbacks: dict[str, list] = {}
        self._status_callbacks: dict[str, list] = {}
        # Maps ib_order_id -> Trade object for amendment support
        self._active_trades: dict[str, Trade] = {}
        # Maps con_id -> fully-qualified Contract ready for order placement.
        # Populated by qualify_contract(); reused by place_* methods so that
        # symbol, secType, exchange, and currency are all present in the IB
        # API message.  A bare Contract(conId=...) omits these fields and
        # causes IB to reject orders with "Missing order exchange".
        self._contract_cache: dict[int, Contract] = {}
        # Short-lived price cache to avoid hammering the IB API on every
        # reprice step when live market data is unavailable (e.g. error 10197).
        # Keyed by con_id; value is (bid, ask, last, expiry_monotonic).
        # This is NOT order/trade/position state — it is ephemeral pricing data
        # that is acceptable to cache in memory and safe to lose on crash.
        self._snapshot_cache: dict[int, tuple[Decimal, Decimal, Decimal, float]] = {}
        self._snapshot_cache_ttl: float = 60.0  # seconds
        # IB order-rejection errors captured by the errorEvent callback.
        # Keyed by ib_order_id (str).  The PendingSubmit wait loop in the engine
        # polls this so it can surface the real IB rejection reason instead of
        # a generic timeout message.  Safe to lose on crash (in-flight orders
        # are recovered from SQLite as ABANDONED on next startup).
        self._order_errors: dict[str, str] = {}
        # IB error codes that indicate an order was rejected or has a price/
        # validation problem.  110 = price doesn't conform to min tick.
        # 200-299 = order/account-level errors (201=rejected, 203=no shorting…).
        self._order_error_codes: frozenset[int] = frozenset({110}) | frozenset(range(200, 300))
        # Ref-counted streaming market data subscriptions.
        # Key: con_id, Value: {"ticker": Ticker, "refs": int, "contract": Contract}
        self._streaming: dict[int, dict] = {}
        # Ref-counted 5-second real-time bar subscriptions.
        self._realtime_bars: dict[int, dict] = {}
        # Disconnect handling. _expected_disconnect is set True before we
        # intentionally call disconnect() so the disconnectedEvent handler
        # knows not to scream about it. _on_unexpected_disconnect is an
        # optional callback the engine wires up to write a CATASTROPHIC
        # alert when the IB Gateway dies under us. Decoupled this way so
        # InsyncClient stays free of repository imports.
        self._expected_disconnect: bool = False
        self._on_unexpected_disconnect = None  # type: ignore[assignment]

    def set_disconnect_callback(self, cb) -> None:
        """Register a callback fired on unexpected IB disconnect.

        The callback receives no arguments and is called synchronously from
        the ib_async event loop. It MUST be fast and non-blocking — schedule
        any DB work via asyncio.create_task or call repository methods that
        complete quickly.
        """
        self._on_unexpected_disconnect = cb

    async def connect(self) -> None:
        """Connect to TWS or IB Gateway."""
        # Silence ib_async's own logger — it logs every IB error message at
        # ERROR level before our errorEvent callback fires, producing duplicates.
        # We handle all error-level reporting ourselves in _on_error.
        import logging as _logging
        _logging.getLogger("ib_async").setLevel(_logging.CRITICAL)

        await self._throttle()
        await self._ib.connectAsync(
            self._host,
            self._port,
            clientId=self._client_id,
            timeout=self._connect_timeout,
        )
        self._ib.disconnectedEvent += self._on_disconnected
        self._ib.execDetailsEvent += self._on_exec_details
        self._ib.orderStatusEvent += self._on_order_status
        self._ib.errorEvent += self._on_error
        self._ib.reqMarketDataType(self._market_data_type)
        logger.info(
            '{"event": "IB_CONNECTED", "host": "%s", "port": %d, "client_id": %d}',
            self._host, self._port, self._client_id,
        )

    async def disconnect(self) -> None:
        """Disconnect from TWS or IB Gateway."""
        # Mark this as an intentional disconnect so _on_disconnected does
        # not raise a CATASTROPHIC alert during normal shutdown.
        self._expected_disconnect = True
        self._ib.disconnect()
        logger.info('{"event": "IB_DISCONNECTED"}')

    def is_connected(self) -> bool:
        """Return True if the underlying IB connection is alive."""
        return self._ib.isConnected()

    async def qualify_contract(
        self,
        symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
    ) -> dict:
        """Qualify an IB contract and return its details."""
        await self._throttle()
        contract = Contract(symbol=symbol, secType=sec_type, exchange=exchange, currency=currency)
        [qualified] = await self._ib.qualifyContractsAsync(contract)
        raw = json.dumps({
            "conId": qualified.conId,
            "symbol": qualified.symbol,
            "secType": qualified.secType,
            "exchange": qualified.exchange,
            "currency": qualified.currency,
            "multiplier": qualified.multiplier,
        })
        # Build a fully-specified contract for order placement.  We keep the
        # symbol and secType from the qualified result so that ib_async
        # includes them in the serialised API message.  exchange is forced to
        # 'SMART' regardless of what IB reports as the primary exchange —
        # SMART is the correct routing value for order submission.
        self._contract_cache[qualified.conId] = Contract(
            conId=qualified.conId,
            symbol=qualified.symbol,
            secType=qualified.secType,
            exchange="SMART",
            currency=qualified.currency,
        )
        logger.info(
            '{"event": "CONTRACT_FETCHED", "symbol": "%s", "con_id": %d}',
            symbol, qualified.conId,
        )
        return {
            "con_id": qualified.conId,
            "exchange": qualified.exchange,
            "currency": qualified.currency,
            "multiplier": qualified.multiplier or None,
            "raw": raw,
        }

    async def get_market_snapshot(self, con_id: int) -> dict:
        """Fetch a bid/ask/last snapshot for a contract.

        Primary path: reqMktData snapshot.  Polls up to 5 s for valid data
        and breaks early once bid+ask or last arrive.

        Fallback path: reqHistoricalData MIDPOINT.  Used when reqMktData
        returns zeros — this happens on paper accounts that share credentials
        with a live account (IB error 10197 'competing live session').
        Historical data uses a separate IB data pathway not subject to the
        competing-session restriction, so it succeeds where reqMktData fails.
        When falling back, bid and ask are both set to the midpoint so that
        calc_mid() returns the same value, keeping order pricing consistent.

        Cache: results are cached for snapshot_cache_ttl seconds.  Without
        this, every reprice step triggers the full 5 s reqMktData polling loop,
        burning the entire reprice window on a single step.
        """
        cached = self._snapshot_cache.get(con_id)
        if cached is not None:
            bid, ask, last, expiry = cached
            if time.monotonic() < expiry:
                logger.debug(
                    '{"event": "SNAPSHOT_CACHE_HIT", "con_id": %d}', con_id
                )
                return {"bid": bid, "ask": ask, "last": last}

        await self._throttle()
        contract = self._contract_cache.get(con_id) or Contract(
            conId=con_id, exchange="SMART", currency="USD"
        )
        # Use streaming mode (snapshot=False) so IB delivers the next
        # available quote even if nothing is cached right now.  Snapshot
        # mode returns zeros immediately during overnight when the data
        # farm has no warm quote for the symbol.
        ticker = self._ib.reqMktData(contract, "", False, False)
        for _ in range(50):  # up to 5 s in 100 ms steps
            await asyncio.sleep(0.1)
            if ticker.bid and ticker.bid > 0 and ticker.ask and ticker.ask > 0:
                break
            if ticker.last and ticker.last > 0:
                break
        # Cancel the streaming subscription — we only needed one quote.
        self._ib.cancelMktData(contract)
        bid = Decimal(str(ticker.bid)) if ticker.bid and ticker.bid == ticker.bid and ticker.bid > 0 else Decimal("0")
        ask = Decimal(str(ticker.ask)) if ticker.ask and ticker.ask == ticker.ask and ticker.ask > 0 else Decimal("0")
        last = Decimal(str(ticker.last)) if ticker.last and ticker.last == ticker.last and ticker.last > 0 else Decimal("0")

        # When bid/ask unavailable but last price exists (common with delayed data),
        # create a synthetic spread so the engine can calculate mid price.
        if bid == 0 and ask == 0 and last > 0:
            half_spread = max(
                (last * Decimal("0.001") / 2).quantize(Decimal("0.01")),
                Decimal("0.01"),
            )
            bid = last - half_spread
            ask = last + half_spread
            logger.info(
                '{"event": "NO_BID_ASK_USING_LAST", "con_id": %d, '
                '"last": "%s", "synthetic_bid": "%s", "synthetic_ask": "%s"}',
                con_id, last, bid, ask,
            )

        if bid == 0 and ask == 0 and last == 0:
            ref = await self._historical_midpoint(contract)
            if ref > 0:
                # Create a synthetic spread (~0.1% of price, min $0.02) so
                # the reprice loop has room to walk toward a fill.  Without
                # this, bid=ask=mid and calc_step_price returns the same
                # price for every step — the order never becomes more
                # aggressive and expires unfilled.
                half_spread = max(
                    (ref * Decimal("0.001") / 2).quantize(Decimal("0.01")),
                    Decimal("0.01"),
                )
                bid = ref - half_spread
                ask = ref + half_spread
                last = ref
                logger.info(
                    '{"event": "SNAPSHOT_HIST_FALLBACK", "con_id": %d, '
                    '"mid": "%s", "synthetic_bid": "%s", "synthetic_ask": "%s"}',
                    con_id, ref, bid, ask,
                )

        if bid > 0 or ask > 0 or last > 0:
            expiry = time.monotonic() + self._snapshot_cache_ttl
            self._snapshot_cache[con_id] = (bid, ask, last, expiry)

        logger.debug(
            '{"event": "SNAPSHOT_RESULT", "con_id": %d, '
            '"bid": "%s", "ask": "%s", "last": "%s"}',
            con_id, bid, ask, last,
        )
        return {"bid": bid, "ask": ask, "last": last}

    async def _historical_midpoint(self, contract: Contract) -> Decimal:
        """Return the most recent MIDPOINT price from historical data.

        Uses a 1-hour lookback with 1-minute bars.  Historical data requests
        use a separate IB pathway that is not blocked by error 10197
        (competing live session), making this a reliable fallback when
        reqMktData is unavailable.

        Returns Decimal("0") on any error so callers can handle it uniformly.
        """
        await self._throttle()
        try:
            bars = await self._ib.reqHistoricalDataAsync(
                contract,
                endDateTime="",
                durationStr="3600 S",
                barSizeSetting="1 min",
                whatToShow="MIDPOINT",
                useRTH=False,
                formatDate=1,
            )
            if bars:
                return Decimal(str(bars[-1].close))
        except Exception as e:
            logger.warning(
                '{"event": "SNAPSHOT_HIST_FALLBACK_FAILED", "error": "%s"}', str(e)
            )
        return Decimal("0")

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
        """Place a limit order. Returns IB order ID as string."""
        await self._throttle()
        contract = self._contract_cache.get(con_id) or Contract(
            conId=con_id, exchange="SMART", currency="USD"
        )
        order = LimitOrder(side, float(qty), float(price))
        order.account = self._account_id
        order.outsideRth = outside_rth
        order.tif = tif
        if order_ref:
            order.orderRef = order_ref
        overnight = is_overnight_session()
        if overnight:
            # During overnight session (8 PM – 3:50 AM ET), set includeOvernight
            # so SMART routing participates in the overnight venue (Blue Ocean ATS).
            # Requires server version >= 189 (Gateway 10.26+), enabled by
            # overnight_patch.py bumping MaxClientVersion to 189.
            # IB requires tif=DAY (not GTC) with includeOvernight — the order
            # spans the overnight session and the following trading day.
            order.includeOvernight = True
            order.tif = "DAY"
        trade = self._ib.placeOrder(contract, order)
        ib_order_id = str(trade.order.orderId)
        self._active_trades[ib_order_id] = trade
        logger.info(
            '{"event": "ORDER_PLACED", "symbol": "%s", "side": "%s", '
            '"qty": "%s", "price": "%s", "ib_order_id": "%s", "includeOvernight": %s}',
            symbol, side, qty, price, ib_order_id,
            "true" if overnight else "false",
        )
        logger.debug(
            '{"event": "ORDER_PLACED_DETAIL", "ib_order_id": "%s", '
            '"exchange": "%s", "tif": "%s", "outsideRth": %s, '
            '"orderType": "%s", "account": "%s", "conId": %s}',
            ib_order_id, contract.exchange, order.tif,
            "true" if order.outsideRth else "false",
            order.orderType, order.account, contract.conId,
        )
        return ib_order_id

    async def place_market_order(
        self,
        con_id: int,
        symbol: str,
        side: str,
        qty: Decimal,
        outside_rth: bool = True,
        order_ref: str | None = None,
    ) -> str:
        """Place a market order. Returns IB order ID as string."""
        await self._throttle()
        contract = self._contract_cache.get(con_id) or Contract(
            conId=con_id, exchange="SMART", currency="USD"
        )
        order = MarketOrder(side, float(qty))
        order.account = self._account_id
        order.outsideRth = outside_rth
        if order_ref:
            order.orderRef = order_ref
        overnight = is_overnight_session()
        if overnight:
            order.includeOvernight = True
            order.tif = "DAY"
        trade = self._ib.placeOrder(contract, order)
        ib_order_id = str(trade.order.orderId)
        self._active_trades[ib_order_id] = trade
        logger.info(
            '{"event": "ORDER_PLACED", "symbol": "%s", "side": "%s", '
            '"qty": "%s", "type": "MARKET", "ib_order_id": "%s", "includeOvernight": %s}',
            symbol, side, qty, ib_order_id,
            "true" if overnight else "false",
        )
        logger.debug(
            '{"event": "ORDER_PLACED_DETAIL", "ib_order_id": "%s", '
            '"exchange": "%s", "tif": "%s", "outsideRth": %s, '
            '"orderType": "%s", "account": "%s", "conId": %s}',
            ib_order_id, contract.exchange, order.tif,
            "true" if order.outsideRth else "false",
            order.orderType, order.account, contract.conId,
        )
        return ib_order_id

    async def amend_order(self, ib_order_id: str, new_price: Decimal) -> None:
        """Amend an existing limit order to a new price in place."""
        await self._throttle()
        trade = self._active_trades.get(ib_order_id)
        if trade is None:
            logger.warning(
                '{"event": "ORDER_AMENDED", "warning": "trade not in active cache", '
                '"ib_order_id": "%s"}', ib_order_id
            )
            return
        # Wait for IB to acknowledge the order (assign permId) before amending.
        # Amending while still PendingSubmit causes error 103 (duplicate order id)
        # because IB hasn't recorded the order yet and treats the amendment as a
        # second new order with the same client-assigned orderId.
        for _ in range(50):  # up to 5 s in 100 ms steps
            status = trade.orderStatus.status
            if status and status not in ("", "PendingSubmit"):
                break
            await asyncio.sleep(0.1)
        else:
            logger.warning(
                '{"event": "ORDER_AMENDED", "warning": "order still PendingSubmit after 5s, skipping", '
                '"ib_order_id": "%s"}', ib_order_id
            )
            return
        trade.order.lmtPrice = float(new_price)
        trade.order.outsideRth = True  # ib_async resets this on TWS echo-back (GitHub #141)
        # Preserve includeOvernight + DAY tif on amendments during overnight.
        overnight = is_overnight_session()
        if overnight:
            trade.order.includeOvernight = True
            trade.order.tif = "DAY"
        self._ib.placeOrder(trade.contract, trade.order)
        logger.info(
            '{"event": "ORDER_AMENDED", "ib_order_id": "%s", "new_price": "%s"}',
            ib_order_id, new_price,
        )
        logger.debug(
            '{"event": "ORDER_AMENDED_DETAIL", "ib_order_id": "%s", '
            '"tif": "%s", "outsideRth": true, "includeOvernight": %s, '
            '"exchange": "%s", "status_before_amend": "%s"}',
            ib_order_id, trade.order.tif,
            "true" if overnight else "false",
            trade.contract.exchange,
            trade.orderStatus.status,
        )

    async def cancel_order(self, ib_order_id: str) -> None:
        """Cancel an open order."""
        await self._throttle()
        trade = self._active_trades.get(ib_order_id)
        if trade is None:
            logger.warning(
                '{"event": "ORDER_CANCELED", "warning": "trade not in active cache", '
                '"ib_order_id": "%s"}', ib_order_id
            )
            return
        # Skip if order is already in a terminal state — sending a cancel in that
        # case causes IB error 10147 "OrderId not found for cancellation".
        terminal = {"Cancelled", "Filled", "Inactive"}
        if trade.orderStatus.status in terminal:
            logger.info(
                '{"event": "ORDER_CANCEL_SKIPPED", "reason": "already terminal", '
                '"status": "%s", "ib_order_id": "%s"}',
                trade.orderStatus.status, ib_order_id,
            )
            return
        self._ib.cancelOrder(trade.order)
        logger.info('{"event": "ORDER_CANCELED", "ib_order_id": "%s"}', ib_order_id)

    async def get_order_status(self, ib_order_id: str) -> dict:
        """Get current status of an order from IB."""
        await self._throttle()
        trade = self._active_trades.get(ib_order_id)
        if trade is None:
            return {
                "status": "UNKNOWN",
                "qty_filled": Decimal("0"),
                "avg_fill_price": None,
                "commission": None,
                "why_held": None,
            }
        status = trade.orderStatus.status
        qty_filled = Decimal(str(trade.orderStatus.filled))
        avg_price = (
            Decimal(str(trade.orderStatus.avgFillPrice))
            if trade.orderStatus.avgFillPrice
            else None
        )
        commission = None
        if trade.fills:
            total_commission = sum(
                Decimal(str(f.commissionReport.commission))
                for f in trade.fills
                if f.commissionReport
            )
            commission = total_commission if total_commission else None
        why_held = getattr(trade.orderStatus, "whyHeld", None) or None
        return {
            "status": status,
            "qty_filled": qty_filled,
            "avg_fill_price": avg_price,
            "commission": commission,
            "why_held": why_held,
        }

    async def get_open_orders(self) -> list[dict]:
        """Get all currently open orders from IB.

        Filters out terminal statuses (Cancelled, Filled, Inactive) since
        IB keeps them in the open orders list until session reset.
        """
        await self._throttle()
        open_trades = await self._ib.reqAllOpenOrdersAsync()
        logger.debug(
            '{"event": "OPEN_ORDERS_RAW", "count": %d, "orders": [%s]}',
            len(open_trades),
            ", ".join(
                '{"id": %s, "symbol": "%s", "status": "%s"}'
                % (trade.order.orderId, trade.contract.symbol, trade.orderStatus.status)
                for trade in open_trades
            ),
        )
        result = []
        for trade in open_trades:
            status = trade.orderStatus.status
            if status in self._TERMINAL_STATUSES:
                continue
            # IB Gateway keeps stale rejected/cancelled orders from previous
            # client sessions with non-terminal statuses (e.g. PreSubmitted
            # with empty string status).  Filter out orders with no status.
            if not status or status == "":
                continue
            result.append({
                "ib_order_id": str(trade.order.orderId),
                "symbol": trade.contract.symbol,
                "side": trade.order.action,
                "qty": Decimal(str(trade.order.totalQuantity)),
                "status": status,
                "qty_filled": Decimal(str(trade.orderStatus.filled)),
                "avg_fill_price": (
                    Decimal(str(trade.orderStatus.avgFillPrice))
                    if trade.orderStatus.avgFillPrice
                    else None
                ),
            })
        return result

    def get_order_error(self, ib_order_id: str) -> str | None:
        """Return the stored IB rejection message for this order, or None."""
        return self._order_errors.get(ib_order_id)

    def get_live_order_status(self, ib_order_id: str) -> str | None:
        """Return the live IB status string for an order, or None if unknown."""
        trade = self._active_trades.get(ib_order_id)
        if trade is None:
            return None
        return trade.orderStatus.status or None

    async def subscribe_market_data(self, con_id: int, symbol: str) -> None:
        """Subscribe to streaming market data (ref-counted)."""
        _GENERIC_TICKS = "165"  # Misc Stats: avVolume, 52-week high/low

        if con_id in self._streaming:
            entry = self._streaming[con_id]
            entry["refs"] += 1

            # If the existing subscription was created without enriched ticks
            # (e.g. by position loop before watchlist loop), upgrade it by
            # cancelling and re-subscribing with generic ticks.
            if not entry.get("enriched"):
                try:
                    self._ib.cancelMktData(entry["contract"])
                except Exception:
                    pass
                ticker = self._ib.reqMktData(entry["contract"], _GENERIC_TICKS, False, False)
                entry["ticker"] = ticker
                entry["enriched"] = True
                logger.info(
                    '{"event": "STREAMING_SUB_UPGRADED", "con_id": %d, "symbol": "%s"}',
                    con_id, symbol,
                )

            logger.debug(
                '{"event": "STREAMING_REF_INC", "con_id": %d, "symbol": "%s", "refs": %d}',
                con_id, symbol, entry["refs"],
            )
            return

        await self._throttle()
        contract = self._contract_cache.get(con_id) or Contract(
            conId=con_id, symbol=symbol, secType="STK",
            exchange="SMART", currency="USD",
        )
        ticker = self._ib.reqMktData(contract, _GENERIC_TICKS, False, False)
        self._streaming[con_id] = {"ticker": ticker, "refs": 1, "contract": contract, "enriched": True}
        logger.info(
            '{"event": "STREAMING_SUB_ADDED", "con_id": %d, "symbol": "%s"}',
            con_id, symbol,
        )

    async def unsubscribe_market_data(self, con_id: int) -> None:
        """Unsubscribe from streaming market data (ref-counted)."""
        entry = self._streaming.get(con_id)
        if entry is None:
            return
        entry["refs"] -= 1
        if entry["refs"] <= 0:
            try:
                self._ib.cancelMktData(entry["contract"])
            except Exception:
                pass
            del self._streaming[con_id]
            logger.info(
                '{"event": "STREAMING_SUB_CANCELLED", "con_id": %d}', con_id,
            )
        else:
            logger.debug(
                '{"event": "STREAMING_REF_DEC", "con_id": %d, "refs": %d}',
                con_id, entry["refs"],
            )

    def get_ticker(self, con_id: int) -> dict | None:
        """Return current streaming ticker data, or None."""
        entry = self._streaming.get(con_id)
        if entry is None:
            return None
        t = entry["ticker"]

        def _val(v):
            """Return float if valid, else None. NaN check: v != v."""
            if v is None or v != v or v <= 0:
                return None
            return float(v)

        last = _val(t.last)
        close = _val(t.close)

        # IB often doesn't stream previous close for ETFs (QQQ, GLD, SPY)
        # outside regular hours. Fall back to prevClose or halted last price
        # from the ticker's misc fields, then to cached close from previous
        # successful reads.
        if close is None:
            close = _val(getattr(t, 'prevClose', None))
        if close is None and last is not None:
            # Store and reuse the first valid close we ever compute.
            # Once we see a close, it stays valid for the session.
            cached_close = entry.get("_cached_close")
            if cached_close is not None:
                close = cached_close
        if close is not None:
            entry["_cached_close"] = close

        # Expose the ticker's last-update timestamp so callers can detect
        # stale data (Ticker.time stops advancing when IB stops pushing).
        ticker_time = getattr(t, 'time', None)

        return {
            "bid": _val(t.bid),
            "ask": _val(t.ask),
            "last": last,
            "open": _val(t.open),
            "high": _val(t.high),
            "low": _val(t.low),
            "close": close,
            "volume": _val(t.volume),
            "avg_volume": _val(getattr(t, 'avVolume', None)),
            "high_52w": _val(getattr(t, 'high52week', None)),
            "low_52w": _val(getattr(t, 'low52week', None)),
            "ticker_time": ticker_time,
        }

    def has_contract_cached(self, con_id: int) -> bool:
        """Return True if the in-memory contract cache has a fully-specified
        Contract for this con_id."""
        return con_id in self._contract_cache

    def register_fill_callback(self, callback, ib_order_id: str | None = None) -> None:
        """Register a callback for fill events.

        Args:
            callback: Async callable dispatched on fill.
            ib_order_id: Scope callback to this order (auto-removed on terminal
                state).  None means fire for all orders, never auto-removed.
        """
        key = ib_order_id or "_GLOBAL"
        self._fill_callbacks.setdefault(key, []).append(callback)

    def register_status_callback(self, callback, ib_order_id: str | None = None) -> None:
        """Register a callback for order status change events.

        Args:
            callback: Async callable dispatched on status change.
            ib_order_id: Scope callback to this order (auto-removed on terminal
                state).  None means fire for all orders, never auto-removed.
        """
        key = ib_order_id or "_GLOBAL"
        self._status_callbacks.setdefault(key, []).append(callback)

    def unregister_callbacks(self, ib_order_id: str) -> None:
        """Remove all fill and status callbacks registered for an order.

        Also cleans up _order_errors and _active_trades entries to prevent
        unbounded memory growth over long sessions.
        """
        removed = False
        if ib_order_id in self._fill_callbacks:
            del self._fill_callbacks[ib_order_id]
            removed = True
        if ib_order_id in self._status_callbacks:
            del self._status_callbacks[ib_order_id]
            removed = True
        self._order_errors.pop(ib_order_id, None)
        if removed:
            logger.debug(
                '{"event": "CALLBACKS_UNREGISTERED", "ib_order_id": "%s"}',
                ib_order_id,
            )

    # ------------------------------------------------------------------
    # Real-time bars (5-second streaming)
    # ------------------------------------------------------------------

    async def subscribe_realtime_bars(
        self, con_id: int, symbol: str,
        what_to_show: str = "TRADES",
        callback=None,
    ) -> None:
        """Subscribe to 5-second real-time bars (ref-counted)."""
        if con_id in self._realtime_bars:
            entry = self._realtime_bars[con_id]
            entry["refs"] += 1
            if callback and callback not in entry["callbacks"]:
                entry["callbacks"].append(callback)
            logger.debug(
                '{"event": "RT_BARS_REF_INC", "con_id": %d, "symbol": "%s", "refs": %d}',
                con_id, symbol, entry["refs"],
            )
            return

        await self._throttle()
        contract = self._contract_cache.get(con_id) or Contract(
            conId=con_id, symbol=symbol, secType="STK",
            exchange="SMART", currency="USD",
        )
        bars = self._ib.reqRealTimeBars(
            contract, barSize=5, whatToShow=what_to_show, useRTH=False,
        )
        callbacks = [callback] if callback else []
        self._realtime_bars[con_id] = {
            "bars": bars, "refs": 1, "contract": contract,
            "callbacks": callbacks, "symbol": symbol,
        }

        # Register update handler
        def on_bar_update(bars, has_new_bar):
            if has_new_bar and bars:
                bar = bars[-1]
                bar_data = {
                    "time": bar.time,
                    "open": float(bar.open_),
                    "high": float(bar.high),
                    "low": float(bar.low),
                    "close": float(bar.close),
                    "volume": int(bar.volume),
                }
                entry = self._realtime_bars.get(con_id)
                if entry:
                    for cb in entry["callbacks"]:
                        try:
                            import asyncio
                            if asyncio.iscoroutinefunction(cb):
                                asyncio.ensure_future(cb(bar_data))
                            else:
                                cb(bar_data)
                        except Exception as exc:
                            logger.warning(
                                '{"event": "RT_BAR_CALLBACK_ERROR", "error": "%s"}',
                                exc,
                            )

        bars.updateEvent += on_bar_update
        logger.info(
            '{"event": "RT_BARS_SUBSCRIBED", "con_id": %d, "symbol": "%s", "what": "%s"}',
            con_id, symbol, what_to_show,
        )

    async def unsubscribe_realtime_bars(self, con_id: int) -> None:
        """Cancel real-time bar subscription (ref-counted)."""
        entry = self._realtime_bars.get(con_id)
        if entry is None:
            return
        entry["refs"] -= 1
        if entry["refs"] <= 0:
            try:
                self._ib.cancelRealTimeBars(entry["bars"])
            except Exception:
                pass
            del self._realtime_bars[con_id]
            logger.info(
                '{"event": "RT_BARS_CANCELLED", "con_id": %d}', con_id,
            )
        else:
            logger.debug(
                '{"event": "RT_BARS_REF_DEC", "con_id": %d, "refs": %d}',
                con_id, entry["refs"],
            )

    # IB codes that are purely informational — connectivity/farm status notices.
    # Logged at INFO level; never treated as errors.
    _INFO_CODES: frozenset[int] = frozenset({
        1100,  # Connectivity between IB and TWS lost
        1101,  # Connectivity restored — data lost
        1102,  # Connectivity restored — data maintained
        2104,  # Market data farm connection OK
        2106,  # HMDS data farm connection OK
        2107,  # HMDS data farm connection inactive (not an error)
        2108,  # Market data farm connection inactive (not an error)
        2158,  # Sec-def data farm connection OK
    })

    def _on_error(self, reqId: int, errorCode: int, errorString: str, *_) -> None:
        """Handle IB error events.

        Any error referencing an active order is stored in _order_errors keyed
        by ib_order_id so the PendingSubmit wait loop and bid/ask rejection
        detection in the engine can surface the real rejection reason (including
        after-hours rejections, extended-hours errors, or any other IB error
        code) instead of a generic message.

        Connectivity/farm status codes (1100-1102, 2104-2158) are logged at
        INFO level — they are notifications, not errors.

        The *_ absorbs the optional advancedOrderRejectJson argument present in
        newer ib_async versions without breaking older ones.
        """
        ib_order_id = str(reqId)
        if ib_order_id in self._active_trades:
            logger.error(
                '{"event": "IB_ORDER_ERROR", "ib_order_id": "%s", "code": %d, "msg": "%s"}',
                ib_order_id, errorCode, errorString,
            )
            self._order_errors[ib_order_id] = f"[{errorCode}] {errorString}"
        elif errorCode in self._INFO_CODES:
            logger.info(
                '{"event": "IB_NOTICE", "reqId": %d, "code": %d, "msg": "%s"}',
                reqId, errorCode, errorString,
            )
        else:
            logger.warning(
                '{"event": "IB_ERROR", "reqId": %d, "code": %d, "msg": "%s"}',
                reqId, errorCode, errorString,
            )

    def _on_disconnected(self) -> None:
        """Handle IB disconnect event.

        ib_async fires this for BOTH intentional shutdowns (we called
        ``disconnect()``) and unexpected drops (Gateway killed, network
        died, TWS crashed). We distinguish the two via ``_expected_disconnect``
        and only escalate the unexpected case to the engine via the
        registered callback.
        """
        if self._expected_disconnect:
            logger.info('{"event": "IB_DISCONNECTED", "expected": true}')
            return
        # Print to stdout so it shows up in `make dev` output even when
        # nobody is tailing the JSON log file.
        print(
            "[ENGINE] CATASTROPHIC: IB Gateway connection lost. "
            "The engine cannot place or track orders until the Gateway is restarted.",
            flush=True,
        )
        logger.error('{"event": "IB_DISCONNECTED", "unexpected": true}')
        if self._on_unexpected_disconnect is not None:
            try:
                self._on_unexpected_disconnect()
            except Exception:
                logger.exception('{"event": "IB_DISCONNECT_CALLBACK_FAILED"}')

    # Terminal IB order statuses — callbacks are auto-removed after dispatch.
    # ApiCancelled is used by some IB Gateway versions as an alternative to Cancelled.
    _TERMINAL_STATUSES: frozenset[str] = frozenset({
        "Filled", "Cancelled", "Inactive", "ApiCancelled",
    })

    def _on_exec_details(self, trade: Trade, fill: Fill) -> None:
        """Handle fill event from IB and dispatch to registered callbacks.

        Uses fill.execution data (this specific fill) rather than
        trade.orderStatus (cumulative, lags behind on partial fills).
        """
        ib_order_id = str(trade.order.orderId)
        # Use fill-level data — orderStatus.filled/avgFillPrice lag behind
        qty_filled = Decimal(str(fill.execution.shares))
        avg_price = Decimal(str(fill.execution.price))
        commission = Decimal("0")
        if fill.commissionReport:
            commission = Decimal(str(fill.commissionReport.commission))

        logger.debug(
            '{"event": "IB_FILL_RECEIVED", "ib_order_id": "%s", '
            '"symbol": "%s", "side": "%s", "qty_filled": "%s", '
            '"avg_price": "%s", "commission": "%s", '
            '"fill_price": "%s", "fill_qty": "%s", "exchange": "%s"}',
            ib_order_id, trade.contract.symbol, trade.order.action,
            qty_filled, avg_price, commission,
            fill.execution.price, fill.execution.shares,
            fill.execution.exchange,
        )

        loop = asyncio.get_event_loop()
        # Dispatch to order-specific callbacks, then global callbacks.
        for cb in self._fill_callbacks.get(ib_order_id, []):
            loop.create_task(cb(ib_order_id, qty_filled, avg_price, commission))
        for cb in self._fill_callbacks.get("_GLOBAL", []):
            loop.create_task(cb(ib_order_id, qty_filled, avg_price, commission))

    def _on_order_status(self, trade: Trade) -> None:
        """Handle order status change event from IB and dispatch to callbacks.

        When the status is terminal (Filled, Cancelled, Inactive), callbacks
        for that order are dispatched and then removed to prevent unbounded
        growth over long sessions.
        """
        ib_order_id = str(trade.order.orderId)
        status = trade.orderStatus.status
        why_held = getattr(trade.orderStatus, "whyHeld", "") or ""
        remaining = trade.orderStatus.remaining

        logger.debug(
            '{"event": "IB_STATUS_CHANGE", "ib_order_id": "%s", '
            '"symbol": "%s", "status": "%s", "filled": %s, '
            '"remaining": %s, "why_held": "%s", "perm_id": %s}',
            ib_order_id, trade.contract.symbol, status,
            trade.orderStatus.filled, remaining, why_held,
            trade.order.permId,
        )

        loop = asyncio.get_event_loop()
        # Dispatch to order-specific callbacks, then global callbacks.
        for cb in self._status_callbacks.get(ib_order_id, []):
            loop.create_task(cb(ib_order_id, status))
        for cb in self._status_callbacks.get("_GLOBAL", []):
            loop.create_task(cb(ib_order_id, status))

        # Auto-cleanup after terminal status dispatch.
        if status in self._TERMINAL_STATUSES:
            self.unregister_callbacks(ib_order_id)
