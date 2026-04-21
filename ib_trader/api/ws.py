"""WebSocket endpoint for real-time data delivery.

Polls SQLite at a configurable interval and pushes diffs to connected clients.
Clients subscribe to channels (orders, trades, positions, alerts, commands, bots).
On connect, a full snapshot is sent. Subsequent messages are diffs only.

Protocol:
  Client → Server: {"type": "subscribe", "channels": ["orders", "trades", ...]}
  Server → Client: {"type": "snapshot", "data": {"orders": [...], ...}}
  Server → Client: {"type": "diff", "channel": "orders", "added": [...], "updated": [...], "removed": [...]}
  Client → Server: {"type": "ping"}
  Server → Client: {"type": "pong"}
"""
import asyncio
import hashlib
import json
import logging
from datetime import datetime
from decimal import Decimal
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

try:
    from redis.exceptions import ConnectionError as _RedisConnectionError
except ImportError:
    _RedisConnectionError = type(None)
from sqlalchemy.orm import scoped_session

from ib_trader.data.repository import (
    TradeRepository,
)
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository

logger = logging.getLogger(__name__)

router = APIRouter()

_POLL_INTERVAL_S = 1.5
_FALLBACK_REFRESH_S = 10.0  # upper bound if no activity notifications arrive


class _JSONEncoder(json.JSONEncoder):
    """Custom JSON encoder for Decimal, datetime, and enum types."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, 'value'):
            return obj.value
        return super().default(obj)


def _json_dumps(obj) -> str:
    return json.dumps(obj, cls=_JSONEncoder)


def _serialize_trade(t) -> dict:
    return {
        "id": t.id,
        "serial_number": t.serial_number,
        "symbol": t.symbol,
        "direction": t.direction,
        "status": t.status.value,
        "realized_pnl": str(t.realized_pnl) if t.realized_pnl is not None else None,
        "total_commission": str(t.total_commission) if t.total_commission is not None else None,
        "opened_at": t.opened_at.isoformat() if t.opened_at else None,
        "closed_at": t.closed_at.isoformat() if t.closed_at else None,
    }


def _serialize_order(t) -> dict:
    return {
        "id": str(t.ib_order_id or t.id),
        "symbol": t.symbol,
        "side": t.side,
        "qty_requested": str(t.quantity),
        "order_type": t.order_type,
        "status": t.action.value,
        "price_placed": str(t.price_placed) if t.price_placed else None,
        "ib_order_id": t.ib_order_id,
        "leg_type": t.leg_type.value if t.leg_type else None,
        "trade_serial": t.trade_serial,
        "placed_at": t.requested_at.isoformat() if t.requested_at else None,
    }


def _serialize_alert(a) -> dict:
    return {
        "id": a.id,
        "severity": a.severity.value,
        "trigger": a.trigger,
        "message": a.message,
        "created_at": a.created_at.isoformat() if a.created_at else None,
        "resolved_at": a.resolved_at.isoformat() if a.resolved_at else None,
    }


def _serialize_command(c) -> dict:
    return {
        "id": c.id,
        "source": c.source,
        "broker": c.broker,
        "command_text": c.command_text,
        "status": c.status.value,
        "output": c.output,
        "error": c.error,
        "submitted_at": c.submitted_at.isoformat() if c.submitted_at else None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


def _serialize_heartbeat(h) -> dict:
    return {
        "process": h.process,
        "last_seen_at": h.last_seen_at.isoformat() if h.last_seen_at else None,
        "pid": h.pid,
    }


def _hash_snapshot(data: list[dict]) -> str:
    """Hash a list of dicts for diff detection."""
    return hashlib.md5(_json_dumps(data).encode(), usedforsecurity=False).hexdigest()


def _compute_diff(old: list[dict], new: list[dict], key: str = "id") -> dict:
    """Compute added/updated/removed between two lists of dicts keyed by `key`.

    If a dict doesn't have the key field, falls back to its index as the key.
    """
    old_map = {item.get(key, f"_idx_{i}"): item for i, item in enumerate(old)}
    new_map = {item.get(key, f"_idx_{i}"): item for i, item in enumerate(new)}

    added = [v for k, v in new_map.items() if k not in old_map]
    removed = [v for k, v in old_map.items() if k not in new_map]
    updated = [
        v for k, v in new_map.items()
        if k in old_map and v != old_map[k]
    ]

    return {"added": added, "updated": updated, "removed": removed}


# Channel-specific primary keys for diff computation
_CHANNEL_KEYS: dict[str, str] = {
    "trades": "id",
    "orders": "id",
    "alerts": "id",
    "commands": "id",
    "heartbeats": "process",
    "bot_events": "id",
}


class _ChannelState:
    """Tracks snapshot state for a single channel to compute diffs."""

    def __init__(self, channel: str):
        self.data: list[dict] = []
        self.hash: str = ""
        self._key = _CHANNEL_KEYS.get(channel, "id")

    def update(self, new_data: list[dict]) -> dict | None:
        """Update state and return diff if anything changed, else None."""
        new_hash = _hash_snapshot(new_data)
        if new_hash == self.hash:
            return None

        diff = _compute_diff(self.data, new_data, key=self._key)
        self.data = new_data
        self.hash = new_hash

        if not diff["added"] and not diff["updated"] and not diff["removed"]:
            return None
        return diff


def _fetch_channel_data(channel: str, sf: scoped_session) -> list[dict]:
    """Fetch current data for a channel from SQLite."""
    # Each poll must get a fresh session, query, and fully release the
    # connection back to the pool.  scoped_session.remove() does this —
    # it closes the session AND removes it from the thread-local registry
    # so the next sf() call gets a brand-new session.  Without this,
    # concurrent async coroutines on the same thread accumulate sessions
    # and exhaust the QueuePool.
    try:
        session = sf()
        session.rollback()

        if channel == "trades":
            # TODO: migrate to trades:open Redis hash + trades:recent_closed list.
            # For now, still reads SQLite as the trades panel is rarely
            # viewed in real-time and is closer to archival than live state.
            return [_serialize_trade(t) for t in TradeRepository(sf).get_all()]

        elif channel == "orders":
            # Orders now served from Redis — handled in _fetch_channel_data_async.
            # Return empty here; the async wrapper overrides.
            return []

        elif channel == "alerts":
            # Alerts now served from Redis — handled in _fetch_channel_data_async.
            return []

        elif channel == "commands":
            return [_serialize_command(c)
                    for c in PendingCommandRepository(sf).get_by_source("api", limit=50)]

        elif channel == "heartbeats":
            # Heartbeats now served from Redis — handled in _fetch_channel_data_async.
            return []

        elif channel == "bot_events":
            from ib_trader.data.repositories.bot_repository import BotEventRepository
            events = BotEventRepository(sf).get_recent(limit=50)
            return [
                {
                    "id": e.id,
                    "bot_id": e.bot_id,
                    "event_type": e.event_type,
                    "message": e.message,
                    "payload": e.payload_json,
                    "trade_serial": e.trade_serial,
                    "recorded_at": e.recorded_at.isoformat() if e.recorded_at else None,
                }
                for e in events
            ]

        elif channel == "bots":
            # Delegate to the REST serializer so the WS channel and
            # /api/bots stay shape-identical. Redis runtime state is
            # overlaid by the async wrapper `_fetch_channel_data_async`
            # before this result reaches the wire — fetching it here
            # (sync) would need a nested loop.
            from ib_trader.bots import registry_config
            from ib_trader.api.routes.bots import _serialize_bot_from_defn
            return [_serialize_bot_from_defn(d) for d in registry_config.all_definitions()]

        elif channel == "status":
            # Now served from Redis via async wrapper
            return []

        return []
    finally:
        sf.remove()


_VALID_CHANNELS = {
    "trades", "orders", "alerts", "commands", "heartbeats",
    "bot_events", "bots", "status",
}


async def _fetch_channel_data_async(
    channel: str, sf: scoped_session, redis,
) -> list[dict]:
    """Async wrapper around ``_fetch_channel_data`` that overlays the
    FSM state for the "bots" channel. Non-bots channels delegate to the
    sync implementation unchanged.
    """
    data = _fetch_channel_data(channel, sf)

    # Heartbeats: read from Redis hb:* keys instead of SQLite
    if channel == "heartbeats" and redis:
        try:
            import json as _json
            data = []
            for process in ("REPL", "DAEMON", "ENGINE", "API", "BOT_RUNNER"):
                from ib_trader.redis.state import StateKeys
                raw = await redis.get(StateKeys.process_heartbeat(process))
                if raw:
                    try:
                        doc = _json.loads(raw)
                        data.append({
                            "id": process,
                            "process": process,
                            "pid": doc.get("pid"),
                            "last_seen_at": doc.get("ts"),
                        })
                    except (ValueError, TypeError):
                        pass
        except Exception:
            logger.exception(json.dumps({"event": "HEARTBEATS_REDIS_READ_FAILED"}))

    # Status: compute from Redis
    if channel == "status" and redis:
        try:
            from ib_trader.api.routes.system import get_status as _get_status_async
            payload = await _get_status_async(redis=redis)
            data = [{"id": "__status__", **payload}]
        except Exception:
            logger.exception(json.dumps({"event": "STATUS_REDIS_FETCH_FAILED"}))

    # Alerts: read from Redis hash
    if channel == "alerts" and redis:
        try:
            import json as _json
            from ib_trader.redis.state import StateKeys
            raw = await redis.hgetall(StateKeys.alerts_active())
            data = []
            for _aid, val in raw.items():
                try:
                    data.append(_json.loads(val))
                except (ValueError, TypeError) as e:
                    logger.debug("failed to decode alert", exc_info=e)
        except Exception:
            logger.exception(json.dumps({"event": "ALERTS_REDIS_READ_FAILED"}))

    # Orders: read from Redis hash instead of SQLite
    if channel == "orders" and redis:
        try:
            from ib_trader.redis.state import StateKeys
            import json as _json
            raw = await redis.hgetall(StateKeys.orders_open())
            data = []
            for _oid, val in raw.items():
                try:
                    data.append(_json.loads(val))
                except (ValueError, TypeError) as e:
                    logger.debug("failed to decode order", exc_info=e)
        except Exception:
            logger.exception(json.dumps({"event": "ORDERS_REDIS_READ_FAILED"}))

    if channel == "bots" and data:
        try:
            import asyncio as _asyncio
            from ib_trader.bots.lifecycle import BotState, bot_doc_key
            from ib_trader.bots.state import BotStateStore
            from ib_trader.redis.state import StateStore
            bss = BotStateStore(redis)
            if redis is not None:
                _store = StateStore(redis)
                docs = await _asyncio.gather(
                    *[_store.get(bot_doc_key(d["id"])) for d in data]
                )
                docs = [doc or {} for doc in docs]
            else:
                docs = [{} for _ in data]
            heartbeats = await _asyncio.gather(
                *[bss.get_heartbeat(d["id"]) for d in data]
            ) if redis else [None] * len(data)
            for d, doc, hb in zip(data, docs, heartbeats, strict=True):
                state = doc.get("state") or BotState.OFF.value
                d["state"] = state
                # Legacy alias until frontend is migrated
                if state == BotState.OFF.value:
                    d["status"] = "STOPPED"
                elif state == BotState.ERRORED.value:
                    d["status"] = "ERROR"
                else:
                    d["status"] = "RUNNING"
                d["error_reason"] = doc.get("error_reason")
                d["error_message"] = doc.get("error_message")
                d["last_action"] = doc.get("order_origin")
                d["last_action_at"] = doc.get("updated_at")
                d["last_heartbeat"] = hb
                if state in (
                    BotState.ENTRY_ORDER_PLACED.value,
                    BotState.AWAITING_EXIT_TRIGGER.value,
                    BotState.EXIT_ORDER_PLACED.value,
                ):
                    d["position"] = {
                        "qty": doc.get("qty") or "0",
                        "entry_price": doc.get("entry_price"),
                        "high_water_mark": doc.get("high_water_mark"),
                        "current_stop": doc.get("current_stop"),
                        "trail_activated": doc.get("trail_activated", False),
                        "last_price": doc.get("last_price"),
                    }
                else:
                    d["position"] = None
        except Exception:
            logger.exception(json.dumps({"event": "BOTS_FSM_OVERLAY_FAILED"}))
    return data

# When any activity in this set fires, the "status" channel is also marked
# dirty. Keeps /api/status in sync with its backing state without a poll.
_STATUS_TRIGGERS = frozenset({"trades", "orders", "alerts", "heartbeats", "commands"})


async def _stream_quote_to_ws(websocket: WebSocket, redis, symbol: str) -> None:
    """Push live quote updates from Redis stream to WebSocket client."""
    stream = f"quote:{symbol}"
    last_id = "$"
    while True:
        try:
            results = await redis.xread({stream: last_id}, block=10000)
            if not results:
                continue
            for _stream_name, entries in results:
                for entry_id, raw_data in entries:
                    last_id = entry_id
                    data = {}
                    for k, v in raw_data.items():
                        try:
                            data[k] = json.loads(v)
                        except (ValueError, TypeError):
                            data[k] = v
                    await websocket.send_text(_json_dumps({
                        "type": "quote",
                        "symbol": symbol,
                        "data": data,
                    }))
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except (ConnectionError, OSError, _RedisConnectionError):
            return  # Redis shut down — exit cleanly
        except Exception:
            logger.exception('{"event": "WS_QUOTE_STREAM_ERROR", "symbol": "%s"}', symbol)
            await asyncio.sleep(1)


async def _stream_bot_state_to_ws(websocket: WebSocket, redis, bot_ref: str, symbol: str) -> None:
    """Push bot position/strategy state updates to WebSocket.

    Subscribes to TWO streams:
      - bot:state:{bot_ref}:{symbol} — emitted by PersistenceMiddleware on
        every state write (driven by quotes, bars, fills — anything that
        causes the strategy to update its state).
      - fill:{bot_ref} — fill events from the engine (covers position
        changes from the engine side, which may not have a corresponding
        strategy state write yet).

    Either trigger reads the current state snapshot from Redis keys and
    pushes it. Stream payloads are markers; the keys are the source of truth.
    """
    from ib_trader.redis.streams import StreamNames
    from ib_trader.redis.state import StateStore

    # Resolve bot_ref → UUID via registry
    from ib_trader.bots import registry_config
    defn = registry_config.get_by_name(bot_ref)
    if defn is None:
        return
    bot_key = f"bot:{defn.id}"
    store = StateStore(redis)

    # Use activity stream as wake signal (fires on every FSM transition)
    streams = {StreamNames.ACTIVITY: "$"}

    async def push_snapshot():
        state_doc = await store.get(bot_key)
        try:
            await websocket.send_text(_json_dumps({
                "type": "bot_state",
                "bot_ref": bot_ref,
                "symbol": symbol,
                "strategy": state_doc,
                "position": state_doc,
            }))
        except (RuntimeError, WebSocketDisconnect) as e:
            raise asyncio.CancelledError() from e

    while True:
        try:
            results = await redis.xread(streams, block=10000)
            if not results:
                continue
            for stream_name, entries in results:
                for entry_id, _raw_data in entries:
                    streams[stream_name] = entry_id
            await push_snapshot()
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except (ConnectionError, OSError, _RedisConnectionError):
            return
        except Exception:
            logger.exception('{"event": "WS_BOT_STATE_STREAM_ERROR"}')
            await asyncio.sleep(1)


def _read_log_tail(log_path: str, backlog: int) -> list[str]:
    """Return the last `backlog` lines of a text file. Runs in a worker
    thread via asyncio.to_thread so the event loop stays responsive."""
    from collections import deque
    try:
        with open(log_path, "r") as f:
            return list(deque(f, maxlen=backlog))
    except FileNotFoundError:
        return []


def _parse_log_entries(lines: list[str]) -> list[dict]:
    """Parse structured JSON log lines into the shape the LogStream UI reads."""
    entries: list[dict] = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        try:
            entry = json.loads(ln)
            # Keep the standard fields, and forward every remaining
            # structured key under "fields" so the frontend can render
            # them inline (e.g. PAGER_ALERT_RAISED carries trigger,
            # symbol, alert_id that would otherwise be lost).
            standard = {"timestamp", "level", "event", "message", "exc_info"}
            fields = {
                k: v for k, v in entry.items()
                if k not in standard and v is not None and v != ""
            }
            entries.append({
                "timestamp": entry.get("timestamp", ""),
                "level": entry.get("level", "INFO"),
                "event": entry.get("event", entry.get("message", "")),
                "message": entry.get("message", entry.get("event", "")),
                "fields": fields,
                "exc_info": entry.get("exc_info") or "",
            })
        except json.JSONDecodeError:
            entries.append({
                "timestamp": "",
                "level": "INFO",
                "event": "log",
                "message": ln,
                "fields": {},
                "exc_info": "",
            })
    return entries


async def _stream_logs_to_ws(websocket: WebSocket, log_path: str, backlog: int = 100) -> None:
    """Tail the structured log file and push each new JSON line to the WS.

    Replaces the 5s /api/logs poll. On subscribe we send the last `backlog`
    entries as a hydration batch, then stream new ones as they're written.
    Handles log rotation by re-opening the file when the inode changes.
    File I/O runs in a thread pool so the WS event loop stays responsive.
    """
    import os as _os

    async def _send(entries: list[dict]) -> None:
        if entries:
            await websocket.send_text(_json_dumps({
                "type": "log_batch",
                "data": entries,
            }))

    try:
        tail_lines = await asyncio.to_thread(_read_log_tail, log_path, backlog)
        await _send(_parse_log_entries(tail_lines))

        # Follow — reopen on inode change (log rotation).
        current_inode: int | None = None
        f = None

        def _open_at_tail() -> tuple[object, int]:
            fh = open(log_path, "r")
            fh.seek(0, 2)
            return fh, _os.fstat(fh.fileno()).st_ino

        def _read_available(fh) -> list[str]:
            # Drain whatever bytes are there; blocking read of small buffer
            # is fine, returns empty string at EOF on a regular file.
            out: list[str] = []
            while True:
                line = fh.readline()
                if not line:
                    break
                out.append(line)
            return out

        while True:
            try:
                st = await asyncio.to_thread(_os.stat, log_path)
            except FileNotFoundError:
                await asyncio.sleep(0.5)
                continue

            if current_inode != st.st_ino:
                if f is not None:
                    try:
                        f.close()
                    except Exception as e:
                        logger.debug("log file close failed", exc_info=e)
                f, current_inode = await asyncio.to_thread(_open_at_tail)

            new_lines = await asyncio.to_thread(_read_available, f)
            if new_lines:
                await _send(_parse_log_entries(new_lines))
            else:
                await asyncio.sleep(0.25)
    except (WebSocketDisconnect, asyncio.CancelledError):
        return
    except Exception:
        logger.exception(json.dumps({"event": "WS_LOGS_STREAM_ERROR"}))


async def _stream_command_output_to_ws(
    websocket: WebSocket, redis, cmd_id: str,
) -> None:
    """Push live command-output lines from Redis to the WebSocket client.

    The engine XADDs each ``ctx.router.emit`` to ``cmd:{cmd_id}:output``
    with a ``{"type":"line","message":"…","severity":"…"}`` payload, and
    emits a final ``{"type":"done","status":"SUCCESS"|"FAILURE",...}``
    marker. We XREAD from "0" so subscriptions that race the first XADD
    still replay everything from the start. When the terminal marker
    arrives the stream is done — stop listening.
    """
    from ib_trader.redis.streams import StreamNames

    stream = StreamNames.command_output(cmd_id)
    last_id = "0"
    while True:
        try:
            results = await redis.xread({stream: last_id}, block=30000)
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except (ConnectionError, OSError, _RedisConnectionError):
            return
        except Exception:
            logger.exception('{"event": "WS_CMD_OUTPUT_READ_ERROR", "cmd_id": "%s"}', cmd_id)
            await asyncio.sleep(1)
            continue

        if not results:
            continue

        for _stream_name, entries in results:
            for entry_id, raw_data in entries:
                last_id = entry_id
                data = {}
                for k, v in raw_data.items():
                    try:
                        data[k] = json.loads(v)
                    except (ValueError, TypeError):
                        data[k] = v
                try:
                    await websocket.send_text(_json_dumps({
                        "type": "command_output",
                        "cmd_id": cmd_id,
                        "data": data,
                    }))
                except (WebSocketDisconnect, RuntimeError):
                    return
                if data.get("type") == "done":
                    return


async def _stream_positions_to_ws(websocket: WebSocket, redis) -> None:
    """Push broker positions to the WebSocket on every position:changes event.

    Sends an initial snapshot, then re-reads and pushes on each event from
    the engine's position:changes stream. A 30s fallback XREAD timeout
    re-issues the snapshot so missed events can't strand the client.
    """
    import os
    import httpx
    from ib_trader.redis.streams import StreamNames

    engine_port = os.environ.get("IB_TRADER_ENGINE_INTERNAL_PORT", "8081")
    engine_url = f"http://127.0.0.1:{engine_port}"

    async def push_snapshot():
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{engine_url}/engine/positions")
                positions = resp.json() if resp.status_code == 200 else []
        except Exception:
            positions = []
        try:
            await websocket.send_text(_json_dumps({
                "type": "positions",
                "data": positions,
            }))
        except (RuntimeError, WebSocketDisconnect) as e:
            raise asyncio.CancelledError() from e  # WS closed — exit the stream task

    # Initial snapshot so the UI hydrates immediately
    await push_snapshot()

    last_id = "$"
    while True:
        try:
            results = await redis.xread(
                {StreamNames.position_changes(): last_id}, block=30000,
            )
            # Whether we got an event or timed out, re-push the snapshot.
            # The event payload isn't rich enough to patch in place, and
            # position data benefits from a fresh read on every event.
            if results:
                for _stream, entries in results:
                    for entry_id, _raw in entries:
                        last_id = entry_id
            await push_snapshot()
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
        except (ConnectionError, OSError, _RedisConnectionError):
            return  # Redis shut down
        except Exception:
            logger.exception('{"event": "WS_POSITIONS_STREAM_ERROR"}')
            await asyncio.sleep(1)


async def _activity_listener(
    redis, dirty: set[str], signal: asyncio.Event,
    subscribed: set[str],
) -> None:
    """Mark channels dirty when the engine publishes an activity notification.

    Replaces the 1.5s SQLite poll — the WS diff path now wakes on the
    engine's own state-change events. The fallback refresh in the main
    loop catches any missed notifications.
    """
    from ib_trader.redis.streams import StreamNames
    if redis is None:
        return
    last_id = "$"
    while True:
        try:
            results = await redis.xread({StreamNames.ACTIVITY: last_id}, block=30000)
        except asyncio.CancelledError:
            raise
        except (ConnectionError, OSError, _RedisConnectionError):
            return  # Redis shut down
        except Exception:
            logger.exception('{"event": "WS_ACTIVITY_READ_ERROR"}')
            await asyncio.sleep(1)
            continue

        if not results:
            continue
        for _stream, entries in results:
            for entry_id, raw_data in entries:
                last_id = entry_id
                try:
                    ch = raw_data.get("channel")
                    if isinstance(ch, str) and ch.startswith('"') and ch.endswith('"'):
                        ch = ch[1:-1]
                except Exception:
                    ch = None
                if not ch:
                    continue
                if ch in subscribed:
                    dirty.add(ch)
                    signal.set()
                # Status aggregates trades/orders/alerts/heartbeats/commands;
                # mark it dirty whenever any of those fire so /api/status pushes
                # without its own publisher wiring.
                if ch in _STATUS_TRIGGERS and "status" in subscribed:
                    dirty.add("status")
                    signal.set()


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time data updates."""
    await websocket.accept()

    from ib_trader.api.deps import get_session_factory, get_redis
    try:
        sf = get_session_factory()
    except RuntimeError:
        await websocket.close(code=1011, reason="Server not ready")
        return

    redis = get_redis()
    subscribed_channels: set[str] = set()
    channel_states: dict[str, _ChannelState] = {}
    stream_tasks: list[asyncio.Task] = []

    # Activity-driven diff refresh. `dirty` tracks which channels the engine
    # has marked as changed; `signal` wakes the main loop so we diff
    # without waiting out the client-message timeout.
    dirty_channels: set[str] = set()
    activity_signal = asyncio.Event()
    activity_task: asyncio.Task | None = None
    if redis is not None:
        activity_task = asyncio.create_task(
            _activity_listener(redis, dirty_channels, activity_signal, subscribed_channels)
        )
    last_full_refresh = 0.0

    async def _emit_diffs(channels: set[str]) -> None:
        for ch in channels:
            if ch not in subscribed_channels:
                continue
            data = await _fetch_channel_data_async(ch, sf, redis)
            diff = channel_states[ch].update(data)
            if diff:
                await websocket.send_text(_json_dumps({
                    "type": "diff",
                    "channel": ch,
                    **diff,
                }))

    try:
        loop = asyncio.get_event_loop()
        while True:
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=_POLL_INTERVAL_S,
                )
                msg = json.loads(raw)
                msg_type = msg.get("type")

                if msg_type == "subscribe":
                    channels = msg.get("channels", [])
                    subscribed_channels.clear()
                    subscribed_channels.update(
                        c for c in channels if c in _VALID_CHANNELS
                    )
                    channel_states = {c: _ChannelState(c) for c in subscribed_channels}

                    snapshot_data = {}
                    for ch in subscribed_channels:
                        data = await _fetch_channel_data_async(ch, sf, redis)
                        channel_states[ch].update(data)
                        snapshot_data[ch] = data

                    await websocket.send_text(_json_dumps({
                        "type": "snapshot",
                        "data": snapshot_data,
                    }))
                    last_full_refresh = loop.time()

                elif msg_type == "subscribe_quote" and redis:
                    # Subscribe to a symbol's live quote stream
                    symbol = msg.get("symbol", "")
                    if symbol:
                        task = asyncio.create_task(_stream_quote_to_ws(websocket, redis, symbol))
                        stream_tasks.append(task)

                elif msg_type == "subscribe_bot" and redis:
                    # Subscribe to a bot's state changes
                    bot_ref = msg.get("bot_ref", "")
                    symbol = msg.get("symbol", "")
                    if bot_ref and symbol:
                        task = asyncio.create_task(
                            _stream_bot_state_to_ws(websocket, redis, bot_ref, symbol)
                        )
                        stream_tasks.append(task)

                elif msg_type == "subscribe_positions" and redis:
                    task = asyncio.create_task(
                        _stream_positions_to_ws(websocket, redis)
                    )
                    stream_tasks.append(task)

                elif msg_type == "subscribe_command_output" and redis:
                    cmd_id = msg.get("cmd_id", "")
                    if cmd_id:
                        task = asyncio.create_task(
                            _stream_command_output_to_ws(websocket, redis, cmd_id)
                        )
                        stream_tasks.append(task)

                elif msg_type == "subscribe_logs":
                    import os as _os
                    log_path = _os.environ.get("IB_TRADER_LOG_FILE", "logs/ib_trader.log")
                    try:
                        backlog = int(msg.get("backlog", 100))
                    except (TypeError, ValueError):
                        backlog = 100
                    task = asyncio.create_task(
                        _stream_logs_to_ws(websocket, log_path, backlog=backlog),
                    )
                    stream_tasks.append(task)

                elif msg_type == "ping":
                    await websocket.send_text(_json_dumps({"type": "pong"}))

            except asyncio.TimeoutError:
                pass

            if not subscribed_channels:
                continue

            # Diff dirty channels (activity-driven) or refresh all on fallback.
            now = loop.time()
            due_for_full = (now - last_full_refresh) >= _FALLBACK_REFRESH_S
            if activity_signal.is_set() or due_for_full or redis is None:
                if activity_signal.is_set() and not due_for_full and redis is not None:
                    to_refresh = set(dirty_channels)
                    dirty_channels.clear()
                    activity_signal.clear()
                else:
                    to_refresh = set(subscribed_channels)
                    dirty_channels.clear()
                    activity_signal.clear()
                    last_full_refresh = now
                await _emit_diffs(to_refresh)

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception(json.dumps({"event": "WEBSOCKET_ERROR"}))
        try:
            await websocket.close(code=1011)
        except Exception as e:
            logger.debug("websocket close failed", exc_info=e)
    finally:
        for t in stream_tasks:
            t.cancel()
        if activity_task is not None:
            activity_task.cancel()
