"""Bot management endpoints.

GET /api/bots — list all bots
GET /api/bots/{bot_id} — get a single bot
GET /api/bots/{bot_id}/events — get bot events (audit trail)
POST /api/bots/{bot_id}/start — start a bot
POST /api/bots/{bot_id}/stop — stop a bot
"""
from fastapi import APIRouter, Depends, HTTPException, Query

from ib_trader.api.deps import get_session_factory
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


@router.get("/{bot_id}/state")
def get_bot_state(bot_id: str, sf=Depends(get_session_factory)):
    """Return the bot's live position state from its JSON state file."""
    import json
    from pathlib import Path

    repo = BotRepository(sf)
    b = repo.get(bot_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Bot not found")

    # Read the JSON state file
    config = json.loads(b.config_json) if b.config_json else {}
    symbol = config.get("symbol", "")
    state_dir = Path.home() / ".ib-trader" / "bot-state"
    state_file = state_dir / f"{bot_id}-{symbol}.json"

    if not state_file.exists():
        return {"position_state": "FLAT"}

    try:
        return json.loads(state_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {"position_state": "FLAT"}


@router.post("/{bot_id}/force-buy", status_code=202)
def force_buy(bot_id: str, sf=Depends(get_session_factory)):
    """Signal a running bot to place a forced buy on its next tick.

    Only works when bot is RUNNING. The bot checks last_action at
    the top of each tick and clears the flag immediately.
    """
    repo = BotRepository(sf)
    b = repo.get(bot_id)
    if b is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    if b.status != BotStatus.RUNNING:
        raise HTTPException(status_code=409, detail="Bot is not running")
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
