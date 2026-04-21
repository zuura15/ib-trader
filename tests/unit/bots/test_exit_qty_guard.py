"""Regression coverage for the qty > 0 invariant guard in exit strategies.

Runaway diagnosis: when the state doc ended up with
state=AWAITING_EXIT_TRIGGER + qty=0 (FSM/position desync), the strategy
kept emitting PlaceOrder(SELL, qty=Decimal("0")) on every quote tick.
The engine rejected each with 400 "QTY must be a positive number" but
nothing transitioned the FSM to stop the emission. Now the strategy
bails loudly when it sees qty<=0 in AWAITING_EXIT_TRIGGER.
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime, timezone

import pytest

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategies.close_trend_rsi import CloseTrendRsiStrategy
from ib_trader.bots.strategies.sawtooth_rsi import SawtoothRsiStrategy
from ib_trader.bots.strategy import QuoteUpdate, StrategyContext


def _quote(symbol: str = "QQQ") -> QuoteUpdate:
    return QuoteUpdate(
        symbol=symbol,
        bid=Decimal("644.30"),
        ask=Decimal("644.32"),
        last=Decimal("644.31"),
        timestamp=datetime.now(timezone.utc),
    )


def _ctx_awaiting_exit(symbol: str, qty_str: str, entry_price: str = "644.26") -> StrategyContext:
    return StrategyContext(
        state={
            "state": BotState.AWAITING_EXIT_TRIGGER.value,
            "symbol": symbol,
            "qty": qty_str,
            "entry_price": entry_price,
        },
        fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        bot_id="test-bot",
        config={"symbol": symbol, "_redis": None},
    )


def _default_config(symbol: str = "QQQ") -> dict:
    return {
        "symbol": symbol,
        "exit": {
            "hard_stop_loss_pct": 0.003,
            "trail_activation_pct": 0.00005,
            "trail_width_pct": 0.0005,
            "exit_price": "bid",
        },
        "risk": {"account_value": "10000"},
    }


@pytest.mark.parametrize("strategy_cls", [CloseTrendRsiStrategy, SawtoothRsiStrategy])
@pytest.mark.parametrize("qty_str", ["0", "0.0", "-1", ""])
def test_on_quote_refuses_zero_qty(strategy_cls, qty_str):
    """Strategy must not emit any PlaceOrder when qty<=0 in AWAITING_EXIT_TRIGGER."""
    strategy = strategy_cls(_default_config())
    ctx = _ctx_awaiting_exit("QQQ", qty_str)
    actions = strategy._on_quote(_quote(), ctx, BotState.AWAITING_EXIT_TRIGGER)
    from ib_trader.bots.strategy import PlaceOrder
    place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
    assert place_orders == [], (
        f"{strategy_cls.__name__} emitted a PlaceOrder despite qty={qty_str!r}"
    )


@pytest.mark.parametrize("strategy_cls", [CloseTrendRsiStrategy, SawtoothRsiStrategy])
def test_on_quote_accepts_positive_qty(strategy_cls):
    """Sanity: a positive qty is still processed (no false positives)."""
    strategy = strategy_cls(_default_config())
    ctx = _ctx_awaiting_exit("QQQ", "10")
    # Should not raise / bail on the qty guard; exit-logic may or may
    # not emit depending on price action, but the call must return.
    actions = strategy._on_quote(_quote(), ctx, BotState.AWAITING_EXIT_TRIGGER)
    assert isinstance(actions, list)
