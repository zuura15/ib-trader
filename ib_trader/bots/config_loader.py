"""Load bot definitions from YAML files on disk.

Bot identity + config lives in `config/bots/<name>.yaml`. The runner
reads this directory on startup and again when `POST /api/bots/reload`
is called. Runtime state (status, heartbeat, etc.) is NOT persisted
here — it lives in Redis.

YAML shape:
    id: <stable-uuid-string>
    name: <unique-name>
    strategy: <registry-key>          # e.g. "strategy_bot", "mean_revert"
    broker: ib                        # optional, default "ib"
    tick_interval_seconds: 5          # optional, default 10
    manual_entry_only: false          # optional, default false
    symbols: [F]                      # optional, informational
    config:                           # opaque strategy config blob
      strategy_config: config/strategies/<file>.yaml
      symbol: F
      qty: 10
      max_orders: 10

The id must be stable — it keys Redis state, bot_events, and order
refs. Rename the file freely; changing the id in-place while a bot is
running will orphan its state.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterable

import yaml

from ib_trader.bots.definition import BotDefinition

logger = logging.getLogger(__name__)

DEFAULT_BOTS_DIR = Path("config/bots")


class BotConfigError(ValueError):
    """Raised when a bot YAML is malformed or duplicates an id/name."""


def _load_one(path: Path) -> BotDefinition:
    """Parse a single bot YAML into a BotDefinition.

    Raises BotConfigError with a path-qualified message on any problem
    so the startup log points the operator at the exact bad file.
    """
    try:
        raw = yaml.safe_load(path.read_text())
    except yaml.YAMLError as exc:
        raise BotConfigError(f"{path}: invalid YAML — {exc}") from exc

    if not isinstance(raw, dict):
        raise BotConfigError(f"{path}: top-level YAML must be a mapping")

    try:
        bot_id = raw["id"]
        name = raw["name"]
        strategy = raw["strategy"]
    except KeyError as exc:
        raise BotConfigError(f"{path}: missing required field {exc}") from exc

    symbols = raw.get("symbols") or ()
    if isinstance(symbols, str):
        # Tolerate `symbols: F` (single string) as well as `symbols: [F, G]`.
        symbols = (symbols,)
    else:
        symbols = tuple(symbols)

    return BotDefinition(
        id=str(bot_id),
        name=str(name),
        strategy=str(strategy),
        broker=str(raw.get("broker", "ib")),
        tick_interval_seconds=int(raw.get("tick_interval_seconds", 10)),
        manual_entry_only=bool(raw.get("manual_entry_only", False)),
        config=dict(raw.get("config") or {}),
        symbols=symbols,
        source_path=str(path),
    )


def load_all_bots(bots_dir: Path | str = DEFAULT_BOTS_DIR) -> list[BotDefinition]:
    """Return every bot definition found under ``bots_dir``.

    Scans ``*.yaml`` and ``*.yml`` files, skips anything starting with
    ``_`` or ``.`` (e.g. disabled fixtures, editor swap files). Sorted
    by name for deterministic start order.

    Raises BotConfigError on:
      - duplicate ``id`` across files
      - duplicate ``name`` across files
      - malformed YAML in any file
    """
    base = Path(bots_dir)
    if not base.exists():
        logger.info(
            '{"event": "BOT_CONFIG_DIR_MISSING", "path": "%s"}', base,
        )
        return []

    defs: list[BotDefinition] = []
    seen_ids: dict[str, str] = {}
    seen_names: dict[str, str] = {}

    for path in sorted(base.glob("*.y*ml")):
        if path.name.startswith(("_", ".")):
            continue
        if path.suffix not in (".yaml", ".yml"):
            continue
        bot = _load_one(path)

        if bot.id in seen_ids:
            raise BotConfigError(
                f"{path}: duplicate bot id {bot.id!r} "
                f"(also in {seen_ids[bot.id]})"
            )
        if bot.name in seen_names:
            raise BotConfigError(
                f"{path}: duplicate bot name {bot.name!r} "
                f"(also in {seen_names[bot.name]})"
            )

        seen_ids[bot.id] = str(path)
        seen_names[bot.name] = str(path)
        defs.append(bot)

    defs.sort(key=lambda d: d.name)
    logger.info(
        '{"event": "BOT_CONFIG_LOADED", "path": "%s", "count": %d}',
        base, len(defs),
    )
    return defs


def diff_definitions(
    old: Iterable[BotDefinition],
    new: Iterable[BotDefinition],
) -> tuple[list[BotDefinition], list[BotDefinition], list[tuple[BotDefinition, BotDefinition]]]:
    """Compare two sets of definitions by id.

    Returns (added, removed, changed) where `changed` is a list of
    (old_def, new_def) pairs whose id matched but whose content differs.
    Used by the reload endpoint to decide what notifications to emit and
    to refuse hot-swaps on running bots.
    """
    old_by_id = {d.id: d for d in old}
    new_by_id = {d.id: d for d in new}

    added = [d for i, d in new_by_id.items() if i not in old_by_id]
    removed = [d for i, d in old_by_id.items() if i not in new_by_id]
    changed: list[tuple[BotDefinition, BotDefinition]] = []
    for i, n in new_by_id.items():
        o = old_by_id.get(i)
        if o is not None and o != n:
            changed.append((o, n))
    return added, removed, changed
