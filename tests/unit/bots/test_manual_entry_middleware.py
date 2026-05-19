"""Tests for ManualEntryMiddleware.

This gate decides which strategy-emitted ``PlaceOrder`` actions reach
the execution middleware. The invariants the middleware must uphold:

  1. Disabled → pass-through (covers every existing production bot).
  2. Enabled → drop ANY ``PlaceOrder(origin="strategy")`` — both
     LONG entries (BUY) and SHORT entries (SELL).
  3. Enabled → exits (``origin="exit"``) pass, regardless of side.
  4. Enabled → manual overrides (``origin="manual_override"``) pass.
  5. Enabled → non-PlaceOrder actions (LogSignal, UpdateState, …) pass.
  6. Blocked entries leave a LogSignal audit trail so bot_events
     records the intent even though nothing shipped to the broker.

Pre-2026-05-19 the middleware only blocked BUYs and let SELL strategy
entries through, which meant chart_signal SHORTs auto-fired even with
the flag on. The current invariant gates entries by ``origin`` alone.
"""
from decimal import Decimal

import pytest

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.middleware import ManualEntryMiddleware
from ib_trader.bots.strategy import (
    LogSignal, PlaceOrder, StrategyContext, UpdateState,
)


def _buy(origin: str = "strategy") -> PlaceOrder:
    return PlaceOrder(
        symbol="F", side="BUY", qty=Decimal("10"), order_type="mid", origin=origin,
    )


def _sell(origin: str = "strategy") -> PlaceOrder:
    return PlaceOrder(
        symbol="F", side="SELL", qty=Decimal("10"), order_type="market", origin=origin,
    )


@pytest.fixture
def ctx():
    # StrategyContext is only used as a bag for strategy state here; the
    # middleware doesn't read anything off it.
    return StrategyContext(
        state={}, fsm_state=BotState.AWAITING_ENTRY_TRIGGER, bot_id="bot1", config={},
    )


class TestDisabled:
    def test_passthrough_buy(self, ctx):
        mw = ManualEntryMiddleware("bot1", manual_entry_only=False)
        out = mw.process([_buy()], ctx)
        assert out == [_buy()]

    def test_passthrough_sell(self, ctx):
        mw = ManualEntryMiddleware("bot1", manual_entry_only=False)
        out = mw.process([_sell()], ctx)
        assert out == [_sell()]


class TestEnabled:
    def test_blocks_strategy_buy(self, ctx):
        mw = ManualEntryMiddleware("bot1", manual_entry_only=True)
        out = mw.process([_buy("strategy")], ctx)
        assert len(out) == 1
        assert isinstance(out[0], LogSignal)
        assert out[0].event_type == "MANUAL_ENTRY_ONLY"
        assert "F" in out[0].message

    def test_passes_exit_buy(self, ctx):
        # Exits can be BUYs too (covering a short). Must pass.
        mw = ManualEntryMiddleware("bot1", manual_entry_only=True)
        out = mw.process([_buy("exit")], ctx)
        assert len(out) == 1
        assert isinstance(out[0], PlaceOrder)
        assert out[0].origin == "exit"

    def test_passes_manual_override_buy(self, ctx):
        mw = ManualEntryMiddleware("bot1", manual_entry_only=True)
        out = mw.process([_buy("manual_override")], ctx)
        assert out == [_buy("manual_override")]

    def test_blocks_strategy_sell(self, ctx):
        # Strategy-emitted SELLs are SHORT entries — must be blocked.
        mw = ManualEntryMiddleware("bot1", manual_entry_only=True)
        out = mw.process([_sell("strategy")], ctx)
        assert len(out) == 1
        assert isinstance(out[0], LogSignal)
        assert out[0].event_type == "MANUAL_ENTRY_ONLY"
        assert "F" in out[0].message
        assert "SELL" in out[0].message

    def test_passes_exit_sell(self, ctx):
        # SELL exits (closing a long) must pass.
        mw = ManualEntryMiddleware("bot1", manual_entry_only=True)
        out = mw.process([_sell("exit")], ctx)
        assert out == [_sell("exit")]

    def test_passes_manual_override_sell(self, ctx):
        # Force SHORT goes out as SELL origin=manual_override — must pass.
        mw = ManualEntryMiddleware("bot1", manual_entry_only=True)
        out = mw.process([_sell("manual_override")], ctx)
        assert out == [_sell("manual_override")]

    def test_passes_non_placeorder(self, ctx):
        mw = ManualEntryMiddleware("bot1", manual_entry_only=True)
        actions = [
            LogSignal(event_type="SIGNAL", message="hi"),
            UpdateState(state={"last_price": "100.00"}),
        ]
        out = mw.process(actions, ctx)
        assert out == actions

    def test_mixed_batch(self, ctx):
        mw = ManualEntryMiddleware("bot1", manual_entry_only=True)
        actions = [
            _buy("strategy"),              # dropped (LONG entry)
            _sell("exit"),                 # kept (exit)
            _buy("manual_override"),       # kept (force LONG)
            _sell("strategy"),             # dropped (SHORT entry)
            LogSignal(event_type="SIGNAL", message="hi"),  # kept
            _sell("manual_override"),      # kept (force SHORT)
            _buy("strategy"),              # dropped (LONG entry)
        ]
        out = mw.process(actions, ctx)
        # Three LogSignals injected for the three dropped entries,
        # plus four pass-throughs (one SignalLog + three orders) = 7.
        assert len(out) == 7
        dropped = [
            a for a in out
            if isinstance(a, LogSignal) and a.event_type == "MANUAL_ENTRY_ONLY"
        ]
        assert len(dropped) == 3
        kept_orders = [a for a in out if isinstance(a, PlaceOrder)]
        assert len(kept_orders) == 3
        assert {a.origin for a in kept_orders} == {"exit", "manual_override"}


class TestActionOrigin:
    def test_placeorder_default_origin_is_strategy(self):
        # The default matters: strategies that haven't been updated for
        # provenance still produce origin="strategy" and thus get gated
        # when manual_entry_only is on — the safer default.
        po = PlaceOrder(symbol="F", side="BUY", qty=Decimal("1"), order_type="mid")
        assert po.origin == "strategy"
