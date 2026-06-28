"""Redis key-value state store with JSON serialization and TTL support.

Used for current-value state: latest quote, position state, strategy state.
Read once on startup (GET), then maintained in-memory by stream consumers.
"""
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)


class _DecimalEncoder(json.JSONEncoder):
    """JSON encoder that handles Decimal and datetime values."""

    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


class StateStore:
    """Key-value state store backed by Redis SET/GET.

    Usage:
        store = StateStore(redis)
        await store.set("pos:saw-rsi:QQQ", {"state": "OPEN", "qty": 20}, ttl=None)
        data = await store.get("pos:saw-rsi:QQQ")
    """

    def __init__(self, redis: aioredis.Redis) -> None:
        self._redis = redis

    async def set(self, key: str, value: dict, ttl: Optional[int] = None) -> None:
        """Set a key to a JSON-serialized value.

        Args:
            key: Redis key.
            value: Dict to serialize and store.
            ttl: Optional TTL in seconds. None means no expiry.
        """
        serialized = json.dumps(value, cls=_DecimalEncoder)
        if ttl is not None:
            await self._redis.setex(key, ttl, serialized)
        else:
            await self._redis.set(key, serialized)

    async def get(self, key: str) -> Optional[dict]:
        """Get a JSON-deserialized value from a key.

        Returns:
            Deserialized dict, or None if the key does not exist or has expired.
        """
        raw = await self._redis.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            logger.warning('{"event": "STATE_DESERIALIZE_ERROR", "key": "%s"}', key)
            return None

    async def delete(self, key: str) -> None:
        """Delete a key."""
        await self._redis.delete(key)


class StateKeys:
    """Constants and factories for all Redis state key names."""

    @staticmethod
    def quote_latest(symbol: str) -> str:
        return f"quote:{symbol}:latest"

    @staticmethod
    def quotes_heartbeat() -> str:
        """Single key updated by the engine's quote publisher on every IB
        quote tick for any tracked symbol. Purpose: give bots a way to
        decide whether the QUOTE STREAM is alive as a whole, separate
        from any one symbol's natural tick cadence. Illiquid symbols
        (PSQ etc.) can go 120 s+ between ticks mid-session without
        anything being wrong, so per-symbol freshness is a poor halt
        signal; this key ticks whenever ANY subscribed symbol does.

        This is NOT the engine process heartbeat (that lives on the
        ``heartbeats`` SQLite table / ``ENGINE`` row and is updated
        independently by the engine itself). The two are intentionally
        decoupled: the engine can be alive but market data could stall
        (IB subscription dropped, gateway hung on the md side) — bots
        care about the latter, not the former."""
        return "quotes:heartbeat"

    @staticmethod
    def position(bot_ref: str, symbol: str) -> str:
        if ":" in bot_ref:
            raise ValueError(f"bot_ref must not contain ':': {bot_ref!r}")
        return f"pos:{bot_ref}:{symbol}"

    @staticmethod
    def strategy(bot_ref: str, symbol: str) -> str:
        if ":" in bot_ref:
            raise ValueError(f"bot_ref must not contain ':': {bot_ref!r}")
        return f"strat:{bot_ref}:{symbol}"

    # ---- Bot runtime state (replaces the SQLite `bots` table) ----
    #
    # Identity + config is authoritative on disk (config/bots/*.yaml).
    # Everything mutable moves here so the `bots` table can be retired
    # and the hot-path KILL_SWITCH check reads Redis, not SQLite.

    @staticmethod
    def bot_status(bot_id: str) -> str:
        """RUNNING / STOPPED / ERROR / PAUSED. No TTL — persistent."""
        return f"bot:{bot_id}:status"

    @staticmethod
    def bot_heartbeat(bot_id: str) -> str:
        """ISO timestamp of the bot's last supervisory tick.

        TTL'd so a crashed bot's heartbeat expires automatically and the
        UI can show it as stale without us chasing cleanup.
        """
        return f"bot:{bot_id}:heartbeat"

    @staticmethod
    def bot_last_action(bot_id: str) -> str:
        """Dict {action, ts} — last manual override or strategy signal."""
        return f"bot:{bot_id}:last_action"

    @staticmethod
    def bot_kill_switch(bot_id: str) -> str:
        """Safety circuit breaker. Presence of the key == engaged.

        Read on every BUY by RiskMiddleware. The reader fails CLOSED: if
        Redis is unreachable OR the read raises, BUY is rejected. No TTL.
        """
        return f"bot:{bot_id}:kill_switch"

    @staticmethod
    def bot_error_message(bot_id: str) -> str:
        """Free-form error context shown by the UI on ERROR status."""
        return f"bot:{bot_id}:error_message"

    @staticmethod
    def heartbeat(process: str) -> str:
        return f"hb:{process}"

    # ---- Live-state hashes (replace SQLite reads for UI) ----

    @staticmethod
    def orders_open() -> str:
        """Redis hash: currently open orders keyed by ib_order_id."""
        return "orders:open"

    @staticmethod
    def trades_open() -> str:
        """Redis hash: currently open trade groups keyed by trade_id."""
        return "trades:open"

    @staticmethod
    def trades_recent_closed() -> str:
        """Redis list: last N closed trades (LRU, capped by caller)."""
        return "trades:recent_closed"

    @staticmethod
    def alerts_active() -> str:
        """Redis hash: currently unresolved alerts keyed by alert_id."""
        return "alerts:active"

    @staticmethod
    def bot_stats(bot_id: str) -> str:
        """Per-bot trade stats: {trades_total, trades_today, pnl_today}."""
        return f"bot:stats:{bot_id}"

    @staticmethod
    def process_heartbeat(process: str) -> str:
        """Per-process liveness key. TTL = PROCESS_HEARTBEAT_TTL."""
        return f"hb:{process}"

    @staticmethod
    def console_pnl_24h() -> str:
        """Rolling 24h log of realized P&L from console-initiated closes.

        Redis sorted set. score = epoch ms (also embedded in member so
        equal-ms entries don't dedup). member = JSON-encoded
        ``{pnl, symbol, qty, ts, serial, source}``. Producers ZADD on
        every console close fill (full or partial) and ZREMRANGEBYSCORE
        to evict entries older than 24h. The API endpoint sums what
        remains. Bot-driven closes are NOT recorded here — this surface
        is operator-facing console P&L only.
        """
        return "console:pnl:24h"

    @staticmethod
    def watchlist_symbols() -> str:
        """Durable operator watchlist (JSON list of symbol roots).

        Authoritative live watchlist, edited via the UI / API. Seeded once
        from ``config/watchlist.yaml`` when absent; runtime never writes
        the git-tracked YAML, so manual edits don't conflict with
        ``git pull``. See ``ib_trader.config.watchlist_runtime``.
        """
        return "watchlist:symbols"

    @staticmethod
    def chart_pnl_rollup() -> str:
        """IB-authoritative realized-P&L rollup per contract for chart panes.

        Plain string key holding a JSON map
        ``{ localSymbol: {pnl_24h, pnl_session, sec_type} }``. Written by the
        engine's ``_pnl_rollup_loop`` from a periodic ``reqExecutions`` sweep
        — so it includes trades the operator placed directly in TWS, not just
        orders our system originated. ``pnl_session`` is realized P&L since
        the most recent futures-session boundary and is null for non-FUT.
        """
        return "chart:pnl:rollup"

    @staticmethod
    def chart_exec_accum() -> str:
        """Durable trailing-24h execution store for the chart rollup.

        Plain string key holding a JSON map ``{exec_id: record}`` (see
        ``ib_trader.engine.exec_accumulator``). Accumulated across sweeps so
        executions survive IB's daily Gateway restart, which otherwise
        drops them from ``reqExecutions`` and undercounts the 24 h P&L.
        Written only by the engine's ``_pnl_rollup_loop``.
        """
        return "chart:exec:accum"

    @staticmethod
    def chart_exec_markers() -> str:
        """IB-authoritative execution markers per contract for chart panes.

        Plain string key holding a JSON map
        ``{ localSymbol: [ {time, side, qty, price}, ... ] }``. Written by
        the engine's ``_pnl_rollup_loop`` from the same ``reqExecutions``
        sweep — account-wide, so it includes fills the operator placed
        directly in TWS. One marker per order (partial fills summed). No
        open/close pairing.
        """
        return "chart:exec:markers"

    @staticmethod
    def engine_session() -> str:
        """Metadata about the engine's current IB connection.

        Value shape: {account_id, account_mode ("paper"|"live"), port,
        paper (bool), connected_at (isoformat)}. Written by the engine
        on connect; read by /api/status so the UI reports what the
        engine is actually talking to rather than parsing .env.
        """
        return "engine:session"

    PROCESS_HEARTBEAT_TTL = 120

    # ---- Helpers for writing live state from sync code ----

    @staticmethod
    async def publish_alert(redis, alert_id: str, alert_dict: dict) -> None:
        """Write an alert to the alerts:active Redis hash.

        Called alongside the SQLite archival write. The ``alert_dict``
        should be a JSON-serializable dict with at least {id, severity,
        trigger, message, created_at}.
        """
        if redis is None:
            return
        import json as _json
        await redis.hset(StateKeys.alerts_active(), alert_id, _json.dumps(alert_dict))

    # TTL constants
    QUOTE_TTL = 60
    # Heartbeat outlives the default bot halt threshold (120 s) so the
    # key only disappears when the engine is genuinely offline — a
    # missing key IS the halt signal, not a false positive.
    QUOTES_HEARTBEAT_TTL = 600
    HEARTBEAT_TTL = 120
    BOT_HEARTBEAT_TTL = 300          # 5 min — bot supervisor fires every 10s
    BOT_LAST_ACTION_TTL = 300        # 5 min — UI surface, not decision input
    BOT_ERROR_MESSAGE_TTL = 3600     # 1 h — long enough for operator triage
