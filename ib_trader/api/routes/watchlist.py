"""Watchlist endpoints.

GET  /api/watchlist         — live market data for watchlist symbols
GET  /api/watchlist/symbols — current symbol list from config
PUT  /api/watchlist/symbols — update symbol list in config
"""
import json
import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ib_trader.config.loader import load_watchlist, save_watchlist

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])

_WATCHLIST_FILE = Path("run/watchlist.json")
_WATCHLIST_YAML = "config/watchlist.yaml"
_MAX_SYMBOLS = 50


@router.get("")
def get_watchlist():
    """Return live watchlist data from the engine's JSON cache."""
    try:
        data = json.loads(_WATCHLIST_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = {"generated_at": None, "items": []}

    return JSONResponse(
        content=data,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
        },
    )


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
