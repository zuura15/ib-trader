"""System status endpoints.

GET /api/status — heartbeats, alerts, system health, account info, P&L
All live state reads from Redis. SQLite is not queried.
"""
import json as _json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from ib_trader.api.deps import get_bot_trades, get_redis
from ib_trader.data.repositories.bot_trade_repository import BotTradeRepository
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


@router.post("/system/ib/reconnect")
async def post_ib_reconnect():
    """Proxy to the engine's manual IB-reconnect endpoint.

    Surfaces ``POST /engine/ib/reconnect`` to the LAN so the operator
    can trigger a reconnect cycle from the browser console / a curl
    on the laptop, e.g. when the tick-silence watchdog has fired.
    Returns the engine's structured ReconnectResult dict.
    """
    import os as _os
    import httpx
    from fastapi import HTTPException

    port = _os.environ.get("IB_TRADER_ENGINE_INTERNAL_PORT", "8081")
    url = f"http://127.0.0.1:{port}/engine/ib/reconnect"
    # Reconnect can take 10–30 s on a slow IB Gateway; give it room.
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(url)
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail=f"engine unreachable: {e}") from e
    except httpx.TimeoutException as e:
        raise HTTPException(status_code=504, detail=f"engine reconnect timeout: {e}") from e
    if resp.status_code >= 400:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
    return resp.json()


@router.post("/system/resync")
async def post_system_resync(body: dict | None = None):
    """Operator-driven resync — the "untie the knot" button.

    Default (``body = {"deep": false}``):
      * POST ``/engine/reload-watchlist`` — re-subscribes any watchlist
        symbol that has fallen out of ``_streaming`` (e.g. after a
        scheduled IB reconnect dropped a bot-owned symbol).
      * GET  ``/engine/positions/refresh`` for a sentinel symbol —
        forces ``reqPositionsAsync`` so positionEvents fire fresh
        snapshots for every held position.

    Deep (``body = {"deep": true}``):
      * Above PLUS POST ``/engine/prophylactic-resub`` — cycles every
        active ``reqMktData`` to unstick IB-side parked subscriptions.
        Reserve for "everything is stuck and the light resync did
        not help" — briefly interrupts every quote stream.

    Best-effort: each engine call's success/failure is reported
    individually so the UI can show what landed. A single failed
    call does not abort the rest.
    """
    import os as _os
    import httpx
    from fastapi import HTTPException

    deep = bool((body or {}).get("deep"))
    port = _os.environ.get("IB_TRADER_ENGINE_INTERNAL_PORT", "8081")
    base = f"http://127.0.0.1:{port}"

    results: dict[str, dict] = {}

    async def _call(name: str, method: str, path: str, *, params=None, timeout=10.0):
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                if method == "POST":
                    resp = await client.post(f"{base}{path}", params=params)
                else:
                    resp = await client.get(f"{base}{path}", params=params)
            results[name] = {
                "ok": resp.status_code < 400,
                "status": resp.status_code,
            }
            try:
                payload = resp.json()
                # Cap nested payloads — the engine endpoints can return
                # multi-symbol details and we don't need them all in
                # the proxy response.
                results[name]["data"] = payload
            except Exception:
                results[name]["text"] = resp.text[:500]
        except httpx.ConnectError as e:
            results[name] = {"ok": False, "error": f"engine unreachable: {e}"}
        except httpx.TimeoutException as e:
            results[name] = {"ok": False, "error": f"timeout: {e}"}
        except Exception as e:
            results[name] = {"ok": False, "error": str(e)}

    await _call("reload_watchlist", "POST", "/engine/reload-watchlist")
    # positions/refresh takes a ``symbol=`` arg to pull a single
    # symbol's qty back, but the side-effect (reqPositionsAsync →
    # fresh positionEvents for every position) is what we want
    # regardless of which symbol is passed. Use a benign sentinel.
    await _call(
        "positions_refresh", "GET", "/engine/positions/refresh",
        params={"symbol": "_resync_sentinel"},
    )

    if deep:
        # Prophylactic resub takes ~30 s for ~14 subs at 2 s stagger;
        # give it head room without leaving the operator hanging.
        await _call(
            "prophylactic_resub", "POST", "/engine/prophylactic-resub",
            timeout=90.0,
        )

    any_ok = any(r.get("ok") for r in results.values())
    if not any_ok:
        raise HTTPException(
            status_code=502,
            detail={"message": "every engine call failed", "results": results},
        )
    return {"deep": deep, "results": results}


@router.get("/status")
async def get_status(
    redis=Depends(get_redis),
    bot_trades: BotTradeRepository = Depends(get_bot_trades),
):
    """Return full system status from Redis."""
    now = datetime.now(timezone.utc)

    # Heartbeats from Redis hb:* keys
    hb_list = []
    service_health = {}
    engine_uptime_seconds = 0
    engine_started_at: str | None = None

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
                        "started_at": doc.get("started_at"),
                        "pid": doc.get("pid"),
                        "alive": alive,
                        "age_seconds": round(age),
                    })
                    service_health[process_name.lower()] = alive

                    if process_name == "ENGINE" and alive:
                        # ``engine_uptime_seconds`` historically held the
                        # heartbeat age, which is misleading. Compute real
                        # uptime from ``started_at`` when the engine writes
                        # it; fall back to heartbeat age otherwise.
                        engine_started_at = doc.get("started_at")
                        if engine_started_at:
                            try:
                                started_dt = datetime.fromisoformat(engine_started_at)
                                if started_dt.tzinfo is None:
                                    started_dt = started_dt.replace(tzinfo=timezone.utc)
                                engine_uptime_seconds = round(
                                    (now - started_dt).total_seconds(),
                                )
                            except Exception as e:
                                logger.debug("started_at parse failed", exc_info=e)
                                engine_uptime_seconds = round(age)
                        else:
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

    # Realized P&L — rolling 24h window summed from bot_trades. Aligns
    # with the Bot Trades panel (same source, same window) so the
    # header number equals the sum of visible rows. Replaces the
    # previous calendar-day pnl_today read off Redis bot:stats:*,
    # which (a) midnight-rotated to zero, and (b) drifted from the
    # panel when force-quit / crash-path exits bypassed
    # ``_risk_mw.record_pnl``.
    try:
        realized_pnl = float(
            bot_trades.sum_realized_pnl_last_hours(24.0)
        )
    except Exception as e:
        logger.debug("rolling 24h pnl query failed", exc_info=e)
        realized_pnl = 0.0

    return {
        "heartbeats": hb_list,
        "alerts": alert_list,
        "connection_status": connection_status,
        "account_mode": account_mode,
        "account_id": acct or None,
        "service_health": service_health,
        "realized_pnl": realized_pnl,
        "engine_uptime_seconds": engine_uptime_seconds,
        "engine_started_at": engine_started_at,
        "alert_count": alert_count,
    }
