"""Redis Streams publisher and consumer abstractions.

StreamWriter wraps XADD with MAXLEN trimming and JSON serialization.
StreamReader wraps XREAD BLOCK as an async generator for push-based consumption.
StreamNames provides constants for all stream key patterns.
"""
import json
import logging
from datetime import datetime
from decimal import Decimal
from typing import AsyncIterator, Optional

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


def _serialize(data: dict) -> dict[str, str]:
    """Serialize a dict for Redis stream storage.

    Each value is JSON-encoded to preserve Decimal precision and datetime format.
    """
    return {k: json.dumps(v, cls=_DecimalEncoder) for k, v in data.items()}


def _deserialize(raw: dict[str, str]) -> dict:
    """Deserialize a Redis stream entry back to Python types."""
    result = {}
    for k, v in raw.items():
        try:
            result[k] = json.loads(v)
        except (json.JSONDecodeError, TypeError):
            result[k] = v
    return result


class StreamWriter:
    """Publishes events to a Redis stream with MAXLEN trimming.

    Usage:
        writer = StreamWriter(redis, "quote:QQQ", maxlen=5000)
        await writer.add({"bid": Decimal("494.28"), "ask": Decimal("494.32")})
    """

    def __init__(self, redis: aioredis.Redis, stream: str, maxlen: int = 5000) -> None:
        self._redis = redis
        self._stream = stream
        self._maxlen = maxlen

    async def add(self, data: dict) -> str:
        """Add an entry to the stream.

        Args:
            data: Dict of field-value pairs. Decimal and datetime values
                  are automatically serialized.

        Returns:
            The stream entry ID assigned by Redis.
        """
        entry_id = await self._redis.xadd(
            self._stream,
            _serialize(data),
            maxlen=self._maxlen,
            approximate=True,
        )
        return entry_id


class StreamReader:
    """Consumes events from one or more Redis streams via XREAD BLOCK.

    Usage:
        reader = StreamReader(redis, {"quote:QQQ": "$", "fill:saw-rsi": "$"})
        async for stream_name, entry_id, data in reader.listen():
            process(stream_name, data)
    """

    def __init__(
        self,
        redis: aioredis.Redis,
        streams: dict[str, str],
        block_ms: int = 5000,
    ) -> None:
        """Initialize the stream reader.

        Args:
            redis: Async Redis client.
            streams: Dict of stream_name → last_seen_id. Use "$" for new
                     entries only, "0" to read from the beginning.
            block_ms: XREAD BLOCK timeout in milliseconds. The reader re-issues
                      XREAD after each timeout, allowing cooperative cancellation.
        """
        self._redis = redis
        self._streams = dict(streams)
        self._block_ms = block_ms

    async def listen(self) -> AsyncIterator[tuple[str, str, dict]]:
        """Async generator that yields (stream_name, entry_id, data) tuples.

        Blocks until data arrives on any subscribed stream, then yields each
        entry. Updates the last-seen ID automatically so entries are not
        re-delivered.
        """
        while True:
            try:
                results = await self._redis.xread(
                    self._streams,
                    block=self._block_ms,
                )
            except Exception:
                logger.exception('{"event": "STREAM_READ_ERROR"}')
                break

            if not results:
                # Timeout — no new data. Re-issue XREAD.
                continue

            for stream_name, entries in results:
                for entry_id, raw_data in entries:
                    self._streams[stream_name] = entry_id
                    yield stream_name, entry_id, _deserialize(raw_data)

    @staticmethod
    async def read_latest(
        redis: aioredis.Redis, stream: str
    ) -> Optional[tuple[str, dict]]:
        """Read the most recent entry from a stream.

        Used on startup to get current state without subscribing.

        Returns:
            (entry_id, data) tuple, or None if the stream is empty.
        """
        results = await redis.xrevrange(stream, count=1)
        if not results:
            return None
        entry_id, raw_data = results[0]
        return entry_id, _deserialize(raw_data)


class StreamNames:
    """Constants and factories for all Redis stream names."""

    @staticmethod
    def quote(symbol: str) -> str:
        return f"quote:{symbol}"

    @staticmethod
    def bar(symbol: str, interval: str) -> str:
        return f"bar:{symbol}:{interval}"

    @staticmethod
    def fill(bot_ref: str) -> str:
        return f"fill:{bot_ref}"

    @staticmethod
    def position_changes() -> str:
        return "position:changes"

    @staticmethod
    def bot_event(bot_id: str) -> str:
        return f"bot:event:{bot_id}"

    @staticmethod
    def alert(severity: str) -> str:
        return f"alert:{severity}"

    @staticmethod
    def bot_control(bot_id: str) -> str:
        return f"bot:control:{bot_id}"

    @staticmethod
    def bot_state(bot_ref: str, symbol: str) -> str:
        """Stream of 'state changed' markers — emitted whenever the bot
        writes new strategy state to Redis. UI subscribes to this to push
        live updates to the browser without polling.
        """
        return f"bot:state:{bot_ref}:{symbol}"

    ACTIVITY = "events:activity"
    """Lightweight channel-change notifier consumed by the WebSocket API.

    Writers publish {"channel": "<name>"} whenever a SQLite-backed domain
    (trades, orders, alerts, commands, bot_events, heartbeats) changes, so
    the WS endpoint can re-fetch and diff that channel without polling.
    """


async def publish_activity(redis, channel: str) -> None:
    """Notify WS consumers that a diff-tracked domain has changed.

    No-op on Redis errors — activity notifications are observational,
    never gating. WS subscribers also refresh on a fallback timeout so
    a dropped notification does not strand the UI.
    """
    if redis is None:
        return
    try:
        writer = StreamWriter(redis, StreamNames.ACTIVITY, maxlen=500)
        await writer.add({"channel": channel})
    except Exception:
        logger.debug('{"event": "ACTIVITY_PUBLISH_FAILED", "channel": "%s"}', channel)
