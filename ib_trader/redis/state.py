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
    def position(bot_ref: str, symbol: str) -> str:
        if ":" in bot_ref:
            raise ValueError(f"bot_ref must not contain ':': {bot_ref!r}")
        return f"pos:{bot_ref}:{symbol}"

    @staticmethod
    def strategy(bot_ref: str, symbol: str) -> str:
        if ":" in bot_ref:
            raise ValueError(f"bot_ref must not contain ':': {bot_ref!r}")
        return f"strat:{bot_ref}:{symbol}"

    @staticmethod
    def bot_status(bot_id: str) -> str:
        return f"bot:{bot_id}:status"

    @staticmethod
    def heartbeat(process: str) -> str:
        return f"hb:{process}"

    # TTL constants
    QUOTE_TTL = 60
    HEARTBEAT_TTL = 120
