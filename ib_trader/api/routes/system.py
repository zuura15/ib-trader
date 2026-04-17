"""System status endpoints.

GET /api/status — heartbeats, alerts, system health, account info, P&L
All live state reads from Redis. SQLite is not queried.
"""
import json as _json
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from ib_trader.api.deps import get_redis
from ib_trader.redis.state import StateKeys

router = APIRouter(prefix="/api", tags=["system"])

_HEARTBEAT_STALE_SECONDS = 60


@router.get("/status")
async def get_status(redis=Depends(get_redis)):
    """Return full system status from Redis."""
    now = datetime.now(timezone.utc)

    # Heartbeats from Redis hb:* keys
    hb_list = []
    service_health = {}
    engine_uptime_seconds = 0

    if redis:
        for process_name in ("ENGINE", "DAEMON", "API", "BOT_RUNNER", "REPL"):
            raw = await redis.get(StateKeys.process_heartbeat(process_name))
            if raw:
                try:
                    doc = _json.loads(raw)
                    ts_str = doc.get("ts", "")
                    last_seen = datetime.fromisoformat(ts_str) if ts_str else now
                    if last_seen.tzinfo is None:
                        last_seen = last_seen.replace(tzinfo=timezone.utc)
                    age = (now - last_seen).total_seconds()
                    alive = age < _HEARTBEAT_STALE_SECONDS

                    hb_list.append({
                        "process": process_name,
                        "last_seen_at": ts_str,
                        "pid": doc.get("pid"),
                        "alive": alive,
                        "age_seconds": round(age),
                    })
                    service_health[process_name.lower()] = alive

                    if process_name == "ENGINE" and alive:
                        engine_uptime_seconds = round(age)
                except Exception:
                    pass

    # Connection status
    engine_alive = service_health.get("engine", False)
    connection_status = "connected" if engine_alive else "disconnected"

    # Account mode from .env
    from ib_trader.config.loader import load_env
    account_mode = "unknown"
    acct = ""
    try:
        env_vars = load_env()
        acct = env_vars.get("IB_ACCOUNT_ID", "")
    except Exception:
        pass
    if acct:
        account_mode = "paper" if acct.startswith("DU") else "live"

    # Open alerts from Redis
    alert_list = []
    alert_count = 0
    if redis:
        try:
            raw_alerts = await redis.hgetall(StateKeys.alerts_active())
            for aid, val in raw_alerts.items():
                try:
                    alert_list.append(_json.loads(val))
                except (ValueError, TypeError):
                    pass
            alert_count = len(alert_list)
        except Exception:
            pass

    # Realized P&L from bot:stats:* Redis hashes
    realized_pnl = 0.0
    if redis:
        try:
            async for key in redis.scan_iter(match="bot:stats:*"):
                raw = await redis.get(key)
                if raw:
                    try:
                        stats = _json.loads(raw)
                        realized_pnl += float(stats.get("pnl_today", 0))
                    except (ValueError, TypeError):
                        pass
        except Exception:
            pass

    return {
        "heartbeats": hb_list,
        "alerts": alert_list,
        "connection_status": connection_status,
        "account_mode": account_mode,
        "account_id": acct or None,
        "service_health": service_health,
        "realized_pnl": realized_pnl,
        "engine_uptime_seconds": engine_uptime_seconds,
        "alert_count": alert_count,
    }
