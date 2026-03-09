"""Integration tests for reconciliation logic."""
from datetime import datetime, timezone
from decimal import Decimal

from ib_trader.daemon.reconciler import run_reconciliation
from ib_trader.daemon.monitor import check_repl_heartbeat, check_ib_connectivity
from ib_trader.data.models import (
    Order, TradeGroup, OrderStatus, TradeStatus, LegType, SecurityType, AlertSeverity
)


def _now():
    return datetime.now(timezone.utc)


def _make_open_order(ctx, ib_order_id: str, serial: int = 1):
    trade = ctx.trades.create(TradeGroup(
        serial_number=serial, symbol="MSFT", direction="LONG",
        status=TradeStatus.OPEN, opened_at=_now(),
    ))
    order = ctx.orders.create(Order(
        trade_id=trade.id, serial_number=serial, leg_type=LegType.ENTRY,
        symbol="MSFT", side="BUY", security_type=SecurityType.STK,
        qty_requested=Decimal("10"), qty_filled=Decimal("0"),
        order_type="MID", status=OrderStatus.OPEN, placed_at=_now(),
    ))
    ctx.orders.update_ib_order_id(order.id, ib_order_id)
    return order


class TestReconciliation:
    async def test_no_discrepancy_returns_no_changes(self, ctx):
        """If IB shows no open orders and SQLite has none, no changes."""
        result = await run_reconciliation(ctx)
        assert result["changes"] == 0

    async def test_reconciles_externally_canceled_order(self, ctx):
        """Order open in SQLite but canceled in IB gets marked CANCELED."""
        order = _make_open_order(ctx, "IB5000", serial=1)

        # Mock IB: order shows as Cancelled
        ctx.ib._order_statuses["IB5000"] = {
            "status": "Cancelled",
            "qty_filled": Decimal("0"),
            "avg_fill_price": None,
            "commission": None,
        }

        result = await run_reconciliation(ctx)
        assert result["changes"] >= 1

        updated = ctx.orders.get_by_id(order.id)
        assert updated.status == OrderStatus.CANCELED

    async def test_reconciles_externally_filled_order(self, ctx):
        """Order open in SQLite but filled in IB gets marked CLOSED_EXTERNAL."""
        order = _make_open_order(ctx, "IB6000", serial=2)

        ctx.ib._order_statuses["IB6000"] = {
            "status": "Filled",
            "qty_filled": Decimal("10"),
            "avg_fill_price": Decimal("100.50"),
            "commission": Decimal("1.00"),
        }

        result = await run_reconciliation(ctx)
        assert result["changes"] >= 1

        updated = ctx.orders.get_by_id(order.id)
        assert updated.status == OrderStatus.CLOSED_EXTERNAL


class TestMonitor:
    async def test_repl_alive_returns_true(self, ctx):
        """If REPL heartbeat is fresh, returns True."""
        ctx.heartbeats.upsert("REPL", 1234)
        result = await check_repl_heartbeat(ctx)
        assert result is True

    async def test_repl_missing_returns_false(self, ctx):
        """If no REPL heartbeat row, returns False (clean exit, not crash)."""
        result = await check_repl_heartbeat(ctx)
        assert result is False

    async def test_stale_repl_creates_catastrophic_alert(self, ctx):
        """Stale REPL heartbeat triggers CATASTROPHIC alert."""
        from datetime import timedelta
        # Write a heartbeat that's older than the stale threshold
        from ib_trader.data.models import SystemHeartbeat
        stale_time = _now() - timedelta(seconds=400)  # > 300s threshold
        session = ctx.heartbeats._session()
        session.add(SystemHeartbeat(process="REPL", last_seen_at=stale_time, pid=999))
        session.commit()

        result = await check_repl_heartbeat(ctx)
        assert result is False

        # Should have raised a CATASTROPHIC alert
        open_alerts = ctx.alerts.get_open()
        catastrophic = [a for a in open_alerts if a.severity == AlertSeverity.CATASTROPHIC]
        assert len(catastrophic) >= 1

    async def test_ib_connectivity_ok(self, ctx):
        """IB connectivity check passes with working mock."""
        failures = []
        result = await check_ib_connectivity(ctx, failures)
        assert result is True
        assert len(failures) == 0

    async def test_ib_connectivity_failure_adds_to_count(self, ctx):
        """IB connectivity failure increments failure count."""
        from unittest.mock import AsyncMock
        ctx.ib.get_open_orders = AsyncMock(side_effect=Exception("Connection refused"))
        failures = []
        result = await check_ib_connectivity(ctx, failures)
        assert result is False
        assert len(failures) == 1

    async def test_three_consecutive_failures_raises_catastrophic(self, ctx):
        """3 consecutive IB failures trigger CATASTROPHIC alert."""
        from unittest.mock import AsyncMock
        ctx.ib.get_open_orders = AsyncMock(side_effect=Exception("timeout"))
        failures = []
        for _ in range(3):
            await check_ib_connectivity(ctx, failures)

        open_alerts = ctx.alerts.get_open()
        catastrophic = [a for a in open_alerts if a.severity == AlertSeverity.CATASTROPHIC
                        and a.trigger == "IB_CONNECTIVITY_FAILURE"]
        assert len(catastrophic) >= 1
