"""System status endpoints.

GET /api/status — heartbeats, alerts, system health, account info, P&L
All live state reads from Redis. SQLite is not queried.
"""
import json as _json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from ib_trader.api.deps import get_redis
from ib_trader.redis.state import StateKeys, StateStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["system"])

_HEARTBEAT_STALE_SECONDS = 60


@router.get("/system/health")
async def get_system_health():
    """Liveness probe for the API process.

    Lightweight and dependency-free — no Redis call, no DB call. The
    external pager (see `ops/health_check.sh`, GH #47) polls this
    every 60s to decide whether the process itself is responsive.
    Keep this endpoint intentionally boring; for richer signals use
    ``/api/status``.
    """
    import os as _os
    return {"status": "ok", "pid": _os.getpid()}


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
                except Exception as e:
                    logger.debug("failed to parse heartbeat for %s", process_name, exc_info=e)

    # Connection status
    engine_alive = service_health.get("engine", False)
    connection_status = "connected" if engine_alive else "disconnected"

    # Account mode — prefer what the engine actually connected to (written
    # to Redis at engine startup). Fall back to a best-effort .env parse
    # only when the engine hasn't run yet; that path is an educated guess,
    # not a source of truth.
    account_mode = "unknown"
    acct = ""
    if redis:
        try:
            session = await StateStore(redis).get(StateKeys.engine_session())
            if session:
                account_mode = session.get("account_mode") or "unknown"
                acct = session.get("account_id") or ""
        except Exception as e:
            logger.debug("engine session read failed", exc_info=e)
    if account_mode == "unknown":
        from ib_trader.config.loader import load_env
        try:
            env_vars = load_env()
            acct = env_vars.get("IB_ACCOUNT_ID", "")
            if acct:
                account_mode = "paper" if acct.startswith("DU") else "live"
        except Exception as e:
            logger.debug("failed to load account env", exc_info=e)

    # Open alerts from Redis
    alert_list = []
    alert_count = 0
    if redis:
        try:
            raw_alerts = await redis.hgetall(StateKeys.alerts_active())
            for _aid, val in raw_alerts.items():
                try:
                    alert_list.append(_json.loads(val))
                except (ValueError, TypeError) as e:
                    logger.debug("failed to decode alert", exc_info=e)
            alert_count = len(alert_list)
        except Exception as e:
            logger.debug("alerts fetch failed", exc_info=e)

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
                    except (ValueError, TypeError) as e:
                        logger.debug("failed to parse bot stats", exc_info=e)
        except Exception as e:
            logger.debug("realized pnl scan failed", exc_info=e)

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
