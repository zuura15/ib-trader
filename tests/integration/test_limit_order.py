"""Integration tests for fire-and-forget limit order strategy.

Tests the complete limit order path: place at user-specified price,
confirm IB acceptance, return immediately. No reprice loop, no timeout.
"""
import asyncio
import pytest
from decimal import Decimal

from ib_trader.repl.commands import BuyCommand, SellCommand, Strategy
from ib_trader.data.models import OrderStatus, TradeStatus, LegType
from ib_trader.engine.order import execute_order


class TestLimitOrderPlacement:
    """Tests for 'limit' strategy — fire-and-forget at user price."""

    async def test_buy_limit_places_at_specified_price(self, ctx):
        """BUY limit places a limit order at the user-specified price."""
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.LIMIT,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
            limit_price=Decimal("400.00"),
        )
        await execute_order(cmd, ctx)

        placed = ctx.ib.placed_orders
        assert len(placed) == 1
        assert placed[0]["price"] == Decimal("400.00")
        assert placed[0]["side"] == "BUY"
        assert placed[0]["symbol"] == "MSFT"
        assert placed[0]["tif"] == "GTC"

    async def test_sell_limit_places_at_specified_price(self, ctx):
        """SELL limit places a limit order at the user-specified price."""
        cmd = SellCommand(
            symbol="AAPL",
            qty=Decimal("2"),
            dollars=None,
            strategy=Strategy.LIMIT,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
            limit_price=Decimal("250.00"),
        )
        await execute_order(cmd, ctx)

        placed = ctx.ib.placed_orders
        assert len(placed) == 1
        assert placed[0]["price"] == Decimal("250.00")
        assert placed[0]["side"] == "SELL"

    async def test_limit_order_status_is_open(self, ctx):
        """Limit order leaves the order in OPEN status (not REPRICING)."""
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.LIMIT,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
            limit_price=Decimal("400.00"),
        )
        await execute_order(cmd, ctx)

        orders = ctx.orders.get_all_open()
        assert len(orders) == 1
        assert orders[0].status == OrderStatus.OPEN
        assert orders[0].price_placed == Decimal("400.00")

    async def test_limit_order_records_trade_group(self, ctx):
        """Limit order creates a trade group with OPEN status."""
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.LIMIT,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
            limit_price=Decimal("400.00"),
        )
        await execute_order(cmd, ctx)

        trades = ctx.trades.get_open()
        assert len(trades) == 1
        assert trades[0].symbol == "MSFT"
        assert trades[0].direction == "LONG"
        assert trades[0].status == TradeStatus.OPEN

    async def test_limit_order_immediate_fill(self, ctx):
        """If the limit price crosses the spread, handle immediate fill."""
        # Set up mock to report fill immediately
        original_place = ctx.ib.place_limit_order

        async def place_and_fill(con_id, symbol, side, qty, price, outside_rth=True, tif="GTC"):
            ib_id = await original_place(con_id, symbol, side, qty, price, outside_rth=outside_rth, tif=tif)
            # Simulate immediate fill
            ctx.ib._order_statuses[ib_id] = {
                "status": "Filled",
                "qty_filled": qty,
                "avg_fill_price": price,
                "commission": Decimal("1.00"),
            }
            return ib_id

        ctx.ib.place_limit_order = place_and_fill

        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.LIMIT,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
            limit_price=Decimal("400.00"),
        )
        await execute_order(cmd, ctx)

        # Should be filled, not OPEN
        orders = ctx.orders.get_all_open()
        assert len(orders) == 0  # no longer open — it's filled

    async def test_limit_order_rejected_raises(self, ctx):
        """If IB rejects the limit order, raise IBOrderRejectedError."""
        from ib_trader.engine.exceptions import IBOrderRejectedError

        # Make orders rejected immediately
        original_place = ctx.ib.place_limit_order

        async def place_rejected(con_id, symbol, side, qty, price, outside_rth=True, tif="GTC"):
            ib_id = await original_place(con_id, symbol, side, qty, price, outside_rth=outside_rth, tif=tif)
            ctx.ib._order_statuses[ib_id]["status"] = "Cancelled"
            return ib_id

        ctx.ib.place_limit_order = place_rejected

        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.LIMIT,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
            limit_price=Decimal("400.00"),
        )
        with pytest.raises(IBOrderRejectedError):
            await execute_order(cmd, ctx)

    async def test_limit_order_does_not_cancel_on_timeout(self, ctx):
        """Limit orders should NOT be cancelled after any timeout — they persist."""
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.LIMIT,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
            limit_price=Decimal("400.00"),
        )
        await execute_order(cmd, ctx)

        # No cancellation should have been sent
        assert len(ctx.ib.canceled_orders) == 0

    async def test_limit_order_with_profit_taker(self, ctx):
        """Limit order with immediate fill should place profit taker."""
        original_place = ctx.ib.place_limit_order

        async def place_and_fill(con_id, symbol, side, qty, price, outside_rth=True, tif="GTC"):
            ib_id = await original_place(con_id, symbol, side, qty, price, outside_rth=outside_rth, tif=tif)
            ctx.ib._order_statuses[ib_id] = {
                "status": "Filled",
                "qty_filled": qty,
                "avg_fill_price": price,
                "commission": Decimal("1.00"),
            }
            return ib_id

        ctx.ib.place_limit_order = place_and_fill

        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.LIMIT,
            profit_amount=Decimal("500"),
            take_profit_price=None,
            stop_loss=None,
            limit_price=Decimal("400.00"),
        )
        await execute_order(cmd, ctx)

        # Should have placed 2 orders: entry + profit taker
        assert len(ctx.ib.placed_orders) == 2

    async def test_limit_order_records_ib_order_id(self, ctx):
        """Limit order records the IB order ID in the database."""
        cmd = BuyCommand(
            symbol="MSFT",
            qty=Decimal("1"),
            dollars=None,
            strategy=Strategy.LIMIT,
            profit_amount=None,
            take_profit_price=None,
            stop_loss=None,
            limit_price=Decimal("400.00"),
        )
        await execute_order(cmd, ctx)

        orders = ctx.orders.get_all_open()
        assert len(orders) == 1
        assert orders[0].ib_order_id is not None
        assert orders[0].ib_order_id == ctx.ib.placed_orders[0]["ib_order_id"]
