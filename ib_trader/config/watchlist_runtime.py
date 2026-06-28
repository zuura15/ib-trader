"""Runtime watchlist resolution: durable in Redis, seeded from YAML.

The operator's live watchlist is stored in Redis (``watchlist:symbols``)
so UI / manual edits persist without touching the git-tracked
``config/watchlist.yaml`` — which previously caused ``git pull`` conflicts
because the API wrote the live list back to it. The YAML is now only a
first-run SEED: when the Redis key is absent we load it from YAML and
write it once; thereafter Redis is authoritative.

Futures chart contracts are NOT kept in the watchlist any more — the
engine auto-anchors its ``chart-bot-*`` symbols (:func:`chart_anchor_symbols`)
so their quote subs never lapse, independent of the operator watchlist.
"""
from __future__ import annotations

import json
import logging

from ib_trader.config.loader import load_watchlist
from ib_trader.redis.state import StateKeys

logger = logging.getLogger(__name__)

_SEED_PATH = "config/watchlist.yaml"


async def resolve_watchlist_symbols(redis, *, seed_path: str = _SEED_PATH) -> list[str]:
    """Live operator watchlist (roots), seeding Redis from YAML on first use.

    Falls back to the YAML when Redis is unavailable so the engine still
    subscribes a sane set during a Redis outage.
    """
    if redis is None:
        return load_watchlist(seed_path)
    try:
        raw = await redis.get(StateKeys.watchlist_symbols())
    except Exception:
        logger.exception('{"event": "WATCHLIST_REDIS_READ_FAILED"}')
        return load_watchlist(seed_path)
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, list):
                return [str(s).upper() for s in data]
        except (ValueError, TypeError):
            pass  # corrupt value — fall through and re-seed
    # Absent/invalid → seed from YAML and persist once.
    seeded = load_watchlist(seed_path)
    try:
        await redis.set(StateKeys.watchlist_symbols(), json.dumps(seeded))
        logger.info('{"event": "WATCHLIST_SEEDED", "count": %d}', len(seeded))
    except Exception:
        logger.exception('{"event": "WATCHLIST_SEED_FAILED"}')
    return seeded


async def set_watchlist_symbols(redis, symbols: list[str]) -> list[str]:
    """Persist the operator watchlist to Redis (upper-cased, de-duped,
    order-preserving). Returns the normalized list."""
    norm = list(dict.fromkeys(s.upper().strip() for s in symbols if s.strip()))
    if redis is not None:
        await redis.set(StateKeys.watchlist_symbols(), json.dumps(norm))
    return norm


def chart_anchor_symbols() -> list[str]:
    """Chart-bot contract symbols, auto-anchored so their quote subs never
    lapse independent of the operator watchlist. Best-effort: returns []
    if the bot registry can't be read."""
    try:
        from ib_trader.bots import registry_config
        defns = registry_config.all_definitions()
        if not defns:
            # Registry not loaded in this process yet (the engine doesn't
            # always pre-load it) — load the default bots dir. Idempotent.
            try:
                registry_config.load()
                defns = registry_config.all_definitions()
            except Exception:
                logger.exception('{"event": "CHART_ANCHOR_REGISTRY_LOAD_FAILED"}')
                defns = []
        out: list[str] = []
        for defn in defns:
            if not str(getattr(defn, "id", "")).startswith("chart-bot"):
                continue
            sym = (getattr(defn, "config", None) or {}).get("symbol")
            if sym:
                out.append(str(sym).upper())
        return out
    except Exception:
        logger.exception('{"event": "CHART_ANCHOR_ENUM_FAILED"}')
        return []
