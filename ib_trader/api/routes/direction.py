"""Directional callout endpoints (#100) — read-only Redis passthrough."""
import json

from fastapi import APIRouter, Depends

from ib_trader.api.deps import get_redis

router = APIRouter(prefix="/api/direction", tags=["direction"])


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
