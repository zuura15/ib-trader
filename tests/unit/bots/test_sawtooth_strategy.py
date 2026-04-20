"""Tests for the Sawtooth RSI Reversal strategy."""

from datetime import datetime, timezone, timedelta
from decimal import Decimal

import numpy as np
import pytest

from ib_trader.bots.fsm import BotState
from ib_trader.bots.strategy import (
    StrategyContext,
    BarCompleted, QuoteUpdate, OrderFilled, OrderRejected,
    PlaceOrder, UpdateState, LogSignal,
)
from ib_trader.bots.strategies.sawtooth_rsi import SawtoothRsiStrategy


def _default_config() -> dict:
    return {
        "symbol": "META",
        "bar_size_seconds": 180,
        "lookback_bars": 100,
        "max_position_value": "10000",
        "max_shares": 20,
        "order_strategy": "mid",
        "entry": {
            "sawtooth_trend_swings": 3,
            "max_bars_since_swing_low": 5,
            "max_rsi": 60,
            "swing_proximity_pct": 0.001,
        },
        "exit": {
            "hard_stop_loss_pct": 0.001,
            "trail_activation_pct": 0.0005,
            "trail_width_pct": 0.0015,
            "time_stop_minutes": 108,
            "entry_timeout_seconds": 30,
            "exit_price": "bid",
        },
        "session_filter": {
            "skip_close_transition": True,
            "skip_turn_minutes": 5,
        },
        "risk": {
            "max_daily_loss_pct": 0.02,
            "max_concurrent_positions": 1,
            "max_trades_per_day": 10,
            "account_value": "10000",
        },
    }


def _make_ctx(
    state: dict | None = None,
    fsm_state: BotState = BotState.AWAITING_ENTRY_TRIGGER,
) -> StrategyContext:
    return StrategyContext(
        state=state or {},
        fsm_state=fsm_state,
        bot_id="test-bot",
        config=_default_config(),
    )


def _make_bars_window(n: int = 100, uptrend: bool = True) -> list[dict]:
    """Generate a window of bars for testing. If uptrend, creates ascending pattern."""
    bars = []
    base_price = 500.0
    for i in range(n):
        if uptrend:
            # Create a sawtooth: generally rising with pullbacks
            trend = i * 0.1
            cycle = 2.0 * np.sin(i * 0.3)  # oscillation
            price = base_price + trend + cycle
        else:
            price = base_price - i * 0.05

        bars.append({
            "timestamp_utc": (datetime(2026, 4, 9, 10, 0, 0, tzinfo=timezone.utc)
                              + timedelta(minutes=i * 3)).isoformat(),
            "open": price - 0.2,
            "high": price + 0.5,
            "low": price - 0.5,
            "close": price,
            "volume": 1000,
        })
    return bars


class TestSawtoothRsiStrategy:
    def test_creation(self):
        strategy = SawtoothRsiStrategy(_default_config())
        assert strategy.manifest.name == "sawtooth_rsi_reversal"
        assert len(strategy.manifest.subscriptions) == 1
        assert strategy.manifest.subscriptions[0].type == "bars"

    @pytest.mark.asyncio
    async def test_on_start_initializes_state(self):
        strategy = SawtoothRsiStrategy(_default_config())
        ctx = _make_ctx(state={})
        actions = await strategy.on_start(ctx)
        assert any(isinstance(a, LogSignal) and a.event_type == "STATE" for a in actions)

    @pytest.mark.asyncio
    async def test_bar_while_awaiting_entry_produces_log(self):
        strategy = SawtoothRsiStrategy(_default_config())
        ctx = _make_ctx()
        window = _make_bars_window(100, uptrend=False)

        event = BarCompleted(
            symbol="META", bar=window[-1], window=window, bar_count=100,
        )
        actions = await strategy.on_event(event, ctx)
        # Should have at least a BAR log
        bar_logs = [a for a in actions if isinstance(a, LogSignal) and a.event_type == "BAR"]
        assert len(bar_logs) == 1

    @pytest.mark.asyncio
    async def test_quote_ignored_while_awaiting_entry(self):
        strategy = SawtoothRsiStrategy(_default_config())
        ctx = _make_ctx()
        event = QuoteUpdate(
            symbol="META", bid=Decimal("500"), ask=Decimal("501"),
            last=Decimal("500.50"), timestamp=datetime.now(timezone.utc),
        )
        actions = await strategy.on_event(event, ctx)
        assert actions == []

    @pytest.mark.asyncio
    async def test_hard_stop_loss_triggers_while_awaiting_exit(self):
        strategy = SawtoothRsiStrategy(_default_config())
        ctx = _make_ctx(
            state={
                "entry_price": "500.00",
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "high_water_mark": "500.00",
                "current_stop": "499.50",
                "trail_activated": False,
                "trade_serial": 47,
                "qty": "10",
            },
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )

        # Price below hard SL (-0.1% = 499.50)
        event = QuoteUpdate(
            symbol="META", bid=Decimal("499.40"), ask=Decimal("499.90"),
            last=Decimal("499.50"), timestamp=datetime.now(timezone.utc),
        )
        actions = await strategy.on_event(event, ctx)

        exit_logs = [a for a in actions if isinstance(a, LogSignal)
                     and a.event_type == "EXIT_CHECK"]
        orders = [a for a in actions if isinstance(a, PlaceOrder)]

        assert len(exit_logs) >= 1
        assert "HARD_STOP_LOSS" in exit_logs[0].message
        assert len(orders) == 1
        assert orders[0].side == "SELL"
        assert orders[0].origin == "exit"

    @pytest.mark.asyncio
    async def test_trail_activation(self):
        strategy = SawtoothRsiStrategy(_default_config())
        ctx = _make_ctx(
            state={
                "entry_price": "500.00",
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "high_water_mark": "500.00",
                "current_stop": "499.50",
                "trail_activated": False,
                "trade_serial": 47,
                "qty": "10",
            },
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )

        # Price at +0.05% = 500.25 — should activate trail
        event = QuoteUpdate(
            symbol="META", bid=Decimal("500.25"), ask=Decimal("500.75"),
            last=Decimal("500.50"), timestamp=datetime.now(timezone.utc),
        )
        actions = await strategy.on_event(event, ctx)

        trail_logs = [a for a in actions if isinstance(a, LogSignal)
                      and "TRAIL ACTIVATED" in (a.message or "")]
        state_updates = [a for a in actions if isinstance(a, UpdateState)]

        assert len(trail_logs) == 1
        assert any(u.state.get("trail_activated") is True for u in state_updates)

    @pytest.mark.asyncio
    async def test_fill_records_trade_state(self):
        strategy = SawtoothRsiStrategy(_default_config())
        ctx = _make_ctx(
            state={"entry_time": datetime.now(timezone.utc).isoformat()},
            fsm_state=BotState.ENTRY_ORDER_PLACED,
        )

        event = OrderFilled(
            trade_serial=47, symbol="META", side="BUY",
            fill_price=Decimal("500.00"), qty=Decimal("10"),
            commission=Decimal("1.00"), ib_order_id="123",
        )
        actions = await strategy.on_event(event, ctx)

        state_updates = [a for a in actions if isinstance(a, UpdateState)]
        # Strategy writes trade-scoped state (entry_price, qty, etc.) —
        # the FSM transition to AWAITING_EXIT_TRIGGER is the runtime's
        # job, not the strategy's.
        assert any(u.state.get("entry_price") == "500.00" for u in state_updates)
        assert any(u.state.get("qty") == "10" for u in state_updates)
        # position_state is a deleted concept — should never be written.
        assert not any("position_state" in u.state for u in state_updates)

    @pytest.mark.asyncio
    async def test_rejection_clears_trade_scoped_fields(self):
        strategy = SawtoothRsiStrategy(_default_config())
        ctx = _make_ctx(fsm_state=BotState.ENTRY_ORDER_PLACED)

        event = OrderRejected(
            trade_serial=None, symbol="META",
            reason="Insufficient funds", command_id="cmd-1",
        )
        actions = await strategy.on_event(event, ctx)

        state_updates = [a for a in actions if isinstance(a, UpdateState)]
        # Strategy clears trade-scoped fields; FSM handles the lifecycle
        # transition back to AWAITING_ENTRY_TRIGGER via EntryCancelled.
        assert any(u.state.get("trade_serial") is None for u in state_updates)
        assert not any("position_state" in u.state for u in state_updates)

    @pytest.mark.asyncio
    async def test_recovers_from_rejected_exit_and_re_triggers_on_next_tick(self):
        """Regression for bug #1: a rejected SELL must not strand the bot.

        Scenario:
          1. Bot hit a stop earlier, fired a SELL, FSM → EXIT_ORDER_PLACED.
          2. IB rejected the SELL (e.g. market order during ETH).
          3. Runtime dispatches EXIT_CANCELLED to FSM → AWAITING_EXIT_TRIGGER.
          4. Next quote tick with a stop-triggering price must fire another
             SELL PlaceOrder — the bot is not trapped in a dead EXITING state.
        """
        strategy = SawtoothRsiStrategy(_default_config())

        # Step 1: while the SELL was in flight, fsm_state is EXIT_ORDER_PLACED.
        # _on_quote should be a no-op in that state.
        ctx_pre = _make_ctx(
            state={
                "entry_price": "500.00",
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "high_water_mark": "500.00",
                "current_stop": "499.50",
                "trail_activated": False,
                "trade_serial": 47,
                "qty": "10",
            },
            fsm_state=BotState.EXIT_ORDER_PLACED,
        )
        bad_quote = QuoteUpdate(
            symbol="META", bid=Decimal("499.00"), ask=Decimal("499.50"),
            last=Decimal("499.25"), timestamp=datetime.now(timezone.utc),
        )
        pre = await strategy.on_event(bad_quote, ctx_pre)
        assert pre == [], "quote tick while EXIT_ORDER_PLACED must be a no-op"

        # Step 3+4: after EXIT_CANCELLED recovery, fsm_state is
        # AWAITING_EXIT_TRIGGER again. Same quote now retriggers the stop.
        ctx_post = _make_ctx(
            state={
                "entry_price": "500.00",
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "high_water_mark": "500.00",
                "current_stop": "499.50",
                "trail_activated": False,
                "trade_serial": 47,
                "qty": "10",
            },
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )
        post = await strategy.on_event(bad_quote, ctx_post)
        orders = [a for a in post if isinstance(a, PlaceOrder)]
        assert len(orders) == 1
        assert orders[0].side == "SELL"
        assert orders[0].origin == "exit"

    @pytest.mark.asyncio
    async def test_time_stop(self):
        strategy = SawtoothRsiStrategy(_default_config())
        # Entry 2 hours ago — past the 108-min time stop
        entry_time = datetime.now(timezone.utc) - timedelta(hours=2)
        ctx = _make_ctx(
            state={
                "entry_price": "500.00",
                "entry_time": entry_time.isoformat(),
                "high_water_mark": "500.50",
                "current_stop": "499.75",
                "trail_activated": True,
                "trade_serial": 47,
                "qty": "10",
            },
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )

        event = QuoteUpdate(
            symbol="META", bid=Decimal("500.30"), ask=Decimal("500.80"),
            last=Decimal("500.50"), timestamp=datetime.now(timezone.utc),
        )
        actions = await strategy.on_event(event, ctx)

        exit_logs = [a for a in actions if isinstance(a, LogSignal)
                     and a.event_type == "EXIT_CHECK"]
        assert any("TIME_STOP" in log.message for log in exit_logs)
