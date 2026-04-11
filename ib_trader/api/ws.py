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

        return []
    finally:
        sf.remove()


_VALID_CHANNELS = {"trades", "orders", "alerts", "commands", "heartbeats", "bot_events"}


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket endpoint for real-time data updates."""
    await websocket.accept()

    # Get session factory from deps module
    from ib_trader.api.deps import get_session_factory
    try:
        sf = get_session_factory()
    except RuntimeError:
        await websocket.close(code=1011, reason="Server not ready")
        return

    subscribed_channels: set[str] = set()
    channel_states: dict[str, _ChannelState] = {}

    try:
        while True:
            # Check for incoming messages (non-blocking with timeout)
            try:
                raw = await asyncio.wait_for(
                    websocket.receive_text(), timeout=_POLL_INTERVAL_S,
                )
                msg = json.loads(raw)

                if msg.get("type") == "subscribe":
                    channels = msg.get("channels", [])
                    subscribed_channels = {
                        c for c in channels if c in _VALID_CHANNELS
                    }
                    channel_states = {c: _ChannelState(c) for c in subscribed_channels}

                    # Send initial snapshot
                    snapshot_data = {}
                    for ch in subscribed_channels:
                        data = _fetch_channel_data(ch, sf)
                        channel_states[ch].update(data)
                        snapshot_data[ch] = data

                    await websocket.send_text(_json_dumps({
                        "type": "snapshot",
                        "data": snapshot_data,
                    }))

                elif msg.get("type") == "ping":
                    await websocket.send_text(_json_dumps({"type": "pong"}))

            except asyncio.TimeoutError:
                pass  # No message received — proceed to poll

            # Poll and send diffs for subscribed channels
            if subscribed_channels:
                for ch in subscribed_channels:
                    data = _fetch_channel_data(ch, sf)
                    diff = channel_states[ch].update(data)
                    if diff:
                        await websocket.send_text(_json_dumps({
                            "type": "diff",
                            "channel": ch,
                            **diff,
                        }))

    except WebSocketDisconnect:
        pass
    except Exception:
        logger.exception(json.dumps({"event": "WEBSOCKET_ERROR"}))
        try:
            await websocket.close(code=1011)
        except Exception:
            pass
