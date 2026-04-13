"""Positions endpoint.

GET /api/positions — returns current positions.

Primary: reads from Redis keys (pos:* and quote:*:latest) for real-time data.
Fallback: reads from run/positions.json (engine writes every 10s).
"""
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ib_trader.api.deps import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/positions", tags=["positions"])


@router.get("")
async def list_positions(redis=Depends(get_redis)):
    """Return current broker positions from Redis."""
    if redis is None:
        return JSONResponse(
            content={"error": "Redis not available"},
            status_code=503,
        )

    try:
        positions = await _positions_from_redis(redis)
    except Exception:
        logger.exception('{"event": "REDIS_POSITIONS_ERROR"}')
        return JSONResponse(
            content={"error": "Redis read failed"},
            status_code=503,
        )

    return JSONResponse(
        content=positions,
        headers={"Cache-Control": "no-store, max-age=0"},
    )


async def _positions_from_redis(redis) -> list[dict]:
    """Read positions from Redis.

    Reads from two key patterns:
    - ibpos:* — all IB positions (written by engine on startup + positionEvent)
    - pos:* — bot-managed positions with state tracking
    """
    from ib_trader.redis.state import StateStore

    store = StateStore(redis)
    positions = []
    seen_symbols = set()

    # 1. IB positions (all account positions from the broker)
    async for key in redis.scan_iter(match="ibpos:*"):
        data = await store.get(key)
        if data and data.get("quantity", "0") != "0":
            symbol = data.get("symbol", "")
            seen_symbols.add(symbol)

            # Get latest quote for market price
            quote = await store.get(f"quote:{symbol}:latest")
            mkt_price = None
            if quote:
                bid = quote.get("bid")
                ask = quote.get("ask")
                last = quote.get("last")
                if bid and ask:
                    try:
                        mkt_price = (float(bid) + float(ask)) / 2
                    except (ValueError, TypeError):
                        pass
                elif last:
                    try:
                        mkt_price = float(last)
                    except (ValueError, TypeError):
                        pass

            positions.append({
                "id": f"{symbol}_{data.get('sec_type', 'STK')}_{data.get('con_id', 0)}",
                "account_id": data.get("account", ""),
                "symbol": symbol,
                "sec_type": data.get("sec_type", "STK"),
                "quantity": data.get("quantity", "0"),
                "avg_cost": data.get("avg_cost", "0"),
                "market_price": f"{mkt_price:.4f}" if mkt_price else None,
                "broker": "ib",
            })

    # 2. Bot-managed positions (supplement with state info if not already shown)
    async for key in redis.scan_iter(match="pos:*"):
        data = await store.get(key)
        if data and data.get("state") != "FLAT" and data.get("qty", "0") != "0":
            parts = key.split(":")
            if len(parts) == 3:
                _, bot_ref, symbol = parts
                if symbol not in seen_symbols:
                    positions.append({
                        "symbol": symbol,
                        "bot_ref": bot_ref,
                        "quantity": data.get("qty", "0"),
                        "avg_cost": data.get("avg_price", "0"),
                        "market_price": None,
                        "broker": "ib",
                    })

    return positions
