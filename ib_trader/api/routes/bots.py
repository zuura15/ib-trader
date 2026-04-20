"""Bot management endpoints.

GET /api/bots — list all bots
GET /api/bots/{bot_id} — get a single bot
GET /api/bots/{bot_id}/events — get bot events (audit trail)
POST /api/bots/{bot_id}/start — start a bot
POST /api/bots/{bot_id}/stop — stop a bot
"""
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Query

from ib_trader.api.deps import get_session_factory, get_redis
from ib_trader.data.models import BotStatus
from ib_trader.data.repositories.bot_repository import BotRepository, BotEventRepository
from ib_trader.bots.definition import BotDefinition
from ib_trader.bots.fsm import FSM, BotState
from ib_trader.bots.state import BotStateStore

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bots", tags=["bots"])


def _status_for_ui(state: str) -> str:
    """Legacy status label for UI/back-compat — maps FSM state to the
    old RUNNING/STOPPED/ERROR/PAUSED vocabulary for the frontend badge
    until it's migrated to use the `state` field directly.
    """
    if state == BotState.OFF.value:
        return "STOPPED"
    if state == BotState.ERRORED.value:
        return "ERROR"
    return "RUNNING"


def _serialize_bot(b, fsm_doc: dict | None = None,
                   heartbeat: dict | None = None,
                   trade_stats: dict | None = None) -> dict:
    """Compose the API response for a bot row.

    Single source of truth for runtime state is ``fsm_doc`` — the
    bot:<id>:fsm Redis document. Heartbeat is fetched separately (still
    lives on its own Redis key with TTL). SQLite fields are unused for
    live state.
    """
    ref_id = None
    try:
        cfg = json.loads(b.config_json) if b.config_json else {}
        strategy_config_path = cfg.get("strategy_config")
        if strategy_config_path:
            import yaml
            with open(strategy_config_path) as f:
                strat = yaml.safe_load(f)
                ref_id = strat.get("ref_id")
    except Exception as e:
        logger.debug("failed to load strategy ref_id", exc_info=e)

    fsm_doc = fsm_doc or {"state": BotState.OFF.value}
    state = fsm_doc.get("state", BotState.OFF.value)
    error_reason = fsm_doc.get("error_reason")
    error_message = fsm_doc.get("error_message")
    last_heartbeat = (heartbeat or {}).get("ts")

    ts = trade_stats or {}
    trades_total = ts.get("total", 0)
    trades_today = ts.get("today", 0)
    pnl_today = ts.get("pnl_today", 0)

    return {
        "id": b.id,
        "name": b.name,
        "strategy": b.strategy,
        "broker": b.broker,
        "state": state,
        "status": _status_for_ui(state),   # legacy alias for unmigrated frontend
        "error_reason": error_reason,
        "tick_interval_seconds": b.tick_interval_seconds,
        "last_heartbeat": last_heartbeat,
        "last_signal": b.last_signal,
        "last_action": fsm_doc.get("order_origin"),
        "last_action_at": fsm_doc.get("updated_at"),
        "error_message": error_message,
        "trades_total": trades_total,
        "trades_today": trades_today,
        "pnl_today": str(pnl_today),
        "symbols_json": b.symbols_json,
        "ref_id": ref_id,
        # Position snapshot for the UI's PositionLine — only populated
        # when the bot actually has a position.
        "position": {
            "qty": fsm_doc.get("qty") or "0",
            "entry_price": fsm_doc.get("entry_price"),
            "high_water_mark": fsm_doc.get("high_water_mark"),
            "current_stop": fsm_doc.get("current_stop"),
            "trail_activated": fsm_doc.get("trail_activated", False),
            "last_price": fsm_doc.get("last_price"),
        } if state in (
            BotState.ENTRY_ORDER_PLACED.value,
            BotState.AWAITING_EXIT_TRIGGER.value,
            BotState.EXIT_ORDER_PLACED.value,
        ) else None,
    }


def _serialize_bot_from_defn(
    defn: BotDefinition,
    fsm_doc: dict | None = None,
    heartbeat: dict | None = None,
) -> dict:
    """Compose the API response from a BotDefinition + FSM doc. No SQLite."""
    fsm_doc = fsm_doc or {"state": BotState.OFF.value}
    state = fsm_doc.get("state", BotState.OFF.value)
    error_reason = fsm_doc.get("error_reason")
    error_message = fsm_doc.get("error_message")
    last_heartbeat = (heartbeat or {}).get("ts")

    ref_id = defn.config.get("ref_id") or defn.name

    return {
        "id": defn.id,
        "name": defn.name,
        "strategy": defn.strategy,
        "broker": defn.broker,
        "state": state,
        "status": _status_for_ui(state),
        "error_reason": error_reason,
        "tick_interval_seconds": defn.tick_interval_seconds,
        "last_heartbeat": last_heartbeat,
        "last_signal": None,
        "last_action": fsm_doc.get("order_origin"),
        "last_action_at": fsm_doc.get("updated_at"),
        "error_message": error_message,
        "trades_total": 0,
        "trades_today": 0,
        "pnl_today": "0",
        "symbols_json": json.dumps(list(defn.symbols)) if defn.symbols else "[]",
        "ref_id": ref_id,
        "position": {
            "qty": fsm_doc.get("qty") or "0",
            "entry_price": fsm_doc.get("entry_price"),
            "high_water_mark": fsm_doc.get("high_water_mark"),
            "current_stop": fsm_doc.get("current_stop"),
            "trail_activated": fsm_doc.get("trail_activated", False),
            "last_price": fsm_doc.get("last_price"),
        } if state in (
            BotState.ENTRY_ORDER_PLACED.value,
            BotState.AWAITING_EXIT_TRIGGER.value,
            BotState.EXIT_ORDER_PLACED.value,
        ) else None,
    }


async def _fetch_fsm_docs(redis, bot_ids: list[str]) -> dict[str, dict]:
    """Read each bot's FSM doc. Empty dict per bot if Redis is down."""
    if redis is None:
        return {bid: {"state": BotState.OFF.value} for bid in bot_ids}
    import asyncio as _asyncio
    fsms = [FSM(bid, redis) for bid in bot_ids]
    docs = await _asyncio.gather(*[f.load() for f in fsms])
    return {bid: doc for bid, doc in zip(bot_ids, docs, strict=True)}


# FSM states that indicate the bot is actively running (not OFF / ERRORED).
# Used by the reload endpoint to gate immutable-field edits.
_RUNNING_FSM_STATES = frozenset({
    BotState.AWAITING_ENTRY_TRIGGER,
    BotState.ENTRY_ORDER_PLACED,
    BotState.AWAITING_EXIT_TRIGGER,
    BotState.EXIT_ORDER_PLACED,
})


async def _running_bot_ids(redis) -> frozenset[str]:
    """Snapshot the set of bot IDs whose FSM is in a running state.

    Returns an empty set when Redis isn't available — the reload
    endpoint then degrades to "no running bots known", which means YAML
    edits to immutable fields will be accepted. That is the correct
    behaviour during a Redis outage where we have no way to confirm
    what's actually running.
    """
    from ib_trader.bots import registry_config
    if redis is None:
        return frozenset()
    defs = registry_config.all_definitions()
    if not defs:
        return frozenset()
    docs = await _fetch_fsm_docs(redis, [d.id for d in defs])
    out = set()
    for bid, doc in docs.items():
        try:
            cur = BotState(doc.get("state", BotState.OFF.value))
        except ValueError:
            continue
        if cur in _RUNNING_FSM_STATES:
            out.add(bid)
    return frozenset(out)


@router.get("")
async def list_bots(redis=Depends(get_redis)):
    """List all bots. Identity from YAML registry, state from Redis FSM.
    No SQLite reads.
    """
    from ib_trader.bots import registry_config
    import asyncio as _asyncio

    defs = registry_config.all_definitions()
    if not defs:
        return []

    fsm_docs = await _fetch_fsm_docs(redis, [d.id for d in defs])
    bss = BotStateStore(redis)
    heartbeats = await _asyncio.gather(
        *[bss.get_heartbeat(d.id) for d in defs]
    ) if redis else [None] * len(defs)
    hb_map = {d.id: ({"ts": hb} if hb else None) for d, hb in zip(defs, heartbeats, strict=True)}

    return [
        _serialize_bot_from_defn(
            d,
            fsm_doc=fsm_docs.get(d.id),
            heartbeat=hb_map.get(d.id),
        )
        for d in defs
    ]


@router.post("/reload", status_code=202)
async def reload_bots(
    force: bool = False,
    sf=Depends(get_session_factory),
    redis=Depends(get_redis),
):
    """Rescan ``config/bots/*.yaml`` and reconcile the SQLite table.

    Semantics (codex fix F):
      - Bots added to disk → inserted as STOPPED.
      - Bots edited on disk:
         - mutable fields (name, tick_interval) update in place.
         - immutable fields (strategy, ref_id, symbol, broker) on a
           RUNNING bot → refused (409). Stop the bot first.
      - Bots deleted from disk:
         - STOPPED bot → refused unless ``?force=true`` (409).
         - RUNNING bot → refused outright; stop it first, then reload.

    Reload is additive / next-start only. A bot that is currently
    running keeps the snapshot it was started with; YAML edits take
    effect only on the next start.
    """
    from ib_trader.bots.bootstrap import bootstrap_bots_from_yaml, BootstrapError
    # Snapshot which bots are running according to the live FSM in
    # Redis. Bootstrap uses this to gate immutable-field edits + deletes
    # — the SQLite ``bots.status`` column is no longer authoritative.
    running_ids = await _running_bot_ids(redis)
    try:
        report = bootstrap_bots_from_yaml(
            sf, force=force, running_bot_ids=running_ids,
        )
    except BootstrapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    finally:
        sf.remove()

    if redis is not None:
        from ib_trader.redis.streams import publish_activity
        await publish_activity(redis, "bots")

    return {
        "added": report.added,
        "updated": report.updated,
        "unchanged": report.unchanged,
        "removed": report.removed,
    }


@router.get("/{bot_id}")
async def get_bot(bot_id: str, redis=Depends(get_redis)):
    defn = _get_defn_or_404(bot_id)
    fsm_docs = await _fetch_fsm_docs(redis, [defn.id])
    bss = BotStateStore(redis)
    hb_ts = await bss.get_heartbeat(defn.id) if redis else None
    return _serialize_bot_from_defn(
        defn,
        fsm_doc=fsm_docs.get(defn.id),
        heartbeat={"ts": hb_ts} if hb_ts else None,
    )


def _get_defn_or_404(bot_id: str) -> BotDefinition:
    """Look up a bot by id in the in-memory YAML registry. No SQLite."""
    from ib_trader.bots import registry_config
    defn = registry_config.get(bot_id)
    if defn is None:
        raise HTTPException(status_code=404, detail="Bot not found")
    return defn


def _runner_url() -> str:
    import os
    port = os.environ.get("IB_TRADER_BOT_RUNNER_PORT", "8082")
    return f"http://127.0.0.1:{port}"


@router.post("/{bot_id}/start", status_code=202)
async def start_bot(bot_id: str, sf=Depends(get_session_factory), redis=Depends(get_redis)):
    """Start a bot. Proxies to the runner's internal API.

    The runner is the sole FSM writer — it transitions state AFTER the
    task actually spawns. API doesn't write FSM state itself.
    """
    _get_defn_or_404(bot_id)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.post(f"{_runner_url()}/bots/{bot_id}/start")
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            result = resp.json()
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail="Bot runner unavailable") from e
    # SQLite archival write
    BotRepository(sf).update_status(bot_id, BotStatus.RUNNING)
    if redis:
        from ib_trader.redis.streams import publish_activity
        await publish_activity(redis, "bots")
    return result


@router.post("/{bot_id}/stop", status_code=202)
async def stop_bot(bot_id: str, sf=Depends(get_session_factory), redis=Depends(get_redis)):
    """Clean stop — proxies to the runner's internal API."""
    _get_defn_or_404(bot_id)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.post(f"{_runner_url()}/bots/{bot_id}/stop")
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            result = resp.json()
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail="Bot runner unavailable") from e
    BotRepository(sf).update_status(bot_id, BotStatus.STOPPED)
    if redis:
        from ib_trader.redis.streams import publish_activity
        await publish_activity(redis, "bots")
    return result


@router.post("/{bot_id}/force-stop", status_code=202)
async def force_stop_bot(bot_id: str, sf=Depends(get_session_factory), redis=Depends(get_redis)):
    """Emergency stop — proxies to the runner."""
    _get_defn_or_404(bot_id)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            resp = await c.post(f"{_runner_url()}/bots/{bot_id}/force-stop")
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            result = resp.json()
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail="Bot runner unavailable") from e
    BotRepository(sf).update_status(bot_id, BotStatus.ERROR)
    if redis:
        from ib_trader.redis.streams import publish_activity
        await publish_activity(redis, "bots")
    return result


@router.get("/{bot_id}/state")
async def get_bot_state(bot_id: str, redis=Depends(get_redis)):
    """Return the bot's full state from the single bot:<uuid> key."""
    _get_defn_or_404(bot_id)
    if not redis:
        return {"state": BotState.OFF.value}
    from ib_trader.redis.state import StateStore
    doc = await StateStore(redis).get(f"bot:{bot_id}")
    return doc or {"state": BotState.OFF.value}


@router.post("/{bot_id}/force-buy", status_code=202)
async def force_buy(bot_id: str, redis=Depends(get_redis)):
    """Force-buy — proxies to the runner.

    The runner's handler synchronously walks the full order lifecycle
    (PlaceOrder → engine → IB → reprice loop). The engine's reprice
    window is 10s and IB fill latency adds a few more, so the timeout
    has to comfortably exceed that — 30s keeps us safely above the
    steady-state submission path while still failing loud if the
    runner truly hangs.
    """
    _get_defn_or_404(bot_id)
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            resp = await c.post(f"{_runner_url()}/bots/{bot_id}/force-buy")
            if resp.status_code >= 400:
                raise HTTPException(status_code=resp.status_code, detail=resp.text)
            return resp.json()
    except httpx.ConnectError as e:
        raise HTTPException(status_code=503, detail="Bot runner unavailable") from e
    except httpx.ReadTimeout as e:
        raise HTTPException(
            status_code=504,
            detail="Bot runner did not respond within 30s; the order may still be in flight — check the positions and orders panes before retrying.",
        ) from e


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
