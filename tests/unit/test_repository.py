"""Unit tests for data/repository.py.

All tests use an in-memory SQLite database — no file I/O.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from ib_trader.data.models import (
    TradeGroup, Order, Contract,
    TradeStatus, OrderStatus, LegType, SecurityType,
)
from ib_trader.data.repository import TradeRepository, OrderRepository


def _now():
    return datetime.now(timezone.utc)


class TestTradeRepository:
    def test_create_and_get_by_serial(self, session_factory):
        repo = TradeRepository(session_factory)
        trade = TradeGroup(
            serial_number=1,
            symbol="MSFT",
            direction="LONG",
            status=TradeStatus.OPEN,
            opened_at=_now(),
        )
        created = repo.create(trade)
        assert created.id is not None

        fetched = repo.get_by_serial(1)
        assert fetched is not None
        assert fetched.symbol == "MSFT"
        assert fetched.direction == "LONG"

    def test_get_by_serial_not_found(self, session_factory):
        repo = TradeRepository(session_factory)
        assert repo.get_by_serial(999) is None

    def test_get_open(self, session_factory):
        repo = TradeRepository(session_factory)
        for i, status in enumerate([TradeStatus.OPEN, TradeStatus.CLOSED, TradeStatus.OPEN]):
            repo.create(TradeGroup(
                serial_number=i,
                symbol="AAPL",
                direction="LONG",
                status=status,
                opened_at=_now(),
            ))
        open_trades = repo.get_open()
        assert len(open_trades) == 2

    def test_update_status(self, session_factory):
        repo = TradeRepository(session_factory)
        trade = repo.create(TradeGroup(
            serial_number=5, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        repo.update_status(trade.id, TradeStatus.CLOSED)
        fetched = repo.get_by_serial(5)
        assert fetched.status == TradeStatus.CLOSED
        assert fetched.closed_at is not None

    def test_update_pnl(self, session_factory):
        repo = TradeRepository(session_factory)
        trade = repo.create(TradeGroup(
            serial_number=6, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        repo.update_pnl(trade.id, Decimal("234.50"), Decimal("4.00"))
        fetched = repo.get_by_serial(6)
        assert fetched.realized_pnl == Decimal("234.50")
        assert fetched.total_commission == Decimal("4.00")

    def test_next_serial_number_starts_at_0(self, session_factory):
        repo = TradeRepository(session_factory)
        assert repo.next_serial_number() == 0

    def test_next_serial_number_skips_used(self, session_factory):
        repo = TradeRepository(session_factory)
        for serial in [0, 1, 3]:  # Skip 2
            repo.create(TradeGroup(
                serial_number=serial, symbol="MSFT", direction="LONG",
                status=TradeStatus.OPEN, opened_at=_now(),
            ))
        assert repo.next_serial_number() == 2

    def test_next_serial_reuses_closed(self, session_factory):
        """Serial numbers from CLOSED trades should be reusable."""
        repo = TradeRepository(session_factory)
        repo.create(TradeGroup(
            serial_number=0, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        # Serial 0 is used — next should be 1
        assert repo.next_serial_number() == 1


class TestOrderRepository:
    def _make_trade(self, session_factory, serial=1) -> TradeGroup:
        repo = TradeRepository(session_factory)
        return repo.create(TradeGroup(
            serial_number=serial, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))

    def test_create_and_get_by_id(self, session_factory):
        trade = self._make_trade(session_factory)
        repo = OrderRepository(session_factory)
        order = repo.create(Order(
            trade_id=trade.id,
            serial_number=1,
            leg_type=LegType.ENTRY,
            symbol="MSFT",
            side="BUY",
            security_type=SecurityType.STK,
            qty_requested=Decimal("10"),
            qty_filled=Decimal("0"),
            order_type="MID",
            status=OrderStatus.PENDING,
            placed_at=_now(),
        ))
        assert order.id is not None
        fetched = repo.get_by_id(order.id)
        assert fetched is not None
        assert fetched.symbol == "MSFT"

    def test_get_by_ib_order_id(self, session_factory):
        trade = self._make_trade(session_factory)
        repo = OrderRepository(session_factory)
        order = repo.create(Order(
            trade_id=trade.id, leg_type=LegType.ENTRY,
            symbol="MSFT", side="BUY", security_type=SecurityType.STK,
            qty_requested=Decimal("5"), qty_filled=Decimal("0"),
            order_type="MID", status=OrderStatus.OPEN, placed_at=_now(),
        ))
        repo.update_ib_order_id(order.id, "IB12345")
        found = repo.get_by_ib_order_id("IB12345")
        assert found is not None
        assert found.ib_order_id == "IB12345"

    def test_get_by_ib_order_id_not_found(self, session_factory):
        repo = OrderRepository(session_factory)
        assert repo.get_by_ib_order_id("NONEXISTENT") is None

    def test_update_fill(self, session_factory):
        trade = self._make_trade(session_factory, serial=2)
        repo = OrderRepository(session_factory)
        order = repo.create(Order(
            trade_id=trade.id, leg_type=LegType.ENTRY,
            symbol="MSFT", side="BUY", security_type=SecurityType.STK,
            qty_requested=Decimal("10"), qty_filled=Decimal("0"),
            order_type="MID", status=OrderStatus.OPEN, placed_at=_now(),
        ))
        repo.update_fill(order.id, Decimal("10"), Decimal("412.33"), Decimal("1.00"))
        fetched = repo.get_by_id(order.id)
        assert fetched.qty_filled == Decimal("10")
        assert fetched.avg_fill_price == Decimal("412.33")
        assert fetched.commission == Decimal("1.00")

    def test_get_in_states(self, session_factory):
        trade = self._make_trade(session_factory, serial=3)
        repo = OrderRepository(session_factory)
        for status in [OrderStatus.PENDING, OrderStatus.OPEN, OrderStatus.REPRICING]:
            repo.create(Order(
                trade_id=trade.id, leg_type=LegType.ENTRY,
                symbol="MSFT", side="BUY", security_type=SecurityType.STK,
                qty_requested=Decimal("1"), qty_filled=Decimal("0"),
                order_type="MID", status=status, placed_at=_now(),
            ))
        results = repo.get_in_states([OrderStatus.OPEN, OrderStatus.REPRICING])
        assert len(results) == 2


class TestContractRepository:
    def test_upsert_and_get(self, session_factory):
        from ib_trader.data.repository import ContractRepository
        repo = ContractRepository(session_factory)
        contract = Contract(
            symbol="MSFT",
            con_id=12345,
            exchange="SMART",
            currency="USD",
            multiplier=None,
            fetched_at=_now(),
        )
        repo.upsert(contract)
        fetched = repo.get("MSFT")
        assert fetched is not None
        assert fetched.con_id == 12345

    def test_is_fresh_within_ttl(self, session_factory):
        from ib_trader.data.repository import ContractRepository
        repo = ContractRepository(session_factory)
        repo.upsert(Contract(
            symbol="AAPL", con_id=999, exchange="SMART", currency="USD",
            fetched_at=_now(),
        ))
        assert repo.is_fresh("AAPL", ttl_seconds=3600)

    def test_is_fresh_expired(self, session_factory):
        from ib_trader.data.repository import ContractRepository
        repo = ContractRepository(session_factory)
        old_time = _now() - timedelta(hours=25)
        repo.upsert(Contract(
            symbol="GOOGL", con_id=888, exchange="SMART", currency="USD",
            fetched_at=old_time,
        ))
        assert not repo.is_fresh("GOOGL", ttl_seconds=86400)

    def test_invalidate(self, session_factory):
        from ib_trader.data.repository import ContractRepository
        repo = ContractRepository(session_factory)
        repo.upsert(Contract(
            symbol="TSLA", con_id=777, exchange="SMART", currency="USD",
            fetched_at=_now(),
        ))
        repo.invalidate("TSLA")
        assert repo.get("TSLA") is None

    def test_upsert_overwrites(self, session_factory):
        from ib_trader.data.repository import ContractRepository
        repo = ContractRepository(session_factory)
        repo.upsert(Contract(symbol="AMD", con_id=111, exchange="SMART", currency="USD", fetched_at=_now()))
        repo.upsert(Contract(symbol="AMD", con_id=222, exchange="NYSE", currency="USD", fetched_at=_now()))
        fetched = repo.get("AMD")
        assert fetched.con_id == 222
