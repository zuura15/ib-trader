"""Alert endpoints.

GET /api/alerts — list active alerts from Redis alerts:active hash
POST /api/alerts/{alert_id}/resolve — resolve: remove from Redis + archive in SQLite
"""
import json

from fastapi import APIRouter, Depends, HTTPException

from ib_trader.api.deps import get_alerts, get_redis
from ib_trader.api.serializers import AlertResponse
from ib_trader.data.repository import AlertRepository
from ib_trader.redis.state import StateKeys

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


@router.get("")
async def list_alerts(redis=Depends(get_redis)):
    """List active (unresolved) alerts from Redis."""
    if redis is None:
        return []
    raw = await redis.hgetall(StateKeys.alerts_active())
    alerts = []
    for aid, val in raw.items():
        try:
            alerts.append(json.loads(val))
        except (json.JSONDecodeError, TypeError):
            pass
    return alerts


@router.post("/{alert_id}/resolve", status_code=204)
async def resolve_alert(
    alert_id: str,
    alerts: AlertRepository = Depends(get_alerts),
    redis=Depends(get_redis),
):
    """Resolve an alert: remove from Redis active hash + archive in SQLite."""
    if redis:
        await redis.hdel(StateKeys.alerts_active(), alert_id)
    # SQLite archival write
    try:
        alerts.resolve(alert_id)
    except Exception:
        pass  # alert may not exist in SQLite if it was created post-migration
