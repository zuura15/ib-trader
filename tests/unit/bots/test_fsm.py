"""Tests for ib_trader.bots.fsm — one case per (state, event) cell.

Valid transitions verify the expected new_state + patch + side effects.
Invalid combinations verify that dispatch returns None and does not
mutate the stored doc.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from ib_trader.bots.fsm import (
    FSM, BotState, BotEvent, EventType, _TRANSITIONS,
)


class _FakeRedis:
    """Tiny in-memory stand-in for aioredis that handles the subset
    StateStore uses — set / get / delete / no TTL semantics."""
    def __init__(self):
        self._kv: dict[str, str] = {}

    async def set(self, key, value):
        self._kv[key] = value

    async def setex(self, key, ttl, value):
        self._kv[key] = value

    async def get(self, key):
        return self._kv.get(key)

    async def delete(self, key):
        self._kv.pop(key, None)


@pytest.fixture
def redis():
    return _FakeRedis()


@pytest.fixture
def fsm(redis):
    return FSM(bot_id="test-bot", redis=redis)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _prime(fsm: FSM, state: BotState, extra: dict | None = None) -> None:
    doc = {"state": state.value}
    if extra:
        doc.update(extra)
    await fsm.save(doc)


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_initial_state_is_off(fsm):
    assert (await fsm.current_state()) == BotState.OFF


# ---------------------------------------------------------------------------
# Start / Stop / ForceStop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_start_from_off_goes_to_awaiting_entry(fsm):
    result = await fsm.dispatch(BotEvent(EventType.START))
    assert result is not None
    assert result.new_state == BotState.AWAITING_ENTRY_TRIGGER
    assert (await fsm.current_state()) == BotState.AWAITING_ENTRY_TRIGGER


@pytest.mark.asyncio
async def test_start_from_errored_clears_error_fields(fsm):
    await _prime(fsm, BotState.ERRORED, {
        "error_reason": "crash",
        "error_message": "boom",
    })
    result = await fsm.dispatch(BotEvent(EventType.START))
    assert result.new_state == BotState.AWAITING_ENTRY_TRIGGER
    doc = await fsm.load()
    assert doc["error_reason"] is None
    assert doc["error_message"] is None


@pytest.mark.asyncio
async def test_start_in_awaiting_entry_is_invalid(fsm):
    await _prime(fsm, BotState.AWAITING_ENTRY_TRIGGER)
    result = await fsm.dispatch(BotEvent(EventType.START))
    assert result is None
    assert (await fsm.current_state()) == BotState.AWAITING_ENTRY_TRIGGER


@pytest.mark.asyncio
async def test_stop_from_awaiting_entry(fsm):
    await _prime(fsm, BotState.AWAITING_ENTRY_TRIGGER)
    result = await fsm.dispatch(BotEvent(EventType.STOP))
    assert result.new_state == BotState.OFF
    assert result.side_effects == []


@pytest.mark.asyncio
async def test_stop_from_entry_order_placed_requests_cancel(fsm):
    # cancel-by-symbol is the runtime-side resolution path because we
    # need to handle pre-fill orders that have no trade serial yet.
    await _prime(fsm, BotState.ENTRY_ORDER_PLACED, {
        "serial": 42, "symbol": "F", "ib_order_id": "ib-123",
    })
    result = await fsm.dispatch(BotEvent(EventType.STOP))
    assert result.new_state == BotState.OFF
    assert len(result.side_effects) == 1
    assert result.side_effects[0].action == "cancel_order"
    assert result.side_effects[0].args == {
        "symbol": "F", "serial": 42, "ib_order_id": "ib-123",
    }


@pytest.mark.asyncio
async def test_stop_from_off_is_invalid(fsm):
    # Stop while already OFF should be a no-op drop.
    result = await fsm.dispatch(BotEvent(EventType.STOP))
    assert result is None
    assert (await fsm.current_state()) == BotState.OFF


@pytest.mark.asyncio
async def test_force_stop_records_reason(fsm):
    await _prime(fsm, BotState.AWAITING_EXIT_TRIGGER)
    result = await fsm.dispatch(BotEvent(
        EventType.FORCE_STOP,
        payload={"message": "operator abort"},
    ))
    assert result.new_state == BotState.ERRORED
    doc = await fsm.load()
    assert doc["error_reason"] == "force_stop"
    assert doc["error_message"] == "operator abort"


@pytest.mark.asyncio
async def test_force_stop_from_off_is_invalid(fsm):
    result = await fsm.dispatch(BotEvent(EventType.FORCE_STOP))
    assert result is None


# ---------------------------------------------------------------------------
# Entry flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_place_entry_order_goes_to_entry_order_placed(fsm):
    await _prime(fsm, BotState.AWAITING_ENTRY_TRIGGER)
    result = await fsm.dispatch(BotEvent(
        EventType.PLACE_ENTRY_ORDER,
        payload={"symbol": "F", "qty": Decimal("10"), "origin": "strategy", "serial": 7},
    ))
    assert result.new_state == BotState.ENTRY_ORDER_PLACED
    doc = await fsm.load()
    assert doc["order_qty"] == "10"
    assert doc["filled_qty"] == "0"
    assert doc["serial"] == 7
    assert doc["order_origin"] == "strategy"
    assert result.side_effects[0].action == "place_order"
    assert result.side_effects[0].args["side"] == "BUY"
    assert result.side_effects[0].args["origin"] == "strategy"


@pytest.mark.asyncio
async def test_place_entry_order_force_buy_origin(fsm):
    await _prime(fsm, BotState.AWAITING_ENTRY_TRIGGER)
    result = await fsm.dispatch(BotEvent(
        EventType.PLACE_ENTRY_ORDER,
        payload={"symbol": "F", "qty": 5, "origin": "manual_override"},
    ))
    doc = await fsm.load()
    assert doc["order_origin"] == "manual_override"
    assert result.side_effects[0].args["origin"] == "manual_override"


@pytest.mark.asyncio
async def test_place_entry_order_invalid_outside_awaiting_entry(fsm):
    await _prime(fsm, BotState.AWAITING_EXIT_TRIGGER)
    result = await fsm.dispatch(BotEvent(
        EventType.PLACE_ENTRY_ORDER,
        payload={"symbol": "F", "qty": 10},
    ))
    assert result is None


@pytest.mark.asyncio
async def test_entry_filled_terminal_transitions_and_inits_hwm(fsm):
    await _prime(fsm, BotState.ENTRY_ORDER_PLACED, {
        "order_qty": "10",
        "filled_qty": "0",
        "serial": 7,
    })
    result = await fsm.dispatch(BotEvent(
        EventType.ENTRY_FILLED,
        payload={"qty": "10", "price": "12.73", "serial": 7, "terminal": True},
    ))
    assert result.new_state == BotState.AWAITING_EXIT_TRIGGER
    doc = await fsm.load()
    assert doc["qty"] == "10"
    assert doc["entry_price"] == "12.73"
    assert doc["high_water_mark"] == "12.73"
    assert doc["trail_activated"] is False


@pytest.mark.asyncio
async def test_entry_fill_progress_does_not_transition(fsm):
    """Non-terminal progress events track cumulative qty but don't
    move the bot into AWAITING_EXIT_TRIGGER — the bot is only
    'active' once IB declares the order terminal."""
    await _prime(fsm, BotState.ENTRY_ORDER_PLACED, {
        "order_qty": "15",
        "filled_qty": "0",
        "serial": 7,
    })
    result = await fsm.dispatch(BotEvent(
        EventType.ENTRY_FILLED,
        payload={"qty": "9", "price": "12.73", "serial": 7, "terminal": False},
    ))
    assert result.new_state == BotState.ENTRY_ORDER_PLACED
    doc = await fsm.load()
    assert doc["filled_qty"] == "9"
    assert doc["qty"] == "9"
    # Position-anchor fields must NOT be written on a progress event.
    assert doc.get("entry_price") is None
    assert doc.get("entry_time") is None
    assert doc.get("high_water_mark") is None


@pytest.mark.asyncio
async def test_entry_fill_progress_then_terminal_uses_cumulative(fsm):
    """Engine emits cumulative filled_qty in every event. The FSM must
    assign, not accumulate — otherwise 9 then terminal-9 would end up
    as filled_qty=18.
    """
    await _prime(fsm, BotState.ENTRY_ORDER_PLACED, {
        "order_qty": "15",
        "filled_qty": "0",
        "serial": 7,
    })
    # Progress: 9 filled so far
    await fsm.dispatch(BotEvent(
        EventType.ENTRY_FILLED,
        payload={"qty": "9", "price": "12.73", "serial": 7, "terminal": False},
    ))
    # Terminal (PartialFillCancelled): final cumulative still 9
    result = await fsm.dispatch(BotEvent(
        EventType.ENTRY_FILLED,
        payload={"qty": "9", "price": "12.73", "serial": 7, "terminal": True},
    ))
    assert result.new_state == BotState.AWAITING_EXIT_TRIGGER
    doc = await fsm.load()
    assert doc["filled_qty"] == "9"   # not 18
    assert doc["qty"] == "9"
    assert doc["entry_price"] == "12.73"
    assert doc["high_water_mark"] == "12.73"


@pytest.mark.asyncio
async def test_entry_fill_terminal_partial_cancelled_still_active(fsm):
    """PartialFillCancelled with filled_qty > 0: bot becomes active
    with whatever shares IB delivered (per the 'any qty on terminal'
    rule)."""
    await _prime(fsm, BotState.ENTRY_ORDER_PLACED, {
        "order_qty": "15",
        "filled_qty": "0",
        "serial": 7,
    })
    result = await fsm.dispatch(BotEvent(
        EventType.ENTRY_FILLED,
        payload={"qty": "1", "price": "12.73", "serial": 7, "terminal": True},
    ))
    assert result.new_state == BotState.AWAITING_EXIT_TRIGGER
    doc = await fsm.load()
    assert doc["qty"] == "1"
    assert doc["entry_price"] == "12.73"


@pytest.mark.asyncio
async def test_entry_fill_terminal_zero_qty_returns_to_awaiting_entry(fsm):
    """Terminal with 0 fills is a safety-net path — the runtime
    normally routes 0-fill terminals through ENTRY_CANCELLED, but if
    ENTRY_FILLED arrives with qty=0 we treat it as a cancel."""
    await _prime(fsm, BotState.ENTRY_ORDER_PLACED, {
        "order_qty": "10",
        "filled_qty": "0",
        "serial": 7,
    })
    result = await fsm.dispatch(BotEvent(
        EventType.ENTRY_FILLED,
        payload={"qty": "0", "price": "0", "serial": 7, "terminal": True},
    ))
    assert result.new_state == BotState.AWAITING_ENTRY_TRIGGER
    doc = await fsm.load()
    assert doc["qty"] == "0"
    assert doc.get("entry_price") is None


@pytest.mark.asyncio
async def test_entry_fill_on_awaiting_exit_is_invalid_transition(fsm):
    """Once terminal has moved the bot into AWAITING_EXIT_TRIGGER, a
    late duplicate ENTRY_FILLED must not re-run the handler.
    """
    await _prime(fsm, BotState.AWAITING_EXIT_TRIGGER, {
        "qty": "9",
        "filled_qty": "9",
        "entry_price": "12.73",
    })
    result = await fsm.dispatch(BotEvent(
        EventType.ENTRY_FILLED,
        payload={"qty": "9", "price": "12.80", "serial": 7, "terminal": True},
    ))
    assert result is None  # invalid transition — dropped
    doc = await fsm.load()
    assert doc["qty"] == "9"           # unchanged
    assert doc["entry_price"] == "12.73"


@pytest.mark.asyncio
async def test_entry_cancelled_returns_to_awaiting_entry(fsm):
    await _prime(fsm, BotState.ENTRY_ORDER_PLACED, {
        "order_qty": "10", "serial": 7,
    })
    result = await fsm.dispatch(BotEvent(
        EventType.ENTRY_CANCELLED,
        payload={"reason": "ib_reject"},
    ))
    assert result.new_state == BotState.AWAITING_ENTRY_TRIGGER
    doc = await fsm.load()
    assert doc["qty"] == "0"
    assert doc["serial"] is None
    assert any(s.action == "emit_strategy_event" for s in result.side_effects)


@pytest.mark.asyncio
async def test_entry_timeout_cancels_order(fsm):
    await _prime(fsm, BotState.ENTRY_ORDER_PLACED, {
        "serial": 42, "symbol": "F", "ib_order_id": "ib-123",
    })
    result = await fsm.dispatch(BotEvent(EventType.ENTRY_TIMEOUT))
    assert result.new_state == BotState.AWAITING_ENTRY_TRIGGER
    assert any(
        s.action == "cancel_order"
        and s.args == {"symbol": "F", "serial": 42, "ib_order_id": "ib-123"}
        for s in result.side_effects
    )


@pytest.mark.asyncio
async def test_entry_filled_in_off_is_invalid(fsm):
    result = await fsm.dispatch(BotEvent(EventType.ENTRY_FILLED, payload={"qty": "1"}))
    assert result is None


# ---------------------------------------------------------------------------
# Exit flow
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_quote_tick_updates_hwm(fsm):
    await _prime(fsm, BotState.AWAITING_EXIT_TRIGGER, {
        "qty": "10", "entry_price": "12.73", "high_water_mark": "12.73",
    })
    result = await fsm.dispatch(BotEvent(
        EventType.QUOTE_TICK,
        payload={"price": "12.80"},
    ))
    assert result.new_state == BotState.AWAITING_EXIT_TRIGGER
    doc = await fsm.load()
    assert doc["high_water_mark"] == "12.80"
    assert doc["last_price"] == "12.80"


@pytest.mark.asyncio
async def test_quote_tick_below_hwm_doesnt_lower_it(fsm):
    await _prime(fsm, BotState.AWAITING_EXIT_TRIGGER, {
        "qty": "10", "entry_price": "12.73", "high_water_mark": "12.90",
    })
    await fsm.dispatch(BotEvent(
        EventType.QUOTE_TICK,
        payload={"price": "12.85"},
    ))
    doc = await fsm.load()
    assert doc["high_water_mark"] == "12.90"  # unchanged
    assert doc["last_price"] == "12.85"


@pytest.mark.asyncio
async def test_quote_tick_in_awaiting_entry_is_invalid(fsm):
    await _prime(fsm, BotState.AWAITING_ENTRY_TRIGGER)
    result = await fsm.dispatch(BotEvent(
        EventType.QUOTE_TICK, payload={"price": "12.80"},
    ))
    assert result is None


@pytest.mark.asyncio
async def test_place_exit_order_goes_to_exit_order_placed(fsm):
    await _prime(fsm, BotState.AWAITING_EXIT_TRIGGER, {
        "qty": "10", "entry_price": "12.73", "symbol": "F",
    })
    result = await fsm.dispatch(BotEvent(
        EventType.PLACE_EXIT_ORDER,
        payload={"origin": "trail", "symbol": "F"},
    ))
    assert result.new_state == BotState.EXIT_ORDER_PLACED
    doc = await fsm.load()
    assert doc["order_origin"] == "trail"
    assert result.side_effects[0].action == "place_order"
    assert result.side_effects[0].args["side"] == "SELL"


@pytest.mark.asyncio
async def test_exit_filled_full_returns_to_awaiting_entry(fsm):
    await _prime(fsm, BotState.EXIT_ORDER_PLACED, {
        "qty": "10", "entry_price": "12.73", "order_qty": "10", "filled_qty": "0",
    })
    result = await fsm.dispatch(BotEvent(
        EventType.EXIT_FILLED,
        payload={"qty": "10", "price": "12.80", "terminal": True},
    ))
    assert result.new_state == BotState.AWAITING_ENTRY_TRIGGER
    doc = await fsm.load()
    assert doc["qty"] == "0"
    assert doc["entry_price"] is None
    # P&L should be computed: (12.80 - 12.73) * 10 = 0.7
    assert any(
        s.action == "record_trade_closed"
        for s in result.side_effects
    )


@pytest.mark.asyncio
async def test_exit_filled_progress_stays_in_exit_order_placed(fsm):
    """Non-terminal partials track cumulative but don't transition."""
    await _prime(fsm, BotState.EXIT_ORDER_PLACED, {
        "qty": "10", "entry_price": "12.73", "order_qty": "10", "filled_qty": "0",
    })
    result = await fsm.dispatch(BotEvent(
        EventType.EXIT_FILLED,
        payload={"qty": "4", "price": "12.80", "terminal": False},
    ))
    assert result.new_state == BotState.EXIT_ORDER_PLACED
    doc = await fsm.load()
    assert doc["filled_qty"] == "4"
    # Position qty untouched during progress — only terminal decrements it.
    assert doc["qty"] == "10"


@pytest.mark.asyncio
async def test_exit_filled_terminal_partial_retries_for_residual(fsm):
    """Terminal with residual triggers a retry: new SELL for the
    remaining position qty, exit_retries counter bumped, stay in
    EXIT_ORDER_PLACED because another order is going in."""
    await _prime(fsm, BotState.EXIT_ORDER_PLACED, {
        "qty": "10", "entry_price": "12.73",
        "order_qty": "10", "filled_qty": "0",
        "symbol": "QQQ",
    })
    result = await fsm.dispatch(BotEvent(
        EventType.EXIT_FILLED,
        payload={"qty": "7", "price": "12.80", "terminal": True},
    ))
    assert result.new_state == BotState.EXIT_ORDER_PLACED
    doc = await fsm.load()
    assert doc["qty"] == "3"            # position reduced by the 7 filled
    assert doc["order_qty"] == "3"      # next retry order is for residual
    assert doc["filled_qty"] == "0"     # reset for the retry order
    assert int(doc["exit_retries"]) == 1
    retry = next(s for s in result.side_effects if s.action == "retry_exit_order")
    assert retry.args["qty"] == "3"
    assert retry.args["symbol"] == "QQQ"
    assert retry.args["attempt"] == 1


@pytest.mark.asyncio
async def test_exit_filled_retries_exhausted_raises_pager(fsm):
    """Fourth partial terminal (after 3 retries) -> ERRORED + pager."""
    await _prime(fsm, BotState.EXIT_ORDER_PLACED, {
        "qty": "3", "entry_price": "12.73",
        "order_qty": "3", "filled_qty": "0",
        "symbol": "QQQ",
        "exit_retries": 3,
    })
    result = await fsm.dispatch(BotEvent(
        EventType.EXIT_FILLED,
        payload={"qty": "1", "price": "12.80", "terminal": True},
    ))
    assert result.new_state == BotState.ERRORED
    doc = await fsm.load()
    assert doc["qty"] == "2"           # 2 shares still unsold
    assert doc["error_reason"] == "exit_retries_exhausted"
    pager = next(s for s in result.side_effects if s.action == "pager_alert")
    assert pager.args["severity"] == "CATASTROPHIC"
    assert pager.args["trigger"] == "BOT_EXIT_RETRIES_EXHAUSTED"
    assert pager.args["residual_qty"] == "2"


@pytest.mark.asyncio
async def test_exit_filled_progress_does_not_count_as_retry(fsm):
    """Progress (non-terminal) must not bump the retry counter or
    trigger escalation, even if the running cumulative is below
    order_qty."""
    await _prime(fsm, BotState.EXIT_ORDER_PLACED, {
        "qty": "10", "entry_price": "12.73",
        "order_qty": "10", "filled_qty": "0",
        "symbol": "QQQ",
        "exit_retries": 3,     # would escalate on terminal
    })
    result = await fsm.dispatch(BotEvent(
        EventType.EXIT_FILLED,
        payload={"qty": "1", "price": "12.80", "terminal": False},
    ))
    assert result.new_state == BotState.EXIT_ORDER_PLACED
    doc = await fsm.load()
    assert int(doc["exit_retries"]) == 3   # unchanged
    assert doc["qty"] == "10"              # unchanged
    # No pager from a progress event.
    assert not any(s.action == "pager_alert" for s in result.side_effects)


@pytest.mark.asyncio
async def test_crash_raises_pager_alert(fsm):
    await _prime(fsm, BotState.AWAITING_EXIT_TRIGGER, {
        "qty": "5", "symbol": "QQQ",
    })
    result = await fsm.dispatch(BotEvent(
        EventType.CRASH,
        payload={"message": "IB disconnected mid-exit"},
    ))
    assert result.new_state == BotState.ERRORED
    pager = next(s for s in result.side_effects if s.action == "pager_alert")
    assert pager.args["trigger"] == "BOT_CRASH"
    assert "IB disconnected" in pager.args["message"]


@pytest.mark.asyncio
async def test_exit_cancelled_returns_to_awaiting_exit(fsm):
    await _prime(fsm, BotState.EXIT_ORDER_PLACED, {
        "qty": "10", "entry_price": "12.73",
    })
    result = await fsm.dispatch(BotEvent(EventType.EXIT_CANCELLED))
    assert result.new_state == BotState.AWAITING_EXIT_TRIGGER
    doc = await fsm.load()
    assert doc["qty"] == "10"            # preserved
    assert doc["entry_price"] == "12.73"  # preserved


# ---------------------------------------------------------------------------
# ManualClose / Crash / IBPositionMismatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_manual_close_from_awaiting_exit(fsm):
    await _prime(fsm, BotState.AWAITING_EXIT_TRIGGER, {
        "qty": "10", "entry_price": "12.73",
    })
    result = await fsm.dispatch(BotEvent(
        EventType.MANUAL_CLOSE,
        payload={"message": "user flattened in TWS"},
    ))
    assert result.new_state == BotState.AWAITING_ENTRY_TRIGGER
    assert any(
        s.action == "log_event" and s.args.get("type") == "MANUAL_CLOSE"
        for s in result.side_effects
    )


@pytest.mark.asyncio
async def test_manual_close_from_exit_order_placed(fsm):
    await _prime(fsm, BotState.EXIT_ORDER_PLACED, {"qty": "10"})
    result = await fsm.dispatch(BotEvent(EventType.MANUAL_CLOSE))
    assert result.new_state == BotState.AWAITING_ENTRY_TRIGGER


@pytest.mark.asyncio
async def test_manual_close_in_awaiting_entry_is_invalid(fsm):
    await _prime(fsm, BotState.AWAITING_ENTRY_TRIGGER)
    result = await fsm.dispatch(BotEvent(EventType.MANUAL_CLOSE))
    assert result is None


@pytest.mark.asyncio
@pytest.mark.parametrize("state", [
    BotState.AWAITING_ENTRY_TRIGGER,
    BotState.ENTRY_ORDER_PLACED,
    BotState.AWAITING_EXIT_TRIGGER,
    BotState.EXIT_ORDER_PLACED,
])
async def test_crash_from_any_non_off_goes_to_errored(fsm, state):
    await _prime(fsm, state)
    result = await fsm.dispatch(BotEvent(
        EventType.CRASH,
        payload={"message": "boom"},
    ))
    assert result.new_state == BotState.ERRORED
    doc = await fsm.load()
    assert doc["error_reason"] == "crash"


@pytest.mark.asyncio
async def test_crash_from_off_is_invalid(fsm):
    result = await fsm.dispatch(BotEvent(EventType.CRASH))
    assert result is None


@pytest.mark.asyncio
async def test_ib_mismatch_goes_to_errored(fsm):
    await _prime(fsm, BotState.AWAITING_EXIT_TRIGGER, {"qty": "10"})
    result = await fsm.dispatch(BotEvent(
        EventType.IB_POSITION_MISMATCH,
        payload={"message": "ib says 0, bot says 10"},
    ))
    assert result.new_state == BotState.ERRORED
    doc = await fsm.load()
    assert doc["error_reason"] == "ib_mismatch"


# ---------------------------------------------------------------------------
# Invalid transition cells — every (state, event) NOT in _TRANSITIONS must
# be a no-op drop. This is a coverage test: enumerate the full matrix and
# verify that anything outside _TRANSITIONS returns None.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("state", list(BotState))
@pytest.mark.parametrize("event_type", list(EventType))
async def test_invalid_transitions_return_none(fsm, state, event_type):
    if (state, event_type) in _TRANSITIONS:
        return  # valid cell, covered by targeted tests above
    await _prime(fsm, state)
    result = await fsm.dispatch(BotEvent(event_type))
    assert result is None, f"({state.value}, {event_type.value}) should be invalid"
    # State must not have changed.
    assert (await fsm.current_state()) == state


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_dispatch_persists_state(redis, fsm):
    await fsm.dispatch(BotEvent(EventType.START))
    # A fresh FSM instance pointing at the same Redis should see AWAITING_ENTRY.
    fsm2 = FSM(bot_id="test-bot", redis=redis)
    assert (await fsm2.current_state()) == BotState.AWAITING_ENTRY_TRIGGER


@pytest.mark.asyncio
async def test_corrupt_state_defaults_to_off(redis, fsm):
    # Write a garbage state value directly.
    from ib_trader.redis.state import StateStore
    await StateStore(redis).set(fsm.key, {"state": "NONSENSE"})
    assert (await fsm.current_state()) == BotState.OFF
