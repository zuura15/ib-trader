"""Unit tests for crash recovery logic."""
from datetime import datetime, timezone
from decimal import Decimal

from ib_trader.data.models import (
    TradeGroup, Order, TradeStatus, OrderStatus, LegType, SecurityType
)
from ib_trader.data.repository import TradeRepository, OrderRepository
from ib_trader.engine.recovery import (
    recover_in_flight_orders, format_recovery_warnings, close_orphaned_trade_groups,
)


def _now():
    return datetime.now(timezone.utc)


def _make_order(
    session_factory, status: OrderStatus, serial: int = 1,
    ib_order_id: str | None = None, avg_fill_price=None,
) -> Order:
    trade_repo = TradeRepository(session_factory)
    order_repo = OrderRepository(session_factory)
    trade = trade_repo.create(TradeGroup(
        serial_number=serial, symbol="MSFT", direction="LONG",
        status=TradeStatus.OPEN, opened_at=_now(),
    ))
    return order_repo.create(Order(
        trade_id=trade.id,
        serial_number=serial,
        ib_order_id=ib_order_id,
        leg_type=LegType.ENTRY,
        symbol="MSFT",
        side="BUY",
        security_type=SecurityType.STK,
        qty_requested=Decimal("10"),
        qty_filled=Decimal("0"),
        order_type="MID",
        status=status,
        placed_at=_now(),
        avg_fill_price=avg_fill_price,
    ))


class TestRecoverInFlightOrders:
    def test_no_in_flight_returns_empty(self, session_factory):
        order_repo = OrderRepository(session_factory)
        result = recover_in_flight_orders(order_repo)
        assert result == []

    def test_repricing_order_is_abandoned(self, session_factory):
        _make_order(session_factory, OrderStatus.REPRICING, serial=1)
        order_repo = OrderRepository(session_factory)
        result = recover_in_flight_orders(order_repo)
        assert len(result) == 1
        assert result[0]["previous_status"] == "REPRICING"

        # Verify status was actually updated in DB
        in_states = order_repo.get_in_states([OrderStatus.ABANDONED])
        assert len(in_states) == 1

    def test_amending_order_is_abandoned(self, session_factory):
        _make_order(session_factory, OrderStatus.AMENDING, serial=2)
        order_repo = OrderRepository(session_factory)
        result = recover_in_flight_orders(order_repo)
        assert len(result) == 1
        assert result[0]["previous_status"] == "AMENDING"

    def test_filled_order_not_abandoned(self, session_factory):
        _make_order(session_factory, OrderStatus.FILLED, serial=3)
        order_repo = OrderRepository(session_factory)
        result = recover_in_flight_orders(order_repo)
        assert result == []

    def test_open_order_with_ib_id_not_abandoned(self, session_factory):
        """OPEN order with an ib_order_id may still be live — leave it alone."""
        _make_order(session_factory, OrderStatus.OPEN, serial=4, ib_order_id="123")
        order_repo = OrderRepository(session_factory)
        result = recover_in_flight_orders(order_repo)
        assert result == []

    def test_open_order_without_ib_id_is_abandoned(self, session_factory):
        """OPEN order with no ib_order_id is stale — abandon it."""
        _make_order(session_factory, OrderStatus.OPEN, serial=4)
        order_repo = OrderRepository(session_factory)
        result = recover_in_flight_orders(order_repo)
        assert len(result) == 1
        assert result[0]["previous_status"] == "OPEN"

    def test_pending_order_without_ib_id_is_abandoned(self, session_factory):
        """PENDING order with no ib_order_id was never sent — abandon it."""
        _make_order(session_factory, OrderStatus.PENDING, serial=7)
        order_repo = OrderRepository(session_factory)
        result = recover_in_flight_orders(order_repo)
        assert len(result) == 1
        assert result[0]["previous_status"] == "PENDING"

    def test_pending_order_with_ib_id_not_abandoned(self, session_factory):
        """PENDING order with ib_order_id may still be processing — leave it."""
        _make_order(session_factory, OrderStatus.PENDING, serial=8, ib_order_id="456")
        order_repo = OrderRepository(session_factory)
        result = recover_in_flight_orders(order_repo)
        assert result == []

    def test_multiple_in_flight_all_abandoned(self, session_factory):
        _make_order(session_factory, OrderStatus.REPRICING, serial=5)
        _make_order(session_factory, OrderStatus.AMENDING, serial=6)
        order_repo = OrderRepository(session_factory)
        result = recover_in_flight_orders(order_repo)
        assert len(result) == 2


class TestCloseOrphanedTradeGroups:
    def test_no_orphans(self, session_factory):
        """OPEN trade with a non-terminal order leg is not closed."""
        _make_order(session_factory, OrderStatus.OPEN, serial=1, ib_order_id="10")
        trade_repo = TradeRepository(session_factory)
        order_repo = OrderRepository(session_factory)
        closed = close_orphaned_trade_groups(trade_repo, order_repo)
        assert closed == 0
        assert len(trade_repo.get_open()) == 1

    def test_all_legs_terminal_no_fill_closes_trade(self, session_factory):
        """Trade with all terminal legs and no fills is closed as orphan."""
        _make_order(session_factory, OrderStatus.CANCELED, serial=2)
        trade_repo = TradeRepository(session_factory)
        order_repo = OrderRepository(session_factory)
        closed = close_orphaned_trade_groups(trade_repo, order_repo)
        assert closed == 1
        assert len(trade_repo.get_open()) == 0

    def test_all_legs_terminal_with_fill_not_closed(self, session_factory):
        """Trade with all terminal legs but a fill is NOT auto-closed.

        This represents a real position that needs manual reconciliation.
        """
        _make_order(
            session_factory, OrderStatus.FILLED, serial=3,
            ib_order_id="99", avg_fill_price=Decimal("100.50"),
        )
        trade_repo = TradeRepository(session_factory)
        order_repo = OrderRepository(session_factory)
        closed = close_orphaned_trade_groups(trade_repo, order_repo)
        assert closed == 0
        assert len(trade_repo.get_open()) == 1

    def test_trade_with_no_legs_is_closed(self, session_factory):
        """Trade group with zero order legs is closed."""
        trade_repo = TradeRepository(session_factory)
        trade_repo.create(TradeGroup(
            serial_number=10, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        order_repo = OrderRepository(session_factory)
        closed = close_orphaned_trade_groups(trade_repo, order_repo)
        assert closed == 1


class TestFormatRecoveryWarnings:
    def test_empty_list_returns_empty(self):
        assert format_recovery_warnings([]) == []

    def test_warning_contains_serial(self):
        warnings = format_recovery_warnings([{
            "order_id": "uuid",
            "serial_number": 4,
            "symbol": "MSFT",
            "previous_status": "REPRICING",
            "last_amended_at": None,
        }])
        assert len(warnings) == 1
        assert "#4" in warnings[0]
        assert "MSFT" in warnings[0]
        assert "ABANDONED" in warnings[0]

    def test_warning_with_timestamp(self):
        warnings = format_recovery_warnings([{
            "order_id": "uuid",
            "serial_number": 2,
            "symbol": "AAPL",
            "previous_status": "AMENDING",
            "last_amended_at": datetime(2026, 3, 8, 10, 32, 1, tzinfo=timezone.utc),
        }])
        assert "10:32:01" in warnings[0]
