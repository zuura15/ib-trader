"""Bot management endpoints.

GET /api/bots — list all bots
GET /api/bots/{bot_id} — get a single bot
POST /api/bots/{bot_id}/start — start a bot
POST /api/bots/{bot_id}/stop — stop a bot
"""
from fastapi import APIRouter, Depends, HTTPException

from ib_trader.api.deps import get_session_factory
from ib_trader.data.models import BotStatus
from ib_trader.data.repositories.bot_repository import BotRepository

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
    repo = BotRepository(sf)
    return [_serialize_bot(b) for b in repo.get_all()]


@router.get("/{bot_id}")
def get_bot(bot_id: str, sf=Depends(get_session_factory)):
    repo = BotRepository(sf)
    b = repo.get(bot_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return _serialize_bot(b)


@router.post("/{bot_id}/start", status_code=202)
def start_bot(bot_id: str, sf=Depends(get_session_factory)):
    """Set bot status to RUNNING. The bot runner will pick it up.

    Idempotent: if already RUNNING, returns 200 with no-op.
    """
    repo = BotRepository(sf)
    b = repo.get(bot_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    if b.status == BotStatus.RUNNING:
        return {"bot_id": bot_id, "status": "RUNNING", "message": "already running"}
    repo.update_status(bot_id, BotStatus.RUNNING)
    return {"bot_id": bot_id, "status": "RUNNING"}


@router.post("/{bot_id}/stop", status_code=202)
def stop_bot(bot_id: str, sf=Depends(get_session_factory)):
    """Set bot status to STOPPED. The bot runner will stop the task.

    Idempotent: if already STOPPED, returns 200 with no-op.
    """
    repo = BotRepository(sf)
    b = repo.get(bot_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    if b.status == BotStatus.STOPPED:
        return {"bot_id": bot_id, "status": "STOPPED", "message": "already stopped"}
    repo.update_status(bot_id, BotStatus.STOPPED)
    return {"bot_id": bot_id, "status": "STOPPED"}
