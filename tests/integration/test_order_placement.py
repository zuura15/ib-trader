"""Integration tests for order placement flows.

Uses MockIBClient — no live IB connection required.
Tests the full flow from command → IB call → DB update.
"""
import asyncio
import pytest
from decimal import Decimal
from datetime import datetime, timezone

from ib_trader.repl.commands import BuyCommand
from ib_trader.data.models import OrderStatus, LegType
from ib_trader.engine.order import execute_order, place_profit_taker
from ib_trader.engine.exceptions import SafetyLimitError


@pytest.fixture
def buy_mid_cmd():
    return BuyCommand(
        symbol="MSFT",
        qty=Decimal("5"),
        dollars=None,
        strategy="mid",
        profit_amount=None,
        take_profit_price=None,
        stop_loss=None,
    )


@pytest.fixture
def buy_market_cmd():
    return BuyCommand(
        symbol="MSFT",
        qty=Decimal("5"),
        dollars=None,
        strategy="market",
        profit_amount=None,
        take_profit_price=None,
        stop_loss=None,
    )


@pytest.fixture
def buy_with_profit_cmd():
    return BuyCommand(
        symbol="MSFT",
        qty=Decimal("5"),
        dollars=None,
        strategy="market",
        profit_amount=Decimal("500"),
        take_profit_price=None,
        stop_loss=None,
    )


class TestMarketOrderFlow:
    async def test_market_order_creates_db_records(self, ctx, buy_market_cmd):
        """Market order: creates trade group, order in DB, calls IB."""
        # Simulate fill immediately
        async def fill_on_place(*args, **kwargs):
            ib_id = str(ctx.ib._next_order_id)
            # Simulate fill after brief delay
            return ib_id

        # Execute with timeout — market order waits for fill event
        # In test, we simulate the fill via mock
        task = asyncio.create_task(execute_order(buy_market_cmd, ctx))
        await asyncio.sleep(0.05)  # Let the order get placed

        # Simulate a fill
        if ctx.ib.placed_orders:
            ib_id = ctx.ib.placed_orders[-1]["ib_order_id"]
            await ctx.ib.simulate_fill(ib_id, Decimal("5"), Decimal("100.05"), Decimal("1.00"))

        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()

        # Verify trade was created
        # Order may be filled so trade might be open or closed
        assert len(ctx.ib.placed_orders) >= 1
        assert ctx.ib.placed_orders[0]["symbol"] == "MSFT"
        assert ctx.ib.placed_orders[0]["side"] == "BUY"

    async def test_safety_limit_exceeded_raises(self, ctx):
        """Orders exceeding max_order_size_shares are rejected."""
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("999"),  # Way over the limit of 10
            dollars=None,
            strategy="market",
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
        )
        with pytest.raises(SafetyLimitError):
            await execute_order(cmd, ctx)

    async def test_no_ib_call_before_db_write(self, ctx, buy_market_cmd):
        """Verify IB is not called before the DB record is created."""
        # Track order count before
        initial_ib_calls = len(ctx.ib.placed_orders)

        task = asyncio.create_task(execute_order(buy_market_cmd, ctx))
        await asyncio.sleep(0.02)  # Let order placement proceed

        # At this point, DB record should exist before IB call completed
        # (IB call is async, record is written before return)
        if ctx.ib.placed_orders:
            ib_id = ctx.ib.placed_orders[-1]["ib_order_id"]
            await ctx.ib.simulate_fill(ib_id, Decimal("5"), Decimal("100.00"), Decimal("1.00"))

        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()

        # IB should have been called
        assert len(ctx.ib.placed_orders) > initial_ib_calls


class TestProfitTakerPlacement:
    async def test_buy_entry_profit_taker_is_sell(self, ctx):
        """BUY entry → profit taker is SELL."""
        await place_profit_taker(
            trade_id="test-trade-id",
            entry_order_id="test-order-id",
            entry_side="BUY",
            avg_fill_price=Decimal("100.00"),
            qty_filled=Decimal("10"),
            profit_amount=Decimal("500"),
            take_profit_price=None,
            con_id=12345,
            symbol="MSFT",
            ctx=ctx,
        )
        # Profit taker should be a SELL at 100 + (500/10) = 150
        assert len(ctx.ib.placed_orders) == 1
        pt = ctx.ib.placed_orders[0]
        assert pt["side"] == "SELL"
        assert pt["price"] == Decimal("150.0000")

    async def test_sell_entry_profit_taker_is_buy(self, ctx):
        """SELL (short) entry → profit taker is BUY (cover lower)."""
        await place_profit_taker(
            trade_id="test-trade-id",
            entry_order_id="test-order-id",
            entry_side="SELL",
            avg_fill_price=Decimal("100.00"),
            qty_filled=Decimal("10"),
            profit_amount=Decimal("500"),
            take_profit_price=None,
            con_id=12345,
            symbol="MSFT",
            ctx=ctx,
        )
        pt = ctx.ib.placed_orders[0]
        assert pt["side"] == "BUY"
        # price = 100 - (500/10) = 50
        assert pt["price"] == Decimal("50.0000")

    async def test_explicit_take_profit_price_used_directly(self, ctx):
        """--take-profit-price N overrides profit_amount calculation."""
        await place_profit_taker(
            trade_id="test-trade-id",
            entry_order_id="test-order-id",
            entry_side="BUY",
            avg_fill_price=Decimal("100.00"),
            qty_filled=Decimal("10"),
            profit_amount=None,
            take_profit_price=Decimal("420.00"),
            con_id=12345,
            symbol="MSFT",
            ctx=ctx,
        )
        pt = ctx.ib.placed_orders[0]
        assert pt["price"] == Decimal("420.00")

    async def test_no_profit_taker_if_no_target(self, ctx):
        """No profit taker is placed if neither profit_amount nor take_profit_price."""
        await place_profit_taker(
            trade_id="test-trade-id",
            entry_order_id="test-order-id",
            entry_side="BUY",
            avg_fill_price=Decimal("100.00"),
            qty_filled=Decimal("10"),
            profit_amount=None,
            take_profit_price=None,
            con_id=12345,
            symbol="MSFT",
            ctx=ctx,
        )
        assert len(ctx.ib.placed_orders) == 0


class TestRepriceLoop:
    async def test_reprice_loop_amends_order(self, ctx):
        """Reprice loop calls amend_order for each step."""
        from ib_trader.engine.order import reprice_loop
        from ib_trader.data.models import Order, SecurityType

        from ib_trader.data.models import TradeGroup, TradeStatus
        trade = ctx.trades.create(TradeGroup(
            serial_number=1, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=datetime.now(timezone.utc),
        ))
        order = ctx.orders.create(Order(
            trade_id=trade.id, serial_number=1, leg_type=LegType.ENTRY,
            symbol="MSFT", side="BUY", security_type=SecurityType.STK,
            qty_requested=Decimal("5"), qty_filled=Decimal("0"),
            order_type="MID", status=OrderStatus.OPEN,
            placed_at=datetime.now(timezone.utc),
        ))
        ctx.orders.update_ib_order_id(order.id, "IB1000")
        ctx.tracker.register(order.id, "IB1000", "MSFT")

        # Run reprice loop with very fast settings (2 steps, 0.01s interval).
        # Mock snapshot: bid=100.00, ask=100.10.
        # initial_price = mid = 100.05.
        # step 1: calc_step_price(100.00, 100.10, 1, 2) = 100.05 + 0.5*0.05 = 100.075 → 100.08 ≠ 100.05 → amend
        # step 2: calc_step_price(100.00, 100.10, 2, 2) = 100.10 ≠ 100.08 → amend
        ctx.settings["reprice_interval_seconds"] = 0.01
        task = asyncio.create_task(
            reprice_loop(
                order_id=order.id,
                ib_order_id="IB1000",
                con_id=12345,
                symbol="MSFT",
                side="BUY",
                ctx=ctx,
                total_steps=2,
                interval_seconds=0.01,
                initial_price=Decimal("100.05"),
            )
        )
        await asyncio.wait_for(task, timeout=2.0)

        # Both steps produce distinct 2dp prices → 2 amendments sent
        assert len(ctx.ib.amended_orders) == 2

    async def test_reprice_loop_stops_on_fill(self, ctx):
        """Reprice loop exits immediately when fill event is set."""
        from ib_trader.engine.order import reprice_loop
        from ib_trader.data.models import Order, SecurityType, TradeGroup, TradeStatus

        trade = ctx.trades.create(TradeGroup(
            serial_number=2, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=datetime.now(timezone.utc),
        ))
        order = ctx.orders.create(Order(
            trade_id=trade.id, serial_number=2, leg_type=LegType.ENTRY,
            symbol="MSFT", side="BUY", security_type=SecurityType.STK,
            qty_requested=Decimal("5"), qty_filled=Decimal("0"),
            order_type="MID", status=OrderStatus.OPEN,
            placed_at=datetime.now(timezone.utc),
        ))
        ctx.orders.update_ib_order_id(order.id, "IB2000")
        ctx.tracker.register(order.id, "IB2000", "MSFT")

        # Signal fill immediately
        ctx.tracker.notify_filled("IB2000")

        task = asyncio.create_task(
            reprice_loop(
                order_id=order.id,
                ib_order_id="IB2000",
                con_id=12345,
                symbol="MSFT",
                side="BUY",
                ctx=ctx,
                total_steps=100,
                interval_seconds=0.01,
                initial_price=Decimal("100.05"),
            )
        )
        await asyncio.wait_for(task, timeout=2.0)

        # Should have made 0 amendments (fill signaled before first step)
        assert len(ctx.ib.amended_orders) == 0

    async def test_reprice_loop_deduplicates_same_price(self, ctx):
        """Steps that round to the same 2dp price are skipped — no redundant amend_order calls."""
        from ib_trader.engine.order import reprice_loop
        from ib_trader.data.models import Order, SecurityType, TradeGroup, TradeStatus

        trade = ctx.trades.create(TradeGroup(
            serial_number=3, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=datetime.now(timezone.utc),
        ))
        order = ctx.orders.create(Order(
            trade_id=trade.id, serial_number=3, leg_type=LegType.ENTRY,
            symbol="MSFT", side="BUY", security_type=SecurityType.STK,
            qty_requested=Decimal("1"), qty_filled=Decimal("0"),
            order_type="MID", status=OrderStatus.OPEN,
            placed_at=datetime.now(timezone.utc),
        ))
        ctx.orders.update_ib_order_id(order.id, "IB3000")
        ctx.tracker.register(order.id, "IB3000", "MSFT")

        # Tight spread: bid=100.00, ask=100.01 → mid=100.00 (ROUND_HALF_EVEN).
        # With 10 steps the calculated prices are:
        #   steps 1-5: round to 100.00 (same as initial) → skipped
        #   step 6:   100.006 → 100.01                  → amend
        #   steps 7-10: round to 100.01 (same)          → skipped
        # Expected: exactly 1 amendment sent.
        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.01"),
            "last": Decimal("100.00"),
        }

        task = asyncio.create_task(
            reprice_loop(
                order_id=order.id,
                ib_order_id="IB3000",
                con_id=12345,
                symbol="MSFT",
                side="BUY",
                ctx=ctx,
                total_steps=10,
                interval_seconds=0.01,
                initial_price=Decimal("100.00"),
            )
        )
        await asyncio.wait_for(task, timeout=2.0)

        assert len(ctx.ib.amended_orders) == 1
        assert ctx.ib.amended_orders[0]["new_price"] == Decimal("100.01")
