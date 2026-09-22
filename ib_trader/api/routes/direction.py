"""Directional callout endpoints (#100): Redis read + compute proxy."""
import json
import os

import httpx
from fastapi import APIRouter, Depends, HTTPException

from ib_trader.api.deps import get_redis


def _engine_url() -> str:
    port = os.environ.get("IB_TRADER_ENGINE_INTERNAL_PORT", "8081")
    return f"http://127.0.0.1:{port}"

router = APIRouter(prefix="/api/direction", tags=["direction"])


# Declared before /{symbol} — FastAPI matches routes in order.
@router.get("/symbols")
async def get_direction_symbols():
    """Symbols with live engine sample buffers (Direction Lab dropdown).

    [] when the engine is down/unreachable — same safe-sentinel
    convention as get_direction's {}.
    """
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            resp = await client.get(f"{_engine_url()}/engine/direction/symbols")
        except httpx.HTTPError:
            return {"symbols": []}
    if resp.status_code != 200:
        return {"symbols": []}
    return resp.json()


@router.get("/{symbol}")
async def get_direction(symbol: str, redis=Depends(get_redis)):
    """Latest callout payload for a symbol; {} when absent/unavailable."""
    if redis is None:
        return {}
    from ib_trader.redis.state import StateKeys
    raw = await redis.get(StateKeys.direction_callout(symbol))
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}


@router.post("/compute")
async def compute_direction(body: dict):
    """Button-invoked direction computation (Direction Lab) — proxy to engine."""
    symbol = (body.get("symbol") or "").upper()
    if not symbol:
        raise HTTPException(status_code=422, detail="symbol required")
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{_engine_url()}/engine/direction/compute", json={"symbol": symbol},
        )
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json()
