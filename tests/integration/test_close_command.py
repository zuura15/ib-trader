"""Integration tests for the close command flow.

Assertions use TransactionEvent rows instead of Order rows.
"""
import pytest
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from ib_trader.repl.commands import CloseCommand
from ib_trader.data.models import (
    TradeGroup, TradeStatus, LegType, TransactionAction, TransactionEvent,
)
from ib_trader.engine.order import execute_close
from ib_trader.engine.exceptions import TradeNotFoundError


def _now():
    return datetime.now(timezone.utc)


async def _setup_filled_trade(ctx, serial: int = 1, side: str = "BUY") -> tuple:
    """Create a trade group with a filled entry via TransactionEvent rows."""
    trade = ctx.trades.create(TradeGroup(
        serial_number=serial, symbol="MSFT", direction="LONG" if side == "BUY" else "SHORT",
        status=TradeStatus.OPEN, opened_at=_now(),
    ))
    correlation_id = str(uuid.uuid4())
    # Insert PLACE_ATTEMPT
    ctx.transactions.insert(TransactionEvent(
        ib_order_id=serial * 1000, action=TransactionAction.PLACE_ATTEMPT,
        symbol="MSFT", side=side, order_type="MID",
        quantity=Decimal("10"), account_id="U1234567",
        requested_at=_now(), trade_id=trade.id,
        leg_type=LegType.ENTRY, correlation_id=correlation_id,
        security_type="STK",
    ))
    # Insert PLACE_ACCEPTED
    ctx.transactions.insert(TransactionEvent(
        ib_order_id=serial * 1000, action=TransactionAction.PLACE_ACCEPTED,
        symbol="MSFT", side=side, order_type="MID",
        quantity=Decimal("10"), account_id="U1234567",
        requested_at=_now(), trade_id=trade.id,
        leg_type=LegType.ENTRY, correlation_id=correlation_id,
        security_type="STK",
    ))
    # Insert FILLED
    ctx.transactions.insert(TransactionEvent(
        ib_order_id=serial * 1000, action=TransactionAction.FILLED,
        symbol="MSFT", side=side, order_type="MID",
        quantity=Decimal("10"), account_id="U1234567",
        requested_at=_now(), trade_id=trade.id,
        leg_type=LegType.ENTRY, correlation_id=correlation_id,
        security_type="STK", is_terminal=True,
        ib_filled_qty=Decimal("10"), ib_avg_fill_price=Decimal("100.00"),
        commission=Decimal("1.00"),
    ))
    return trade, correlation_id


class TestCloseCommand:
    async def test_close_not_found_serial(self, ctx):
        """Close with unknown serial prints error and raises."""
        cmd = CloseCommand(serial=999, strategy="mid", profit_amount=None, take_profit_price=None)
        with pytest.raises(TradeNotFoundError):
            await execute_close(cmd, ctx)

    async def test_close_buy_entry_places_sell(self, ctx):
        """Close of a BUY entry places a SELL closing order."""
        _trade, _entry = await _setup_filled_trade(ctx, serial=1, side="BUY")
        cmd = CloseCommand(serial=1, strategy="market", profit_amount=None, take_profit_price=None)

        await execute_close(cmd, ctx)

        # Should have placed a SELL to close the LONG
        assert len(ctx.ib.placed_orders) >= 1
        close_order = ctx.ib.placed_orders[-1]
        assert close_order["side"] == "SELL"
        assert close_order["qty"] == Decimal("10")

    async def test_close_sell_entry_places_buy(self, ctx):
        """Close of a SELL (short) entry places a BUY to cover."""
        _trade, _entry = await _setup_filled_trade(ctx, serial=2, side="SELL")
        cmd = CloseCommand(serial=2, strategy="market", profit_amount=None, take_profit_price=None)

        await execute_close(cmd, ctx)

        close_order = ctx.ib.placed_orders[-1]
        assert close_order["side"] == "BUY"

    async def test_close_cancels_profit_taker(self, ctx):
        """Close cancels any linked profit taker before placing closing order."""
        trade, _entry_corr = await _setup_filled_trade(ctx, serial=3, side="BUY")

        # Add a profit taker leg as non-terminal transaction
        pt_correlation_id = str(uuid.uuid4())
        ctx.transactions.insert(TransactionEvent(
            ib_order_id=30001, action=TransactionAction.PLACE_ACCEPTED,
            symbol="MSFT", side="SELL", order_type="LMT",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            leg_type=LegType.PROFIT_TAKER, correlation_id=pt_correlation_id,
            security_type="STK", is_terminal=False,
        ))

        cmd = CloseCommand(serial=3, strategy="market", profit_amount=None, take_profit_price=None)
        await execute_close(cmd, ctx)

        # Profit taker should have been canceled (ib_order_id may be int or str)
        canceled_ids = [str(x) for x in ctx.ib.canceled_orders]
        assert "30001" in canceled_ids

    async def test_close_mid_strategy_places_limit(self, ctx):
        """Close with mid strategy places a limit order at mid price."""
        _trade, _entry = await _setup_filled_trade(ctx, serial=4, side="BUY")
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
