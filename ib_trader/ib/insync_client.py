"""ib_async concrete implementation of IBClientBase.

This is the ONLY file in the project that imports ib_async.
All IB API interaction goes through this class.
outsideRth = True is enforced here — engine code never sets IB order fields directly.
"""
import asyncio
import json
import logging
import time
from datetime import date
from decimal import Decimal
from zoneinfo import ZoneInfo

from ib_async import IB, Contract, Future, LimitOrder, MarketOrder, Order, Trade, Fill

from ib_trader.broker.exceptions import AmbiguousInstrument, ExpiredContractError
from ib_trader.broker.types import FutureExpiryCandidate
from ib_trader.engine.market_hours import is_overnight_session
from ib_trader.ib.base import IBClientBase
from ib_trader.ib.overnight_patch import apply as _apply_overnight_patch


# Exchange → product-session timezone mapping. CME-family products settle in
# America/Chicago; unmapped exchanges fall back to UTC (with a warning at
# the call site). See Epic 1 D9.
_EXCHANGE_TZ: dict[str, ZoneInfo] = {
    "CME": ZoneInfo("America/Chicago"),
    "CBOT": ZoneInfo("America/Chicago"),
    "NYMEX": ZoneInfo("America/Chicago"),
    "COMEX": ZoneInfo("America/Chicago"),
    "GLOBEX": ZoneInfo("America/Chicago"),
}


def _product_today(exchange: str) -> date:
    """Return today's date in the exchange's session timezone.

    Falls back to UTC for unmapped exchanges; callers log a WARNING via
    the structured logger when this path is hit.
    """
    tz = _EXCHANGE_TZ.get(exchange.upper(), ZoneInfo("UTC"))
    import datetime as _dt
    return _dt.datetime.now(tz).date()


def _normalize_expiry(expiry: str | None) -> str | None:
    """Return a YYYYMMDD expiry string from a YYYYMM or YYYYMMDD input.

    YYYYMM alone is ambiguous pre-qualification — we pass it through to
    IB's qualifier, which resolves it to the contract's last-trade date.
    This helper validates shape only; the authoritative value is the
    string IB returns in ``lastTradeDateOrContractMonth`` post-qualify.
    """
    if expiry is None:
        return None
    if not expiry.isdigit() or len(expiry) not in (6, 8):
        raise ValueError(f"expiry must be YYYYMM or YYYYMMDD: {expiry!r}")
    return expiry


def _expiry_as_date(expiry: str) -> date:
    """Parse a YYYYMMDD (or YYYYMM) expiry into a date.

    YYYYMM expiries are treated as the last day of that month, which is
    always >= the actual last-trade date. This is conservative for the
    expired-check in ``_qualify_future`` (we never reject a contract
    that might still be tradable).
    """
    if len(expiry) == 8:
        return date(int(expiry[:4]), int(expiry[4:6]), int(expiry[6:8]))
    if len(expiry) == 6:
        import calendar
        y, m = int(expiry[:4]), int(expiry[4:6])
        return date(y, m, calendar.monthrange(y, m)[1])
    raise ValueError(f"expiry must be YYYYMM or YYYYMMDD: {expiry!r}")


def _candidate_from_details(d, root: str) -> FutureExpiryCandidate:
    """Build a ``FutureExpiryCandidate`` from an ib_async ContractDetails."""
    c = d.contract
    return FutureExpiryCandidate(
        con_id=c.conId,
        root=root,
        expiry=c.lastTradeDateOrContractMonth,
        trading_class=c.tradingClass or root,
        exchange=c.exchange,
        multiplier=Decimal(str(c.multiplier)) if c.multiplier else Decimal("1"),
        tick_size=Decimal(str(d.minTick)) if d.minTick else Decimal("0.01"),
    )

# Patch ib_async to support includeOvernight (server version 189).
# Must run before any IB connection is established.
_apply_overnight_patch()

logger = logging.getLogger(__name__)

# Retain references to fire-and-forget asyncio tasks so the loop's weakref
# collection doesn't cancel them mid-flight. See Python docs on create_task.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """Create an asyncio task, track it, and auto-discard on completion."""
    task = asyncio.ensure_future(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


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
        self.__ib = IB()
        # Callbacks keyed by ib_order_id.  Each value is a list (usually one
        # entry) of async callables.  A special key _GLOBAL holds callbacks that
        # fire for every order and are never auto-removed.
        self._fill_callbacks: dict[str, list] = {}
        self._status_callbacks: dict[str, list] = {}
        # Tracks the last NON-CANCELLED status we saw for each ib_order_id.
        # When ib_async synthesizes a fake Cancelled (see _on_order_status),
        # we patch trade.orderStatus.status back to this previous clean
        # value while reqOpenOrdersAsync verifies. Defaults to "Submitted"
        # on lookup miss — every order ib_async tracks as alive must be
        # at least Submitted by the time a synthesized cancel could fire.
        self._previous_clean_status_map: dict[str, str] = {}
        # Commission callbacks fire when IB delivers a CommissionReport
        # (typically slightly after the matching execDetails). Same
        # keying convention as _fill_callbacks: per-ib_order_id scoped
        # callbacks + a _GLOBAL bucket that fires for all orders.
        # Non-gating — handlers only update persisted state.
        self._commission_callbacks: dict[str, list] = {}
        # Seen exec_ids for commission-dedup: IB occasionally re-delivers
        # the same CommissionReport on reconnect.
        self._seen_commission_execs: set[str] = set()
        # Order-placed callbacks: fire synchronously inside place_*_order
        # right after ib_async returns the Trade. Used by the engine to
        # snapshot the broker-side position *before* any fill events can
        # arrive, then register the order with the OrderLedger including
        # the snapshot. Single-threaded asyncio guarantees no fill
        # callback runs between placeOrder() and our callback dispatch
        # below, so the snapshot is genuinely pre-fill.
        self._order_placed_callbacks: list = []
        # Per-order asyncio locks. Every dispatched callback (fill,
        # status, commission) for a given ib_order_id acquires this
        # lock before running, so IB events for the same order are
        # processed strictly in arrival order even when individual
        # handlers yield mid-flight. Different orders remain parallel.
        # asyncio.Lock serves waiters FIFO, so tasks created in IB's
        # dispatch order acquire in IB's dispatch order. Locks are
        # retained for the life of the engine — per-order memory
        # overhead is negligible and late events (e.g. a commission
        # arriving after terminal) must find the same lock to
        # maintain ordering against any fills that were still in
        # flight when the status flipped.
        self._order_locks: dict[str, asyncio.Lock] = {}
        # Maps ib_order_id -> Trade object for amendment support
        self.__active_trades: dict[str, Trade] = {}
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
        self._on_connected_callback = None  # type: ignore[assignment]

    def set_disconnect_callback(self, cb) -> None:
        """Register a callback fired on unexpected IB disconnect.

        The callback receives no arguments and is called synchronously from
        the ib_async event loop. It MUST be fast and non-blocking — schedule
        any DB work via asyncio.create_task or call repository methods that
        complete quickly.
        """
        self._on_unexpected_disconnect = cb

    def set_connect_callback(self, cb) -> None:
        """Register a callback fired on every successful IB connect.

        Used by the engine to auto-resolve the IB_GATEWAY_DISCONNECTED
        alert on reconnect. Same contract as ``set_disconnect_callback``
        — synchronous, fast, non-blocking.
        """
        self._on_connected_callback = cb

    async def connect(self) -> None:
        """Connect to TWS or IB Gateway."""
        # Silence ib_async's own logger — it logs every IB error message at
        # ERROR level before our errorEvent callback fires, producing duplicates.
        # We handle all error-level reporting ourselves in _on_error.
        import logging as _logging
        _logging.getLogger("ib_async").setLevel(_logging.CRITICAL)

        await self._throttle()
        await self.__ib.connectAsync(
            self._host,
            self._port,
            clientId=self._client_id,
            timeout=self._connect_timeout,
        )
        self.__ib.disconnectedEvent += self._on_disconnected
        self.__ib.connectedEvent += self._on_connected
        self.__ib.execDetailsEvent += self._on_exec_details
        self.__ib.orderStatusEvent += self._on_order_status
        self.__ib.commissionReportEvent += self._on_commission_report
        self.__ib.errorEvent += self._on_error
        self.__ib.reqMarketDataType(self._market_data_type)
        logger.info(
            '{"event": "IB_CONNECTED", "host": "%s", "port": %d, "client_id": %d}',
            self._host, self._port, self._client_id,
        )

    async def disconnect(self) -> None:
        """Disconnect from TWS or IB Gateway."""
        # Mark this as an intentional disconnect so _on_disconnected does
        # not raise a CATASTROPHIC alert during normal shutdown.
        self._expected_disconnect = True
        self.__ib.disconnect()
        logger.info('{"event": "IB_DISCONNECTED"}')

    def is_connected(self) -> bool:
        """Return True if the underlying IB connection is alive."""
        return self.__ib.isConnected()

    def managed_accounts(self) -> list[str]:
        """Return the IB account IDs attached to this Gateway session."""
        return list(self.__ib.managedAccounts())

    async def qualify_contract(
        self,
        symbol: str,
        sec_type: str = "STK",
        exchange: str = "SMART",
        currency: str = "USD",
        *,
        expiry: str | None = None,
        trading_class: str | None = None,
    ) -> dict:
        """Qualify an IB contract and return its details.

        For STK the original single-step behaviour is preserved. For
        FUT the caller must supply ``expiry`` (YYYYMM or YYYYMMDD); the
        method builds an ``ib_async.Future`` with optional
        ``trading_class``. When IB returns >1 candidate we raise
        ``AmbiguousInstrument`` (ES vs MES etc). Past-expiry contracts
        raise ``ExpiredContractError``. The returned dict grows
        ``trading_class`` and ``tick_size`` fields for FUT.
        """
        await self._throttle()
        sec_type_u = sec_type.upper()
        if sec_type_u == "FUT":
            # When `expiry` is supplied, caller has explicit (root,
            # YYYYMM) — use the standard symbol+expiry path. Otherwise
            # treat `symbol` as the IB-paste localSymbol (``MESM6``)
            # and qualify by that. Both paths land on the same dict
            # response and seed the contract cache identically.
            if expiry:
                return await self._qualify_future(
                    root=symbol, exchange=exchange, currency=currency,
                    expiry=_normalize_expiry(expiry), trading_class=trading_class,
                )
            return await self._qualify_future_by_local_symbol(
                local_symbol=symbol, exchange=exchange, currency=currency,
            )

        contract = Contract(symbol=symbol, secType=sec_type, exchange=exchange, currency=currency)
        results = await self.__ib.qualifyContractsAsync(contract)
        # ib-async returns ``[None]`` (not an empty list) when IB can't
        # match the contract — destructuring would silently bind None
        # and we'd crash on the next ``.conId`` access. Raise a clear
        # error so callers can decide to retry with a different
        # sec_type / fall back to a FUT-localSymbol path.
        qualified = results[0] if results else None
        if qualified is None or not getattr(qualified, "conId", 0):
            raise ValueError(
                f"qualify_contract: IB returned no match for "
                f"{symbol!r} (sec_type={sec_type})",
            )
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

    async def _qualify_future(
        self,
        root: str,
        exchange: str,
        currency: str,
        expiry: str | None,
        trading_class: str | None,
    ) -> dict:
        """Qualify a single futures contract via ``reqContractDetailsAsync``.

        ``reqContractDetails`` gives us both the con_id and the
        tick_size (minTick) in a single round-trip; ``qualifyContracts``
        would require a second call for tick metadata.
        """
        if not expiry:
            raise ValueError("FUT qualify requires expiry (YYYYMM or YYYYMMDD)")

        future = Future(
            symbol=root,
            lastTradeDateOrContractMonth=expiry,
            exchange=exchange,
            currency=currency,
            tradingClass=trading_class or "",
        )
        details = await self.__ib.reqContractDetailsAsync(future)
        if not details:
            raise ValueError(f"no IB contract matched {root} {expiry} on {exchange}")

        # Filter to non-expired candidates in the exchange's tz.
        today = _product_today(exchange)
        if exchange.upper() not in _EXCHANGE_TZ:
            logger.warning(
                '{"event": "UNKNOWN_EXCHANGE_TZ", "exchange": "%s", "fallback": "UTC"}',
                exchange,
            )
        fresh = [d for d in details if _expiry_as_date(d.contract.lastTradeDateOrContractMonth) >= today]
        if not fresh:
            # Every candidate is expired — flag with the first one's date.
            raise ExpiredContractError(root, details[0].contract.lastTradeDateOrContractMonth)

        if len(fresh) > 1:
            candidates = [_candidate_from_details(d, root) for d in fresh]
            raise AmbiguousInstrument(root=root, candidates=candidates)

        d = fresh[0]
        qualified = d.contract
        tick = Decimal(str(d.minTick)) if d.minTick else Decimal("0.01")
        multiplier = qualified.multiplier or None
        raw = json.dumps({
            "conId": qualified.conId,
            "symbol": qualified.symbol,
            "secType": qualified.secType,
            "exchange": qualified.exchange,
            "currency": qualified.currency,
            "multiplier": multiplier,
            "tradingClass": qualified.tradingClass,
            "lastTradeDateOrContractMonth": qualified.lastTradeDateOrContractMonth,
            "minTick": str(tick),
        })
        # Order-placement cache: futures route to their primary exchange
        # (not SMART) since futures use a single exchange per product.
        self._contract_cache[qualified.conId] = Contract(
            conId=qualified.conId,
            symbol=qualified.symbol,
            secType=qualified.secType,
            exchange=qualified.exchange,
            currency=qualified.currency,
            lastTradeDateOrContractMonth=qualified.lastTradeDateOrContractMonth,
            tradingClass=qualified.tradingClass,
            multiplier=qualified.multiplier,
        )
        logger.info(
            '{"event": "CONTRACT_FETCHED", "symbol": "%s", "con_id": %d, "sec_type": "FUT",'
            ' "expiry": "%s", "trading_class": "%s", "tick": "%s"}',
            root, qualified.conId, qualified.lastTradeDateOrContractMonth,
            qualified.tradingClass, tick,
        )
        return {
            "con_id": qualified.conId,
            "exchange": qualified.exchange,
            "currency": qualified.currency,
            "multiplier": multiplier,
            "trading_class": qualified.tradingClass or None,
            "expiry": qualified.lastTradeDateOrContractMonth,
            "tick_size": str(tick),
            "raw": raw,
        }

    async def _qualify_future_by_local_symbol(
        self,
        local_symbol: str,
        exchange: str,
        currency: str,
    ) -> dict:
        """Qualify a futures contract by its IB-paste localSymbol.

        ``local_symbol`` is the form IB displays in TWS / order tickets:
        root + month-letter + 1-2 digit year (e.g. ``ESM6``, ``MESM6``,
        ``GCM26``). IB resolves the contract uniquely from this string
        — no need for the caller to pre-split into (root, expiry).

        ``exchange`` may be empty; ib-async accepts ``""`` and IB picks
        the primary listing automatically.
        """
        # localSymbol qualifies uniquely across all exchanges, so we
        # let IB pick the listing rather than guessing per-product
        # (COMEX gold, NYMEX oil, CBOT treasuries, GLOBEX equities all
        # coexist). Caller-supplied exchange is intentionally ignored
        # here — the localSymbol form is its own answer to "where".
        contract = Future(
            localSymbol=local_symbol,
            exchange="",
            currency=currency,
        )
        details = await self.__ib.reqContractDetailsAsync(contract)
        if not details:
            raise ValueError(f"no IB contract matched localSymbol {local_symbol!r}")

        # Filter expired (in product-local tz, falling back to UTC).
        # We don't know the exchange yet — IB's response carries it on
        # each candidate. Pick the freshest non-expired candidate.
        candidates: list = []
        for d in details:
            ex = (d.contract.exchange or "").upper()
            today = _product_today(ex)
            if _expiry_as_date(d.contract.lastTradeDateOrContractMonth) >= today:
                candidates.append(d)

        if not candidates:
            raise ExpiredContractError(
                local_symbol, details[0].contract.lastTradeDateOrContractMonth,
            )
        if len(candidates) > 1:
            cands = [_candidate_from_details(d, d.contract.symbol) for d in candidates]
            raise AmbiguousInstrument(root=local_symbol, candidates=cands)

        d = candidates[0]
        qualified = d.contract
        tick = Decimal(str(d.minTick)) if d.minTick else Decimal("0.01")
        multiplier = qualified.multiplier or None
        raw = json.dumps({
            "conId": qualified.conId,
            "symbol": qualified.symbol,
            "secType": qualified.secType,
            "exchange": qualified.exchange,
            "currency": qualified.currency,
            "multiplier": multiplier,
            "tradingClass": qualified.tradingClass,
            "lastTradeDateOrContractMonth": qualified.lastTradeDateOrContractMonth,
            "localSymbol": qualified.localSymbol,
            "minTick": str(tick),
        })
        # Cache a fully-qualified contract for downstream placement.
        # Futures route to their primary exchange (not SMART).
        self._contract_cache[qualified.conId] = Contract(
            conId=qualified.conId,
            symbol=qualified.symbol,
            secType=qualified.secType,
            exchange=qualified.exchange,
            currency=qualified.currency,
            lastTradeDateOrContractMonth=qualified.lastTradeDateOrContractMonth,
            tradingClass=qualified.tradingClass,
            multiplier=qualified.multiplier,
            localSymbol=qualified.localSymbol,
        )
        logger.info(
            '{"event": "CONTRACT_FETCHED", "local_symbol": "%s", "con_id": %d,'
            ' "sec_type": "FUT", "root": "%s", "expiry": "%s",'
            ' "trading_class": "%s", "tick": "%s"}',
            local_symbol, qualified.conId, qualified.symbol,
            qualified.lastTradeDateOrContractMonth, qualified.tradingClass, tick,
        )
        return {
            "con_id": qualified.conId,
            "exchange": qualified.exchange,
            "currency": qualified.currency,
            "multiplier": multiplier,
            "trading_class": qualified.tradingClass or None,
            "expiry": qualified.lastTradeDateOrContractMonth,
            "tick_size": str(tick),
            "raw": raw,
        }

    async def list_future_expiries(
        self,
        root: str,
        exchange: str,
        trading_class: str | None = None,
        currency: str = "USD",
    ) -> list[FutureExpiryCandidate]:
        """Return upcoming futures expiries for ``root``.

        One round-trip to ``reqContractDetails`` with no expiry supplied
        returns every listed contract. Expired entries are filtered,
        and results are sorted ascending by last-trade date.
        """
        await self._throttle()
        if exchange.upper() not in _EXCHANGE_TZ:
            logger.warning(
                '{"event": "UNKNOWN_EXCHANGE_TZ", "exchange": "%s", "fallback": "UTC"}',
                exchange,
            )
        future = Future(
            symbol=root,
            exchange=exchange,
            currency=currency,
            tradingClass=trading_class or "",
        )
        details = await self.__ib.reqContractDetailsAsync(future)
        today = _product_today(exchange)
        fresh = [d for d in details if _expiry_as_date(d.contract.lastTradeDateOrContractMonth) >= today]
        candidates = [_candidate_from_details(d, root) for d in fresh]
        candidates.sort(key=lambda c: c.expiry)
        return candidates

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
        ticker = self.__ib.reqMktData(contract, "", False, False)
        for _ in range(50):  # up to 5 s in 100 ms steps
            await asyncio.sleep(0.1)
            if ticker.bid and ticker.bid > 0 and ticker.ask and ticker.ask > 0:
                break
            if ticker.last and ticker.last > 0:
                break
        # Cancel the streaming subscription — we only needed one quote.
        self.__ib.cancelMktData(contract)
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
            bars = await self.__ib.reqHistoricalDataAsync(
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
        oca_group: str | None = None,
    ) -> str:
        """Place a limit order. Returns IB order ID as string.

        ``oca_group`` (One-Cancels-All): when supplied, IB cancels every
        other order with the same OCA tag once any of them fills.
        Caller pairs this LMT with a TRAIL/STP that shares the tag so
        the broker handles the exit-leg cancellation atomically.
        """
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
        if oca_group:
            order.ocaGroup = oca_group
            order.ocaType = 1  # CANCEL_WITH_BLOCK
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
        trade = self.__ib.placeOrder(contract, order)
        ib_order_id = str(trade.order.orderId)
        self.__active_trades[ib_order_id] = trade
        # Fire pre-fill snapshot callback (engine wires this to the
        # OrderLedger so the position-diff reconcile has a baseline).
        self._fire_order_placed(
            ib_order_id, symbol, contract.secType or "STK",
            int(contract.conId or 0), side, qty, order_ref or "",
        )
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
        trade = self.__ib.placeOrder(contract, order)
        ib_order_id = str(trade.order.orderId)
        self.__active_trades[ib_order_id] = trade
        self._fire_order_placed(
            ib_order_id, symbol, contract.secType or "STK",
            int(contract.conId or 0), side, qty, order_ref or "",
        )
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

    async def place_trailing_stop_order(
        self,
        con_id: int,
        symbol: str,
        side: str,
        qty: Decimal,
        *,
        trailing_percent: Decimal | None = None,
        aux_price: Decimal | None = None,
        oca_group: str | None = None,
        order_ref: str | None = None,
        tif: str = "GTC",
    ) -> str:
        """Place an IB-server-managed TRAIL stop. Returns the IB order id.

        Exactly one of ``trailing_percent`` or ``aux_price`` should be
        set. ``trailing_percent=Decimal("0.5")`` produces a 0.5%
        trailing stop; ``aux_price=Decimal("2.0")`` produces a $2 (or
        2-point) fixed-offset trail.

        ``oca_group`` links this order with a profit-target LMT under
        the same OCA tag so IB cancels one when the other fills.
        Caller should reuse the same OCA string across the linked
        orders for a single trade group. ``ocaType=1`` = cancel all
        remaining orders with block.

        TRAIL on FUT runs 24h on Globex. Caller should not place this
        on STK — IB-simulated STK trails are RTH-only.
        """
        if (trailing_percent is None) == (aux_price is None):
            raise ValueError(
                "place_trailing_stop_order: pass exactly one of "
                "trailing_percent or aux_price",
            )
        await self._throttle()
        contract = self._contract_cache.get(con_id) or Contract(
            conId=con_id, exchange="SMART", currency="USD",
        )
        order = Order()
        order.action = side.upper()
        order.totalQuantity = float(qty)
        order.orderType = "TRAIL"
        order.tif = tif
        order.outsideRth = True
        if trailing_percent is not None:
            order.trailingPercent = float(trailing_percent)
        if aux_price is not None:
            order.auxPrice = float(aux_price)
        if oca_group:
            order.ocaGroup = oca_group
            order.ocaType = 1  # CANCEL_WITH_BLOCK
        if order_ref:
            order.orderRef = order_ref
        if self._account_id:
            order.account = self._account_id

        trade = self.__ib.placeOrder(contract, order)
        ib_order_id = str(trade.order.orderId)
        self.__active_trades[ib_order_id] = trade
        self._fire_order_placed(
            ib_order_id, symbol, contract.secType or "FUT",
            int(contract.conId or 0), side, qty, order_ref or "",
        )
        logger.info(
            '{"event": "TRAIL_STOP_PLACED", "symbol": "%s", "side": "%s", '
            '"qty": "%s", "trailing_percent": %s, "aux_price": %s, '
            '"oca_group": "%s", "ib_order_id": "%s"}',
            symbol, side, qty,
            float(trailing_percent) if trailing_percent is not None else "null",
            float(aux_price) if aux_price is not None else "null",
            oca_group or "", ib_order_id,
        )
        return ib_order_id

    async def amend_order(self, ib_order_id: str, new_price: Decimal) -> None:
        """Amend an existing limit order to a new price in place."""
        await self._throttle()
        trade = self.__active_trades.get(ib_order_id)
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
        self.__ib.placeOrder(trade.contract, trade.order)
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
        trade = self.__active_trades.get(ib_order_id)
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
        self.__ib.cancelOrder(trade.order)
        logger.info('{"event": "ORDER_CANCELED", "ib_order_id": "%s"}', ib_order_id)

    async def get_order_status(self, ib_order_id: str) -> dict:
        """Get current status of an order from IB."""
        await self._throttle()
        trade = self.__active_trades.get(ib_order_id)
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
        open_trades = await self.__ib.reqAllOpenOrdersAsync()
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
        trade = self.__active_trades.get(ib_order_id)
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
                    self.__ib.cancelMktData(entry["contract"])
                except Exception as e:
                    logger.debug("cancelMktData failed during upgrade", exc_info=e)
                ticker = self.__ib.reqMktData(entry["contract"], _GENERIC_TICKS, False, False)
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
        ticker = self.__ib.reqMktData(contract, _GENERIC_TICKS, False, False)
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
                self.__ib.cancelMktData(entry["contract"])
            except Exception as e:
                logger.debug("cancelMktData failed on unsubscribe", exc_info=e)
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

    # ------------------------------------------------------------------
    # Wrapper public APIs that absorb direct ib-async access from outside
    # the wrapper. Added so external modules don't reach into ``self.__ib``
    # (which is class-level name-mangled to ``_InsyncClient__ib`` and not
    # accessible by convention) or ``self.__active_trades``.
    #
    # See ib_trader/ib/__init__.py architectural note. Import-linter
    # contract "ib_async direct imports forbidden outside the wrapper"
    # catches static drift on the import path; these methods absorb the
    # attribute-access drift on the runtime path.
    # ------------------------------------------------------------------

    async def req_positions_async(self, timeout: float = 10.0) -> None:
        """Request positions from IB. Returns when IB acknowledges. The
        position event stream pushes individual positions to whatever
        callback is registered via ``register_position_event_callback``.
        """
        try:
            await asyncio.wait_for(self.__ib.reqPositionsAsync(), timeout=timeout)
        except asyncio.TimeoutError:
            raise

    def get_raw_positions(self) -> list:
        """Return ib-async Position objects currently held at the broker.

        The returned list contains ib-async Position instances; callers
        should treat them as opaque except for documented attributes
        (``contract``, ``position``, ``avgCost``, ``account``).
        """
        return list(self.__ib.positions())

    def get_tickers(self) -> list:
        """Return ib-async Ticker objects for currently subscribed contracts.

        Snapshot of whatever ``ib.tickers()`` reports. Callers should
        treat each Ticker as opaque except for documented attributes
        (``contract``, ``bid``, ``ask``, ``last``, ``time``).
        """
        return list(self.__ib.tickers())

    def register_position_event_callback(self, callback) -> None:
        """Subscribe to ib-async ``positionEvent``. Callback receives a
        single ``Position`` object on every position change pushed by IB.
        """
        self.__ib.positionEvent += callback

    def unregister_position_event_callback(self, callback) -> None:
        """Reverse of ``register_position_event_callback``."""
        try:
            self.__ib.positionEvent -= callback
        except Exception as e:
            logger.debug(
                '{"event": "POSITION_EVENT_UNWIRE_FAILED", "error": "%s"}',
                str(e),
            )

    def register_pending_tickers_callback(self, callback) -> None:
        """Subscribe to ib-async ``pendingTickersEvent``. Callback fires
        synchronously whenever ib-async has new tick data on one or more
        of our subscribed tickers; receives a set of Ticker objects.
        """
        self.__ib.pendingTickersEvent += callback

    def unregister_pending_tickers_callback(self, callback) -> None:
        """Reverse of ``register_pending_tickers_callback``."""
        try:
            self.__ib.pendingTickersEvent -= callback
        except Exception as e:
            logger.debug(
                '{"event": "PENDING_TICKERS_UNWIRE_FAILED", "error": "%s"}',
                str(e),
            )

    async def req_historical_data_async(
        self,
        contract,
        *,
        end_date_time: str = "",
        duration_str: str,
        bar_size: str,
        what_to_show: str = "TRADES",
        use_rth: bool = False,
        format_date: int = 2,
    ) -> list:
        """Fetch historical bars for a contract via ib-async.

        Wraps ib-async's ``reqHistoricalDataAsync`` with the rate-limit
        throttle applied. ``contract`` should be a fully-qualified ib-
        async Contract (use ``qualify_contract`` first if you only have
        a symbol).
        """
        await self._throttle()
        return await self.__ib.reqHistoricalDataAsync(
            contract,
            endDateTime=end_date_time,
            durationStr=duration_str,
            barSizeSetting=bar_size,
            whatToShow=what_to_show,
            useRTH=use_rth,
            formatDate=format_date,
        )

    def get_trade_meta(self, ib_order_id: str) -> dict | None:
        """Return a snapshot of a tracked Trade's metadata, or None if
        we don't have the order in our active-trades cache.

        Returned shape:
            {
                "order_ref": str,
                "symbol": str,
                "sec_type": str,
                "con_id": int,
                "side": str,           # "BUY" or "SELL"
                "total_qty": Decimal,  # original totalQuantity (static)
                "remaining": Decimal,  # IB's current remaining (volatile)
                "contract": Contract,  # ib-async Contract object
            }

        Callers that only need a subset can pick fields off the dict.
        ``contract`` is the ib-async Contract — exposed because
        consumers (orders-open enrichment in engine/main.py) need
        ``expiry`` / ``trading_class`` / ``multiplier`` for futures
        display. Treat it as opaque-with-documented-attributes.
        """
        trade = self.__active_trades.get(str(ib_order_id))
        if trade is None:
            return None
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        order_status = getattr(trade, "orderStatus", None)

        order_ref = getattr(order, "orderRef", "") or "" if order else ""
        side = getattr(order, "action", "") or "" if order else ""
        symbol = getattr(contract, "symbol", "") or "" if contract else ""
        sec_type = getattr(contract, "secType", "STK") or "STK" if contract else "STK"
        con_id = getattr(contract, "conId", 0) or 0 if contract else 0

        total_qty = Decimal("-1")
        if order is not None:
            tq = getattr(order, "totalQuantity", None)
            if tq is not None:
                total_qty = Decimal(str(tq))

        remaining = Decimal("-1")
        if order_status is not None:
            rem = getattr(order_status, "remaining", -1)
            if rem >= 0:
                remaining = Decimal(str(rem))

        return {
            "order_ref": order_ref,
            "symbol": symbol,
            "sec_type": sec_type,
            "con_id": con_id,
            "side": side,
            "total_qty": total_qty,
            "remaining": remaining,
            "contract": contract,
        }

    def register_fill_callback(self, callback, ib_order_id: str | None = None) -> None:
        """Register a callback for fill events.

        Args:
            callback: Async callable dispatched on fill.
            ib_order_id: Scope callback to this order (auto-removed on terminal
                state).  None means fire for all orders, never auto-removed.
        """
        key = ib_order_id or "_GLOBAL"
        self._fill_callbacks.setdefault(key, []).append(callback)

    def register_order_placed_callback(self, callback) -> None:
        """Register a synchronous callback fired right after every order
        placement. Receives ``(ib_order_id, symbol, sec_type, con_id,
        side, qty, order_ref)``. Used by the engine to snapshot
        broker-side position *before* any fill events can arrive."""
        self._order_placed_callbacks.append(callback)

    def _fire_order_placed(
        self, ib_order_id: str, symbol: str, sec_type: str,
        con_id: int, side: str, qty: Decimal, order_ref: str,
    ) -> None:
        for cb in self._order_placed_callbacks:
            try:
                cb(ib_order_id, symbol, sec_type, con_id, side, qty, order_ref)
            except Exception:
                logger.exception(
                    '{"event": "ORDER_PLACED_CALLBACK_FAILED", '
                    '"ib_order_id": "%s"}',
                    ib_order_id,
                )

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
        self._previous_clean_status_map.pop(ib_order_id, None)
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
        bars = self.__ib.reqRealTimeBars(
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
                                _spawn_background(cb(bar_data))
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
                self.__ib.cancelRealTimeBars(entry["bars"])
            except Exception as e:
                logger.debug("cancelRealTimeBars failed", exc_info=e)
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
        10340, # ManualOrderIndicator not supported (delayed warning, not an error)
    })

    # Benign order-race codes — the order already terminalized by the
    # time our code tried to amend/cancel. The engine's walker and
    # close paths catch these cleanly (e.g. SMART_MARKET_AMEND_RACE),
    # so they're noise, not failures. Logged at WARNING; NOT written
    # to _order_errors so the PendingSubmit wait loop and bid/ask
    # rejection detector don't surface a spurious rejection.
    _BENIGN_ORDER_RACE_CODES: frozenset[int] = frozenset({
        104,    # Cannot modify a filled order
        105,    # Order does not match any existing order
        135,    # Can't find order with id
        201,    # Order rejected - reason: Order is already filled (amend-race)
        10147,  # OrderId is not an active order
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
        is_info = errorCode in self._INFO_CODES
        is_benign_race = errorCode in self._BENIGN_ORDER_RACE_CODES
        if ib_order_id in self.__active_trades:
            # INFO-class codes (e.g. 10340 ManualOrderIndicator) still
            # reference an active order but must NOT be treated as a
            # rejection — log at INFO and skip the _order_errors write
            # so downstream code doesn't surface a false failure.
            if is_info:
                logger.info(
                    '{"event": "IB_NOTICE", "ib_order_id": "%s", "code": %d, "msg": "%s"}',
                    ib_order_id, errorCode, errorString,
                )
            elif is_benign_race:
                # Expected race when our walker/close path amends or
                # cancels an order that IB just terminalized. The engine
                # side handles these via SMART_MARKET_AMEND_RACE (or the
                # close-path guard) and returns cleanly — this is a
                # diagnostic, not an error.
                logger.warning(
                    '{"event": "IB_ORDER_RACE", "ib_order_id": "%s", '
                    '"code": %d, "msg": "%s"}',
                    ib_order_id, errorCode, errorString,
                )
            else:
                logger.error(
                    '{"event": "IB_ORDER_ERROR", "ib_order_id": "%s", "code": %d, "msg": "%s"}',
                    ib_order_id, errorCode, errorString,
                )
                self._order_errors[ib_order_id] = f"[{errorCode}] {errorString}"
        elif is_info:
            logger.info(
                '{"event": "IB_NOTICE", "reqId": %d, "code": %d, "msg": "%s"}',
                reqId, errorCode, errorString,
            )
        else:
            logger.warning(
                '{"event": "IB_ERROR", "reqId": %d, "code": %d, "msg": "%s"}',
                reqId, errorCode, errorString,
            )

    def _on_connected(self) -> None:
        """Fire the engine's connect callback (if registered). Used to
        auto-resolve the IB_GATEWAY_DISCONNECTED alert so the UI clears
        the CATASTROPHIC banner the moment the Gateway comes back."""
        if self._on_connected_callback is not None:
            try:
                self._on_connected_callback()
            except Exception:
                logger.exception('{"event": "IB_CONNECT_CALLBACK_FAILED"}')

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

    # Verify-cancel scope used to be gated on a known-error-code allowlist
    # (originally just 462). Now generalised: every Cancelled / ApiCancelled
    # the wrapper sees is verified against IB's open-orders list before
    # propagating to callbacks (see ``_on_order_status``). The allowlist
    # was a reactive trust model — only suppress when we'd already
    # observed the false-cancel pattern. The wrapper-as-gate architecture
    # (no allowlist, always verify) closes the door on unknown error
    # codes that produce the same false-cancel shape.

    def _get_order_lock(self, ib_order_id: str) -> asyncio.Lock:
        """Return the per-order asyncio.Lock for ``ib_order_id``, creating
        it lazily on first use. Lazy creation avoids loop-binding issues
        during construction and only pays for orders that actually see
        traffic."""
        lock = self._order_locks.get(ib_order_id)
        if lock is None:
            lock = asyncio.Lock()
            self._order_locks[ib_order_id] = lock
        return lock

    async def _dispatch_ordered(
        self, ib_order_id: str, cb, *args,
    ) -> None:
        """Run ``cb(*args)`` under the per-order lock.

        Every IB-event callback (fill / status / commission) for a given
        ``ib_order_id`` funnels through this wrapper, so they execute
        strictly in the order they were scheduled — which is IB's
        dispatch order, since ib_async invokes our sync event handlers
        in arrival order and each handler wraps callbacks via
        ``_spawn_background`` immediately. Without this lock, a handler
        that yields (e.g. ``await order_writer.add(...)``) can be
        overtaken by the next event's handler, letting a status=Filled
        terminalize the ledger before a still-queued fill has
        accumulated — which left orphan positions in IB in live trading.
        """
        async with self._get_order_lock(ib_order_id):
            await cb(*args)

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

        # Dispatch to order-specific callbacks, then global callbacks.
        # Every dispatch for this ib_order_id funnels through the
        # per-order lock so fills/statuses for the same order run in
        # IB's arrival order even when individual handlers yield.
        for cb in self._fill_callbacks.get(ib_order_id, []):
            _spawn_background(self._dispatch_ordered(
                ib_order_id, cb, ib_order_id, qty_filled, avg_price, commission,
            ))
        for cb in self._fill_callbacks.get("_GLOBAL", []):
            _spawn_background(self._dispatch_ordered(
                ib_order_id, cb, ib_order_id, qty_filled, avg_price, commission,
            ))

    def _on_commission_report(self, trade, fill, report) -> None:
        """Handle CommissionReport from IB (fires shortly AFTER execDetails).

        IB delivers commission info on a separate event because the
        number is computed server-side after the fill is booked. If
        ``execDetailsEvent`` already had ``fill.commissionReport`` set,
        our ``_on_exec_details`` captured the commission inline and the
        handlers here are "extra" — dedup via ``exec_id`` to avoid
        double-posting.
        """
        try:
            ib_order_id = str(trade.order.orderId)
        except Exception:
            return
        exec_id = getattr(fill.execution, "execId", "") or ""
        commission_raw = getattr(report, "commission", 0) or 0
        try:
            commission = Decimal(str(commission_raw))
        except Exception:
            return
        # CommissionReport.realizedPNL is the position's cumulative realized
        # P&L at this execution, set by IB. Opening fills carry a sentinel
        # ~1.7976931348623157e308 ("not applicable" — Python: sys.float_info.max).
        # Filter that and pass real values through so the engine can record
        # IB-authoritative P&L on closes (GH #48 follow-up).
        realized_pnl: Decimal | None = None
        pnl_raw = getattr(report, "realizedPNL", None)
        if pnl_raw is not None:
            try:
                pnl_f = float(pnl_raw)
                if abs(pnl_f) < 1e15:
                    realized_pnl = Decimal(str(pnl_raw))
            except (ValueError, TypeError):
                pass
        # Dedup. On reconnect IB sometimes redelivers the same report.
        if exec_id and exec_id in self._seen_commission_execs:
            logger.debug(
                '{"event": "IB_COMMISSION_REPORT_DEDUPED", '
                '"ib_order_id": "%s", "exec_id": "%s"}',
                ib_order_id, exec_id,
            )
            return
        if exec_id:
            self._seen_commission_execs.add(exec_id)

        logger.info(
            '{"event": "IB_COMMISSION_REPORT", "ib_order_id": "%s", '
            '"exec_id": "%s", "commission": "%s", "realized_pnl": "%s"}',
            ib_order_id, exec_id, commission,
            realized_pnl if realized_pnl is not None else "",
        )

        # Fire per-order callbacks then global. Handlers do DB updates
        # only — never gate anything waiting for commission. Funnel
        # through the per-order lock so commission writes stay ordered
        # relative to any still-in-flight fill/status handlers for the
        # same ib_order_id.
        for cb in self._commission_callbacks.get(ib_order_id, []):
            _spawn_background(self._dispatch_ordered(
                ib_order_id, cb, ib_order_id, exec_id, commission, realized_pnl,
            ))
        for cb in self._commission_callbacks.get("_GLOBAL", []):
            _spawn_background(self._dispatch_ordered(
                ib_order_id, cb, ib_order_id, exec_id, commission, realized_pnl,
            ))

    def register_commission_callback(
        self, callback, ib_order_id: str | None = None,
    ) -> None:
        """Register a callback for CommissionReport events.

        Args:
            callback: async callable taking (ib_order_id, exec_id, commission).
            ib_order_id: Scope to a single order, or None for the _GLOBAL
                bucket that fires for every order. Per-order callbacks are
                NOT auto-removed on terminal — commission can arrive after
                the order is marked Filled.
        """
        key = ib_order_id or "_GLOBAL"
        self._commission_callbacks.setdefault(key, []).append(callback)

    def _previous_clean_status(self, ib_order_id: str) -> str:
        """Return the last non-Cancelled status this wrapper saw for the
        order. Used to patch trade.orderStatus.status during verify-pending
        so direct readers (get_order_status, walker polls) don't see
        ib_async's mutated 'Cancelled' field.

        Default of 'Submitted' is the safe lower bound: ib_async only
        synthesizes Cancelled events for orders it tracks as live, and
        such orders are by definition past PendingSubmit / PreSubmitted.
        """
        return self._previous_clean_status_map.get(ib_order_id, "Submitted")

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
        perm_id = trade.order.permId
        filled = trade.orderStatus.filled

        logger.debug(
            '{"event": "IB_STATUS_CHANGE", "ib_order_id": "%s", '
            '"symbol": "%s", "status": "%s", "filled": %s, '
            '"remaining": %s, "why_held": "%s", "perm_id": %s}',
            ib_order_id, trade.contract.symbol, status,
            filled, remaining, why_held, perm_id,
        )

        # IB sometimes fires a spurious `Cancelled` status with perm_id=0 when
        # it rejects some order attribute (e.g. unsupported TIF combined with
        # a destination) and re-routes the order with a corrected attribute.
        # The resulting sequence is:
        #   Cancelled (perm_id=0, filled=0, remaining=0, ValidationError)
        #   → PreSubmitted → Submitted → Filled (with a real perm_id)
        # Treating that first Cancelled as terminal removes the per-order
        # fill callback, which then never fires on the real fill. Skip the
        # auto-cleanup when perm_id is 0 and nothing filled — the order has
        # not actually reached the exchange yet.
        is_prerouting_cancel = (
            status == "Cancelled"
            and perm_id == 0
            and (filled or 0) == 0
        )

        if is_prerouting_cancel:
            # Swallow — the order has not reached the exchange yet and will be
            # reissued momentarily. Propagating a "Cancelled" here would make
            # status listeners mark the order dead.
            logger.debug(
                '{"event": "IB_PREROUTING_CANCEL_IGNORED", "ib_order_id": "%s"}',
                ib_order_id,
            )
            return

        # Universal verify-cancel gate. ib_async synthesizes a Cancelled
        # status (and mutates trade.orderStatus.status="Cancelled") from
        # any non-warning error on a live trade — including modify-
        # rejection errors where IB's actual behavior is "modify failed,
        # order still live" (wrapper.py:1657-1668; ib_insync issue #502;
        # GH #48). The wrapper acts as the gate: every Cancelled goes to
        # IB authoritatively via reqOpenOrdersAsync. If still open at IB,
        # we suppress callback dispatch AND patch trade.orderStatus.status
        # back to the last clean value so direct readers (get_order_status,
        # walker polls) don't see ib_async's mutated field. ib_async will
        # overwrite the patched value on the next real wire status; if
        # verify confirms the cancel was real, we restore "Cancelled" and
        # dispatch.
        if status in ("Cancelled", "ApiCancelled"):
            err_code = (
                getattr(trade.log[-1], "errorCode", None)
                if trade.log else None
            )
            patched_to = self._previous_clean_status(ib_order_id)
            trade.orderStatus.status = patched_to
            logger.info(
                '{"event": "IB_CANCEL_VERIFY_DEFERRED", '
                '"ib_order_id": "%s", "code": %s, "patched_to": "%s"}',
                ib_order_id, err_code, patched_to,
            )
            _spawn_background(self._verify_cancel(trade, ib_order_id, err_code))
            return

        # Non-Cancelled status: record it as the last clean value for
        # this order, so a future synthesized Cancelled can patch back
        # to the right "alive" state (PreSubmitted vs Submitted vs ...).
        self._previous_clean_status_map[ib_order_id] = status

        self._dispatch_status_to_callbacks(trade, ib_order_id, status)

    def _dispatch_status_to_callbacks(
        self, trade: Trade, ib_order_id: str, status: str,
    ) -> None:
        """Dispatch an order-status update to registered callbacks.

        Funnel through the per-order lock — critical here, because a terminal
        status that overtakes a still-queued fill would terminalize the
        ledger with under-counted fills and leave orphan position in IB
        (PSQ incident, 2026-04-21).
        """
        for cb in self._status_callbacks.get(ib_order_id, []):
            _spawn_background(self._dispatch_ordered(
                ib_order_id, cb, ib_order_id, status,
            ))
        for cb in self._status_callbacks.get("_GLOBAL", []):
            _spawn_background(self._dispatch_ordered(
                ib_order_id, cb, ib_order_id, status,
            ))

        # Auto-cleanup after terminal status dispatch.
        if status in self._TERMINAL_STATUSES:
            self.unregister_callbacks(ib_order_id)

    async def _verify_cancel(
        self, trade: Trade, ib_order_id: str, err_code: int | None,
    ) -> None:
        """Verify a suspect Cancelled status against IB's open-orders list.

        ib_async manufactures Cancelled status events for some modify-rejection
        errors that leave the underlying order live on the exchange (see
        ``_on_order_status``). This routine asks IB authoritatively whether
        the order is still open. If yes, the synthetic Cancelled is suppressed
        — the trade.orderStatus.status was already patched back to the
        previous clean value in _on_order_status before this verify was
        scheduled. If no, we restore "Cancelled" on the Trade field and
        dispatch as terminal.

        On query failure we default to suppress: a missed cancel surfaces via
        the engine's existing 120s active+passive timeout with no orphan
        position, while a missed fill leaves money exposed (GH #48).
        """
        try:
            await self._throttle()
            open_trades = await self.__ib.reqOpenOrdersAsync()
            still_open = any(
                str(t.order.orderId) == ib_order_id for t in open_trades
            )
        except Exception:
            logger.exception(
                '{"event": "IB_CANCEL_VERIFY_FAILED", '
                '"ib_order_id": "%s", "code": %s, "default": "suppress"}',
                ib_order_id, err_code,
            )
            still_open = True

        if still_open:
            logger.warning(
                '{"event": "IB_CANCEL_SUPPRESSED_OPEN_AT_IB", '
                '"ib_order_id": "%s", "code": %s}',
                ib_order_id, err_code,
            )
            return  # patch stays — ib_async will overwrite on next real status

        # Race: the real terminal may have landed via _on_order_status during
        # the IB round-trip. Filled goes through this same method and
        # dispatches synchronously, so trade.orderStatus.status reflects it.
        # Re-check before propagating a stale Cancelled that would re-fire
        # callbacks on an already-Filled order.
        current = trade.orderStatus.status
        if current == "Filled":
            logger.info(
                '{"event": "IB_CANCEL_SUPERSEDED_BY_FILL", '
                '"ib_order_id": "%s", "code": %s}',
                ib_order_id, err_code,
            )
            return

        # Verified: cancel was real. Restore "Cancelled" on the Trade
        # field (we patched it to the previous clean status earlier) so
        # direct readers see the truth, then dispatch as terminal.
        trade.orderStatus.status = "Cancelled"
        logger.info(
            '{"event": "IB_CANCEL_VERIFIED_TERMINAL", '
            '"ib_order_id": "%s", "code": %s}',
            ib_order_id, err_code,
        )
        self._dispatch_status_to_callbacks(trade, ib_order_id, "Cancelled")
