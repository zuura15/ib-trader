"""Positions endpoint.

GET /api/positions — proxies to the engine's in-memory position cache.

No Redis, no SQLite. The engine holds the IB connection and maintains
an in-memory positions dict refreshed by positionEvent + 30s poll.
This endpoint proxies to GET /engine/positions on the engine's internal
HTTP API. If the engine is down, returns 503 — honest "I don't know"
instead of serving stale data.
"""
import os
import logging

import httpx
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from ib_trader.api.deps import get_redis

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/positions", tags=["positions"])


def _engine_url() -> str:
    port = os.environ.get("IB_TRADER_ENGINE_INTERNAL_PORT", "8081")
    return f"http://127.0.0.1:{port}"


@router.get("")
async def list_positions(redis=Depends(get_redis)):
    """Return current broker positions from the engine.

    Primary: proxy to GET /engine/positions (engine's in-memory cache).
    Supplement: scan Redis pos:* keys for bot-managed positions on symbols
    the engine doesn't report (edge case: bot fill landed but positionEvent
    hasn't fired yet).
    """
    positions = []
    seen_symbols: set[str] = set()

    # 1. Engine positions — authoritative for IB account state.
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{_engine_url()}/engine/positions")
            if resp.status_code == 200:
                positions = resp.json()
                seen_symbols = {p.get("symbol", "") for p in positions}
            else:
                logger.warning(
                    '{"event": "ENGINE_POSITIONS_FAILED", "status": %d}',
                    resp.status_code,
                )
    except Exception:
        logger.exception('{"event": "ENGINE_POSITIONS_UNREACHABLE"}')
        return JSONResponse(
            content={"error": "Engine unavailable"},
            status_code=503,
        )

    # 2. Bot-managed positions — from bot:<uuid> keys for bots with open positions
    #    that the engine doesn't yet report (fill arrived but positionEvent hasn't fired).
    if redis is not None:
        try:
            from ib_trader.redis.state import StateStore
            from ib_trader.bots import registry_config
            store = StateStore(redis)
            for defn in registry_config.all_definitions():
                data = await store.get(f"bot:{defn.id}")
                if not data:
                    continue
                symbol = data.get("symbol") or defn.config.get("symbol", "")
                if symbol in seen_symbols:
                    continue
                qty = data.get("qty", "0")
                state = data.get("state", "FLAT")
                if state == "FLAT" or state == "OFF" or qty == "0":
                    continue
                positions.append({
                    "symbol": symbol,
                    "bot_ref": defn.config.get("ref_id", defn.name),
                    "quantity": qty,
                    "avg_cost": data.get("avg_price") or data.get("entry_price") or "0",
                    "market_price": None,
                    "broker": "ib",
                })
        except Exception:
            logger.exception('{"event": "BOT_POSITIONS_READ_ERROR"}')

    return JSONResponse(
        content=positions,
        headers={"Cache-Control": "no-store, max-age=0"},
    )
