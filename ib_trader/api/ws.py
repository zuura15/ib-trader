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
from datetime import datetime, timezone
from decimal import Decimal
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from sqlalchemy.orm import scoped_session

from ib_trader.data.models import (
    PendingCommandStatus,
)
from ib_trader.data.repository import (
    TradeRepository, HeartbeatRepository, AlertRepository,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository

logger = logging.getLogger(__name__)

router = APIRouter()

_POLL_INTERVAL_S = 1.5
_FALLBACK_REFRESH_S = 30.0  # upper bound if no activity notifications arrive


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
    return hashlib.md5(_json_dumps(data).encode()).hexdigest()


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
            return [_serialize_trade(t) for t in TradeRepository(sf).get_all()]

        elif channel == "orders":
            return [_serialize_order(t) for t in TransactionRepository(sf).get_open_orders()]

        elif channel == "alerts":
            return [_serialize_alert(a) for a in AlertRepository(sf).get_open()]

        elif channel == "commands":
            return [_serialize_command(c)
                    for c in PendingCommandRepository(sf).get_by_source("api", limit=50)]

        elif channel == "heartbeats":
            repo = HeartbeatRepository(sf)
            results = []
            for process_name in ("REPL", "DAEMON", "ENGINE", "API", "BOT_RUNNER"):
                hb = repo.get(process_name)
                if hb:
                    results.append(_serialize_heartbeat(hb))
            return results

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
            # /api/bots stay shape-identical.
            from ib_trader.data.repositories.bot_repository import BotRepository
            from ib_trader.api.routes.bots import _serialize_bot
            repo = BotRepository(sf)
            return [_serialize_bot(b) for b in repo.get_all()]

        elif channel == "status":
            # Single-element snapshot. The id is stable so diff logic treats
            # any field change as an "updated" event rather than add+remove.
            from ib_trader.api.routes.system import get_status as _get_status
            heartbeats_repo = HeartbeatRepository(sf)
            alerts_repo = AlertRepository(sf)
            try:
                payload = _get_status(
                    heartbeats=heartbeats_repo, alerts=alerts_repo, sf=sf,
                )
                return [{"id": "__status__", **payload}]
            except Exception:
                logger.exception(json.dumps({"event": "STATUS_FETCH_FAILED"}))
                return []

        return []
    finally:
        sf.remove()


_VALID_CHANNELS = {
    "trades", "orders", "alerts", "commands", "heartbeats",
    "bot_events", "bots", "status",
}

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
            for stream_name, entries in results:
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
    from ib_trader.redis.state import StateStore, StateKeys

    bot_state_stream = StreamNames.bot_state(bot_ref, symbol)
    fill_stream = StreamNames.fill(bot_ref)
    streams = {bot_state_stream: "$", fill_stream: "$"}
    store = StateStore(redis)

    async def push_snapshot():
        strat = await store.get(StateKeys.strategy(bot_ref, symbol))
        pos = await store.get(StateKeys.position(bot_ref, symbol))
        await websocket.send_text(_json_dumps({
            "type": "bot_state",
            "bot_ref": bot_ref,
            "symbol": symbol,
            "strategy": strat,
            "position": pos,
        }))

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
            entries.append({
                "timestamp": entry.get("timestamp", ""),
                "level": entry.get("level", "INFO"),
                "event": entry.get("event", entry.get("message", "")),
                "message": entry.get("message", entry.get("event", "")),
            })
        except json.JSONDecodeError:
            entries.append({
                "timestamp": "",
                "level": "INFO",
                "event": "log",
                "message": ln,
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
                    except Exception:
                        pass
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


async def _stream_positions_to_ws(websocket: WebSocket, redis) -> None:
    """Push broker positions to the WebSocket on every position:changes event.

    Sends an initial snapshot, then re-reads and pushes on each event from
    the engine's position:changes stream. A 30s fallback XREAD timeout
    re-issues the snapshot so missed events can't strand the client.
    """
    from ib_trader.redis.streams import StreamNames
    from ib_trader.api.routes.positions import _positions_from_redis

    async def push_snapshot():
        positions = await _positions_from_redis(redis)
        await websocket.send_text(_json_dumps({
            "type": "positions",
            "data": positions,
        }))

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
            # position aggregation across ibpos:* / pos:* keys benefits from
            # a fresh read.
            if results:
                for _stream, entries in results:
                    for entry_id, _raw in entries:
                        last_id = entry_id
            await push_snapshot()
        except (WebSocketDisconnect, asyncio.CancelledError):
            return
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
            data = _fetch_channel_data(ch, sf)
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
                        data = _fetch_channel_data(ch, sf)
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
        except Exception:
            pass
    finally:
        for t in stream_tasks:
            t.cancel()
        if activity_task is not None:
            activity_task.cancel()
