"""System status endpoints.

GET /api/status — heartbeats, alerts, system health, account info, P&L
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import text

from ib_trader.api.deps import get_heartbeats, get_alerts, get_session_factory
from ib_trader.api.serializers import HeartbeatResponse, AlertResponse
from ib_trader.data.repository import HeartbeatRepository, AlertRepository

router = APIRouter(prefix="/api", tags=["system"])

_HEARTBEAT_STALE_SECONDS = 60


@router.get("/status")
def get_status(
    heartbeats: HeartbeatRepository = Depends(get_heartbeats),
    alerts: AlertRepository = Depends(get_alerts),
    sf=Depends(get_session_factory),
):
    """Return full system status: heartbeats, health, account, P&L."""
    now = datetime.now(timezone.utc)

    # Heartbeats
    hb_list = []
    service_health = {}
    engine_uptime_seconds = 0

    for process_name in ("ENGINE", "DAEMON", "API", "BOT_RUNNER", "REPL"):
        hb = heartbeats.get(process_name)
        if hb:
            last_seen = hb.last_seen_at
            if last_seen.tzinfo is None:
                last_seen = last_seen.replace(tzinfo=timezone.utc)
            age = (now - last_seen).total_seconds()
            alive = age < _HEARTBEAT_STALE_SECONDS

            hb_list.append({
                "process": hb.process,
                "last_seen_at": hb.last_seen_at.isoformat(),
                "pid": hb.pid,
                "alive": alive,
                "age_seconds": round(age),
            })
            service_health[process_name.lower()] = alive

            if process_name == "ENGINE" and alive:
                engine_uptime_seconds = round(age)

    # Connection status — derived from ENGINE heartbeat
    engine_alive = service_health.get("engine", False)
    connection_status = "connected" if engine_alive else "disconnected"

    # Account mode — read from .env via settings or detect from account_id
    # Check the pending_commands or position_cache for account info
    account_mode = "unknown"
    try:
        s = sf()
        row = s.execute(text(
            "SELECT account_id FROM position_cache LIMIT 1"
        )).fetchone()
        if row:
            acct = row[0]
            account_mode = "paper" if acct.startswith("DU") else "live"
    except Exception:
        pass

    # P&L — sum from position_cache (basic: unrealized not available without live prices)
    # Realized P&L from closed trade groups
    realized_pnl = 0.0
    try:
        s = sf()
        row = s.execute(text(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM trade_groups "
            "WHERE status = 'CLOSED' AND realized_pnl IS NOT NULL"
        )).fetchone()
        if row:
            realized_pnl = float(row[0])
    except Exception:
        pass

    # Open alerts
    open_alerts = alerts.get_open()
    alert_list = [
        {
            "id": a.id, "severity": a.severity.value, "trigger": a.trigger,
            "message": a.message, "created_at": a.created_at.isoformat(),
            "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
        }
        for a in open_alerts
    ]

    return {
        "heartbeats": hb_list,
        "alerts": alert_list,
        "connection_status": connection_status,
        "account_mode": account_mode,
        "service_health": service_health,
        "realized_pnl": realized_pnl,
        "engine_uptime_seconds": engine_uptime_seconds,
        "alert_count": len(open_alerts),
    }
