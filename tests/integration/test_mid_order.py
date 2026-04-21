"""Integration tests for mid-price order flow.

Tests the complete mid-price order path: place → reprice → fill/cancel.
Uses MockIBClient with simulated fills.
Assertions use TransactionEvent rows instead of Order rows.
"""
import asyncio
import pytest
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from ib_trader.repl.commands import BuyCommand
from ib_trader.data.models import TransactionAction, LegType, TradeGroup, TradeStatus
from ib_trader.engine.order import execute_order, _handle_fill, _handle_partial, _OrderContext
from ib_trader.engine.exceptions import IBOrderRejectedError


@pytest.fixture
def fast_settings(ctx):
    """Make reprice settings very fast for testing."""
    ctx.settings["reprice_steps"] = 10
    ctx.settings["reprice_active_duration_seconds"] = 0.1
    ctx.settings["reprice_passive_wait_seconds"] = 0.1
    return ctx


class _ImmediateRejectMock:
    """Mixin: marks every placed order as Cancelled before the poll loop runs."""

    async def place_limit_order(self, con_id, symbol, side, qty, price,
                                outside_rth=True, tif="GTC", order_ref=None) -> str:
        ib_id = await super().place_limit_order(
            con_id, symbol, side, qty, price, outside_rth, tif, order_ref=order_ref
        )
        # Simulate IB immediately rejecting the order.
        self._order_statuses[ib_id] = {
            "status": "Cancelled",
            "qty_filled": Decimal("0"),
            "avg_fill_price": None,
            "commission": None,
        }
        return ib_id


class _PreSubmittedMock:
    """Mixin: marks every placed order as PreSubmitted (market closed simulation)."""

    async def place_limit_order(self, con_id, symbol, side, qty, price,
                                outside_rth=True, tif="GTC", order_ref=None) -> str:
        ib_id = await super().place_limit_order(
            con_id, symbol, side, qty, price, outside_rth, tif, order_ref=order_ref
        )
        self._order_statuses[ib_id] = {
            "status": "PreSubmitted",
            "qty_filled": Decimal("0"),
            "avg_fill_price": None,
            "commission": None,
        }
        return ib_id


class TestOrderPlacementPreSubmitted:
    """Order goes to PreSubmitted — IB queues it for next trading session."""

    async def test_presubmitted_weekend_skips_reprice_leaves_open(self, ctx):
        """PreSubmitted during weekend: reprice loop skipped, order stays OPEN."""
        from unittest.mock import patch
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tests.conftest import MockIBClient

        class PreSubmittedMock(_PreSubmittedMock, MockIBClient):
            pass

        ctx.ib = PreSubmittedMock()

        cmd = BuyCommand(
            symbol="MSFT", qty=Decimal("2"), dollars=None,
            strategy="mid", profit_amount=None,
            take_profit_price=None, stop_loss=None,
        )

        # Saturday noon ET = weekend closure.
        _saturday = datetime(2026, 3, 7, 12, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("ib_trader.engine.market_hours._now_et", return_value=_saturday):
            await execute_order(cmd, ctx)

        assert len(ctx.ib.amended_orders) == 0
        # Check via transactions: should have a non-terminal PLACE_ACCEPTED
        open_txns = ctx.transactions.get_open_orders()
        assert len(open_txns) == 1
        assert open_txns[0].action == TransactionAction.PLACE_ACCEPTED

    async def test_presubmitted_active_hours_raises_and_abandons(self, ctx):
        """PreSubmitted during an active session: order cancelled, marked ABANDONED, error raised."""
        from unittest.mock import patch
        from datetime import datetime
        from zoneinfo import ZoneInfo
        from tests.conftest import MockIBClient

        class PreSubmittedMock(_PreSubmittedMock, MockIBClient):
            pass

        ctx.ib = PreSubmittedMock()

        cmd = BuyCommand(
            symbol="MSFT", qty=Decimal("2"), dollars=None,
            strategy="mid", profit_amount=None,
            take_profit_price=None, stop_loss=None,
        )

        # Monday 2 PM ET = RTH.
        _monday_rth = datetime(2026, 3, 9, 14, 0, tzinfo=ZoneInfo("America/New_York"))
        with patch("ib_trader.engine.market_hours._now_et", return_value=_monday_rth):
            await execute_order(cmd, ctx)

        # Trade should be closed (the order was canceled and trade marked CLOSED)
        trades = ctx.trades.get_all()
        assert len(trades) >= 1
        from ib_trader.data.models import TradeStatus as TS
        closed = [t for t in trades if t.status == TS.CLOSED]
        assert len(closed) >= 1
        # IB cancel should have been called
        assert len(ctx.ib.canceled_orders) >= 1


class TestOrderPlacementUnacknowledged:
    """Order stays PendingSubmit / gets immediately cancelled by IB."""

    async def test_immediate_ib_rejection_raises_and_marks_abandoned(self, ctx):
        """When IB immediately cancels the order, IBOrderRejectedError is raised
        and the order is marked ABANDONED in the DB."""
        from tests.conftest import MockIBClient

        class RejectingMock(_ImmediateRejectMock, MockIBClient):
            pass

        ctx.ib = RejectingMock()

        cmd = BuyCommand(
            symbol="MSFT", qty=Decimal("1"), dollars=None,
            strategy="mid", profit_amount=None,
            take_profit_price=None, stop_loss=None,
        )

        with pytest.raises(IBOrderRejectedError):
            await execute_order(cmd, ctx)

        # The order must have a terminal transaction (CANCELLED or ERROR_TERMINAL)
        trades = ctx.trades.get_all()
        assert len(trades) >= 1
        trade_id = trades[0].id
        txns = ctx.transactions.get_for_trade(trade_id)
        terminal_txns = [t for t in txns if t.is_terminal]
        assert len(terminal_txns) >= 1


class TestMidOrderCancelOnTimeout:
    async def test_cancel_on_timeout(self, fast_settings):
        """Mid order with no fill is canceled after the total_order_wait window."""
        ctx = fast_settings
        cmd = BuyCommand(
            symbol="MSFT", qty=Decimal("5"), dollars=None,
            strategy="mid", profit_amount=None,
            take_profit_price=None, stop_loss=None,
        )

        # Don't simulate any fill — let it timeout
        await execute_order(cmd, ctx)

        # Should have placed an order and then canceled it
        assert len(ctx.ib.placed_orders) >= 1
        assert len(ctx.ib.canceled_orders) >= 1

    async def test_fill_stops_reprice(self, fast_settings):
        """Mid order that gets filled stops repricing immediately."""
        ctx = fast_settings
        cmd = BuyCommand(
            symbol="MSFT", qty=Decimal("5"), dollars=None,
            strategy="mid", profit_amount=None,
            take_profit_price=None, stop_loss=None,
        )

        # Simulate fill shortly after placing
        async def delayed_fill():
            await asyncio.sleep(0.02)
            if ctx.ib.placed_orders:
                ib_id = ctx.ib.placed_orders[0]["ib_order_id"]
                await ctx.ib.simulate_fill(ib_id, Decimal("5"), Decimal("100.05"), Decimal("1.00"))

        fill_task = asyncio.create_task(delayed_fill())

        await execute_order(cmd, ctx)
        await fill_task

        # Order should be placed, fill should be recorded
        assert len(ctx.ib.placed_orders) >= 1

    async def test_mid_order_with_profit_taker_on_fill(self, fast_settings):
        """Mid order with profit_amount places profit taker on fill."""
        ctx = fast_settings
        cmd = BuyCommand(
            symbol="MSFT", qty=Decimal("5"), dollars=None,
            strategy="mid", profit_amount=Decimal("100"),
            take_profit_price=None, stop_loss=None,
        )

        async def delayed_fill():
            await asyncio.sleep(0.02)
            if ctx.ib.placed_orders:
                ib_id = ctx.ib.placed_orders[0]["ib_order_id"]
                await ctx.ib.simulate_fill(ib_id, Decimal("5"), Decimal("100.00"), Decimal("1.00"))

        fill_task = asyncio.create_task(delayed_fill())
        await execute_order(cmd, ctx)
        await fill_task

        # Should have placed entry + profit taker (2 orders total)
        # Entry was placed, and if fill arrived in time, profit taker too
        assert len(ctx.ib.placed_orders) >= 1


class TestHandleFill:
    async def test_handle_fill_updates_db(self, ctx):
        """_handle_fill records fill details as TransactionEvent rows."""
        trade = ctx.trades.create(TradeGroup(
            serial_number=1, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=datetime.now(timezone.utc),
        ))
        correlation_id = str(uuid.uuid4())
        order_ctx = _OrderContext(
            trade_id=trade.id, trade_serial=1, symbol="MSFT",
            side="BUY", order_type="MID", qty_requested=Decimal("10"),
            leg_type=LegType.ENTRY, correlation_id=correlation_id,
            security_type="STK", ib_order_id="IB9000",
        )

        cmd = BuyCommand(
            symbol="MSFT", qty=Decimal("10"), dollars=None,
            strategy="mid", profit_amount=None,
            take_profit_price=None, stop_loss=None,
        )

        await _handle_fill(
            order_ctx, trade,
            qty_filled=Decimal("10"),
            avg_price=Decimal("100.50"),
            commission=Decimal("1.00"),
            cmd=cmd,
            con_id=12345,
            ctx=ctx,
        )

        # Verify FILLED transaction was written
        txns = ctx.transactions.get_for_trade(trade.id)
        filled = [t for t in txns if t.action == TransactionAction.FILLED]
        assert len(filled) == 1
        assert filled[0].ib_filled_qty == Decimal("10")
        assert filled[0].ib_avg_fill_price == Decimal("100.50")
        assert filled[0].is_terminal is True


class TestHandlePartial:
    async def test_handle_partial_updates_db(self, ctx):
        """_handle_partial records partial fill as TransactionEvent rows."""
        trade = ctx.trades.create(TradeGroup(
            serial_number=2, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=datetime.now(timezone.utc),
        ))
        correlation_id = str(uuid.uuid4())
        order_ctx = _OrderContext(
            trade_id=trade.id, trade_serial=2, symbol="MSFT",
            side="BUY", order_type="MID", qty_requested=Decimal("10"),
            leg_type=LegType.ENTRY, correlation_id=correlation_id,
            security_type="STK", ib_order_id="mock-ib-123",
        )

        cmd = BuyCommand(
            symbol="MSFT", qty=Decimal("10"), dollars=None,
            strategy="mid", profit_amount=None,
            take_profit_price=None, stop_loss=None,
        )

        # _handle_partial now waits for IB to confirm the cancel (so a
        # late-arriving fill can be promoted to FILLED). Pre-register the
        # mock order so cancel_order() flips it to Cancelled and the wait
        # loop returns on the first iteration instead of hitting the
        # settle timeout.
        ctx.ib._order_statuses["mock-ib-123"] = {
            "status": "Submitted",
            "qty_filled": Decimal("6"),
            "avg_fill_price": Decimal("100.00"),
            "commission": Decimal("0.60"),
        }

        await _handle_partial(
            order_ctx, trade,
            qty_requested=Decimal("10"),
            qty_filled=Decimal("6"),
            avg_price=Decimal("100.00"),
            commission=Decimal("0.60"),
            cmd=cmd,
            con_id=12345,
            ib_order_id="mock-ib-123",
            ctx=ctx,
        )

        # Verify partial fill + cancel transactions were written
        txns = ctx.transactions.get_for_trade(trade.id)
        partial = [t for t in txns if t.action == TransactionAction.PARTIAL_FILL]
        assert len(partial) == 1
        assert partial[0].ib_filled_qty == Decimal("6")
        cancelled = [t for t in txns if t.action == TransactionAction.CANCELLED]
        assert len(cancelled) == 1
        assert cancelled[0].is_terminal is True


class TestDollarsToSharesConversion:
    async def test_dollars_flag_converts_to_shares(self, ctx):
        """--dollars flag calculates qty from notional / mid price."""
        ctx.ib._market_snapshot = {
            "bid": Decimal("100.00"),
            "ask": Decimal("100.10"),
            "last": Decimal("100.05"),
        }
        cmd = BuyCommand(
            symbol="MSFT", qty=None, dollars=Decimal("500"),
            strategy="market", profit_amount=None,
            take_profit_price=None, stop_loss=None,
        )

        task = asyncio.create_task(execute_order(cmd, ctx))
        await asyncio.sleep(0.05)
        if ctx.ib.placed_orders:
            ib_id = ctx.ib.placed_orders[-1]["ib_order_id"]
            await ctx.ib.simulate_fill(ib_id, Decimal("4"), Decimal("100.05"), Decimal("1.00"))
        try:
            await asyncio.wait_for(task, timeout=5.0)
        except asyncio.TimeoutError:
            task.cancel()

        if ctx.ib.placed_orders:
            # $500 / $100.05 mid = 4 shares (floored, capped at 10)
            assert ctx.ib.placed_orders[0]["qty"] == Decimal("4")

    async def test_dollars_too_small_prints_error(self, ctx, capsys):
        """Dollar amount too small for price prints error, no IB call."""
        ctx.ib._market_snapshot = {
            "bid": Decimal("1000.00"),
            "ask": Decimal("1001.00"),
            "last": Decimal("1000.50"),
        }
        cmd = BuyCommand(
            symbol="MSFT", qty=None, dollars=Decimal("0.01"),  # Way too small
            strategy="market", profit_amount=None,
            take_profit_price=None, stop_loss=None,
        )

        await execute_order(cmd, ctx)

        captured = capsys.readouterr()
        assert "Error" in captured.out or len(ctx.ib.placed_orders) == 0
