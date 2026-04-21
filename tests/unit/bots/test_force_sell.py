"""Tests for the bot force-sell path.

Force-sell must produce bit-identical orders to an organic strategy-driven
exit so operator overrides and policy-driven exits share every downstream
code path (order type, pipeline, FSM, audit). Only the ``ExitType`` carried
in the LogSignal payload should differ.
"""
from decimal import Decimal

import pytest

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategy import (
    ExitType, PlaceOrder, LogSignal, StrategyContext,
)
from ib_trader.bots.strategies.close_trend_rsi import CloseTrendRsiStrategy
from ib_trader.bots.strategies.sawtooth_rsi import SawtoothRsiStrategy


def _ctr_config() -> dict:
    return {
        "symbol": "USO",
        "bar_size_seconds": 180,
        "lookback_bars": 20,
        "max_position_value": "10000",
        "max_shares": 20,
        "order_strategy": "mid",
        "entry": {},
        "exit": {
            "hard_stop_loss_pct": 0.003,
            "trail_activation_pct": 0.00005,
            "trail_width_pct": 0.0005,
            "time_stop_minutes": 60,
        },
    }


def _sawtooth_config() -> dict:
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
    }


def _ctx_holding(qty: str = "10", entry: str = "100.00") -> StrategyContext:
    return StrategyContext(
        state={"qty": qty, "entry_price": entry},
        fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        bot_id="test-bot",
        config={},
    )


class TestBuildExitActions:
    """The strategy's public exit builder is the single source of truth for
    exit actions — both organic and force exits go through it."""

    @pytest.mark.parametrize("strategy_cls,config_fn", [
        (CloseTrendRsiStrategy, _ctr_config),
        (SawtoothRsiStrategy, _sawtooth_config),
    ])
    def test_force_vs_organic_differ_only_in_exit_type(self, strategy_cls, config_fn):
        strategy = strategy_cls(config_fn())
        ctx = _ctx_holding()

        organic = strategy.build_exit_actions(ctx, ExitType.TRAILING_STOP, "trail hit")
        forced = strategy.build_exit_actions(ctx, ExitType.FORCE_EXIT, "manual override")

        # Both flows emit exactly one LogSignal + one PlaceOrder.
        assert [type(a).__name__ for a in organic] == ["LogSignal", "PlaceOrder"]
        assert [type(a).__name__ for a in forced] == ["LogSignal", "PlaceOrder"]

        # The PlaceOrder is bit-identical — same side, qty, order_type, origin.
        org_place = next(a for a in organic if isinstance(a, PlaceOrder))
        fce_place = next(a for a in forced if isinstance(a, PlaceOrder))
        assert org_place.side == fce_place.side == "SELL"
        assert org_place.qty == fce_place.qty == Decimal("10")
        assert org_place.order_type == fce_place.order_type == "smart_market"
        assert org_place.origin == fce_place.origin == "exit"
        assert org_place.symbol == fce_place.symbol

        # The LogSignal payload carries the differing ExitType.
        org_log = next(a for a in organic if isinstance(a, LogSignal))
        fce_log = next(a for a in forced if isinstance(a, LogSignal))
        assert org_log.payload["exit_type"] == ExitType.TRAILING_STOP.value
        assert fce_log.payload["exit_type"] == ExitType.FORCE_EXIT.value


class TestExecuteForceSell:
    """The runtime's _execute_force_sell delegates action construction to the
    strategy and refuses to fire when the bot has no position."""

    @pytest.mark.asyncio
    async def test_delegates_to_strategy_and_pipelines_result(self):
        from ib_trader.bots.runtime import StrategyBotRunner
        sentinel_actions: list = [object()]
        captured: dict = {}

        class _FakeStrategy:
            def build_exit_actions(self, ctx, exit_type, detail):
                captured["ctx"] = ctx
                captured["exit_type"] = exit_type
                captured["detail"] = detail
                return sentinel_actions

        bot = StrategyBotRunner.__new__(StrategyBotRunner)
        bot.strategy = _FakeStrategy()
        bot.ctx = _ctx_holding()
        bot.strategy_config = {"symbol": "USO"}

        pipelined: list = []
        async def _capture_pipeline(actions, ctx=None):
            pipelined.append(actions)
        bot._run_pipeline = _capture_pipeline

        await bot._execute_force_sell("USO")

        assert captured["exit_type"] == ExitType.FORCE_EXIT
        assert captured["detail"] == "manual override"
        assert captured["ctx"] is bot.ctx
        assert pipelined == [sentinel_actions]

    @pytest.mark.asyncio
    async def test_rejects_flat_position(self):
        from ib_trader.bots.runtime import StrategyBotRunner

        bot = StrategyBotRunner.__new__(StrategyBotRunner)
        bot.strategy = object()  # never called
        bot.ctx = StrategyContext(
            state={"qty": "0"},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
            bot_id="test-bot",
            config={},
        )
        bot.strategy_config = {"symbol": "USO"}

        async def _unexpected(*a, **kw):
            pytest.fail("_run_pipeline must not be called when qty is zero")
        bot._run_pipeline = _unexpected

        with pytest.raises(RuntimeError, match="no open position"):
            await bot._execute_force_sell("USO")

    @pytest.mark.asyncio
    async def test_rejects_invalid_qty(self):
        from ib_trader.bots.runtime import StrategyBotRunner

        bot = StrategyBotRunner.__new__(StrategyBotRunner)
        bot.strategy = object()
        bot.ctx = StrategyContext(
            state={"qty": "not-a-number"},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
            bot_id="test-bot",
            config={},
        )
        bot.strategy_config = {"symbol": "USO"}
        async def _unexpected(*a, **kw):
            pytest.fail("_run_pipeline must not be called on invalid qty")
        bot._run_pipeline = _unexpected

        with pytest.raises(RuntimeError, match="invalid position qty"):
            await bot._execute_force_sell("USO")
