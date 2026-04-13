"""Positions endpoint.

GET /api/positions — returns current positions.

Primary: reads from Redis keys (pos:* and quote:*:latest) for real-time data.
Fallback: reads from run/positions.json (engine writes every 10s).
"""
import asyncio
import json
import logging
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ib_trader.api.deps import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/positions", tags=["positions"])

_POSITIONS_FILE = Path("run/positions.json")


@router.get("")
async def list_positions(redis=Depends(get_redis)):
    """Return current broker positions.

    Tries Redis first (real-time), falls back to JSON file (10s stale).
    """
    if redis is not None:
        try:
            positions = await _positions_from_redis(redis)
            if positions:
                return JSONResponse(
                    content=positions,
                    headers={"Cache-Control": "no-store, max-age=0"},
                )
        except Exception:
            logger.exception('{"event": "REDIS_POSITIONS_ERROR"}')

    # Fallback: JSON file
    try:
        data = json.loads(_POSITIONS_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        data = []

    return JSONResponse(
        content=data,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


async def _positions_from_redis(redis) -> list[dict]:
    """Read position state from Redis keys."""
    from ib_trader.redis.state import StateStore

    store = StateStore(redis)
    positions = []

    async for key in redis.scan_iter(match="pos:*"):
        data = await store.get(key)
        if data and data.get("state") != "FLAT" and data.get("qty", "0") != "0":
            parts = key.split(":")
            if len(parts) == 3:
                _, bot_ref, symbol = parts

                # Get latest quote for market price
                quote_key = f"quote:{symbol}:latest"
                quote = await store.get(quote_key)
                mkt_price = None
                if quote:
                    bid = quote.get("bid")
                    ask = quote.get("ask")
                    if bid and ask:
                        try:
                            mkt_price = (float(bid) + float(ask)) / 2
                        except (ValueError, TypeError):
                            pass

                positions.append({
                    "symbol": symbol,
                    "bot_ref": bot_ref,
                    "quantity": data.get("qty", "0"),
                    "avg_cost": data.get("avg_price", "0"),
                    "entry_price": data.get("entry_price"),
                    "state": data.get("state", "FLAT"),
                    "market_price": f"{mkt_price:.4f}" if mkt_price else None,
                    "broker": "ib",
                })

    return positions
