"""Process-wide registry of bot definitions loaded from YAML.

Shared read-only view of `config/bots/*.yaml`. The bot runner and the
FastAPI process each load this on startup; the reload endpoint rebuilds
it in place. The registry is deliberately separate from the strategy
registry (`ib_trader/bots/registry.py`) which maps strategy names to
classes — one is "what strategies exist in code", the other is "which
bots are deployed from disk".

Concurrency
-----------
The registry is mutated only by `reload()` (or on module import). Reads
return immutable `BotDefinition` dataclasses, so callers can safely
iterate the list without copying. The underlying list reference is
swapped atomically on reload; iterators that began before the swap see
the old list, which is fine.

Running-bot constraints
-----------------------
A `BotDefinition` is frozen once a bot is started. If a YAML changes
while a bot is running, the in-memory snapshot kept by `_run_single_bot`
still reflects the pre-reload config — this protects `ref_id`, `symbol`,
and exit params from mid-position mutation. The reload endpoint uses
`diff_definitions` to detect such cases and refuse the swap unless
explicitly forced.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from ib_trader.bots.config_loader import (
    DEFAULT_BOTS_DIR, load_all_bots,
)
from ib_trader.bots.definition import BotDefinition

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_definitions: list[BotDefinition] = []
_loaded_dir: Path | None = None


def load(bots_dir: Path | str = DEFAULT_BOTS_DIR) -> list[BotDefinition]:
    """Scan ``bots_dir`` and replace the cached definition list."""
    global _definitions, _loaded_dir
    new_defs = load_all_bots(bots_dir)
    with _lock:
        _definitions = new_defs
        _loaded_dir = Path(bots_dir)
    logger.info(
        '{"event": "BOT_REGISTRY_LOADED", "count": %d, "path": "%s"}',
        len(new_defs), bots_dir,
    )
    return list(_definitions)


def reload() -> list[BotDefinition]:
    """Re-read the directory the registry was last loaded from.

    Intended for `POST /api/bots/reload`. Raises RuntimeError if the
    registry has never been loaded (tests should call `load()` first).
    """
    if _loaded_dir is None:
        raise RuntimeError("bot registry has not been loaded yet")
    return load(_loaded_dir)


def all_definitions() -> list[BotDefinition]:
    """Return a shallow copy of the currently registered definitions."""
    with _lock:
        return list(_definitions)


def get(bot_id: str) -> BotDefinition | None:
    """Fetch a definition by id. Returns None if not registered."""
    with _lock:
        for d in _definitions:
            if d.id == bot_id:
                return d
    return None


def get_by_name(name: str) -> BotDefinition | None:
    with _lock:
        for d in _definitions:
            if d.name == name:
                return d
    return None


def clear() -> None:
    """Forget everything — test helper."""
    global _definitions, _loaded_dir
    with _lock:
        _definitions = []
        _loaded_dir = None
