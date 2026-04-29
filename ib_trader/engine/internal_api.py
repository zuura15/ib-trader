"""Internal HTTP API for the engine process.

All command producers (bots, API, REPL) submit orders through this API.
Replaces the pending_commands SQLite polling pattern.

Runs as a uvicorn server inside the engine process on a configurable port
(default 8081). Not exposed to the browser — the public API server on
port 8000 forwards to this when needed.
"""
import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Module-level reference to AppContext, set by start_internal_api().
# Typed as Any since AppContext is not imported here to avoid cycles.
_ctx: "Any | None" = None


_VALID_SIDES = {"BUY", "SELL"}
# Derived from the Strategy enum so adding a new value there
# automatically extends the whitelist + the Pydantic description below.
# Avoids the drift bug where, e.g., bots/strategy.py had a stale
# "mid, market, limit" inline comment missing bid/ask.
from ib_trader.repl.commands import Strategy
_VALID_ORDER_TYPES: frozenset[str] = frozenset(s.value for s in Strategy)
_VALID_ORDER_TYPES_DOC: str = ", ".join(sorted(_VALID_ORDER_TYPES))


class OrderRequest(BaseModel):
    """Request body for placing an order.

    Epic 1 Phase 3: widened with explicit sec-type fields
    (``security_type``/``expiry``/``trading_class``/``exchange``). Legacy
    callers that omit them default to STK and the engine treats the
    payload identically to pre-Epic-1 (schema_version=1 or absent). A
    producer that knows about futures MUST emit schema_version=2 with
    the sec-type fields populated — silent STK fallback is only for
    legacy producers.
    """

    symbol: str
    side: str = Field(description="BUY or SELL")
    qty: str = Field(description="Order quantity as string (Decimal-safe)")
    order_type: str = Field(
        default=Strategy.MID.value,
        description=f"Order strategy: one of {{{_VALID_ORDER_TYPES_DOC}}}",
    )
    price: Optional[str] = Field(default=None, description="Limit price (required for limit orders)")
    bot_ref: Optional[str] = Field(default=None, description="Bot reference ID for orderRef tagging")
    serial: Optional[int] = Field(default=None, description="Trade serial number")
    profit: Optional[str] = Field(default=None, description="Profit target in dollars")
    stop_loss: Optional[str] = Field(default=None, description="Stop loss in dollars")
    cmd_id: Optional[str] = Field(default=None, description="Caller-supplied command id; keys the Redis live-output stream")
    # Epic 1 additions:
    security_type: str = Field(default="STK", description="STK / ETF / FUT / OPT")
    expiry: Optional[str] = Field(default=None, description="YYYYMM (CLI) or YYYYMMDD (post-qualify) for FUT/OPT")
    trading_class: Optional[str] = Field(default=None, description="IB trading-class disambiguator (ES vs MES)")
    exchange: Optional[str] = Field(default=None, description="Primary exchange; defaults per sec_type")
    schema_version: int = Field(default=1, description="1 = legacy STK-only; 2 = sec-type aware")
    # Trailing stop (FUT only). Caller sends one of these — `trail_percent`
    # for ``trailingPercent`` semantics, `trail_amount` for fixed
    # ``auxPrice``. Both None means no trailing stop. Mirrors the
    # ``--trail 0.5%`` / ``--trail 2.0`` CLI flag.
    trail_percent: Optional[str] = Field(default=None, description="Trailing stop percent (e.g. '0.5' for 0.5%)")
    trail_amount: Optional[str] = Field(default=None, description="Trailing stop fixed offset (instrument points)")


class OrderResponse(BaseModel):
    """Response after placing an order."""

    ib_order_id: str
    serial: int
    order_ref: Optional[str] = None
    status: str
    output: Optional[str] = None
    cmd_id: Optional[str] = None


class CloseRequest(BaseModel):
    """Request body for closing a position."""

    serial: int
    strategy: str = "market"
    profit: Optional[str] = None
    bot_ref: Optional[str] = Field(default=None, description="Bot reference ID for orderRef tagging")
    cmd_id: Optional[str] = Field(default=None, description="Caller-supplied command id; keys the Redis live-output stream")


class SubscribeBarsRequest(BaseModel):
    """Request body for subscribing to realtime bars."""

    symbol: str
    interval: str = "5s"


class WarmupBarsRequest(BaseModel):
    """Request body for prefetching historical bars to the Redis bar stream."""

    symbol: str
    duration_seconds: int = 7200


class UnsubscribeBarsRequest(BaseModel):
    """Request body for unsubscribing from realtime bars."""

    symbol: str


class HealthResponse(BaseModel):
    """Engine health check response."""

    status: str
    ib_connected: bool
    pid: int


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """FastAPI lifespan — catch CancelledError for clean shutdown."""
    try:
        yield
    except asyncio.CancelledError:
        pass


app = FastAPI(title="IB Trader Engine Internal API", lifespan=_lifespan)


@app.post("/engine/orders", response_model=OrderResponse)
async def place_order(req: OrderRequest):
    """Place an order through the engine.

    The engine places the order with IB, tags it with orderRef if bot_ref
    is provided, and returns the result synchronously.
    """
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    from ib_trader.engine.service import execute_single_command

    # Validate inputs
    side_upper = req.side.upper()
    if side_upper not in _VALID_SIDES:
        raise HTTPException(status_code=422, detail=f"Invalid side: {req.side!r}. Must be BUY or SELL.")
    if req.order_type not in _VALID_ORDER_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid order_type: {req.order_type!r}. Must be one of {_VALID_ORDER_TYPES}.")

    # Build command text from request. Sec-type metadata flows via
    # explicit ``--sec-type`` / ``--expiry`` / ``--trading-class`` /
    # ``--exchange`` flags (parser accepts them; the CLI shorthand
    # produces the same fields on BuyCommand / SellCommand).
    side_cmd = "buy" if side_upper == "BUY" else "sell"
    cmd_text = f"{side_cmd} {req.symbol} {req.qty} {req.order_type}"
    if req.profit:
        cmd_text += f" --profit {req.profit}"
    if req.stop_loss:
        cmd_text += f" --stop-loss {req.stop_loss}"
    if req.price:
        cmd_text += f" --price {req.price}"
    sec_type_u = (req.security_type or "STK").upper()
    if sec_type_u != "STK":
        cmd_text += f" --sec-type {sec_type_u}"
    if req.expiry:
        cmd_text += f" --expiry {req.expiry}"
    if req.trading_class:
        cmd_text += f" --trading-class {req.trading_class}"
    if req.exchange:
        cmd_text += f" --exchange {req.exchange}"
    if req.trail_percent:
        cmd_text += f" --trail {req.trail_percent}%"
    elif req.trail_amount:
        cmd_text += f" --trail {req.trail_amount}"

    # Pass bot_ref through to execute_single_command — the engine encodes
    # orderRef AFTER allocating the real trade serial (not the bot's stale one).
    try:
        result = await execute_single_command(
            _ctx, cmd_text,
            source=f"bot:{req.bot_ref}" if req.bot_ref else "api",
            bot_ref=req.bot_ref,
            cmd_id=req.cmd_id,
        )
        return OrderResponse(
            ib_order_id=result.get("ib_order_id", ""),
            serial=result.get("serial", 0),
            order_ref=result.get("order_ref"),
            status=result.get("status", "SUBMITTED"),
            output=result.get("output"),
            cmd_id=result.get("cmd_id"),
        )
    except Exception as e:
        logger.exception('{"event": "INTERNAL_API_ORDER_FAILED"}')
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/engine/close")
async def close_position(req: CloseRequest):
    """Close a position by trade serial."""
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    from ib_trader.engine.service import execute_single_command

    cmd_text = f"close {req.serial} {req.strategy}"
    if req.profit:
        cmd_text += f" {req.profit}"

    try:
        result = await execute_single_command(
            _ctx, cmd_text,
            source=f"bot:{req.bot_ref}" if req.bot_ref else "api",
            bot_ref=req.bot_ref,
            cmd_id=req.cmd_id,
        )
        return {"status": "ok", "output": result.get("output"), "result": result, "cmd_id": result.get("cmd_id")}
    except Exception as e:
        logger.exception('{"event": "INTERNAL_API_CLOSE_FAILED"}')
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/engine/cancel-by-symbol")
async def cancel_by_symbol(req: dict):
    """Cancel every open IB order for a given symbol.

    Used by tests / cleanup tooling so we never carry orphan working
    orders across runs (NYSE self-trade prevention will block new BUYs
    against any resting SELL on the same symbol).
    """
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    symbol = (req.get("symbol") or "").upper()
    if not symbol:
        raise HTTPException(status_code=422, detail="symbol required")
    open_orders = await _ctx.ib.get_open_orders()
    targets = [o for o in open_orders if (o.get("symbol") or "").upper() == symbol]
    cancelled: list[str] = []
    for order in targets:
        oid = str(order["ib_order_id"])
        try:
            await _ctx.ib.cancel_order(oid)
            cancelled.append(oid)
        except Exception:
            logger.exception('{"event": "CANCEL_BY_SYMBOL_FAILED", "ib_order_id": "%s"}', oid)
    logger.info(
        '{"event": "CANCEL_BY_SYMBOL", "symbol": "%s", "cancelled": %d}',
        symbol, len(cancelled),
    )
    return {"symbol": symbol, "cancelled": cancelled, "count": len(cancelled)}


@app.post("/engine/subscribe-bars")
async def subscribe_bars(req: SubscribeBarsRequest):
    """Subscribe to realtime bars for a symbol.

    Used by the bot runner during warmup. Returns synchronously when
    the subscription is established.
    """
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    try:
        info = await _ctx.ib.qualify_contract(req.symbol)
        con_id = info["con_id"]
        # Wire a Redis publisher so live bars flow to bar:{symbol}:5s where
        # bots XREAD them. Without this callback, IB receives bars but they
        # land nowhere.
        callback = None
        if _ctx.redis is not None:
            from ib_trader.engine.main import _make_bar_publisher
            callback = _make_bar_publisher(_ctx.redis, req.symbol)
        await _ctx.ib.subscribe_realtime_bars(con_id, req.symbol, callback=callback)
        await _ctx.ib.subscribe_market_data(con_id, req.symbol)
        return {"status": "subscribed", "symbol": req.symbol, "con_id": con_id}
    except Exception as e:
        logger.exception('{"event": "SUBSCRIBE_BARS_FAILED", "symbol": "%s"}', req.symbol)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/engine/warmup-bars")
async def warmup_bars(req: WarmupBarsRequest):
    """Publish historical 5s bars to the Redis bar stream for bot warmup.

    Bots consume bar:{symbol}:5s from "0" during warmup to prefill their
    aggregator. The live reqRealTimeBars callback writes to the same stream
    for ongoing events.
    """
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    from ib_trader.engine.service import _handle_warmup_bars

    try:
        output = await _handle_warmup_bars(req.symbol, req.duration_seconds, _ctx)
        return {"status": "ok", "symbol": req.symbol, "output": output}
    except Exception as e:
        logger.exception('{"event": "WARMUP_BARS_FAILED", "symbol": "%s"}', req.symbol)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/engine/unsubscribe-bars")
async def unsubscribe_bars(req: UnsubscribeBarsRequest):
    """Unsubscribe from live bars and streaming quotes for a symbol."""
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    try:
        info = await _ctx.ib.qualify_contract(req.symbol)
        con_id = info["con_id"]
        await _ctx.ib.unsubscribe_realtime_bars(con_id)
        await _ctx.ib.unsubscribe_market_data(con_id)
        return {"status": "unsubscribed", "symbol": req.symbol, "con_id": con_id}
    except Exception as e:
        logger.exception('{"event": "UNSUBSCRIBE_BARS_FAILED", "symbol": "%s"}', req.symbol)
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.post("/engine/reload-watchlist")
async def reload_watchlist():
    """Reload watchlist from config/watchlist.yaml and subscribe to new symbols.

    Replaces the old 5-second polling loop that re-read the YAML file.
    """
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")

    from ib_trader.config.loader import load_watchlist

    try:
        symbols = load_watchlist("config/watchlist.yaml")
        subscribed = []
        for sym in symbols:
            try:
                info = await _ctx.ib.qualify_contract(sym)
                await _ctx.ib.subscribe_market_data(info["con_id"], sym)
                subscribed.append(sym)
            except Exception:
                logger.warning('{"event": "WATCHLIST_QUALIFY_FAILED", "symbol": "%s"}', sym)
        return {"status": "reloaded", "symbols": subscribed}
    except Exception as e:
        logger.exception('{"event": "RELOAD_WATCHLIST_FAILED"}')
        raise HTTPException(status_code=500, detail=str(e)) from e


@app.get("/engine/instruments/expiries")
async def list_future_expiries(root: str, exchange: str = "CME", trading_class: str | None = None):
    """Return upcoming futures expiries for ``root`` (engine direct IB call).

    Public API proxies here via ``/api/instruments/expiries``.
    """
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    try:
        candidates = await _ctx.ib.list_future_expiries(
            root=root, exchange=exchange, trading_class=trading_class,
        )
    except AttributeError as e:
        # Legacy IBClient without list_future_expiries — should not happen
        # once Phase 1 lands, but fail explicitly rather than silently.
        raise HTTPException(
            status_code=501, detail="broker client lacks list_future_expiries",
        ) from e
    except Exception as e:
        logger.exception('{"event": "LIST_EXPIRIES_FAILED", "root": "%s"}', root)
        raise HTTPException(status_code=502, detail=f"IB discovery failed: {e}") from e

    from ib_trader.utils.symbol import format_display_symbol
    return [
        {
            "con_id": c.con_id,
            "root": c.root,
            "expiry": c.expiry,
            "trading_class": c.trading_class,
            "exchange": c.exchange,
            "multiplier": str(c.multiplier),
            "tick_size": str(c.tick_size),
            "display_symbol": format_display_symbol(c.root, "FUT", c.expiry),
        }
        for c in candidates
    ]


@app.get("/engine/positions")
async def get_positions():
    """Return current IB positions from the engine's in-memory cache.

    The cache is refreshed by positionEvent callbacks (real-time) and a
    30s poll loop (fallback). No Redis — the API proxies here directly.
    """
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    return _ctx.positions_cache


@app.get("/engine/positions/refresh")
async def refresh_position(symbol: str):
    """Force-refresh IB positions via reqPositionsAsync, then read the
    cache for the given symbol.

    Differs from ``/engine/positions`` (which serves the cached push
    state without forcing a refresh). Used by bots as a tiebreaker when
    a positionEvent push disagrees with their own state — the pull goes
    against IB's authoritative position book, so the partial-fill race
    that drove GH #85 cannot be in flight inside the response.
    """
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    try:
        await asyncio.wait_for(_ctx.ib.reqPositionsAsync(), timeout=10)
    except asyncio.TimeoutError as e:
        raise HTTPException(status_code=504, detail="reqPositions timed out") from e
    for p in _ctx.positions_cache:
        if p.get("symbol") == symbol:
            return {"symbol": symbol, "qty": p.get("quantity", "0")}
    return {"symbol": symbol, "qty": "0"}


_HISTORY_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
_HISTORY_TTL_SECONDS = 30.0


@app.get("/engine/history")
async def get_history(
    con_id: int | None = None,
    symbol: str | None = None,
    sec_type: str = "STK",
    hours: int = 24,
    bar_size: str = "1 min",
):
    """Return historical close-price bars for charting.

    Identifier resolution:
      - ``con_id`` is preferred — routes through the contract cache.
      - ``symbol`` (with optional ``sec_type``) is the fallback for
        watchlist clicks where we haven't qualified yet. Triggers
        ``qualify_contract``; result is cached by the wrapper.

    Tiny TTL cache (30s) keyed on (con_id, hours, bar_size) dedupes
    pane refreshes across multiple browser tabs and respects IB's
    2000 req/10min historical-data ceiling.
    """
    import time

    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    if con_id is None and not symbol:
        raise HTTPException(status_code=400, detail="con_id or symbol is required")

    if con_id is None:
        try:
            qualified = await _ctx.ib.qualify_contract(symbol, sec_type=sec_type)
        except Exception as e:
            logger.exception(
                '{"event": "HISTORY_QUALIFY_FAILED", "symbol": "%s", "sec_type": "%s"}',
                symbol, sec_type,
            )
            raise HTTPException(status_code=502, detail=f"qualify_contract failed: {e}") from e
        con_id = int(qualified.get("con_id") or 0)
        if not con_id:
            raise HTTPException(status_code=502, detail="qualify_contract returned no con_id")

    cache_key = (int(con_id), int(hours), bar_size)
    now = time.monotonic()
    cached = _HISTORY_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _HISTORY_TTL_SECONDS:
        return cached[1]

    contract = _ctx.ib._contract_cache.get(int(con_id))
    if contract is None:
        raise HTTPException(
            status_code=409,
            detail=f"contract {con_id} not in cache; qualify it first",
        )

    # IB's durationStr only accepts S/D/W/M/Y — no H. Convert to seconds
    # (works for the 1–24 h range we care about; ~86400 S for 24 h).
    duration_str = f"{int(hours) * 3600} S"
    # BID_ASK (not TRADES/MIDPOINT) maximizes overnight coverage. IB
    # tapes book updates whenever any market maker is quoting, even
    # during quiet AH hours where TRADES (no print) and MIDPOINT (no
    # quote change) both return nothing. Per IB docs, BID_ASK bar
    # fields are repurposed:
    #   open  = average bid in the interval
    #   high  = max ask in the interval
    #   low   = min bid in the interval
    #   close = average ask in the interval
    # We remap to a single mid for the chart line below.
    try:
        bars = await _ctx.ib.req_historical_data_async(
            contract,
            duration_str=duration_str,
            bar_size=bar_size,
            what_to_show="BID_ASK",
            use_rth=False,
            format_date=2,
        )
    except Exception as e:
        logger.exception('{"event": "HISTORY_FETCH_FAILED", "con_id": %d}', con_id)
        raise HTTPException(status_code=502, detail=f"historical data failed: {e}") from e

    out: list[dict] = []
    for bar in bars or []:
        ts = getattr(bar, "date", None)
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        avg_bid = float(bar.open)
        max_ask = float(bar.high)
        min_bid = float(bar.low)
        avg_ask = float(bar.close)
        # mid = (avg_bid + avg_ask) / 2 if both sides quoted, else
        # whichever side is non-zero (illiquid hours can have one-sided
        # quotes).
        if avg_bid > 0 and avg_ask > 0:
            mid = (avg_bid + avg_ask) / 2
        elif avg_ask > 0:
            mid = avg_ask
        else:
            mid = avg_bid
        out.append({
            "ts": ts_str,
            "open": avg_bid,
            "high": max_ask,
            "low": min_bid,
            "close": mid,
            "volume": 0,
        })

    _HISTORY_CACHE[cache_key] = (now, out)
    # Drop stale cache entries opportunistically so the dict doesn't grow
    # unbounded across the daemon's lifetime.
    if len(_HISTORY_CACHE) > 256:
        for k in [k for k, (t, _) in _HISTORY_CACHE.items() if (now - t) > _HISTORY_TTL_SECONDS]:
            _HISTORY_CACHE.pop(k, None)
    return out


@app.get("/engine/health", response_model=HealthResponse)
async def health():
    """Engine health check."""
    import os
    if _ctx is None:
        return HealthResponse(status="initializing", ib_connected=False, pid=os.getpid())

    ib_connected = False
    if hasattr(_ctx.ib, "is_connected"):
        ib_connected = _ctx.ib.is_connected()

    return HealthResponse(
        status="ok" if ib_connected else "degraded",
        ib_connected=ib_connected,
        pid=os.getpid(),
    )


def set_context(ctx) -> None:
    """Set the AppContext for the internal API handlers."""
    global _ctx
    _ctx = ctx


async def start_internal_api(ctx, port: int = 8081) -> asyncio.Task:
    """Start the internal API server as a background asyncio task.

    Args:
        ctx: AppContext instance.
        port: Port to bind (default 8081).

    Returns:
        The asyncio task running the server.
    """
    import uvicorn

    set_context(ctx)
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    logger.info('{"event": "INTERNAL_API_STARTED", "port": %d}', port)
    return task
