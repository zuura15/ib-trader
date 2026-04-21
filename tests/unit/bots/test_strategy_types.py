"""Tests for strategy protocol types."""

from decimal import Decimal

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategy import (
    StrategyManifest, Subscription, StrategyContext,
    BarCompleted, QuoteUpdate, OrderFilled, PlaceOrder, LogSignal,
    UpdateState,
)


class TestStrategyManifest:
    def test_creation(self):
        m = StrategyManifest(
            name="test",
            subscriptions=[Subscription("bars", ["AAPL"], {"bar_seconds": 180})],
            capabilities=["execution"],
            state_schema={"price": "decimal"},
            version="1.0",
        )
        assert m.name == "test"
        assert len(m.subscriptions) == 1
        assert m.subscriptions[0].type == "bars"
        assert m.subscriptions[0].symbols == ["AAPL"]


class TestEvents:
    def test_bar_completed(self):
        event = BarCompleted(
            symbol="META",
            bar={"close": 500.0},
            window=[{"close": 499.0}, {"close": 500.0}],
            bar_count=10,
        )
        assert event.symbol == "META"
        assert len(event.window) == 2

    def test_quote_update(self):
        from datetime import datetime, timezone
        event = QuoteUpdate(
            symbol="META",
            bid=Decimal("499.50"),
            ask=Decimal("500.50"),
            last=Decimal("500.00"),
            timestamp=datetime.now(timezone.utc),
        )
        assert event.bid == Decimal("499.50")

    def test_order_filled(self):
        event = OrderFilled(
            trade_serial=47,
            symbol="META",
            side="BUY",
            fill_price=Decimal("500.00"),
            qty=Decimal("10"),
            commission=Decimal("1.00"),
            ib_order_id="123",
        )
        assert event.fill_price == Decimal("500.00")


class TestActions:
    def test_place_order(self):
        action = PlaceOrder(
            symbol="META", side="BUY", qty=Decimal("10"),
            order_type="mid",
        )
        assert action.price is None

    def test_log_signal(self):
        action = LogSignal(
            event_type="SKIP",
            message="RSI too high",
            payload={"rsi": 65.0},
        )
        assert action.event_type == "SKIP"

    def test_update_state(self):
        action = UpdateState(state={"entry_price": "500.00"})
        assert action.state["entry_price"] == "500.00"


class TestStrategyContext:
    def test_creation(self):
        ctx = StrategyContext(
            state={},
            fsm_state=BotState.AWAITING_ENTRY_TRIGGER,
            bot_id="test-bot",
            config={"symbol": "META"},
        )
        assert ctx.bot_id == "test-bot"
        assert ctx.fsm_state == BotState.AWAITING_ENTRY_TRIGGER

    def test_all_fsm_states_accepted(self):
        for state in BotState:
            ctx = StrategyContext(
                state={}, fsm_state=state, bot_id="b", config={},
            )
            assert ctx.fsm_state == state
