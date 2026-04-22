"""Integration tests for SMART_MARKET order flow.

Covers the four behaviour paths specified in
`docs/design/execution-algos.md`:

  1. RTH + full fill during the aggressive walker.
  2. RTH + residual at walker expiry → crosses to MKT, combined fill.
  3. ETH + cap reached → alert raised, order left resting.
  4. ETH + full fill during the walker.

Uses MockIBClient with simulated fills. Reprice intervals and duration
are aggressively shortened via ctx.settings so the tests run in
milliseconds.
"""
from unittest.mock import patch
from decimal import Decimal

import pytest

from ib_trader.data.models import TransactionAction
from ib_trader.engine.order import execute_order
from ib_trader.repl.commands import SellCommand
from tests.conftest import MockIBClient


@pytest.fixture
def fast_smart_market(ctx):
    """Shrink SMART_MARKET timing + slippage cap so tests run fast."""
    ctx.settings["smart_market_reprice_interval_ms"] = 5
    ctx.settings["smart_market_rth_duration_seconds"] = 0.05
    ctx.settings["smart_market_eth_max_slippage_pct"] = 0.01  # 1%
    ctx.settings["cancel_settle_timeout_seconds"] = 0.5
    return ctx


class _FillOnPlaceMock:
    """Mixin: marks placed LIMIT orders as fully Filled immediately, so
    the walker sees qty_filled == qty on its first poll."""

    async def place_limit_order(self, con_id, symbol, side, qty, price,
                                outside_rth=True, tif="GTC", order_ref=None) -> str:
        ib_id = await super().place_limit_order(
            con_id, symbol, side, qty, price, outside_rth, tif, order_ref=order_ref
        )
        self._order_statuses[ib_id] = {
            "status": "Filled",
            "qty_filled": qty,
            "avg_fill_price": price,
            "commission": Decimal("1.00"),
        }
        return ib_id


class _ResidualLimitMock:
    """Mixin: LIMIT orders stay Submitted with 0 fills (walker will
    expire). MARKET orders fill fully (the RTH terminal)."""

    async def place_limit_order(self, con_id, symbol, side, qty, price,
                                outside_rth=True, tif="GTC", order_ref=None) -> str:
        ib_id = await super().place_limit_order(
            con_id, symbol, side, qty, price, outside_rth, tif, order_ref=order_ref
        )
        # Leave Submitted with 0 fills so the walker exits on duration.
        return ib_id

    async def place_market_order(self, con_id, symbol, side, qty,
                                 outside_rth=True, order_ref=None) -> str:
        ib_id = await super().place_market_order(
            con_id, symbol, side, qty, outside_rth, order_ref=order_ref
        )
        self._order_statuses[ib_id] = {
            "status": "Filled",
            "qty_filled": qty,
            "avg_fill_price": Decimal("99.95"),
            "commission": Decimal("0.50"),
        }
        return ib_id


def _make_cmd() -> SellCommand:
    return SellCommand(
        symbol="MSFT", qty=Decimal("10"), dollars=None,
        strategy="smart_market", profit_amount=None,
        take_profit_price=None, stop_loss=None,
    )


class TestSmartMarketRTH:

    async def test_rth_full_fill_during_walker(self, fast_smart_market):
        """Walker sees a full fill on first poll → _handle_fill path."""
        ctx = fast_smart_market

        class FillMock(_FillOnPlaceMock, MockIBClient):
            pass

        ctx.ib = FillMock()

        # Force RTH regardless of clock.
        with patch("ib_trader.engine.order.is_outside_rth", return_value=False):
            await execute_order(_make_cmd(), ctx)

        trades = ctx.trades.get_all()
        assert len(trades) == 1
        txns = ctx.transactions.get_for_trade(trades[0].id)
        # A terminal FILLED txn with the full qty.
        filled = [t for t in txns if t.action == TransactionAction.FILLED and t.is_terminal]
        assert len(filled) == 1
        assert filled[0].ib_filled_qty == Decimal("10")
        # No MARKET order was needed — walker saw the fill immediately.
        mkt_orders = [o for o in ctx.ib.placed_orders if o.get("type") == "MARKET"]
        assert len(mkt_orders) == 0

    async def test_rth_residual_goes_to_market(self, fast_smart_market):
        """Walker expires with 0 fills → engine cancels, places MKT for residual."""
        ctx = fast_smart_market

        class ResidualMock(_ResidualLimitMock, MockIBClient):
            pass

        ctx.ib = ResidualMock()

        with patch("ib_trader.engine.order.is_outside_rth", return_value=False):
            await execute_order(_make_cmd(), ctx)

        # Exactly one MARKET order was placed for the full residual (10 shares).
        mkt_orders = [o for o in ctx.ib.placed_orders if o.get("type") == "MARKET"]
        assert len(mkt_orders) == 1
        assert mkt_orders[0]["qty"] == Decimal("10")

        # Trade group should have a terminal FILLED txn with combined qty.
        trades = ctx.trades.get_all()
        txns = ctx.transactions.get_for_trade(trades[0].id)
        filled = [t for t in txns if t.action == TransactionAction.FILLED and t.is_terminal]
        assert len(filled) == 1
        assert filled[0].ib_filled_qty == Decimal("10")


class TestSmartMarketETH:

    async def test_eth_full_fill_during_walker(self, fast_smart_market):
        """ETH path: fill happens before the floor cap is reached."""
        ctx = fast_smart_market

        class FillMock(_FillOnPlaceMock, MockIBClient):
            pass

        ctx.ib = FillMock()

        with patch("ib_trader.engine.order.is_outside_rth", return_value=True):
            await execute_order(_make_cmd(), ctx)

        trades = ctx.trades.get_all()
        assert len(trades) == 1
        txns = ctx.transactions.get_for_trade(trades[0].id)
        filled = [t for t in txns if t.action == TransactionAction.FILLED and t.is_terminal]
        assert len(filled) == 1
        # No MARKET crossing in ETH.
        mkt_orders = [o for o in ctx.ib.placed_orders if o.get("type") == "MARKET"]
        assert len(mkt_orders) == 0

    async def test_eth_cap_reached_raises_alert_and_rests(self, fast_smart_market):
        """ETH path: walker amends toward floor, never fills, hits cap,
        raises CATASTROPHIC alert, leaves order resting at IB."""
        ctx = fast_smart_market

        class ResidualMock(_ResidualLimitMock, MockIBClient):
            pass

        ctx.ib = ResidualMock()

        # Prepare a fake Redis that captures hset calls to alerts:active.
        published_alerts: list[tuple[str, dict]] = []

        class _FakeRedis:
            async def hset(self, key, field, value):
                import json as _json
                published_alerts.append((key, _json.loads(value)))

            async def xadd(self, *args, **kwargs):
                # publish_activity target — stub out.
                return "0-0"

            async def get(self, *a, **kw):
                return None

        ctx.redis = _FakeRedis()

        with patch("ib_trader.engine.order.is_outside_rth", return_value=True):
            await execute_order(_make_cmd(), ctx)

        # CATASTROPHIC alert was written to alerts:active.
        cat = [a for (_k, a) in published_alerts if a.get("severity") == "CATASTROPHIC"]
        assert len(cat) >= 1
        assert cat[0]["trigger"] == "EXIT_PRICE_CAP_REACHED"
        assert cat[0]["symbol"] == "MSFT"
        assert cat[0]["pager"] is True

        # Order was NOT cancelled (left resting at the cap).
        limit_orders = [o for o in ctx.ib.placed_orders if o.get("type") != "MARKET"]
        assert len(limit_orders) == 1
        assert limit_orders[0]["ib_order_id"] not in ctx.ib.canceled_orders

        # No MARKET crossing in the ETH branch.
        mkt_orders = [o for o in ctx.ib.placed_orders if o.get("type") == "MARKET"]
        assert len(mkt_orders) == 0

        # No terminal FILLED / CANCELLED txn — human resolves.
        trades = ctx.trades.get_all()
        txns = ctx.transactions.get_for_trade(trades[0].id)
        terminal = [t for t in txns if t.is_terminal]
        assert len(terminal) == 0
