"""Integration tests for the close command flow."""
import pytest
from datetime import datetime, timezone
from decimal import Decimal

from ib_trader.repl.commands import CloseCommand
from ib_trader.data.models import (
    Order, TradeGroup, OrderStatus, TradeStatus, LegType, SecurityType
)
from ib_trader.engine.order import execute_close
from ib_trader.engine.exceptions import TradeNotFoundError


def _now():
    return datetime.now(timezone.utc)


async def _setup_filled_trade(ctx, serial: int = 1, side: str = "BUY") -> tuple:
    """Create a trade group with a filled entry order."""
    trade = ctx.trades.create(TradeGroup(
        serial_number=serial, symbol="MSFT", direction="LONG" if side == "BUY" else "SHORT",
        status=TradeStatus.OPEN, opened_at=_now(),
    ))
    order = ctx.orders.create(Order(
        trade_id=trade.id, serial_number=serial, leg_type=LegType.ENTRY,
        symbol="MSFT", side=side, security_type=SecurityType.STK,
        qty_requested=Decimal("10"), qty_filled=Decimal("10"),
        order_type="MID", status=OrderStatus.FILLED,
        avg_fill_price=Decimal("100.00"),
        placed_at=_now(),
    ))
    ctx.orders.update_ib_order_id(order.id, f"IB{serial}000")
    return trade, order


class TestCloseCommand:
    async def test_close_not_found_serial(self, ctx):
        """Close with unknown serial prints error and raises."""
        cmd = CloseCommand(serial=999, strategy="mid", profit_amount=None, take_profit_price=None)
        with pytest.raises(TradeNotFoundError):
            await execute_close(cmd, ctx)

    async def test_close_buy_entry_places_sell(self, ctx):
        """Close of a BUY entry places a SELL closing order."""
        trade, entry = await _setup_filled_trade(ctx, serial=1, side="BUY")
        cmd = CloseCommand(serial=1, strategy="market", profit_amount=None, take_profit_price=None)

        await execute_close(cmd, ctx)

        # Should have placed a SELL to close the LONG
        assert len(ctx.ib.placed_orders) >= 1
        close_order = ctx.ib.placed_orders[-1]
        assert close_order["side"] == "SELL"
        assert close_order["qty"] == Decimal("10")

    async def test_close_sell_entry_places_buy(self, ctx):
        """Close of a SELL (short) entry places a BUY to cover."""
        trade, entry = await _setup_filled_trade(ctx, serial=2, side="SELL")
        cmd = CloseCommand(serial=2, strategy="market", profit_amount=None, take_profit_price=None)

        await execute_close(cmd, ctx)

        close_order = ctx.ib.placed_orders[-1]
        assert close_order["side"] == "BUY"

    async def test_close_cancels_profit_taker(self, ctx):
        """Close cancels any linked profit taker before placing closing order."""
        trade, entry = await _setup_filled_trade(ctx, serial=3, side="BUY")

        # Add a profit taker leg
        pt_order = ctx.orders.create(Order(
            trade_id=trade.id, leg_type=LegType.PROFIT_TAKER,
            symbol="MSFT", side="SELL", security_type=SecurityType.STK,
            qty_requested=Decimal("10"), qty_filled=Decimal("0"),
            order_type="MID", status=OrderStatus.OPEN,
            placed_at=_now(),
        ))
        ctx.orders.update_ib_order_id(pt_order.id, "PT_IB3000")

        cmd = CloseCommand(serial=3, strategy="market", profit_amount=None, take_profit_price=None)
        await execute_close(cmd, ctx)

        # Profit taker should have been canceled
        assert "PT_IB3000" in ctx.ib.canceled_orders

    async def test_close_mid_strategy_places_limit(self, ctx):
        """Close with mid strategy places a limit order at mid price."""
        trade, entry = await _setup_filled_trade(ctx, serial=4, side="BUY")
        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.10"),
            "last": Decimal("100.05"),
        }
        cmd = CloseCommand(serial=4, strategy="mid", profit_amount=None, take_profit_price=None)

        await execute_close(cmd, ctx)

        close_order = ctx.ib.placed_orders[-1]
        assert close_order["side"] == "SELL"
        # Price should be mid = (100.00 + 100.10) / 2 = 100.05
        assert close_order["price"] == Decimal("100.0500")
