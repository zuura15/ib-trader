"""Integration tests for native STP stop orders (#97).

Covers the ``stop`` entry strategy (fire-and-forget STP via
``execute_order``) and the real ``--stop-loss`` protective leg placed
after an entry fill, including the wrong-side guard and OCA linking.
Assertions use TransactionEvent rows + the mock's ``placed_orders``.
"""
import pytest
from decimal import Decimal

from ib_trader.repl.commands import BuyCommand, SellCommand, Strategy
from ib_trader.data.models import TransactionAction, LegType, TransactionEvent
from ib_trader.engine.order import execute_order


def _buy(**kw):
    defaults = dict(
        symbol="MSFT", qty=Decimal("1"), dollars=None,
        strategy=Strategy.STOP, profit_amount=None,
        take_profit_price=None, stop_loss=None,
    )
    defaults.update(kw)
    return BuyCommand(**defaults)


def _sell(**kw):
    defaults = dict(
        symbol="MSFT", qty=Decimal("1"), dollars=None,
        strategy=Strategy.STOP, profit_amount=None,
        take_profit_price=None, stop_loss=None,
    )
    defaults.update(kw)
    return SellCommand(**defaults)


def _stub_entry_fill(ctx, fill_price: Decimal):
    """Stub market AND marketable-limit placement to fill instantly.

    Outside RTH the market strategy reroutes through
    ``place_limit_order``, so both must be patched (same pattern as
    the close-command tests).
    """
    async def place_and_fill(con_id, symbol, side, qty, *a, **kw):
        ib_id = str(ctx.ib._next_order_id)
        ctx.ib._next_order_id += 1
        ctx.ib._order_statuses[ib_id] = {
            "status": "Filled", "qty_filled": qty,
            "avg_fill_price": fill_price,
            "commission": Decimal("1.00"),
        }
        return ib_id
    ctx.ib.place_market_order = place_and_fill
    ctx.ib.place_limit_order = place_and_fill


class TestStopEntry:
    """buy/sell SYMBOL QTY stop PRICE — native STP through execute_order."""

    async def test_buy_stop_places_native_stp(self, ctx):
        await execute_order(_buy(stop_price=Decimal("420.00")), ctx)

        placed = ctx.ib.placed_orders[-1]
        assert placed["type"] == "STOP"
        assert placed["side"] == "BUY"
        assert placed["stop_price"] == Decimal("420.00")
        assert placed["tif"] == "GTC"
        # Resting STP parks in PreSubmitted — fire-and-forget live.
        status = ctx.ib._order_statuses[placed["ib_order_id"]]
        assert status["status"] == "PreSubmitted"

        session = ctx.transactions._session()
        rows = (
            session.query(TransactionEvent)
            .filter(TransactionEvent.order_type == "STOP")
            .all()
        )
        actions = [r.action for r in rows]
        assert TransactionAction.PLACE_ATTEMPT in actions
        assert TransactionAction.PLACE_ACCEPTED in actions
        accepted = [r for r in rows
                    if r.action == TransactionAction.PLACE_ACCEPTED][0]
        assert accepted.limit_price == Decimal("420.00")  # trigger price column
        assert accepted.leg_type == LegType.ENTRY

    async def test_sell_stop_places_sell_stp(self, ctx):
        await execute_order(_sell(stop_price=Decimal("400.00")), ctx)
        placed = ctx.ib.placed_orders[-1]
        assert placed["type"] == "STOP"
        assert placed["side"] == "SELL"
        assert placed["stop_price"] == Decimal("400.00")

    async def test_off_tick_stop_price_rejected(self, ctx):
        await execute_order(_buy(stop_price=Decimal("420.005")), ctx)
        assert ctx.ib.placed_orders == []


class TestStopLossLeg:
    """--stop-loss PRICE — protective STP placed after the entry fill."""

    async def test_stop_loss_placed_after_buy_fill(self, ctx):
        _stub_entry_fill(ctx, Decimal("420.00"))
        await execute_order(
            _buy(strategy=Strategy.MARKET, stop_loss=Decimal("415.00")), ctx,
        )

        stops = [o for o in ctx.ib.placed_orders if o.get("type") == "STOP"]
        assert len(stops) == 1
        assert stops[0]["side"] == "SELL"          # inverse of BUY entry
        assert stops[0]["stop_price"] == Decimal("415.00")
        assert stops[0]["oca_group"] is None       # single exit leg — no OCA

        session = ctx.transactions._session()
        sl_rows = (
            session.query(TransactionEvent)
            .filter(TransactionEvent.leg_type == LegType.STOP_LOSS)
            .all()
        )
        actions = [r.action for r in sl_rows]
        assert TransactionAction.PLACE_ATTEMPT in actions
        assert TransactionAction.PLACE_ACCEPTED in actions

    async def test_stop_loss_short_entry_places_buy_stop(self, ctx):
        _stub_entry_fill(ctx, Decimal("420.00"))
        await execute_order(
            _sell(strategy=Strategy.MARKET, stop_loss=Decimal("425.00")), ctx,
        )
        stops = [o for o in ctx.ib.placed_orders if o.get("type") == "STOP"]
        assert len(stops) == 1
        assert stops[0]["side"] == "BUY"
        assert stops[0]["stop_price"] == Decimal("425.00")

    async def test_wrong_side_stop_loss_not_placed(self, ctx):
        # BUY entry with stop at/above the fill would trigger instantly
        # — refused with a WARNING, position left unprotected but sane.
        _stub_entry_fill(ctx, Decimal("420.00"))
        await execute_order(
            _buy(strategy=Strategy.MARKET, stop_loss=Decimal("425.00")), ctx,
        )
        assert [o for o in ctx.ib.placed_orders if o.get("type") == "STOP"] == []
        session = ctx.transactions._session()
        assert (
            session.query(TransactionEvent)
            .filter(TransactionEvent.leg_type == LegType.STOP_LOSS)
            .all()
        ) == []

    async def test_stop_loss_and_profit_taker_share_oca(self, ctx):
        _stub_entry_fill(ctx, Decimal("420.00"))
        # place_limit_order is stubbed by _stub_entry_fill (it returns a
        # filled order), which would also swallow the profit taker — so
        # restore the real mock limit placement AFTER the entry by
        # stubbing only the market route.
        ctx2 = ctx
        real_limit = type(ctx.ib).place_limit_order  # unbound mock method

        async def entry_fills_via_market(con_id, symbol, side, qty, *a, **kw):
            ib_id = str(ctx2.ib._next_order_id)
            ctx2.ib._next_order_id += 1
            ctx2.ib._order_statuses[ib_id] = {
                "status": "Filled", "qty_filled": qty,
                "avg_fill_price": Decimal("420.00"),
                "commission": Decimal("1.00"),
            }
            return ib_id

        async def limit_passthrough(con_id, symbol, side, qty, price, *a, **kw):
            # Marketable-limit entry route must also fill; the profit
            # taker (SELL above market) stays resting via the real mock.
            if side == "BUY":
                return await entry_fills_via_market(con_id, symbol, side, qty)
            return await real_limit(ctx2.ib, con_id, symbol, side, qty, price,
                                    *a, **kw)

        ctx.ib.place_market_order = entry_fills_via_market
        ctx.ib.place_limit_order = limit_passthrough

        await execute_order(
            _buy(strategy=Strategy.MARKET,
                 take_profit_price=Decimal("430.00"),
                 stop_loss=Decimal("415.00")), ctx,
        )

        stops = [o for o in ctx.ib.placed_orders if o.get("type") == "STOP"]
        pts = [o for o in ctx.ib.placed_orders
               if o.get("type") is None and o.get("side") == "SELL"
               and o.get("price") == Decimal("430.00")]
        assert len(stops) == 1
        assert len(pts) == 1
        assert stops[0]["oca_group"] is not None
        assert stops[0]["oca_group"] == pts[0]["oca_group"]
