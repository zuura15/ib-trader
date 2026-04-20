"""Integration tests for bid/ask fixed-price order strategy.

Tests the complete bid/ask order path: place at current bid or ask price,
wait briefly for fill, and handle fill / live-GTC outcomes.
No reprice loop runs for these orders.
Assertions use TransactionEvent rows instead of Order rows.
"""
import asyncio
from decimal import Decimal

from ib_trader.repl.commands import BuyCommand, SellCommand, Strategy
from ib_trader.data.models import TransactionAction, LegType, TradeStatus
from ib_trader.engine.order import execute_order


class TestBidOrderFlow:
    """Tests for 'bid' strategy — fixed limit at current bid, GTC."""

    async def test_buy_bid_places_limit_at_bid_price(self, ctx):
        """BUY bid strategy places a limit order at the current bid price."""
        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.10"),
            "last": Decimal("100.05"),
        }
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("2"),
            dollars=None,
            strategy=Strategy.BID,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
        )
        task = asyncio.create_task(execute_order(cmd, ctx))
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        placed = ctx.ib.placed_orders
        assert len(placed) >= 1
        entry = placed[0]
        assert entry["price"] == Decimal("100.00")  # bid price
        assert entry["side"] == "BUY"
        assert entry["tif"] == "GTC"

    async def test_sell_bid_places_limit_at_bid_price(self, ctx):
        """SELL bid strategy places a limit order at the current bid price."""
        ctx.ib._market_snapshot = {
            "bid": Decimal("200.00"),
            "ask": Decimal("200.20"),
            "last": Decimal("200.10"),
        }
        cmd = SellCommand(
            symbol="AAPL",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.BID,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
        )
        task = asyncio.create_task(execute_order(cmd, ctx))
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        placed = ctx.ib.placed_orders
        assert len(placed) >= 1
        assert placed[0]["price"] == Decimal("200.00")  # bid price

    async def test_bid_order_fills_immediately_updates_db(self, ctx):
        """Bid order that fills within the 30s window is recorded as FILLED."""
        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.10"),
            "last": Decimal("100.05"),
        }
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.BID,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
        )

        async def delayed_fill():
            await asyncio.sleep(0.05)
            placed = ctx.ib.placed_orders
            if placed:
                await ctx.ib.simulate_fill(
                    placed[0]["ib_order_id"],
                    Decimal("1"),
                    Decimal("100.00"),
                    Decimal("1.00"),
                )

        fill_task = asyncio.create_task(delayed_fill())
        await execute_order(cmd, ctx)
        await fill_task

        # Entry should be filled — check via transactions
        trades = ctx.trades.get_all()
        assert len(trades) >= 1
        trade_id = trades[0].id
        txns = ctx.transactions.get_for_trade(trade_id)
        filled = [t for t in txns if t.action == TransactionAction.FILLED
                  and t.leg_type == LegType.ENTRY]
        assert len(filled) == 1
        assert filled[0].ib_filled_qty == Decimal("1")
        assert filled[0].ib_avg_fill_price == Decimal("100.00")

    async def test_bid_order_rejected_marks_canceled(self, ctx):
        """Bid order that IB cancels/rejects is marked CANCELED in DB, not OPEN."""
        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.10"),
            "last": Decimal("100.05"),
        }
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.BID,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
        )

        async def delayed_cancel():
            await asyncio.sleep(0.05)
            placed = ctx.ib.placed_orders
            if placed:
                ib_order_id = placed[0]["ib_order_id"]
                ctx.tracker.notify_canceled(ib_order_id)

        cancel_task = asyncio.create_task(delayed_cancel())
        await execute_order(cmd, ctx)
        await cancel_task

        # Trade should be closed after cancellation
        trades = ctx.trades.get_all()
        assert len(trades) >= 1
        closed = [t for t in trades if t.status == TradeStatus.CLOSED]
        assert len(closed) >= 1

    async def test_bid_order_no_fill_leaves_gtc_live(self, ctx):
        """Bid order that does not fill in 30s leaves the DB in OPEN state."""
        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.10"),
            "last": Decimal("100.05"),
        }
        ctx.settings["reprice_duration_seconds"] = 0.05  # Not used for bid

        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.BID,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
        )

        task = asyncio.create_task(execute_order(cmd, ctx))
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        # Order is placed in IB but not filled — transactions should show non-terminal
        open_txns = ctx.transactions.get_open_orders()
        assert len(open_txns) >= 1


class TestPreSubmittedFlow:
    """Tests for orders IB holds as PreSubmitted (market session closed)."""

    async def test_bid_order_presubmitted_weekend_stays_open(self, ctx):
        """Bid order PreSubmitted during weekend closure stays OPEN (queued for reopening)."""
        from unittest.mock import patch
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.10"),
            "last": Decimal("100.05"),
        }
        ctx.settings["bid_ask_wait_seconds"] = 0.05
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.BID,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
        )

        original_place = ctx.ib.place_limit_order

        async def presubmit_place(*args, **kwargs):
            ib_id = await original_place(*args, **kwargs)
            ctx.ib._order_statuses[ib_id]["status"] = "PreSubmitted"
            return ib_id

        ctx.ib.place_limit_order = presubmit_place

        # Saturday noon ET = deep in weekend closure window.
        _saturday = datetime(2026, 3, 7, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("ib_trader.engine.market_hours._now_et", return_value=_saturday):
            await execute_order(cmd, ctx)

        open_txns = ctx.transactions.get_open_orders()
        assert len(open_txns) == 1
        assert open_txns[0].action == TransactionAction.PLACE_ACCEPTED

    async def test_bid_order_presubmitted_active_hours_abandoned(self, ctx):
        """Bid order PreSubmitted during active trading hours is cancelled and marked ABANDONED."""
        from unittest.mock import patch
        from datetime import datetime
        from zoneinfo import ZoneInfo

        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.10"),
            "last": Decimal("100.05"),
        }
        ctx.settings["bid_ask_wait_seconds"] = 0.05
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.BID,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
        )

        original_place = ctx.ib.place_limit_order

        async def presubmit_place(*args, **kwargs):
            ib_id = await original_place(*args, **kwargs)
            ctx.ib._order_statuses[ib_id]["status"] = "PreSubmitted"
            return ib_id

        ctx.ib.place_limit_order = presubmit_place

        # Monday 10 AM ET = active RTH session.
        _monday_rth = datetime(2026, 3, 9, 10, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("ib_trader.engine.market_hours._now_et", return_value=_monday_rth):
            await execute_order(cmd, ctx)

        # Trade should be closed and IB cancel should have been called
        trades = ctx.trades.get_all()
        assert len(trades) >= 1
        closed = [t for t in trades if t.status == TradeStatus.CLOSED]
        assert len(closed) >= 1
        assert len(ctx.ib.canceled_orders) >= 1


class TestAskOrderFlow:
    """Tests for 'ask' strategy — fixed limit at current ask, GTC."""

    async def test_buy_ask_places_limit_at_ask_price(self, ctx):
        """BUY ask strategy places a limit order at the current ask price."""
        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.10"),
            "last": Decimal("100.05"),
        }
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("3"),
            dollars=None,
            strategy=Strategy.ASK,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
        )
        task = asyncio.create_task(execute_order(cmd, ctx))
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        placed = ctx.ib.placed_orders
        assert len(placed) >= 1
        entry = placed[0]
        assert entry["price"] == Decimal("100.10")  # ask price
        assert entry["side"] == "BUY"

    async def test_sell_ask_places_limit_at_ask_price(self, ctx):
        """SELL ask strategy places a limit order at the current ask price."""
        ctx.ib._market_snapshot = {
            "bid": Decimal("50.00"),
            "ask": Decimal("50.05"),
            "last": Decimal("50.02"),
        }
        cmd = SellCommand(
            symbol="AAPL",
            qty=Decimal("2"),
            dollars=None,
            strategy=Strategy.ASK,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
        )
        task = asyncio.create_task(execute_order(cmd, ctx))
        try:
            await asyncio.wait_for(task, timeout=2.0)
        except asyncio.TimeoutError:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        placed = ctx.ib.placed_orders
        assert len(placed) >= 1
        assert placed[0]["price"] == Decimal("50.05")  # ask price

    async def test_ask_order_with_profit_taker_on_fill(self, ctx):
        """Ask order with profit_amount places a profit taker after fill."""
        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.10"),
            "last": Decimal("100.05"),
        }
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("2"),
            dollars=None,
            strategy=Strategy.ASK,
            profit_amount=Decimal("200"),  # $200 profit target
            take_profit_price=None,
            stop_loss=None,
        )

        async def delayed_fill():
            await asyncio.sleep(0.05)
            placed = ctx.ib.placed_orders
            if placed:
                await ctx.ib.simulate_fill(
                    placed[0]["ib_order_id"],
                    Decimal("2"),
                    Decimal("100.10"),
                    Decimal("1.00"),
                )

        fill_task = asyncio.create_task(delayed_fill())
        await execute_order(cmd, ctx)
        await fill_task

        # Should have placed a profit taker order (2nd IB order)
        assert len(ctx.ib.placed_orders) == 2
        pt = ctx.ib.placed_orders[1]
        assert pt["side"] == "SELL"
        # PT price = avg_fill + profit/qty = 100.10 + 200/2 = 200.10
        assert pt["price"] == Decimal("200.10")
