"""Watchlist endpoints.

GET  /api/watchlist         — live market data for watchlist symbols
GET  /api/watchlist/symbols — current symbol list from config
PUT  /api/watchlist/symbols — update symbol list in config
"""
import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ib_trader.api.deps import get_redis
from ib_trader.config.loader import load_watchlist, save_watchlist

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

_WATCHLIST_FILE = Path("run/watchlist.json")
_WATCHLIST_YAML = "config/watchlist.yaml"
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
    from ib_trader.config.loader import load_watchlist

    symbols = load_watchlist(_WATCHLIST_YAML)
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
def get_symbols():
    """Return the current watchlist symbol list."""
    symbols = load_watchlist(_WATCHLIST_YAML)
    return {"symbols": symbols, "max": _MAX_SYMBOLS}


class SymbolsUpdate(BaseModel):
    """Request body for updating watchlist symbols."""
    symbols: list[str]


@router.put("/symbols")
def update_symbols(body: SymbolsUpdate):
    """Update the watchlist symbol list."""
    # Validate and normalize
    symbols = list(dict.fromkeys(s.upper().strip() for s in body.symbols if s.strip()))

    if len(symbols) > _MAX_SYMBOLS:
        raise HTTPException(
            status_code=400,
            detail=f"Maximum {_MAX_SYMBOLS} symbols allowed, got {len(symbols)}",
        )

    save_watchlist(_WATCHLIST_YAML, symbols)
    logger.info(
        '{"event": "WATCHLIST_SYMBOLS_UPDATED", "count": %d}', len(symbols),
    )
    return {"symbols": symbols, "max": _MAX_SYMBOLS}
