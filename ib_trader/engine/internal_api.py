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
    # Optional sec_type so the engine can qualify FUT/FOP/OPT bots
    # correctly. Bots without this field land on the legacy default
    # ("STK") — exactly what was happening before chart_signal added
    # futures support and silently broke on MGCM6/MES/MNQ/MCL.
    sec_type: Optional[str] = None
    expiry: Optional[str] = None
    trading_class: Optional[str] = None


class WarmupBarsRequest(BaseModel):
    """Request body for prefetching historical bars to the Redis bar stream."""

    symbol: str
    duration_seconds: int = 7200
    sec_type: Optional[str] = None
    expiry: Optional[str] = None
    trading_class: Optional[str] = None


class UnsubscribeBarsRequest(BaseModel):
    """Request body for unsubscribing from realtime bars."""

    symbol: str
    sec_type: Optional[str] = None
    expiry: Optional[str] = None
    trading_class: Optional[str] = None


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
        qualify_kwargs: dict = {}
        if req.sec_type:
            qualify_kwargs["sec_type"] = req.sec_type
        if req.expiry:
            qualify_kwargs["expiry"] = req.expiry
        if req.trading_class:
            qualify_kwargs["trading_class"] = req.trading_class
        info = await _ctx.ib.qualify_contract(req.symbol, **qualify_kwargs)
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
        output = await _handle_warmup_bars(
            req.symbol, req.duration_seconds, _ctx,
            sec_type=req.sec_type, expiry=req.expiry,
            trading_class=req.trading_class,
        )
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
        qualify_kwargs: dict = {}
        if req.sec_type:
            qualify_kwargs["sec_type"] = req.sec_type
        if req.expiry:
            qualify_kwargs["expiry"] = req.expiry
        if req.trading_class:
            qualify_kwargs["trading_class"] = req.trading_class
        info = await _ctx.ib.qualify_contract(req.symbol, **qualify_kwargs)
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
    from ib_trader.repl.commands import _is_futures_local_symbol

    try:
        symbols = load_watchlist("config/watchlist.yaml")
        subscribed = []
        for sym in symbols:
            try:
                # Same FUT-detection as the startup subscribe loop —
                # IB-paste localSymbol form (``MESM6``) routes through
                # the FUT qualify path; everything else stays STK.
                sec_type = "FUT" if _is_futures_local_symbol(sym) else "STK"
                info = await _ctx.ib.qualify_contract(sym, sec_type=sec_type)
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
    # All IB calls must go through the snake_case wrapper (per
    # CLAUDE.md / ADR on ib/base.py). ``reqPositionsAsync`` is the
    # raw ib-async camelCase; calling it directly bypasses the
    # wrapper's timeout + rate-limit + audit hooks AND raises
    # ``AttributeError`` because the wrapper doesn't expose
    # camelCase passthroughs.
    try:
        await _ctx.ib.req_positions_async(timeout=10)
    except asyncio.TimeoutError as e:
        raise HTTPException(status_code=504, detail="reqPositions timed out") from e
    for p in _ctx.positions_cache:
        if p.get("symbol") == symbol:
            return {"symbol": symbol, "qty": p.get("quantity", "0")}
    return {"symbol": symbol, "qty": "0"}


_HISTORY_CACHE: dict[tuple, tuple[float, list[dict]]] = {}
# 5-minute TTL. Was 30 s, but the chart pane's parallel fetch pattern
# (/api/sr + /api/sr/fuzzy + /api/regime + /api/history, each with a
# different ``(hours, bar_size, include_partial)`` tuple = different
# cache key) drove 4-5 unique IB historical-data requests per pane
# every 15 s. With multiple panes per box and TWO boxes (dev + prod)
# sharing one IB login, this regularly exceeded IB's 60-requests-per-
# 10-minute pacing limit, surfacing as ``IB_ERROR code=162`` and 502s
# back to the chart. Bars are 3-min cadence so 5-min stale history is
# visually invisible; the live tick stream still drives the chart's
# in-progress bar from a separate WS path.
_HISTORY_TTL_SECONDS = 300.0
# How long to serve STALE cache after an IB fetch fails. When IB error
# 162 (pacing) fires, we'd rather paint slightly older bars than
# blank the chart with a 502 — the pacing window clears within a
# minute or two and the next successful fetch refreshes. Without this
# fallback the chart stayed broken until the operator manually
# refreshed AFTER pacing cleared, which is exactly the experience the
# user reported 2026-05-19 / 2026-05-20.
_HISTORY_STALE_FALLBACK_SECONDS = 1800.0  # 30 min


@app.get("/engine/history")
async def get_history(
    con_id: int | None = None,
    symbol: str | None = None,
    sec_type: str = "STK",
    hours: int = 24,
    bar_size: str = "1 min",
    include_partial: bool = False,
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
        # Auto-route IB-paste FUT localSymbols (``MESM6``, ``GCM6``,
        # ``ESZ6``…) to the FUT qualify path. The frontend's
        # WatchlistPanel click sends ``sec_type=STK`` for everything,
        # so without this fallback every futures symbol in the
        # watchlist would 502 here.
        from ib_trader.repl.commands import _is_futures_local_symbol
        effective_sec_type = sec_type
        if (sec_type or "STK").upper() == "STK" and _is_futures_local_symbol(symbol or ""):
            effective_sec_type = "FUT"
        try:
            qualified = await _ctx.ib.qualify_contract(symbol, sec_type=effective_sec_type)
        except Exception as e:
            logger.exception(
                '{"event": "HISTORY_QUALIFY_FAILED", "symbol": "%s", "sec_type": "%s"}',
                symbol, effective_sec_type,
            )
            raise HTTPException(status_code=502, detail=f"qualify_contract failed: {e}") from e
        con_id = int(qualified.get("con_id") or 0)
        if not con_id:
            raise HTTPException(status_code=502, detail="qualify_contract returned no con_id")

    cache_key = (int(con_id), int(hours), bar_size, bool(include_partial))
    now = time.monotonic()
    cached = _HISTORY_CACHE.get(cache_key)
    # Defensive: an earlier build briefly wrote ``cached[0]`` as a
    # datetime (variable shadowing), so an in-memory cache from
    # before the fix can still trip the subtraction here. Discard
    # any non-float baseline so the request falls through to a
    # fresh fetch.
    if cached and isinstance(cached[0], (int, float)) \
            and (now - cached[0]) < _HISTORY_TTL_SECONDS:
        return cached[1]
    if cached and not isinstance(cached[0], (int, float)):
        _HISTORY_CACHE.pop(cache_key, None)

    contract = _ctx.ib._contract_cache.get(int(con_id))
    if contract is None:
        raise HTTPException(
            status_code=409,
            detail=f"contract {con_id} not in cache; qualify it first",
        )

    # IB's durationStr only accepts S/D/W/M/Y — no H. The "S" format
    # is capped at 86400 (24h); anything longer must use "N D". Round
    # up to whole days so a 48h chart preload (= "2 D") clears the
    # cap. ``use_rth=False`` keeps the day-based request returning
    # 24h calendar windows for futures.
    hours_int = max(1, int(hours))
    if hours_int <= 24:
        duration_str = f"{hours_int * 3600} S"
    else:
        days = (hours_int + 23) // 24
        duration_str = f"{days} D"
    # TRADES is what the bot reasons about and what the operator sees
    # on the chart. The prior BID_ASK feed put bot decisions on
    # avg_ask / mid while the operator's eye reads last-trade prints,
    # which produced "bot fired but the chart shows a different close"
    # discrepancies (2026-05-12 MESM6 08:36). TRADES can be sparse
    # overnight on illiquid contracts; we accept that gap because the
    # bot is gated by RTH / futures dead-zone and won't act on stale
    # ETH bars anyway.
    try:
        bars = await _ctx.ib.req_historical_data_async(
            contract,
            duration_str=duration_str,
            bar_size=bar_size,
            what_to_show="TRADES",
            use_rth=False,
            format_date=2,
        )
    except Exception as e:
        # Stale-cache fallback. The most common failure mode is IB
        # error 162 (historical-data pacing limit) — usually clears in
        # 1-2 minutes. Serving the last good bars (up to
        # ``_HISTORY_STALE_FALLBACK_SECONDS`` old) keeps the chart
        # alive instead of returning 502 on every poll until pacing
        # clears. Stale data is preferable to a broken chart for a
        # 3-min-bar timeframe. ``cached`` was looked up at the top of
        # this function but rejected for being past the fresh TTL;
        # we now revisit it as a degraded-quality fallback.
        if cached is not None:
            stale_age = now - cached[0]
            if stale_age < _HISTORY_STALE_FALLBACK_SECONDS:
                logger.warning(
                    '{"event": "HISTORY_FETCH_STALE_FALLBACK", '
                    '"con_id": %d, "stale_age_s": %.1f, '
                    '"reason": "%s"}',
                    con_id, stale_age, type(e).__name__,
                )
                return cached[1]
        logger.exception('{"event": "HISTORY_FETCH_FAILED", "con_id": %d}', con_id)
        raise HTTPException(status_code=502, detail=f"historical data failed: {e}") from e

    # Drop the in-progress bar (the bar whose slot end is still in
    # the future). IB returns the currently-forming bar at the tail
    # of historical data with whatever the latest tick is as its
    # close — bots that consume this feed for pivot detection then
    # see "bar - 1" as a strict pivot every few ticks even when it
    # isn't, because the in-progress close moves above/below it as
    # the tape ticks. Trimming the in-progress bar makes the feed a
    # pure record of *closed* bars.
    bar_seconds_map = {"1 min": 60, "3 mins": 180, "5 mins": 300,
                        "15 mins": 900, "30 mins": 1800, "1 hour": 3600}
    bar_seconds = bar_seconds_map.get(bar_size, 0)
    from datetime import datetime as _dt, timezone as _tz
    # ``now`` above is ``time.monotonic()`` used by the TTL cache.
    # Use a separate name here so the in-progress filter doesn't
    # shadow it and break the cache check on the next call.
    now_utc = _dt.now(_tz.utc).timestamp()
    out: list[dict] = []
    for bar in bars or []:
        ts = getattr(bar, "date", None)
        if (not include_partial) and bar_seconds > 0 \
                and hasattr(ts, "timestamp"):
            slot_end = ts.timestamp() + bar_seconds
            if slot_end > now_utc + 0.5:
                continue
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        out.append({
            "ts": ts_str,
            "open": float(bar.open),
            "high": float(bar.high),
            "low": float(bar.low),
            "close": float(bar.close),
            "volume": int(getattr(bar, "volume", 0) or 0),
        })

    _HISTORY_CACHE[cache_key] = (now, out)
    # Drop entries older than the stale-fallback horizon (NOT the fresh
    # TTL) so the stale-on-IB-error fallback above still has data to
    # serve. Past the fallback horizon the entry would be too old to
    # be useful anyway.
    if len(_HISTORY_CACHE) > 512:
        for k in [k for k, (t, _) in _HISTORY_CACHE.items()
                  if (now - t) > _HISTORY_STALE_FALLBACK_SECONDS]:
            _HISTORY_CACHE.pop(k, None)
    return out


@app.get("/engine/sr")
async def get_sr(
    con_id: int | None = None,
    symbol: str | None = None,
    sec_type: str = "STK",
    hours: int = 2,
    bar_size: str = "3 mins",
    near_touch_tolerance_fraction: float | None = None,
    break_stale_bars: int | None = None,
    include_broken_wedges: bool = False,
):
    """Canonical SR detection: lines + wedges + pivot timestamps.

    Single source of truth shared by the bot and the chart. Internally:
      1. Re-uses ``get_history`` (with ``include_partial=False`` to
         match the bot's view — chart frontend that wants live-tick
         pivots can still keep its own incremental detector).
      2. Runs ``detect_lines`` + ``find_wedges`` from
         ``ib_trader.signals.sr_fan``.
      3. Returns lines with their key indices remapped to bar
         TIMESTAMPS so the frontend doesn't have to share an index
         coordinate system with the backend.

    Bars used = closed-bar slice (no in-progress). Frontend chart
    can pass the result through unchanged.
    """
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    bars_raw = await get_history(
        con_id=con_id, symbol=symbol, sec_type=sec_type,
        hours=hours, bar_size=bar_size, include_partial=False,
    )
    if not bars_raw or len(bars_raw) < 4:
        return {"bars_count": len(bars_raw or []),
                "lines": [], "wedges": [],
                "pivot_lows": [], "pivot_highs": []}

    from ib_trader.signals.sr_fan import (
        detect_lines, find_pivot_highs, find_pivot_lows,
        find_wedges, NEAR_TOUCH_TOLERANCE_FRACTION,
        TOUCH_TOLERANCE_FRACTION, BREAK_STALE_BARS,
    )

    closes = [float(b["close"]) for b in bars_raw]
    last_idx = len(closes) - 1
    timestamps = [b["ts"] for b in bars_raw]

    # Defaults are the shared sr_fan constants so per-bot YAML
    # overrides can be passed through via query params and the chart
    # paints exactly what the bot reasons on.
    near_frac = (
        near_touch_tolerance_fraction
        if near_touch_tolerance_fraction is not None
        else NEAR_TOUCH_TOLERANCE_FRACTION
    )
    bsb = (
        break_stale_bars
        if break_stale_bars is not None
        else BREAK_STALE_BARS
    )

    supports = detect_lines(
        closes, up_to=last_idx, type_="support",
        near_touch_tolerance_fraction=near_frac,
        break_stale_bars=bsb,
    )
    resistances = detect_lines(
        closes, up_to=last_idx, type_="resistance",
        near_touch_tolerance_fraction=near_frac,
        break_stale_bars=bsb,
    )
    # Flat-slope threshold: a line whose ``|slope|`` is below this
    # value is treated as "flat" for the same-direction filter in
    # ``find_wedges``. Tied to the touch-tolerance so the threshold
    # auto-scales across instruments: roughly "one tick per 20 bars"
    # for a typical contract. Without this gate, a (rising support
    # + barely-positive resistance) ascending-triangle pattern was
    # being misclassified as "both up — keep" instead of "rising
    # into a flat ceiling — squeeze."
    avg_price = (
        sum(closes) / len(closes) if closes else 0.0
    )
    flat_eps = (avg_price * TOUCH_TOLERANCE_FRACTION / 20.0)
    wedges = find_wedges(
        supports, resistances, last_idx,
        include_broken=include_broken_wedges,
        flat_slope_threshold=flat_eps,
    )

    def _line_payload(ln) -> dict:
        from_price = (
            ln.value_at(ln.from_idx)
            if 0 <= ln.from_idx < len(closes) else None
        )
        to_price = (
            ln.value_at(ln.to_idx)
            if 0 <= ln.to_idx < len(closes) else None
        )
        anchor_b_price = (
            ln.value_at(ln.anchor_b_idx)
            if 0 <= ln.anchor_b_idx < len(closes) else None
        )
        break_price = (
            ln.value_at(ln.break_idx)
            if ln.break_idx is not None and 0 <= ln.break_idx < len(closes)
            else None
        )
        return {
            "type": ln.type,
            "from_ts": timestamps[ln.from_idx]
                if 0 <= ln.from_idx < len(timestamps) else None,
            "from_price": from_price,
            "anchor_b_ts": timestamps[ln.anchor_b_idx]
                if 0 <= ln.anchor_b_idx < len(timestamps) else None,
            "anchor_b_price": anchor_b_price,
            "to_ts": timestamps[ln.to_idx]
                if 0 <= ln.to_idx < len(timestamps) else None,
            "to_price": to_price,
            "touches": ln.touches,
            "is_broken": ln.break_idx is not None,
            "break_ts": timestamps[ln.break_idx]
                if ln.break_idx is not None and 0 <= ln.break_idx < len(timestamps)
                else None,
            "break_price": break_price,
            # 3rd strict-touch anchor — used by the chart to thicken
            # the post-3rd segment when a 4th near-touch upgrades the
            # line. ``None`` when the line has only 2 strict touches.
            "third_touch_ts": timestamps[ln.third_touch_idx]
                if ln.third_touch_idx is not None
                and 0 <= ln.third_touch_idx < len(timestamps)
                else None,
            # Numeric line params still exposed for the bot / backtest
            # consumers; the chart renders from the (ts, price) pairs
            # above and doesn't need them.
            "slope_per_bar": ln.slope,
            "intercept": ln.intercept,
        }

    def _wedge_payload(w) -> dict:
        s_left_price = w.support.value_at(w.overlap_start_idx)
        s_right_price = w.support.value_at(last_idx)
        r_left_price = w.resistance.value_at(w.overlap_start_idx)
        r_right_price = w.resistance.value_at(last_idx)
        return {
            "apex_bars_ahead": w.apex_bars_ahead,
            "apex_idx_float": w.apex_idx_float,
            "overlap_start_idx": w.overlap_start_idx,
            "overlap_start_ts": timestamps[w.overlap_start_idx]
                if 0 <= w.overlap_start_idx < len(timestamps) else None,
            "right_ts": timestamps[last_idx],
            "support_slope": w.support.slope,
            "resistance_slope": w.resistance.slope,
            "vertices": {
                "support_left":  {"ts": timestamps[w.overlap_start_idx],
                                  "price": s_left_price},
                "support_right": {"ts": timestamps[last_idx],
                                  "price": s_right_price},
                "resistance_right": {"ts": timestamps[last_idx],
                                     "price": r_right_price},
                "resistance_left":  {"ts": timestamps[w.overlap_start_idx],
                                     "price": r_left_price},
            },
        }

    pivot_lows = find_pivot_lows(closes)
    pivot_highs = find_pivot_highs(closes)
    return {
        "bars_count": len(closes),
        "last_ts": timestamps[last_idx],
        "lines": [_line_payload(ln) for ln in (supports + resistances)],
        "wedges": [_wedge_payload(w) for w in wedges],
        "pivot_lows": [timestamps[i] for i in pivot_lows],
        "pivot_highs": [timestamps[i] for i in pivot_highs],
    }


@app.get("/engine/regime")
async def get_regime(
    con_id: int | None = None,
    symbol: str | None = None,
    sec_type: str = "STK",
    hours: int = 24,
    bar_size: str = "3 mins",
    adx_period: int = 14,
    atr_period: int = 14,
    donchian_period: int = 20,
    trending_threshold: float = 25.0,
    ranging_threshold: float = 20.0,
):
    """Local regime classification (ADX + ATR + Donchian).

    Computes the same regime reading the chart_signal entry gate
    uses, so the chart can display it next to the price action. Bar
    window matches ``/engine/sr`` (closed-bar slice, no in-progress).

    Returns ``RegimeReading.to_audit_payload()`` augmented with the
    bar window's last timestamp so the frontend can show staleness.
    """
    if _ctx is None:
        raise HTTPException(status_code=503, detail="Engine not initialized")
    bars_raw = await get_history(
        con_id=con_id, symbol=symbol, sec_type=sec_type,
        hours=hours, bar_size=bar_size, include_partial=False,
    )
    if not bars_raw:
        return {"regime": "insufficient", "n_bars": 0, "last_ts": None}

    from ib_trader.bots.strategies.regime import (
        compute_regime, detect_v_state,
    )
    reading = compute_regime(
        bars_raw,
        adx_period=adx_period,
        atr_period=atr_period,
        donchian_period=donchian_period,
        trending_threshold=trending_threshold,
        ranging_threshold=ranging_threshold,
    )
    payload = reading.to_audit_payload()
    payload["last_ts"] = bars_raw[-1].get("ts") if bars_raw else None
    payload["sufficient_bars"] = reading.sufficient_bars

    # V-recovery detector (display-only — does NOT gate entries).
    # Uses the same ATR that fed the regime classifier. Three
    # triggers (BOS / retrace / exhaustion) evaluated in parallel;
    # frontend renders the state in a top-left badge.
    v_state = detect_v_state(
        bars_raw,
        atr=(reading.atr or 0.0),
    )
    payload["v_state"] = v_state.to_audit_payload()
    return payload


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
