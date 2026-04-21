"""Bot lifecycle state — enum and doc-level helpers.

Replaces the ``BotState`` enum, ``ERROR_REASONS`` set, and the
``_clear_position_fields`` helper that used to live in ``bots/fsm.py``.
The FSM dispatch layer has been collapsed into methods on
``StrategyBotRunner`` (see ``runtime.py``); what remains here is the
plain data shape and a couple of one-liner helpers that operate on
the persisted doc without needing a bot instance — specifically the
startup panic-reset path and the ``/bots/<id>/reset`` operator
endpoint.

The companion module ``bots/state.py`` (Redis-backed runtime helpers:
BotStateStore, kill-switch) predates this one and owns a different
slice of state entirely. Kept separate to avoid mixing the FSM-style
lifecycle with the observability / safety helpers.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum

logger = logging.getLogger(__name__)


class BotState(str, Enum):
    """Bot lifecycle state. Persisted as ``state`` on the
    ``bot:<id>:fsm`` Redis doc.

    - ``OFF`` — not running
    - ``ERRORED`` — unrecoverable condition; only path out is an
      operator-driven ``/reset`` (writes the doc back to OFF with
      fields cleared) followed by ``/start``
    - ``AWAITING_ENTRY_TRIGGER`` — running, no position, watching
      for entry signals
    - ``ENTRY_ORDER_PLACED`` — entry order in flight, awaiting
      terminal
    - ``AWAITING_EXIT_TRIGGER`` — position open, watching for exit
      trigger (trailing stop, time stop, manual close, …)
    - ``EXIT_ORDER_PLACED`` — exit order in flight, awaiting terminal
    """
    OFF = "OFF"
    ERRORED = "ERRORED"
    AWAITING_ENTRY_TRIGGER = "AWAITING_ENTRY_TRIGGER"
    ENTRY_ORDER_PLACED = "ENTRY_ORDER_PLACED"
    AWAITING_EXIT_TRIGGER = "AWAITING_EXIT_TRIGGER"
    EXIT_ORDER_PLACED = "EXIT_ORDER_PLACED"


# Canonical set of ``error_reason`` values used across the
# runner/runtime/fsm boundaries. Not exhaustive — new triggers can
# be added freely; this frozenset is only referenced where something
# wants to assert "is this a recognised terminal reason".
ERROR_REASONS = frozenset({
    "crash",
    "force_stop",
    "ib_mismatch",
    "exit_retries_exhausted",
    "task_crashed",
    "operator_reset",
})


# Max retry attempts on a partial exit. Exit isn't "done" until every
# share is out — if IB terminates the exit order with a residual, the
# bot places a fresh SELL for the remainder. After this many attempts,
# the bot transitions to ERRORED with a CATASTROPHIC pager alert so
# the operator can intervene.
MAX_EXIT_RETRIES = 3


# States considered "active" — bot is either holding or about to
# hold a position, or sitting in ERRORED with possibly lingering
# position fields. On app restart every bot in one of these states
# raises a CATASTROPHIC pager alert before being forced off.
ACTIVE_STATES = frozenset({
    BotState.ENTRY_ORDER_PLACED,
    BotState.AWAITING_EXIT_TRIGGER,
    BotState.EXIT_ORDER_PLACED,
    BotState.ERRORED,
})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def bot_doc_key(bot_id: str) -> str:
    """Redis key for the bot's lifecycle doc."""
    return f"bot:{bot_id}"


def clear_position_fields() -> dict:
    """Return a patch that zeroes every position-specific field.

    Callers merge this into the bot doc when returning to
    ``AWAITING_ENTRY_TRIGGER`` after a full close, or when resetting
    after an error/crash. Keep in sync with the fields the strategy
    host reads during exit evaluation (sawtooth / close-trend both
    consume ``qty``, ``entry_price``, ``high_water_mark``,
    ``current_stop``, ``trail_activated``).
    """
    return {
        "qty": "0",
        "entry_price": None,
        "entry_time": None,
        "serial": None,
        "ib_order_id": None,
        "awaiting_ib_order_id": None,
        "high_water_mark": None,
        "current_stop": None,
        "trail_activated": False,
        "order_qty": None,
        "filled_qty": "0",
    }


async def force_off_state(
    bot_id: str, redis, *, reason: str | None = None,
) -> dict:
    """Reset the bot's persisted doc to a clean OFF state.

    Used by (1) the runner's startup panic path after it detects a
    stale active state from a prior session, and (2) the operator's
    ``/bots/<id>/reset`` endpoint. Writes only to Redis — does not
    require a bot instance, does not touch the running task table.

    Always clears position fields alongside ``state=OFF`` so a
    subsequent ``/start`` can pass its ``is_clean_for_start`` check.
    A stale ``entry_price`` or ``awaiting_ib_order_id`` leaking past
    reset would cause the bot to wake up with a fictional position
    or a ghost in-flight order.

    Returns the doc that was written (for logging / tests).
    """
    from ib_trader.redis.state import StateStore

    store = StateStore(redis)
    doc = await store.get(bot_doc_key(bot_id)) or {}
    # Preserve identity / config fields (name, symbol, strategy) if
    # the runner already populated them. Overwrite only the lifecycle
    # and position slice.
    doc.update({
        "state": BotState.OFF.value,
        "error_reason": reason,
        "error_message": None,
        "updated_at": now_iso(),
        **clear_position_fields(),
    })
    await store.set(bot_doc_key(bot_id), doc)
    logger.info(
        '{"event": "BOT_STATE_RESET", "bot_id": "%s", "reason": "%s"}',
        bot_id, reason or "",
    )
    return doc


def is_clean_for_start(doc: dict | None) -> tuple[bool, str | None]:
    """Return ``(True, None)`` if the doc is in a state suitable for
    ``/bots/<id>/start``, else ``(False, reason)``.

    Rule: ``state=OFF`` and all position fields zero/None. Anything
    else — ERRORED, lingering qty, stuck ``awaiting_ib_order_id`` —
    means the operator must ``/reset`` first. This is the self-check
    invariant that prevents a crashed bot from auto-resuming with a
    stale mid-trade state.

    An absent doc (``None``) counts as clean (fresh install, never
    started).
    """
    if doc is None:
        return True, None
    state = doc.get("state", BotState.OFF.value)
    if state != BotState.OFF.value:
        return False, f"state={state!r} (expected OFF)"
    qty_raw = doc.get("qty") or "0"
    try:
        if Decimal(str(qty_raw)) != 0:
            return False, f"qty={qty_raw!r} (expected 0)"
    except (InvalidOperation, ValueError, TypeError):
        return False, f"qty={qty_raw!r} (unparseable)"
    for field in ("entry_price", "awaiting_ib_order_id", "ib_order_id"):
        v = doc.get(field)
        if v not in (None, "", "None"):
            return False, f"{field}={v!r} (expected None)"
    return True, None
