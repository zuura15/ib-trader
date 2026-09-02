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


def _open_order(oid: str, local_symbol: str, side: str = "SELL",
                qty: str = "2", order_type: str = "LMT",
                limit_price: str | None = "4470.0") -> dict:
    return {
        "ib_order_id": oid,
        "symbol": local_symbol[:2],
        "local_symbol": local_symbol,
        "side": side,
        "qty": Decimal(qty),
        "order_type": order_type,
        "limit_price": Decimal(limit_price) if limit_price else None,
        "status": "Submitted",
        "qty_filled": Decimal("0"),
        "avg_fill_price": None,
    }


class TestCloseSymbol:
    """close SYMBOL — ticker-wide cancel sweep + net flat (#96)."""

    def _cmd(self, symbol: str = "GCV6", strategy: str = "market",
             security_type: str = "FUT"):
        from ib_trader.repl.commands import Strategy
        return CloseCommand(
            serial=None, strategy=Strategy(strategy), profit_amount=None,
            take_profit_price=None, symbol=symbol,
            security_type=security_type,
        )

    async def test_sweep_cancels_only_matching_ticker(self, ctx):
        from ib_trader.engine.order import execute_close
        ctx.ib.mock_open_orders = [
            _open_order("9001", "GCV6"),
            _open_order("9002", "MGCV6"),   # other ticker — untouched
            _open_order("9003", "GCV6", side="BUY", order_type="STP",
                        limit_price=None),
        ]
        ctx.positions_cache = []            # flat — sweep only
        await execute_close(self._cmd(), ctx)

        assert ctx.ib.canceled_orders == ["9001", "9003"]
        # One CANCEL_ATTEMPT + one CANCELLED row per swept order; the
        # other ticker's order got no rows at all.
        for oid in (9001, 9003):
            rows = ctx.transactions.get_by_ib_order_id(oid)
            actions = [r.action for r in rows]
            assert actions.count(TransactionAction.CANCEL_ATTEMPT) == 1
            assert actions.count(TransactionAction.CANCELLED) == 1
            cancelled_row = [r for r in rows
                             if r.action == TransactionAction.CANCELLED][0]
            assert cancelled_row.is_terminal
            assert cancelled_row.symbol == "GCV6"
        assert ctx.transactions.get_by_ib_order_id(9002) == []

    async def test_flat_reports_nothing_to_close(self, ctx):
        from ib_trader.engine.order import execute_close
        ctx.ib.mock_open_orders = []
        ctx.positions_cache = []
        await execute_close(self._cmd(), ctx)
        assert ctx.ib.canceled_orders == []

    async def test_short_position_places_buy_close(self, ctx):
        # STK keeps the mock qualify fixture simple (FUT requires an
        # expiry there); the sweep + net-flat flow is sec-type agnostic.
        from ib_trader.engine.order import execute_close

        # Place-and-fill stubs (both market and the session-gated
        # marketable-limit route): the sweep is gated on the close
        # leg's FILLED row, so the close must actually fill.
        async def place_and_fill(con_id, symbol, side, qty, *a, **kw):
            ib_id = str(ctx.ib._next_order_id)
            ctx.ib._next_order_id += 1
            ctx.ib._order_statuses[ib_id] = {
                "status": "Filled", "qty_filled": qty,
                "avg_fill_price": Decimal("405.0"),
                "commission": Decimal("1.00"),
            }
            return ib_id
        ctx.ib.place_market_order = place_and_fill
        ctx.ib.place_limit_order = place_and_fill

        ctx.ib.mock_open_orders = [
            _open_order("9010", "MSFT", limit_price="410.0"),
        ]
        ctx.positions_cache = [{
            "symbol": "MSFT", "local_symbol": "MSFT", "sec_type": "STK",
            "quantity": "-2", "avg_cost": "400.0", "con_id": 1,
        }]
        await execute_close(
            self._cmd(symbol="MSFT", security_type="STK"), ctx,
        )

        assert ctx.ib.canceled_orders == ["9010"]
        # The net-flat leg went through execute_order → exactly one
        # BUY 2 MSFT PLACE_ATTEMPT row exists (inverse of the short).
        session = ctx.transactions._session()
        placed = (
            session.query(TransactionEvent)
            .filter(TransactionEvent.symbol == "MSFT",
                    TransactionEvent.action == TransactionAction.PLACE_ATTEMPT)
            .all()
        )
        assert len(placed) == 1
        assert placed[0].side == "BUY"
        assert placed[0].quantity == Decimal("2")

    async def test_failed_close_leaves_orders_untouched(self, ctx):
        # Cancel-only-if-close-succeeds: an order-placement failure
        # propagates and the pre-close snapshot is NOT swept.
        from ib_trader.engine.order import execute_close

        async def _boom(*a, **kw):
            raise RuntimeError("IB rejected")
        # Patch BOTH placement routes — outside RTH the market strategy
        # takes the marketable-limit path.
        ctx.ib.place_market_order = _boom
        ctx.ib.place_limit_order = _boom

        ctx.ib.mock_open_orders = [
            _open_order("9020", "MSFT", limit_price="410.0"),
        ]
        ctx.positions_cache = [{
            "symbol": "MSFT", "local_symbol": "MSFT", "sec_type": "STK",
            "quantity": "-2", "avg_cost": "400.0", "con_id": 1,
        }]
        with pytest.raises(Exception):
            await execute_close(
                self._cmd(symbol="MSFT", security_type="STK"), ctx,
            )
        assert ctx.ib.canceled_orders == []
        # No CANCELLED rows either — the sweep never ran.
        rows = ctx.transactions.get_by_ib_order_id(9020)
        assert [r for r in rows
                if r.action == TransactionAction.CANCELLED] == []
