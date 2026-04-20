"""Order endpoints.

GET /api/orders — open orders from Redis orders:open hash (live state)
GET /api/orders/{ib_order_id} — single order from the hash
POST /api/orders/cancel-by-symbol — cancel every open IB order for a symbol
"""
import json
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ib_trader.api.deps import get_redis

router = APIRouter(prefix="/api/orders", tags=["orders"])


def _engine_url() -> str:
    port = os.environ.get("IB_TRADER_ENGINE_INTERNAL_PORT", "8081")
    return f"http://127.0.0.1:{port}"


@router.get("")
async def list_open_orders(redis=Depends(get_redis)):
    """List all open (non-terminal) orders from the orders:open Redis hash.

    The engine maintains this hash: non-terminal order events upsert,
    terminal events delete. No SQLite reads.
    """
    if redis is None:
        return []

    from ib_trader.redis.state import StateKeys
    raw = await redis.hgetall(StateKeys.orders_open())
    orders = []
    for _oid, val in raw.items():
        try:
            order = json.loads(val)
            orders.append(order)
        except (json.JSONDecodeError, TypeError):
            pass
    return orders


@router.get("/{ib_order_id}")
async def get_order(ib_order_id: str, redis=Depends(get_redis)):
    """Get a single open order by IB order ID from the Redis hash."""
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")

    from ib_trader.redis.state import StateKeys
    raw = await redis.hget(StateKeys.orders_open(), ib_order_id)
    if raw is None:
        raise HTTPException(status_code=404, detail="Order not found (may have already completed)")
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError) as e:
        raise HTTPException(status_code=500, detail="Corrupt order data") from e


@router.post("/cancel-by-symbol")
async def cancel_by_symbol(body: dict):
    """Cancel every open IB order for a symbol — proxy to engine."""
    symbol = (body.get("symbol") or "").upper()
    if not symbol:
        raise HTTPException(status_code=422, detail="symbol required")
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_engine_url()}/engine/cancel-by-symbol",
            json={"symbol": symbol},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json()
