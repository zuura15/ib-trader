"""Unit tests for crash recovery logic.

Tests use TransactionEvent rows instead of Order rows. The recovery functions
now operate on TransactionRepository and TradeRepository.
"""
from datetime import datetime, timezone
from decimal import Decimal
import uuid

from ib_trader.data.models import (
    TradeGroup, TradeStatus, TransactionAction, TransactionEvent, LegType,
)
from ib_trader.data.repository import TradeRepository
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.engine.recovery import (
    recover_in_flight_orders, format_recovery_warnings, close_orphaned_trade_groups,
)


def _now():
    return datetime.now(timezone.utc)


def _make_trade_with_txns(
    session_factory, actions: list[TransactionAction], serial: int = 1,
    ib_order_id: int | None = 1000, has_fill: bool = False,
) -> TradeGroup:
    """Create a trade group and insert TransactionEvent rows for it.

    Args:
        session_factory: SQLAlchemy session factory.
        actions: List of TransactionAction values to insert as rows.
        serial: Trade serial number.
        ib_order_id: IB order ID (or None).
        has_fill: If True, the FILLED action gets fill details populated.
    """
    trade_repo = TradeRepository(session_factory)
    txn_repo = TransactionRepository(session_factory)
    correlation_id = str(uuid.uuid4())

    trade = trade_repo.create(TradeGroup(
        serial_number=serial, symbol="MSFT", direction="LONG",
        status=TradeStatus.OPEN, opened_at=_now(),
    ))

    for action in actions:
        is_terminal = action in (
            TransactionAction.FILLED, TransactionAction.CANCELLED,
            TransactionAction.PLACE_REJECTED, TransactionAction.ERROR_TERMINAL,
        )
        evt = TransactionEvent(
            ib_order_id=ib_order_id,
            action=action,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), is_terminal=is_terminal,
            trade_id=trade.id, leg_type=LegType.ENTRY,
            correlation_id=correlation_id, security_type="STK",
        )
        if has_fill and action == TransactionAction.FILLED:
            evt.ib_filled_qty = Decimal("10")
            evt.ib_avg_fill_price = Decimal("100.50")
        txn_repo.insert(evt)

    return trade


class TestRecoverInFlightOrders:
    def test_no_in_flight_returns_empty(self, session_factory):
        """No open trades returns empty list."""
        txn_repo = TransactionRepository(session_factory)
        trade_repo = TradeRepository(session_factory)
        result = recover_in_flight_orders(txn_repo, trade_repo)
        assert result == []

    def test_unconfirmed_placement_is_abandoned(self, session_factory):
        """Trade with PLACE_ATTEMPT but no PLACE_ACCEPTED is marked CLOSED."""
        _make_trade_with_txns(
            session_factory,
            actions=[TransactionAction.PLACE_ATTEMPT],
            serial=1,
        )
        txn_repo = TransactionRepository(session_factory)
        trade_repo = TradeRepository(session_factory)
        result = recover_in_flight_orders(txn_repo, trade_repo)
        assert len(result) == 1
        assert result[0]["symbol"] == "MSFT"
        assert result[0]["serial_number"] == 1

        # Verify trade was closed in DB
        assert len(trade_repo.get_open()) == 0

    def test_confirmed_placement_not_abandoned(self, session_factory):
        """Trade with PLACE_ATTEMPT + PLACE_ACCEPTED is not abandoned."""
        _make_trade_with_txns(
            session_factory,
            actions=[TransactionAction.PLACE_ATTEMPT, TransactionAction.PLACE_ACCEPTED],
            serial=2,
        )
        txn_repo = TransactionRepository(session_factory)
        trade_repo = TradeRepository(session_factory)
        result = recover_in_flight_orders(txn_repo, trade_repo)
        assert result == []
        assert len(trade_repo.get_open()) == 1

    def test_filled_order_not_abandoned(self, session_factory):
        """Trade with a terminal FILLED event is not abandoned."""
        _make_trade_with_txns(
            session_factory,
            actions=[TransactionAction.PLACE_ATTEMPT, TransactionAction.PLACE_ACCEPTED,
                     TransactionAction.FILLED],
            serial=3, has_fill=True,
        )
        txn_repo = TransactionRepository(session_factory)
        trade_repo = TradeRepository(session_factory)
        result = recover_in_flight_orders(txn_repo, trade_repo)
        assert result == []

    def test_multiple_unconfirmed_all_abandoned(self, session_factory):
        """Multiple trades with unconfirmed placements are all marked CLOSED."""
        _make_trade_with_txns(
            session_factory,
            actions=[TransactionAction.PLACE_ATTEMPT],
            serial=5,
        )
        _make_trade_with_txns(
            session_factory,
            actions=[TransactionAction.PLACE_ATTEMPT],
            serial=6, ib_order_id=2000,
        )
        txn_repo = TransactionRepository(session_factory)
        trade_repo = TradeRepository(session_factory)
        result = recover_in_flight_orders(txn_repo, trade_repo)
        assert len(result) == 2
        assert len(trade_repo.get_open()) == 0

    def test_closed_trade_not_checked(self, session_factory):
        """Already-CLOSED trades are not checked for recovery."""
        trade_repo = TradeRepository(session_factory)
        txn_repo = TransactionRepository(session_factory)
        trade = trade_repo.create(TradeGroup(
            serial_number=7, symbol="MSFT", direction="LONG",
            status=TradeStatus.CLOSED, opened_at=_now(),
        ))
        txn_repo.insert(TransactionEvent(
            ib_order_id=3000, action=TransactionAction.PLACE_ATTEMPT,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            correlation_id=str(uuid.uuid4()),
        ))
        result = recover_in_flight_orders(txn_repo, trade_repo)
        assert result == []


class TestCloseOrphanedTradeGroups:
    def test_no_orphans(self, session_factory):
        """OPEN trade with a non-terminal transaction leg is not closed."""
        _make_trade_with_txns(
            session_factory,
            actions=[TransactionAction.PLACE_ATTEMPT, TransactionAction.PLACE_ACCEPTED],
            serial=1,
        )
        trade_repo = TradeRepository(session_factory)
        txn_repo = TransactionRepository(session_factory)
        closed = close_orphaned_trade_groups(trade_repo, txn_repo)
        assert closed == 0
        assert len(trade_repo.get_open()) == 1

    def test_all_legs_terminal_no_fill_closes_trade(self, session_factory):
        """Trade with all terminal legs and no fills is closed as orphan."""
        _make_trade_with_txns(
            session_factory,
            actions=[TransactionAction.PLACE_ATTEMPT, TransactionAction.CANCELLED],
            serial=2,
        )
        trade_repo = TradeRepository(session_factory)
        txn_repo = TransactionRepository(session_factory)
        closed = close_orphaned_trade_groups(trade_repo, txn_repo)
        assert closed == 1
        assert len(trade_repo.get_open()) == 0

    def test_all_legs_terminal_with_fill_not_closed(self, session_factory):
        """Trade with all terminal legs but a fill is NOT auto-closed.

        This represents a real position that needs manual reconciliation.
        """
        _make_trade_with_txns(
            session_factory,
            actions=[TransactionAction.PLACE_ATTEMPT, TransactionAction.PLACE_ACCEPTED,
                     TransactionAction.FILLED],
            serial=3, has_fill=True,
        )
        trade_repo = TradeRepository(session_factory)
        txn_repo = TransactionRepository(session_factory)
        closed = close_orphaned_trade_groups(trade_repo, txn_repo)
        assert closed == 0
        assert len(trade_repo.get_open()) == 1

    def test_trade_with_no_legs_is_closed(self, session_factory):
        """Trade group with zero transaction legs is closed."""
        trade_repo = TradeRepository(session_factory)
        trade_repo.create(TradeGroup(
            serial_number=10, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        txn_repo = TransactionRepository(session_factory)
        closed = close_orphaned_trade_groups(trade_repo, txn_repo)
        assert closed == 1


class TestFormatRecoveryWarnings:
    def test_empty_list_returns_empty(self):
        assert format_recovery_warnings([]) == []

    def test_warning_contains_serial(self):
        warnings = format_recovery_warnings([{
            "trade_id": "uuid",
            "serial_number": 4,
            "symbol": "MSFT",
        }])
        assert len(warnings) == 1
        assert "#4" in warnings[0]
        assert "MSFT" in warnings[0]

    def test_warning_contains_symbol(self):
        warnings = format_recovery_warnings([{
            "trade_id": "uuid",
            "serial_number": 2,
            "symbol": "AAPL",
        }])
        assert len(warnings) == 1
        assert "AAPL" in warnings[0]
