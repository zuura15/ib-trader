"""Async Redis connection factory.

Creates and manages a single shared Redis connection pool.
All processes (engine, bot runner, API) call get_redis() on startup.
"""
import logging
from typing import Optional

import redis.asyncio as aioredis

logger = logging.getLogger(__name__)

_redis: Optional[aioredis.Redis] = None


async def get_redis(url: str = "redis://localhost:6379/0") -> aioredis.Redis:
    """Get or create the shared async Redis connection.

    Args:
        url: Redis connection URL from settings.yaml.

    Returns:
        Async Redis client instance.
    """
    global _redis
    if _redis is None:
        _redis = aioredis.from_url(
            url,
            decode_responses=True,
            max_connections=20,
        )
        # Verify connectivity
        await _redis.ping()
        logger.info('{"event": "REDIS_CONNECTED", "url": "%s"}', url)
    return _redis


async def close_redis() -> None:
    """Close the shared Redis connection."""
    global _redis
    if _redis is not None:
        await _redis.aclose()
        _redis = None
        logger.info('{"event": "REDIS_DISCONNECTED"}')
