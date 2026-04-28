"""Strategy SELL handler must not clobber state on a partial close.

GH #87: when a force-sell of 116 GLD partial-filled at 72/116 with the
residual cancelled by IB, the runtime's ``on_exit_filled`` correctly
identified the residual (44) and patched ``state.qty="44"`` for the
retry path. The strategy's ``_on_fill`` SELL handler then ran and
unconditionally executed an UpdateState that cleared every position
field — wiping the residual tracking. Subsequent fills tried to read
``state.qty`` (now ``None``) as a Decimal and crashed with
``InvalidOperation``.

These tests pin the contract: when the post-runtime state still shows
a non-zero qty (the residual), the strategy must NOT touch position
fields and must NOT emit a ``CLOSED`` audit row.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from ib_trader.bots.strategies.close_trend_rsi import CloseTrendRsiStrategy
from ib_trader.bots.strategies.sawtooth_rsi import SawtoothRsiStrategy
from ib_trader.bots.strategy import (
    StrategyContext, OrderFilled, UpdateState, LogSignal, LogEventType,
)
from ib_trader.bots.lifecycle import BotState


def _ctx(qty: str | None, entry_price: str = "428.85") -> StrategyContext:
    return StrategyContext(
        state={
            "qty": qty,
            "entry_price": entry_price,
            "entry_time": "2026-04-28T02:53:08+00:00",
            "trade_serial": 385,
            "symbol": "GLD",
        },
        fsm_state=BotState.EXIT_ORDER_PLACED,
        bot_id="test-bot",
        config={"symbol": "GLD"},
    )


def _make_close_trend() -> CloseTrendRsiStrategy:
    return CloseTrendRsiStrategy({
        "symbol": "GLD",
        "max_position_value": "50000",
        "max_shares": 120,
        "exit": {
            "hard_stop_loss_pct": "0.001",
            "trail_activation_pct": "0.0005",
            "trail_width_pct": "0.0015",
        },
    })


def _make_sawtooth() -> SawtoothRsiStrategy:
    return SawtoothRsiStrategy({
        "symbol": "GLD",
        "max_position_value": "50000",
        "max_shares": 120,
        "exit": {
            "hard_stop_loss_pct": "0.001",
            "trail_activation_pct": "0.0005",
            "trail_width_pct": "0.0015",
        },
    })


def _fill_event(qty: str = "72") -> OrderFilled:
    return OrderFilled(
        trade_serial=385, symbol="GLD", side="SELL",
        fill_price=Decimal("429.16"),
        qty=Decimal(qty),
        commission=Decimal("0"),
        ib_order_id="1201",
    )


@pytest.mark.parametrize(
    "make_strategy",
    [_make_close_trend, _make_sawtooth],
    ids=["close_trend_rsi", "sawtooth_rsi"],
)
def test_partial_close_does_not_clobber_state(make_strategy):
    """Runtime set state.qty='44' (residual). Strategy SELL handler must
    NOT emit UpdateState that clears qty / entry_price / etc."""
    strategy = make_strategy()
    ctx = _ctx(qty="44")  # residual remaining
    actions = strategy._on_fill(_fill_event(qty="72"), ctx, BotState.EXIT_ORDER_PLACED)

    # No CLOSED audit on partial.
    closed = [a for a in actions if isinstance(a, LogSignal) and a.event_type == LogEventType.CLOSED]
    assert closed == [], "partial close must not emit CLOSED audit"

    # No UpdateState that nukes position fields.
    for a in actions:
        if isinstance(a, UpdateState):
            assert "qty" not in a.state, \
                "partial close UpdateState must not touch qty (would clobber residual)"
            assert "entry_price" not in a.state, \
                "partial close UpdateState must not touch entry_price"
            assert "high_water_mark" not in a.state
            assert "current_stop" not in a.state


@pytest.mark.parametrize(
    "make_strategy",
    [_make_close_trend, _make_sawtooth],
    ids=["close_trend_rsi", "sawtooth_rsi"],
)
def test_partial_close_emits_informational_log(make_strategy):
    """Partial close should still surface an audit row, but as
    EXIT_CHECK (informational), not CLOSED."""
    strategy = make_strategy()
    ctx = _ctx(qty="44")
    actions = strategy._on_fill(_fill_event(qty="72"), ctx, BotState.EXIT_ORDER_PLACED)

    logs = [a for a in actions if isinstance(a, LogSignal)]
    assert len(logs) >= 1
    assert any("partial sell" in (l.message or "").lower() for l in logs)


@pytest.mark.parametrize(
    "make_strategy,empty_qty",
    [(_make_close_trend, "0"),
     (_make_close_trend, None),
     (_make_sawtooth, "0"),
     (_make_sawtooth, None)],
    ids=["close_trend_zero", "close_trend_none",
         "sawtooth_zero", "sawtooth_none"],
)
def test_full_close_emits_closed_audit_and_clears_strategy_fields(
    make_strategy, empty_qty,
):
    """Runtime cleared state.qty (to '0' or None) — full close. Strategy
    must emit the CLOSED audit and clear strategy-scoped fields
    (trade_serial), but does NOT need to clear runtime-owned position
    fields (those are already wiped)."""
    strategy = make_strategy()
    ctx = _ctx(qty=empty_qty)
    actions = strategy._on_fill(_fill_event(qty="116"), ctx, BotState.EXIT_ORDER_PLACED)

    closed = [a for a in actions if isinstance(a, LogSignal) and a.event_type == LogEventType.CLOSED]
    assert len(closed) == 1, "full close must emit one CLOSED audit"

    # Strategy still clears trade_serial.
    state_writes = [a for a in actions if isinstance(a, UpdateState)]
    assert any("trade_serial" in w.state for w in state_writes)

    # The buggy unconditional clears must NOT happen on full close
    # either — the runtime owns those fields. (Defensive regression
    # guard: if a future change re-introduces them, they'd at least
    # write known values, not None over a "0".)
    for w in state_writes:
        assert w.state.get("qty") is not False  # not the buggy None clobber
        # Specifically: qty must not be in the patch at all.
        assert "qty" not in w.state, \
            "strategy must not touch qty on full close — runtime already cleared it"


def test_apply_fill_does_not_crash_on_qty_none():
    """Defensive: ``_apply_fill`` previously eagerly parsed
    ``state.qty`` as Decimal at the top. With qty=None (legacy path,
    or any future bug), ``Decimal(str(None))`` raised
    ``InvalidOperation`` and broke the dispatch loop. Test pins that
    the function no longer crashes on this input."""
    from ib_trader.bots.runtime import StrategyBotRunner
    from unittest.mock import AsyncMock, MagicMock

    runner = StrategyBotRunner.__new__(StrategyBotRunner)
    runner.bot_id = "test-bot"
    runner.strategy_config = {"symbol": "GLD"}
    runner.ctx = StrategyContext(
        state={"qty": None, "entry_price": None, "symbol": "GLD"},
        fsm_state=BotState.EXIT_ORDER_PLACED,
        bot_id="test-bot",
        config={"symbol": "GLD"},
    )
    runner.config = {"_redis": None, "_engine_url": "http://test"}
    runner.strategy = MagicMock()
    runner.strategy.on_event = AsyncMock(return_value=[])
    runner._refresh_state = AsyncMock()
    runner._read_state_doc = AsyncMock(return_value={"qty": "0", "entry_price": "428.85"})
    runner._write_state = AsyncMock()
    runner._run_pipeline = AsyncMock()

    import asyncio
    # Should NOT raise InvalidOperation on the dead `existing_qty` line.
    asyncio.run(runner._apply_fill(
        bot_ref="test-bot", symbol="GLD", side="S",
        qty=Decimal("44"), price=Decimal("429.0"),
        commission=Decimal("0"), ib_order_id="ib-1202",
    ))
