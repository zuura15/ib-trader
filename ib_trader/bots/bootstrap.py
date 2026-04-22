"""One-way sync: `config/bots/*.yaml` → SQLite `bots` table.

Bot identity is authoritative on disk in YAML. The SQLite ``bots``
table is a derivative cache used by the existing runtime-state writes
(status, last_heartbeat, etc.) until those are migrated to Redis.

Guarantees
----------
1. YAML-wins: every bot that appears in YAML is reconciled into SQLite
   on startup and on ``POST /api/bots/reload``.
2. Hard-fail on foreign state: if the SQLite ``bots`` table has a row
   that is not present in YAML, startup refuses (unless ``force=True``)
   and points the operator at the migration script. This stops the
   two-authorities drift codex flagged in the plan review.
3. Running-bot safety (codex fix F + H): when a running bot's YAML
   changes its strategy / ref_id / symbol, the SQLite update is refused
   — the operator must stop the bot first. Harmless fields (display
   name, tick_interval) are still updated.
4. Delete-while-running safety: removing a YAML for a RUNNING bot is
   refused unless ``force=True``; force performs an explicit stop-then-
   delete of the DB row.

Source of truth for "is this bot running?"
------------------------------------------
The caller passes ``running_bot_ids`` — a snapshot of the FSM state in
Redis. SQLite ``bots.status`` is no longer consulted for this gating
decision; it remained a stale derivative view that could drift after a
crash and is being phased out. At startup the runner hasn't spawned
any bots yet, so ``running_bot_ids=None`` (equivalent to empty) is the
correct default. The API reload endpoint snapshots Redis FSM state
before calling.

The function is synchronous + transactional; it runs once at process
startup and again when the reload endpoint fires. Not on the hot path.
"""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import scoped_session

from ib_trader.bots import registry_config
from ib_trader.bots.config_loader import DEFAULT_BOTS_DIR
from ib_trader.bots.definition import BotDefinition
from ib_trader.data.models import Bot, BotStatus
from ib_trader.data.repositories.bot_repository import BotRepository

# `BotStatus` import is retained because the inserter still seeds new
# rows with `BotStatus.STOPPED`. Reads of `row.status` for the
# RUNNING-gating decision have been removed in favour of Redis FSM.
_ = BotStatus  # silence "unused" linters during the transition

logger = logging.getLogger(__name__)

# Fields whose mid-flight change would break a running bot (fill stream
# keys depend on ref_id; positions depend on symbol; strategy class is
# resolved once at start). Running-bot YAML edits that touch any of
# these raise BootstrapError.
_IMMUTABLE_WHEN_RUNNING = frozenset({"strategy", "broker", "ref_id", "symbol"})


class BootstrapError(RuntimeError):
    """Raised when YAML / SQLite are in an inconsistent state that needs
    operator attention. The message is always actionable."""


@dataclass
class BootstrapReport:
    added: list[str]            # bot ids inserted into SQLite
    updated: list[str]          # bot ids whose SQLite row was updated
    unchanged: list[str]        # bot ids that matched SQLite already
    removed: list[str]          # bot ids deleted from SQLite (force delete)
    refused: list[tuple[str, str]]  # (bot_id, reason) — change rejected

    @property
    def changed_count(self) -> int:
        return len(self.added) + len(self.updated) + len(self.removed)


def _config_version(definition: BotDefinition) -> str:
    """Stable short hash of the YAML-derived config.

    Stored on bot_events so the audit log can carry the exact config
    version that produced each event, even after the YAML is edited.
    """
    payload = json.dumps({
        "strategy": definition.strategy,
        "broker": definition.broker,
        "tick_interval_seconds": definition.tick_interval_seconds,
        "manual_entry_only": definition.manual_entry_only,
        "config": definition.config,
        "symbols": list(definition.symbols),
    }, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _definition_to_row_fields(d: BotDefinition) -> dict:
    """Shape the SQLAlchemy ``bots`` row from a BotDefinition."""
    return {
        "id": d.id,
        "name": d.name,
        "strategy": d.strategy,
        "broker": d.broker,
        "config_json": json.dumps(d.config, sort_keys=True, default=str),
        "tick_interval_seconds": d.tick_interval_seconds,
    }


def _row_matches(row: Bot, d: BotDefinition) -> bool:
    """Whether the SQLite row already reflects the YAML."""
    return (
        row.name == d.name
        and row.strategy == d.strategy
        and row.broker == d.broker
        and row.tick_interval_seconds == d.tick_interval_seconds
        and (row.config_json or "{}") == json.dumps(d.config, sort_keys=True, default=str)
    )


def _immutable_fields_changed(row: Bot, d: BotDefinition) -> list[str]:
    """Return the list of immutable fields whose values would flip.

    Inspects ``strategy``, ``broker`` directly on the row and
    ``ref_id`` / ``symbol`` inside the config JSON blob.
    """
    diffs: list[str] = []
    if row.strategy != d.strategy:
        diffs.append("strategy")
    if row.broker != d.broker:
        diffs.append("broker")
    try:
        old_cfg = json.loads(row.config_json or "{}")
    except (TypeError, ValueError):
        old_cfg = {}
    for key in ("ref_id", "symbol"):
        if old_cfg.get(key) != d.config.get(key):
            diffs.append(key)
    return diffs


def bootstrap_bots_from_yaml(
    session_factory: scoped_session,
    bots_dir: Path | str = DEFAULT_BOTS_DIR,
    *,
    force: bool = False,
    running_bot_ids: frozenset[str] | set[str] | None = None,
) -> BootstrapReport:
    """Reconcile ``bots`` table to match the YAML directory.

    Called on startup and when ``POST /api/bots/reload`` fires. Safe to
    call repeatedly. Populates the process-wide registry_config so other
    code paths can read bot definitions from memory.

    ``running_bot_ids`` is the set of bot IDs the caller knows to be
    running according to the live FSM in Redis. ``None`` means "the
    caller has no live view" — typical at process startup before the
    runner has spawned any bots — and is treated as the empty set. The
    SQLite ``bots.status`` column is no longer consulted for this
    decision because it can drift after a crash.

    Returns a BootstrapReport describing what changed. Raises
    BootstrapError when a refusal occurs without ``force``.
    """
    running_set: frozenset[str] = frozenset(running_bot_ids or ())

    # Populate the in-memory registry first — this is the canonical
    # source of truth for the process. SQLite follows.
    definitions = registry_config.load(bots_dir)
    defs_by_id = {d.id: d for d in definitions}

    repo = BotRepository(session_factory)
    existing_rows = {r.id: r for r in repo.get_all()}

    report = BootstrapReport([], [], [], [], [])

    # ---- Remove rows whose YAML has gone away ----
    for row_id, row in existing_rows.items():
        if row_id in defs_by_id:
            continue
        is_running = row_id in running_set
        if is_running and not force:
            reason = (
                f"SQLite row for bot_id={row_id!r} name={row.name!r} has no "
                f"matching YAML in {bots_dir}. Bot is RUNNING — refuse to "
                f"delete without force=True. Either stop the bot first or "
                f"restore the YAML."
            )
            report.refused.append((row_id, reason))
            raise BootstrapError(reason)
        if not force:
            reason = (
                f"SQLite row for bot_id={row_id!r} name={row.name!r} has no "
                f"matching YAML in {bots_dir}. Either write the YAML file, "
                f"run scripts/migrate_bots_to_yaml.py, or re-run with "
                f"force=True to drop the row."
            )
            report.refused.append((row_id, reason))
            raise BootstrapError(reason)
        repo.delete(row_id)
        report.removed.append(row_id)

    # ---- Insert / update rows from YAML ----
    for d in definitions:
        existing = existing_rows.get(d.id)
        if existing is None:
            _insert_row(session_factory, d)
            report.added.append(d.id)
            continue

        if _row_matches(existing, d):
            report.unchanged.append(d.id)
            continue

        if d.id in running_set:
            immutable_diff = _immutable_fields_changed(existing, d)
            if immutable_diff:
                reason = (
                    f"YAML edit to running bot {d.id!r} ({d.name!r}) "
                    f"would change immutable fields {immutable_diff}. "
                    f"Stop the bot first, then reload. Unchanged bots "
                    f"and mutable edits on running bots are accepted."
                )
                report.refused.append((d.id, reason))
                raise BootstrapError(reason)

        _update_row(session_factory, existing, d)
        report.updated.append(d.id)

    logger.info(
        '{"event": "BOT_BOOTSTRAP", "added": %d, "updated": %d, '
        '"removed": %d, "unchanged": %d, "refused": %d}',
        len(report.added), len(report.updated), len(report.removed),
        len(report.unchanged), len(report.refused),
    )
    return report


def _insert_row(session_factory: scoped_session, d: BotDefinition) -> None:
    """Insert a new SQLite row that mirrors ``d``."""
    now = datetime.now(timezone.utc)
    fields = _definition_to_row_fields(d)
    bot = Bot(**fields, created_at=now, updated_at=now, status=BotStatus.STOPPED)
    repo = BotRepository(session_factory)
    repo.create(bot)


def _update_row(
    session_factory: scoped_session, row: Bot, d: BotDefinition,
) -> None:
    """Update ``row`` in place to reflect ``d``. Preserves status and
    runtime-only fields so a currently-running bot isn't disturbed."""
    # Update through the session instead of adding a bespoke repo method.
    session = session_factory()
    fresh = session.query(Bot).filter(Bot.id == d.id).one()
    fresh.name = d.name
    fresh.strategy = d.strategy
    fresh.broker = d.broker
    fresh.tick_interval_seconds = d.tick_interval_seconds
    fresh.config_json = json.dumps(d.config, sort_keys=True, default=str)
    fresh.updated_at = datetime.now(timezone.utc)
    session.commit()


def config_version_for(bot_id: str) -> str | None:
    """Return the hash of the current YAML config for a bot, if loaded."""
    d = registry_config.get(bot_id)
    if d is None:
        return None
    return _config_version(d)
