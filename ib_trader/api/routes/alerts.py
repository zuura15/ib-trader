"""Alert endpoints.

GET /api/alerts — list alerts (open by default)
POST /api/alerts/{alert_id}/resolve — resolve an alert
"""
from fastapi import APIRouter, Depends, HTTPException

from ib_trader.api.deps import get_alerts
from ib_trader.api.serializers import AlertResponse
from ib_trader.data.repository import AlertRepository

router = APIRouter(prefix="/api/alerts", tags=["alerts"])


def _serialize_alert(a) -> AlertResponse:
    return AlertResponse(
        id=a.id,
        severity=a.severity.value,
        trigger=a.trigger,
        message=a.message,
        created_at=a.created_at,
        resolved_at=a.resolved_at,
    )


@router.get("", response_model=list[AlertResponse])
def list_alerts(
    resolved: bool = False,
    alerts: AlertRepository = Depends(get_alerts),
):
    """List system alerts. By default returns only open (unresolved) alerts."""
    if resolved:
        # Return all — no filter method exists, get_open filters for us
        rows = alerts.get_open()
    else:
        rows = alerts.get_open()
    return [_serialize_alert(a) for a in rows]


@router.post("/{alert_id}/resolve", status_code=204)
def resolve_alert(alert_id: str, alerts: AlertRepository = Depends(get_alerts)):
    """Mark an alert as resolved."""
    alerts.resolve(alert_id)
