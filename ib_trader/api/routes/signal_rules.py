"""Trade-recommendation rules config (GH #102).

GET /api/signal-rules — the stored config document, or ``null`` when the
                        operator hasn't saved one (frontend defaults apply).
PUT /api/signal-rules — replace it.

The document's shape is owned by the frontend rule registry (rules carry
their own param schema), so the server stores it opaquely — validated
only as a bounded JSON object. Durable in Redis, shared by every device.
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ib_trader.api.deps import get_redis
from ib_trader.redis.state import StateKeys

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/signal-rules", tags=["signal-rules"])

_MAX_BYTES = 64 * 1024


class SignalRulesUpdate(BaseModel):
    """Request body: the full rules config document."""
    config: dict


@router.get("")
async def get_signal_rules(redis=Depends(get_redis)):
    """Return ``{"config": <document or null>}``."""
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    try:
        raw = await redis.get(StateKeys.signal_rules_config())
    except Exception as e:
        logger.exception('{"event": "SIGNAL_RULES_READ_FAILED"}')
        raise HTTPException(status_code=503, detail="Redis read failed") from e
    if not raw:
        return {"config": None}
    try:
        cfg = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning('{"event": "SIGNAL_RULES_CORRUPT"}')
        return {"config": None}
    return {"config": cfg if isinstance(cfg, dict) else None}


@router.put("")
async def put_signal_rules(body: SignalRulesUpdate, redis=Depends(get_redis)):
    """Replace the stored config document."""
    if redis is None:
        raise HTTPException(status_code=503, detail="Redis not available")
    raw = json.dumps(body.config)
    if len(raw) > _MAX_BYTES:
        raise HTTPException(status_code=413, detail="config too large")
    try:
        await redis.set(StateKeys.signal_rules_config(), raw)
    except Exception as e:
        logger.exception('{"event": "SIGNAL_RULES_WRITE_FAILED"}')
        raise HTTPException(status_code=503, detail="Redis write failed") from e
    logger.info('{"event": "SIGNAL_RULES_UPDATED", "bytes": %d}', len(raw))
    return {"config": body.config}
