"""Bot management endpoints.

GET /api/bots — list all bots
GET /api/bots/{bot_id} — get a single bot
GET /api/bots/{bot_id}/events — get bot events (audit trail)
POST /api/bots/{bot_id}/start — start a bot
POST /api/bots/{bot_id}/stop — stop a bot
"""
import json

from fastapi import APIRouter, Depends, HTTPException, Query

from ib_trader.api.deps import get_session_factory, get_redis
from ib_trader.data.models import BotStatus
from ib_trader.data.repositories.bot_repository import BotRepository, BotEventRepository

router = APIRouter(prefix="/api/bots", tags=["bots"])


def _serialize_bot(b) -> dict:
    return {
        "id": b.id,
        "name": b.name,
        "strategy": b.strategy,
        "broker": b.broker,
        "status": b.status.value,
        "tick_interval_seconds": b.tick_interval_seconds,
        "last_heartbeat": b.last_heartbeat.isoformat() if b.last_heartbeat else None,
        "last_signal": b.last_signal,
        "last_action": b.last_action,
        "last_action_at": b.last_action_at.isoformat() if b.last_action_at else None,
        "error_message": b.error_message,
        "trades_total": b.trades_total,
        "trades_today": b.trades_today,
        "pnl_today": str(b.pnl_today),
        "symbols_json": b.symbols_json,
    }


@router.get("")
def list_bots(sf=Depends(get_session_factory)):
    try:
        repo = BotRepository(sf)
        return [_serialize_bot(b) for b in repo.get_all()]
    finally:
        sf.remove()


@router.get("/{bot_id}")
def get_bot(bot_id: str, sf=Depends(get_session_factory)):
    try:
        repo = BotRepository(sf)
        b = repo.get(bot_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Bot not found")
        return _serialize_bot(b)
    finally:
        sf.remove()


@router.post("/{bot_id}/start", status_code=202)
async def start_bot(bot_id: str, sf=Depends(get_session_factory), redis=Depends(get_redis)):
    """Start a bot. Publishes START to the global control stream.

    The bot runner wakes immediately via XREAD BLOCK — no polling delay.
    """
    repo = BotRepository(sf)
    b = repo.get(bot_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    if b.status == BotStatus.RUNNING:
        return {"bot_id": bot_id, "status": "RUNNING", "message": "already running"}
    repo.update_status(bot_id, BotStatus.RUNNING)
    # Publish to global control stream — runner wakes immediately
    if redis:
        await redis.xadd("bot:control:global", {"action": "START", "bot_id": bot_id}, maxlen=100)
    return {"bot_id": bot_id, "status": "RUNNING"}


@router.post("/{bot_id}/stop", status_code=202)
async def stop_bot(bot_id: str, sf=Depends(get_session_factory), redis=Depends(get_redis)):
    """Stop a bot. Publishes STOP to the global control stream.

    The bot runner and the bot itself wake immediately — no polling delay.
    """
    repo = BotRepository(sf)
    b = repo.get(bot_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    if b.status == BotStatus.STOPPED:
        return {"bot_id": bot_id, "status": "STOPPED", "message": "already stopped"}
    repo.update_status(bot_id, BotStatus.STOPPED)
    # Publish to global control stream — runner wakes immediately
    if redis:
        await redis.xadd("bot:control:global", {"action": "STOP", "bot_id": bot_id}, maxlen=100)
    return {"bot_id": bot_id, "status": "STOPPED"}


@router.get("/{bot_id}/state")
async def get_bot_state(bot_id: str, sf=Depends(get_session_factory), redis=Depends(get_redis)):
    """Return the bot's live position state from Redis."""
    try:
        repo = BotRepository(sf)
        b = repo.get(bot_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Bot not found")

        config = json.loads(b.config_json) if b.config_json else {}
        symbol = config.get("symbol", "")
        ref_id = config.get("ref_id", bot_id)
    finally:
        sf.remove()

    # Read from Redis
    if redis:
        from ib_trader.redis.state import StateStore, StateKeys
        store = StateStore(redis)
        strat = await store.get(StateKeys.strategy(ref_id, symbol))
        if strat:
            return strat
        pos = await store.get(StateKeys.position(ref_id, symbol))
        if pos:
            return pos

    return {"position_state": "FLAT"}


@router.post("/{bot_id}/force-buy", status_code=202)
async def force_buy(bot_id: str, sf=Depends(get_session_factory), redis=Depends(get_redis)):
    """Signal a running bot to place a forced buy immediately.

    Publishes FORCE_BUY to the global control stream. The bot runner
    forwards it to the bot's control stream. The bot wakes from XREAD
    BLOCK and executes the force-buy — no waiting for next tick.
    """
    repo = BotRepository(sf)
    b = repo.get(bot_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    if b.status != BotStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Bot is not running")
    # Publish to global control stream — forwarded to bot immediately
    if redis:
        await redis.xadd("bot:control:global", {"action": "FORCE_BUY", "bot_id": bot_id}, maxlen=100)
    else:
        # Fallback: write to DB (bot checks on next tick)
        repo.update_action(bot_id, "FORCE_BUY")
    return {"bot_id": bot_id, "action": "FORCE_BUY"}


@router.get("/{bot_id}/events")
def get_bot_events(
    bot_id: str,
    limit: int = Query(100, ge=1, le=1000),
    event_type: str | None = Query(None),
    sf=Depends(get_session_factory),
):
    """Return recent bot events (audit trail).

    Optional filter by event_type (BAR, SKIP, SIGNAL, ORDER, FILL, etc.).
    """
    try:
        repo = BotRepository(sf)
        b = repo.get(bot_id)
        if b is None:
            raise HTTPException(status_code=404, detail="Bot not found")

        events_repo = BotEventRepository(sf)
        if event_type:
            events = events_repo.get_by_type(bot_id, event_type, limit=limit)
        else:
            events = events_repo.get_for_bot(bot_id, limit=limit)

        return [
            {
                "id": e.id,
                "event_type": e.event_type,
                "message": e.message,
                "payload": e.payload_json,
                "trade_serial": e.trade_serial,
                "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
            }
            for e in events
        ]
    finally:
        sf.remove()  # Release connection back to pool
