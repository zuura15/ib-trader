"""System status endpoints.

GET /api/status — heartbeats, alerts, system health, account info, P&L
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
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

    # Account mode — read from .env (always available), fall back to
    # transaction history. The old approach only looked at open orders,
    # so it showed "unknown" when no orders existed (fresh install, or
    # Gateway was down).
    from ib_trader.config.loader import load_env
    account_mode = "unknown"
    acct = ""
    try:
        env_vars = load_env()
        acct = env_vars.get("IB_ACCOUNT_ID", "")
    except Exception:
        pass
    if not acct:
        # Fall back to transaction history
        try:
            from ib_trader.data.repositories.transaction_repository import TransactionRepository
            txn_repo = TransactionRepository(sf)
            open_orders = txn_repo.get_open_orders()
            if open_orders:
                acct = open_orders[0].account_id
        except Exception:
            pass
    if acct:
        account_mode = "paper" if acct.startswith("DU") else "live"

    # Realized P&L from closed trade groups
    from ib_trader.data.repository import TradeRepository
    realized_pnl = 0.0
    try:
        all_trades = TradeRepository(sf).get_all()
        closed = [t for t in all_trades if t.status.value == "CLOSED" and t.realized_pnl is not None]
        realized_pnl = sum(float(t.realized_pnl) for t in closed)
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
        "account_id": acct or None,
        "service_health": service_health,
        "realized_pnl": realized_pnl,
        "engine_uptime_seconds": engine_uptime_seconds,
        "alert_count": len(open_alerts),
    }
