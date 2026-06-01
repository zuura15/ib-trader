"""Rolling 24h console-only realized P&L surface.

The console panel header shows running total of realized P&L from
operator-initiated closes ONLY — explicit ``close <serial>`` verbs and
console buy/sell orders that reduce an opposite-side position. Bot
closes are NOT included; those have their own per-bot stats surfaces.

Producer: ``ib_trader.engine.order._record_console_close_pnl`` ZADDs
on every close fill (and prunes >24h entries inline). This endpoint
reads what survives the cutoff.
"""
import json as _json
import logging
import time
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends

from ib_trader.api.deps import get_redis
from ib_trader.redis.state import StateKeys

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["console"])

_WINDOW_MS = 24 * 60 * 60 * 1000


@router.get("/console/pnl/24h")
async def get_console_pnl_24h(redis=Depends(get_redis)):
    """Sum of console-close realized P&L over the rolling last-24h window.

    Returns ``{pnl, count, since_ms, until_ms, window_ms}``. ``pnl`` is a
    decimal string (caller can format); ``count`` is the number of
    close-fill events that contributed. Empty window returns
    ``{pnl: "0", count: 0, ...}``.
    """
    until_ms = int(time.time() * 1000)
    since_ms = until_ms - _WINDOW_MS

    if redis is None:
        return {
            "pnl": "0", "count": 0,
            "since_ms": since_ms, "until_ms": until_ms,
            "window_ms": _WINDOW_MS,
        }

    try:
        members = await redis.zrangebyscore(
            StateKeys.console_pnl_24h(), since_ms, until_ms,
        )
    except Exception:
        logger.exception('{"event": "CONSOLE_PNL_READ_FAILED"}')
        return {
            "pnl": "0", "count": 0,
            "since_ms": since_ms, "until_ms": until_ms,
            "window_ms": _WINDOW_MS, "error": "redis_unavailable",
        }

    total = Decimal("0")
    count = 0
    for raw in members or []:
        try:
            obj = _json.loads(raw)
            total += Decimal(str(obj["pnl"]))
            count += 1
        except (ValueError, KeyError, TypeError, InvalidOperation):
            continue

    return {
        "pnl": str(total),
        "count": count,
        "since_ms": since_ms,
        "until_ms": until_ms,
        "window_ms": _WINDOW_MS,
    }
