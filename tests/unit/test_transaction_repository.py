"""Unit tests for TransactionRepository.

Verifies:
- insert() writes a row and it is retrievable
- get_open_orders() returns only the most recent row per ib_order_id
  where is_terminal = False
- get_open_orders() excludes orders where the most recent row has
  is_terminal = True
- get_by_ib_order_id() returns all rows in ascending requested_at order
"""
import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import Base, TransactionAction, TransactionEvent
from ib_trader.data.repositories.transaction_repository import TransactionRepository


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def txn_repo():
    """Create an in-memory TransactionRepository."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session_factory = scoped_session(factory)
    return TransactionRepository(session_factory)


def _make_event(
    ib_order_id=1000,
    action=TransactionAction.PLACE_ATTEMPT,
    symbol="MSFT",
    side="BUY",
    order_type="LIMIT",
    quantity=Decimal("1"),
    is_terminal=False,
    requested_at=None,
    **kwargs,
) -> TransactionEvent:
    """Helper to construct a TransactionEvent."""
    return TransactionEvent(
        ib_order_id=ib_order_id,
        action=action,
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        account_id="U1234567",
        requested_at=requested_at or _now(),
        is_terminal=is_terminal,
        **kwargs,
    )


class TestInsert:
    """Tests for TransactionRepository.insert()."""

    def test_insert_and_retrieve(self, txn_repo):
        """insert() writes a row and it is retrievable by ib_order_id."""
        evt = _make_event(ib_order_id=1001)
        txn_repo.insert(evt)

        rows = txn_repo.get_by_ib_order_id(1001)
        assert len(rows) == 1
        assert rows[0].ib_order_id == 1001
        assert rows[0].symbol == "MSFT"
        assert rows[0].action == TransactionAction.PLACE_ATTEMPT

    def test_insert_multiple_rows_same_order(self, txn_repo):
        """Multiple rows can be inserted for the same ib_order_id."""
        txn_repo.insert(_make_event(ib_order_id=2000, action=TransactionAction.PLACE_ATTEMPT))
        txn_repo.insert(_make_event(ib_order_id=2000, action=TransactionAction.PLACE_ACCEPTED))
        txn_repo.insert(_make_event(ib_order_id=2000, action=TransactionAction.FILLED, is_terminal=True))

        rows = txn_repo.get_by_ib_order_id(2000)
        assert len(rows) == 3

    def test_insert_null_ib_order_id(self, txn_repo):
        """Rows with ib_order_id=None can be inserted."""
        evt = _make_event(ib_order_id=None)
        txn_repo.insert(evt)
        # Should not appear in get_open_orders (no ib_order_id)
        assert txn_repo.get_open_orders() == []


class TestGetOpenOrders:
    """Tests for TransactionRepository.get_open_orders()."""

    def test_returns_most_recent_non_terminal(self, txn_repo):
        """Returns only the most recent row per ib_order_id where is_terminal=False."""
        t1 = _now()
        t2 = t1 + timedelta(seconds=1)
        txn_repo.insert(_make_event(ib_order_id=3000, action=TransactionAction.PLACE_ATTEMPT, requested_at=t1))
        txn_repo.insert(_make_event(ib_order_id=3000, action=TransactionAction.PLACE_ACCEPTED, requested_at=t2))

        result = txn_repo.get_open_orders()
        assert len(result) == 1
        assert result[0].action == TransactionAction.PLACE_ACCEPTED

    def test_excludes_terminal_orders(self, txn_repo):
        """Excludes orders whose most recent row has is_terminal=True."""
        t1 = _now()
        t2 = t1 + timedelta(seconds=1)
        txn_repo.insert(_make_event(ib_order_id=4000, action=TransactionAction.PLACE_ACCEPTED, requested_at=t1))
        txn_repo.insert(_make_event(ib_order_id=4000, action=TransactionAction.FILLED, is_terminal=True, requested_at=t2))

        result = txn_repo.get_open_orders()
        assert len(result) == 0

    def test_multiple_orders_mixed(self, txn_repo):
        """Returns only open orders when mix of open and terminal exists."""
        t1 = _now()
        # Order 5000: open
        txn_repo.insert(_make_event(ib_order_id=5000, action=TransactionAction.PLACE_ACCEPTED, requested_at=t1))
        # Order 5001: terminal
        txn_repo.insert(_make_event(ib_order_id=5001, action=TransactionAction.PLACE_ACCEPTED, requested_at=t1))
        txn_repo.insert(_make_event(ib_order_id=5001, action=TransactionAction.CANCELLED, is_terminal=True,
                                     requested_at=t1 + timedelta(seconds=1)))

        result = txn_repo.get_open_orders()
        assert len(result) == 1
        assert result[0].ib_order_id == 5000

    def test_empty_table(self, txn_repo):
        """Returns empty list for empty table."""
        assert txn_repo.get_open_orders() == []

    def test_excludes_null_ib_order_id(self, txn_repo):
        """Rows with null ib_order_id are excluded."""
        txn_repo.insert(_make_event(ib_order_id=None))
        assert txn_repo.get_open_orders() == []


class TestGetByIBOrderId:
    """Tests for TransactionRepository.get_by_ib_order_id()."""

    def test_returns_all_rows_ascending(self, txn_repo):
        """Returns all rows for a given ib_order_id in ascending requested_at order."""
        t1 = _now()
        t2 = t1 + timedelta(seconds=1)
        t3 = t1 + timedelta(seconds=2)
        txn_repo.insert(_make_event(ib_order_id=6000, action=TransactionAction.PLACE_ATTEMPT, requested_at=t1))
        txn_repo.insert(_make_event(ib_order_id=6000, action=TransactionAction.PLACE_ACCEPTED, requested_at=t2))
        txn_repo.insert(_make_event(ib_order_id=6000, action=TransactionAction.FILLED, is_terminal=True, requested_at=t3))

        rows = txn_repo.get_by_ib_order_id(6000)
        assert len(rows) == 3
        assert rows[0].action == TransactionAction.PLACE_ATTEMPT
        assert rows[1].action == TransactionAction.PLACE_ACCEPTED
        assert rows[2].action == TransactionAction.FILLED

    def test_no_rows(self, txn_repo):
        """Returns empty list for unknown ib_order_id."""
        assert txn_repo.get_by_ib_order_id(9999) == []

    def test_only_matching_order(self, txn_repo):
        """Returns only rows for the specified ib_order_id, not others."""
        txn_repo.insert(_make_event(ib_order_id=7000))
        txn_repo.insert(_make_event(ib_order_id=7001))

        rows = txn_repo.get_by_ib_order_id(7000)
        assert len(rows) == 1
        assert rows[0].ib_order_id == 7000
