"""Redis integration layer for IB Trader.

Provides async Redis client, stream publisher/consumer abstractions,
and key-value state store with JSON serialization.
"""
from ib_trader.redis.client import get_redis, close_redis
from ib_trader.redis.streams import StreamWriter, StreamReader, StreamNames
from ib_trader.redis.state import StateStore, StateKeys

__all__ = [
    "StateKeys",
    "StateStore",
    "StreamNames",
    "StreamReader",
    "StreamWriter",
    "close_redis",
    "get_redis",
]
