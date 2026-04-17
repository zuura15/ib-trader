"""Bot state machine — single 6-state model with explicit events.

Replaces the two orthogonal machines that previously lived across
``runner.py``, ``runtime.py``, ``strategy.py``, and the API handlers:

- Lifecycle status (STOPPED / RUNNING / ERROR / PAUSED) in Redis ``bot:<id>:status``
- Position state (FLAT / ENTERING / OPEN / EXITING) in Redis ``strat:<bot_ref>:<symbol>``

Now there is one state key per bot: ``bot:<id>:fsm`` holding the full
operational state document. The bot is the sole writer. All transitions
flow through ``FSM.dispatch(event)``.

Invalid ``(state, event)`` combinations are logged and dropped — the FSM
never silently mutates state when a rule is missing.

States
------
- ``OFF`` — not running
- ``ERRORED`` — unrecoverable condition; only way out is ``Start``
- ``AWAITING_ENTRY_TRIGGER`` — running, no position, watching for entry signals
- ``ENTRY_ORDER_PLACED`` — buy order submitted, awaiting fill / cancel / timeout
- ``AWAITING_EXIT_TRIGGER`` — position open, watching for exit trigger
- ``EXIT_ORDER_PLACED`` — sell order submitted, awaiting fill / cancel

Events
------
See the module-level ``EventType`` enum and the ``_TRANSITIONS`` table
below for the full set of valid transitions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class BotState(str, Enum):
    OFF = "OFF"
    ERRORED = "ERRORED"
    AWAITING_ENTRY_TRIGGER = "AWAITING_ENTRY_TRIGGER"
    ENTRY_ORDER_PLACED = "ENTRY_ORDER_PLACED"
    AWAITING_EXIT_TRIGGER = "AWAITING_EXIT_TRIGGER"
    EXIT_ORDER_PLACED = "EXIT_ORDER_PLACED"


class EventType(str, Enum):
    START = "Start"
    STOP = "Stop"
    FORCE_STOP = "ForceStop"
    PLACE_ENTRY_ORDER = "PlaceEntryOrder"
    ENTRY_FILLED = "EntryFilled"
    ENTRY_CANCELLED = "EntryCancelled"
    ENTRY_TIMEOUT = "EntryTimeout"
    QUOTE_TICK = "QuoteTick"
    PLACE_EXIT_ORDER = "PlaceExitOrder"
    EXIT_FILLED = "ExitFilled"
    EXIT_CANCELLED = "ExitCancelled"
    MANUAL_CLOSE = "ManualClose"
    CRASH = "Crash"
    IB_POSITION_MISMATCH = "IBPositionMismatch"


ERROR_REASONS = frozenset({"crash", "force_stop", "ib_mismatch"})


@dataclass(frozen=True)
class BotEvent:
    """An FSM event. ``payload`` carries event-specific data (e.g. fill qty,
    price, order_ref, origin, error_message). All payload keys are optional;
    handlers pull what they need.
    """
    type: EventType
    payload: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SideEffect:
    """A declarative side effect a transition handler wants the caller to
    carry out. The FSM does not execute side effects itself — the caller
    (bot runtime) inspects ``TransitionResult.side_effects`` and performs
    the actions (place/cancel orders, dispatch events to strategy, etc.).
    """
    action: str             # e.g. "place_order", "cancel_order", "emit_event"
    args: dict = field(default_factory=dict)


@dataclass
class TransitionResult:
    new_state: BotState
    state_patch: dict
    side_effects: list[SideEffect]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Transition handlers — pure functions (current_doc, event) -> TransitionResult
# ---------------------------------------------------------------------------
# Each handler returns the new state, a state_patch (fields to merge into
# the state doc), and any side effects to execute. The FSM itself is pure:
# it reads the current doc, picks the handler, merges the patch, writes back.

def _clear_position_fields() -> dict:
    """Reset position-specific fields when returning to AWAITING_ENTRY_TRIGGER."""
    return {
        "qty": "0",
        "entry_price": None,
        "entry_time": None,
        "serial": None,
        "high_water_mark": None,
        "current_stop": None,
        "trail_activated": False,
        "order_qty": None,
        "filled_qty": "0",
    }


def _h_start(doc: dict, event: BotEvent) -> TransitionResult:
    return TransitionResult(
        new_state=BotState.AWAITING_ENTRY_TRIGGER,
        state_patch={
            "error_reason": None,
            "error_message": None,
            **_clear_position_fields(),
        },
        side_effects=[],
    )


def _h_stop(doc: dict, event: BotEvent) -> TransitionResult:
    # If an order is in flight, request cancellation as a side effect.
    cur = BotState(doc.get("state", BotState.OFF.value))
    side_effects: list[SideEffect] = []
    if cur in (BotState.ENTRY_ORDER_PLACED, BotState.EXIT_ORDER_PLACED):
        serial = doc.get("serial")
        if serial:
            side_effects.append(SideEffect(
                action="cancel_order",
                args={"serial": serial},
            ))
    return TransitionResult(
        new_state=BotState.OFF,
        state_patch={"error_reason": None, "error_message": None},
        side_effects=side_effects,
    )


def _h_force_stop(doc: dict, event: BotEvent) -> TransitionResult:
    return TransitionResult(
        new_state=BotState.ERRORED,
        state_patch={
            "error_reason": "force_stop",
            "error_message": event.payload.get("message", "Force-stopped by operator"),
        },
        side_effects=[],
    )


def _h_crash(doc: dict, event: BotEvent) -> TransitionResult:
    return TransitionResult(
        new_state=BotState.ERRORED,
        state_patch={
            "error_reason": "crash",
            "error_message": event.payload.get("message", "Unhandled exception"),
        },
        side_effects=[],
    )


def _h_ib_mismatch(doc: dict, event: BotEvent) -> TransitionResult:
    return TransitionResult(
        new_state=BotState.ERRORED,
        state_patch={
            "error_reason": "ib_mismatch",
            "error_message": event.payload.get("message", "Unresolvable IB position drift"),
        },
        side_effects=[],
    )


def _h_place_entry_order(doc: dict, event: BotEvent) -> TransitionResult:
    p = event.payload
    origin = p.get("origin", "strategy")
    return TransitionResult(
        new_state=BotState.ENTRY_ORDER_PLACED,
        state_patch={
            "order_qty": str(p["qty"]),
            "filled_qty": "0",
            "serial": p.get("serial"),
            "order_origin": origin,
        },
        side_effects=[SideEffect(
            action="place_order",
            args={
                "side": "BUY",
                "symbol": p["symbol"],
                "qty": str(p["qty"]),
                "order_type": p.get("order_type", "mid"),
                "origin": origin,
            },
        )],
    )


def _h_entry_filled(doc: dict, event: BotEvent) -> TransitionResult:
    """Handle an entry fill.

    May arrive in either ENTRY_ORDER_PLACED (the first partial or full
    fill that transitions to AWAITING_EXIT_TRIGGER) or in
    AWAITING_EXIT_TRIGGER itself (subsequent partials). In both cases we
    accumulate ``filled_qty`` and update ``qty``; only the first fill
    carries the state transition + HWM init.
    """
    p = event.payload
    qty_incr = Decimal(str(p.get("qty", "0")))
    price = Decimal(str(p.get("price", "0")))
    prev_filled = Decimal(doc.get("filled_qty") or "0")
    new_filled = prev_filled + qty_incr
    cur_state = BotState(doc.get("state", BotState.OFF.value))

    patch: dict = {
        "filled_qty": str(new_filled),
        "qty": str(new_filled),
        "avg_price": str(price),
    }

    if cur_state == BotState.ENTRY_ORDER_PLACED:
        # First partial (or full) — initialize position tracking + HWM
        patch.update({
            "entry_price": str(price),
            "entry_time": _now_iso(),
            "high_water_mark": str(price),
            "current_stop": None,
            "trail_activated": False,
        })
        new_state = BotState.AWAITING_EXIT_TRIGGER
    else:
        # Subsequent partial — stay in AWAITING_EXIT_TRIGGER
        new_state = BotState.AWAITING_EXIT_TRIGGER

    return TransitionResult(
        new_state=new_state,
        state_patch=patch,
        side_effects=[SideEffect(
            action="emit_strategy_event",
            args={
                "type": "OrderFilled",
                "side": "BUY",
                "qty": str(qty_incr),
                "price": str(price),
                "serial": p.get("serial"),
            },
        )],
    )


def _h_entry_cancelled(doc: dict, event: BotEvent) -> TransitionResult:
    return TransitionResult(
        new_state=BotState.AWAITING_ENTRY_TRIGGER,
        state_patch=_clear_position_fields(),
        side_effects=[SideEffect(
            action="emit_strategy_event",
            args={"type": "OrderRejected", "reason": event.payload.get("reason", "cancelled")},
        )],
    )


def _h_entry_timeout(doc: dict, event: BotEvent) -> TransitionResult:
    serial = doc.get("serial")
    side_effects: list[SideEffect] = []
    if serial:
        side_effects.append(SideEffect(
            action="cancel_order",
            args={"serial": serial},
        ))
    return TransitionResult(
        new_state=BotState.AWAITING_ENTRY_TRIGGER,
        state_patch=_clear_position_fields(),
        side_effects=side_effects,
    )


def _h_quote_tick(doc: dict, event: BotEvent) -> TransitionResult:
    """Quote tick in AWAITING_EXIT_TRIGGER — recalculate HWM + current_stop.

    The strategy decides whether this tick triggers an exit. The FSM
    stores updated trail fields and lets the caller run the strategy.
    If the strategy emits a PlaceExitOrder, that is a separate event.
    """
    p = event.payload
    last_price = Decimal(str(p.get("price", "0")))
    hwm = Decimal(doc.get("high_water_mark") or "0")
    new_hwm = max(hwm, last_price)
    return TransitionResult(
        new_state=BotState.AWAITING_EXIT_TRIGGER,
        state_patch={
            "last_price": str(last_price),
            "high_water_mark": str(new_hwm),
        },
        side_effects=[SideEffect(
            action="run_strategy_tick",
            args={"last_price": str(last_price), "high_water_mark": str(new_hwm)},
        )],
    )


def _h_place_exit_order(doc: dict, event: BotEvent) -> TransitionResult:
    p = event.payload
    origin = p.get("origin", "strategy")
    qty = p.get("qty") or doc.get("qty") or "0"
    return TransitionResult(
        new_state=BotState.EXIT_ORDER_PLACED,
        state_patch={
            "order_qty": str(qty),
            "filled_qty": "0",
            "order_origin": origin,
        },
        side_effects=[SideEffect(
            action="place_order",
            args={
                "side": "SELL",
                "symbol": p.get("symbol") or doc.get("symbol"),
                "qty": str(qty),
                "order_type": p.get("order_type", "mid"),
                "origin": origin,
                "serial": doc.get("serial"),
            },
        )],
    )


def _h_exit_filled(doc: dict, event: BotEvent) -> TransitionResult:
    """Handle an exit fill.

    Accumulates ``filled_qty`` on the exit order. When the accumulated
    filled amount reaches ``order_qty``, transitions back to
    AWAITING_ENTRY_TRIGGER.
    """
    p = event.payload
    qty_incr = Decimal(str(p.get("qty", "0")))
    price = Decimal(str(p.get("price", "0")))
    prev_filled = Decimal(doc.get("filled_qty") or "0")
    new_filled = prev_filled + qty_incr
    order_qty = Decimal(doc.get("order_qty") or "0")

    side_effects = [SideEffect(
        action="emit_strategy_event",
        args={
            "type": "OrderFilled",
            "side": "SELL",
            "qty": str(qty_incr),
            "price": str(price),
            "serial": p.get("serial"),
        },
    )]

    if order_qty > 0 and new_filled < order_qty:
        # Partial — stay in EXIT_ORDER_PLACED
        return TransitionResult(
            new_state=BotState.EXIT_ORDER_PLACED,
            state_patch={"filled_qty": str(new_filled)},
            side_effects=side_effects,
        )

    # Fully exited — clear position, back to AWAITING_ENTRY_TRIGGER.
    entry_price = Decimal(doc.get("entry_price") or "0")
    realized_pnl = (price - entry_price) * order_qty if entry_price > 0 else Decimal("0")
    side_effects.append(SideEffect(
        action="record_trade_closed",
        args={"realized_pnl": str(realized_pnl), "serial": doc.get("serial")},
    ))
    return TransitionResult(
        new_state=BotState.AWAITING_ENTRY_TRIGGER,
        state_patch={
            **_clear_position_fields(),
            "last_realized_pnl": str(realized_pnl),
        },
        side_effects=side_effects,
    )


def _h_exit_cancelled(doc: dict, event: BotEvent) -> TransitionResult:
    return TransitionResult(
        new_state=BotState.AWAITING_EXIT_TRIGGER,
        state_patch={"order_qty": None, "filled_qty": "0"},
        side_effects=[],
    )


def _h_manual_close(doc: dict, event: BotEvent) -> TransitionResult:
    """User closed the position manually in TWS.

    Triggered by positionEvent stream when IB's qty drops below the bot's
    tracked qty. Discipline contract: bot doesn't compete with manual
    trades on the same symbol while active, so any external reduction is
    definitive.
    """
    p = event.payload
    return TransitionResult(
        new_state=BotState.AWAITING_ENTRY_TRIGGER,
        state_patch=_clear_position_fields(),
        side_effects=[SideEffect(
            action="log_event",
            args={
                "type": "MANUAL_CLOSE",
                "message": p.get("message", "IB qty dropped below tracked qty"),
                "payload": p,
            },
        )],
    )


# ---------------------------------------------------------------------------
# Transition table — the only place that (state, event) rules are defined.
# Adding a new transition means adding a row here and a handler above.
# ---------------------------------------------------------------------------

_TRANSITIONS: dict[tuple[BotState, EventType], Callable[[dict, BotEvent], TransitionResult]] = {
    # Start / Stop / ForceStop — available from most states
    (BotState.OFF, EventType.START): _h_start,
    (BotState.ERRORED, EventType.START): _h_start,

    (BotState.AWAITING_ENTRY_TRIGGER, EventType.STOP): _h_stop,
    (BotState.ENTRY_ORDER_PLACED, EventType.STOP): _h_stop,
    (BotState.AWAITING_EXIT_TRIGGER, EventType.STOP): _h_stop,
    (BotState.EXIT_ORDER_PLACED, EventType.STOP): _h_stop,
    (BotState.ERRORED, EventType.STOP): _h_stop,

    (BotState.AWAITING_ENTRY_TRIGGER, EventType.FORCE_STOP): _h_force_stop,
    (BotState.ENTRY_ORDER_PLACED, EventType.FORCE_STOP): _h_force_stop,
    (BotState.AWAITING_EXIT_TRIGGER, EventType.FORCE_STOP): _h_force_stop,
    (BotState.EXIT_ORDER_PLACED, EventType.FORCE_STOP): _h_force_stop,

    # Crash / IB mismatch — from any non-OFF state
    (BotState.AWAITING_ENTRY_TRIGGER, EventType.CRASH): _h_crash,
    (BotState.ENTRY_ORDER_PLACED, EventType.CRASH): _h_crash,
    (BotState.AWAITING_EXIT_TRIGGER, EventType.CRASH): _h_crash,
    (BotState.EXIT_ORDER_PLACED, EventType.CRASH): _h_crash,

    (BotState.AWAITING_ENTRY_TRIGGER, EventType.IB_POSITION_MISMATCH): _h_ib_mismatch,
    (BotState.ENTRY_ORDER_PLACED, EventType.IB_POSITION_MISMATCH): _h_ib_mismatch,
    (BotState.AWAITING_EXIT_TRIGGER, EventType.IB_POSITION_MISMATCH): _h_ib_mismatch,
    (BotState.EXIT_ORDER_PLACED, EventType.IB_POSITION_MISMATCH): _h_ib_mismatch,

    # Entry flow
    (BotState.AWAITING_ENTRY_TRIGGER, EventType.PLACE_ENTRY_ORDER): _h_place_entry_order,
    (BotState.ENTRY_ORDER_PLACED, EventType.ENTRY_FILLED): _h_entry_filled,
    # Fill can arrive before PlaceEntryOrder due to async race (fill via
    # stream vs HTTP response). Allow direct transition.
    (BotState.AWAITING_ENTRY_TRIGGER, EventType.ENTRY_FILLED): _h_entry_filled,
    (BotState.AWAITING_EXIT_TRIGGER, EventType.ENTRY_FILLED): _h_entry_filled,  # partial fills
    (BotState.ENTRY_ORDER_PLACED, EventType.ENTRY_CANCELLED): _h_entry_cancelled,
    (BotState.ENTRY_ORDER_PLACED, EventType.ENTRY_TIMEOUT): _h_entry_timeout,

    # Exit flow
    (BotState.AWAITING_EXIT_TRIGGER, EventType.QUOTE_TICK): _h_quote_tick,
    (BotState.AWAITING_EXIT_TRIGGER, EventType.PLACE_EXIT_ORDER): _h_place_exit_order,
    (BotState.EXIT_ORDER_PLACED, EventType.EXIT_FILLED): _h_exit_filled,
    (BotState.EXIT_ORDER_PLACED, EventType.EXIT_CANCELLED): _h_exit_cancelled,

    # Manual close — valid while holding or exiting a position
    (BotState.AWAITING_EXIT_TRIGGER, EventType.MANUAL_CLOSE): _h_manual_close,
    (BotState.EXIT_ORDER_PLACED, EventType.MANUAL_CLOSE): _h_manual_close,
}


# ---------------------------------------------------------------------------
# FSM driver
# ---------------------------------------------------------------------------


class FSM:
    """Single-writer state machine backed by Redis ``bot:<id>:fsm``.

    ``dispatch(event)`` is the only public mutator. It:
      1. Loads the current doc from Redis
      2. Looks up the handler by (state, event.type)
      3. If no handler, logs and returns None
      4. Otherwise merges the returned patch, persists, returns result

    The caller (bot runtime) is responsible for executing the side
    effects the handler returned.
    """

    def __init__(self, bot_id: str, redis) -> None:
        self.bot_id = bot_id
        self._redis = redis

    @property
    def key(self) -> str:
        return f"bot:{self.bot_id}"

    async def load(self) -> dict:
        from ib_trader.redis.state import StateStore
        store = StateStore(self._redis)
        doc = await store.get(self.key)
        if doc is None:
            return {"state": BotState.OFF.value}
        return doc

    async def save(self, doc: dict) -> None:
        from ib_trader.redis.state import StateStore
        store = StateStore(self._redis)
        await store.set(self.key, doc)

    async def current_state(self) -> BotState:
        doc = await self.load()
        try:
            return BotState(doc.get("state", BotState.OFF.value))
        except ValueError:
            logger.error(
                '{"event": "FSM_CORRUPT_STATE", "bot_id": "%s", "state": %r}',
                self.bot_id, doc.get("state"),
            )
            return BotState.OFF

    async def dispatch(self, event: BotEvent) -> Optional[TransitionResult]:
        """Apply the transition rule for the current state + event.

        Returns the TransitionResult on success so the caller can execute
        side effects. Returns None if the (state, event) pair has no
        registered handler — the event is dropped and logged.
        """
        doc = await self.load()
        try:
            cur = BotState(doc.get("state", BotState.OFF.value))
        except ValueError:
            logger.error(
                '{"event": "FSM_CORRUPT_STATE", "bot_id": "%s", "state": %r}',
                self.bot_id, doc.get("state"),
            )
            cur = BotState.OFF

        handler = _TRANSITIONS.get((cur, event.type))
        if handler is None:
            logger.info(
                '{"event": "FSM_INVALID_TRANSITION", "bot_id": "%s", '
                '"state": "%s", "event_type": "%s"}',
                self.bot_id, cur.value, event.type.value,
            )
            return None

        try:
            result = handler(doc, event)
        except Exception:
            logger.exception(
                '{"event": "FSM_HANDLER_ERROR", "bot_id": "%s", '
                '"state": "%s", "event_type": "%s"}',
                self.bot_id, cur.value, event.type.value,
            )
            return None

        new_doc = {**doc, **result.state_patch}
        new_doc["state"] = result.new_state.value
        new_doc["updated_at"] = _now_iso()
        await self.save(new_doc)

        # Nudge the WS bots channel so the UI refreshes immediately
        if self._redis is not None and cur != result.new_state:
            try:
                from ib_trader.redis.streams import publish_activity
                await publish_activity(self._redis, "bots")
            except Exception:
                pass

        logger.info(
            '{"event": "FSM_TRANSITION", "bot_id": "%s", '
            '"from": "%s", "to": "%s", "trigger": "%s"}',
            self.bot_id, cur.value, result.new_state.value, event.type.value,
        )
        return result
