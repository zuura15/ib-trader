"""Watchlist endpoints.

GET  /api/watchlist         — live market data for watchlist symbols
GET  /api/watchlist/symbols — current symbol list from config
PUT  /api/watchlist/symbols — update symbol list in config
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ib_trader.api.deps import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

_MAX_SYMBOLS = 50


@router.get("")
async def get_watchlist(redis=Depends(get_redis)):
    """Return live watchlist data from Redis."""
    if redis is None:
        return JSONResponse(
            content={"error": "Redis not available"},
            status_code=503,
        )

    try:
        data = await _watchlist_from_redis(redis)
        if data is None:
            data = {"generated_at": None, "items": []}
    except Exception:
        logger.exception('{"event": "REDIS_WATCHLIST_ERROR"}')
        return JSONResponse(
            content={"error": "Redis read failed"},
            status_code=503,
        )

    return JSONResponse(
        content=data,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


async def _watchlist_from_redis(redis) -> dict | None:
    """Read watchlist quotes from Redis keys."""
    from datetime import datetime, timezone
    from ib_trader.redis.state import StateStore
    from ib_trader.config.watchlist_runtime import resolve_watchlist_symbols

    # Same authoritative source as GET /symbols (Redis, YAML-seeded) so the
    # quotes panel matches the operator's live watchlist.
    symbols = await resolve_watchlist_symbols(redis)
    if not symbols:
        return None

    store = StateStore(redis)
    items = []
    for sym in symbols:
        quote = await store.get(f"quote:{sym}:latest")
        if quote:
            def _fmt(v):
                return str(v) if v is not None else None
            def _fmt_int(v):
                return str(int(v)) if v is not None else None

            items.append({
                "symbol": sym,
                "last": _fmt(quote.get("last")),
                "change": _fmt(quote.get("change")),
                "change_pct": _fmt(quote.get("change_pct")),
                "volume": _fmt_int(quote.get("volume")),
                "avg_volume": _fmt_int(quote.get("avg_volume")),
                "high": _fmt(quote.get("high")),
                "low": _fmt(quote.get("low")),
                "high_52w": _fmt(quote.get("high_52w")),
                "low_52w": _fmt(quote.get("low_52w")),
                "error": None,
            })
        else:
            items.append({
                "symbol": sym,
                "last": None, "change": None, "change_pct": None,
                "volume": None, "avg_volume": None,
                "high": None, "low": None,
                "high_52w": None, "low_52w": None,
                "error": None,
            })

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "items": items,
    }


@router.get("/symbols")
async def get_symbols(redis=Depends(get_redis)):
    """Return the current watchlist symbol list.

    Live list comes from Redis (seeded from ``config/watchlist.yaml`` on
    first run). ``symbols`` stays a plain list of roots; ``entries``
    carries sec-type-aware dicts (FUT detected from the IB-paste form) for
    clients that render futures.
    """
    from ib_trader.config.watchlist_runtime import resolve_watchlist_symbols
    from ib_trader.repl.commands import _is_futures_local_symbol
    symbols = await resolve_watchlist_symbols(redis)
    entries = [
        {"root": s, "sec_type": "FUT" if _is_futures_local_symbol(s) else "STK"}
        for s in symbols
    ]
    return {"symbols": symbols, "entries": entries, "max": _MAX_SYMBOLS}


class SymbolsUpdate(BaseModel):
    """Request body for updating watchlist symbols."""
    symbols: list[str]


@router.put("/symbols")
async def update_symbols(body: SymbolsUpdate, redis=Depends(get_redis)):
    """Update the watchlist symbol list.

    Persisted to Redis (``watchlist:symbols``), NOT the git-tracked YAML —
    so manual/UI edits never conflict with ``git pull``.
    """
    if len(body.symbols) > _MAX_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {_MAX_SYMBOLS} symbols allowed, got {len(body.symbols)}",
        )
    from ib_trader.config.watchlist_runtime import set_watchlist_symbols
    symbols = await set_watchlist_symbols(redis, body.symbols)
    logger.info(
        '{"event": "WATCHLIST_SYMBOLS_UPDATED", "count": %d}', len(symbols),
    )
    return {"symbols": symbols, "max": _MAX_SYMBOLS}
