"""Internal HTTP API for the engine process.

All command producers (bots, API, REPL) submit orders through this API.
Replaces the pending_commands SQLite polling pattern.

Runs as a uvicorn server inside the engine process on a configurable port
(default 8081). Not exposed to the browser — the public API server on
port 8000 forwards to this when needed.
"""
import asyncio
import json
import logging
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

# Module-level reference to AppContext, set by start_internal_api()
_ctx = None


_VALID_SIDES = {"BUY", "SELL"}
_VALID_ORDER_TYPES = {"mid", "market", "bid", "ask", "limit"}


class OrderRequest(BaseModel):
    """Request body for placing an order."""

    symbol: str
    side: str = Field(description="BUY or SELL")
    qty: str = Field(description="Order quantity as string (Decimal-safe)")
    order_type: str = Field(default="mid", description="Order strategy: mid, limit, market, bid, ask")
    price: Optional[str] = Field(default=None, description="Limit price (required for limit orders)")
    bot_ref: Optional[str] = Field(default=None, description="Bot reference ID for orderRef tagging")
    serial: Optional[int] = Field(default=None, description="Trade serial number")
    profit: Optional[str] = Field(default=None, description="Profit target in dollars")
    stop_loss: Optional[str] = Field(default=None, description="Stop loss in dollars")


class OrderResponse(BaseModel):
    """Response after placing an order."""

    ib_order_id: str
    serial: int
    order_ref: Optional[str] = None
    status: str


class CloseRequest(BaseModel):
    """Request body for closing a position."""

    serial: int
    strategy: str = "market"
    profit: Optional[str] = None
    bot_ref: Optional[str] = Field(default=None, description="Bot reference ID for orderRef tagging")


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

    from ib_trader.repl.commands import parse_command
    from ib_trader.engine.service import execute_single_command

    # Validate inputs
    side_upper = req.side.upper()
    if side_upper not in _VALID_SIDES:
        raise HTTPException(status_code=422, detail=f"Invalid side: {req.side!r}. Must be BUY or SELL.")
    if req.order_type not in _VALID_ORDER_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid order_type: {req.order_type!r}. Must be one of {_VALID_ORDER_TYPES}.")

    # Build command text from request
    side_cmd = "buy" if side_upper == "BUY" else "sell"
    cmd_text = f"{side_cmd} {req.symbol} {req.qty} {req.order_type}"
    if req.profit:
        cmd_text += f" --profit {req.profit}"
    if req.stop_loss:
        cmd_text += f" --stop-loss {req.stop_loss}"
    if req.price:
        cmd_text += f" --price {req.price}"

    # Pass bot_ref through to execute_single_command — the engine encodes
    # orderRef AFTER allocating the real trade serial (not the bot's stale one).
    try:
        result = await execute_single_command(
            _ctx, cmd_text,
            source=f"bot:{req.bot_ref}" if req.bot_ref else "api",
            bot_ref=req.bot_ref,
        )
        return OrderResponse(
            ib_order_id=result.get("ib_order_id", ""),
            serial=result.get("serial", 0),
            order_ref=result.get("order_ref"),
            status=result.get("status", "SUBMITTED"),
        )
    except Exception as e:
        logger.exception('{"event": "INTERNAL_API_ORDER_FAILED"}')
        raise HTTPException(status_code=500, detail=str(e))


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
        )
        return {"status": "ok", "result": result}
    except Exception as e:
        logger.exception('{"event": "INTERNAL_API_CLOSE_FAILED"}')
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
        raise HTTPException(status_code=500, detail=str(e))


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
