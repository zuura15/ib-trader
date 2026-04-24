"""Unit tests for data/repository.py.

All tests use an in-memory SQLite database — no file I/O.
"""
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from ib_trader.data.models import (
    TradeGroup, Contract, TradeStatus, TransactionAction, TransactionEvent, LegType,
)
from ib_trader.data.repository import TradeRepository
from ib_trader.data.repositories.transaction_repository import TransactionRepository


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

    def test_add_ib_realized_pnl_sums_across_calls(self, session_factory):
        """Multi-execution closes call add_ib_realized_pnl once per
        CommissionReport. The contributions must accumulate."""
        repo = TradeRepository(session_factory)
        trade = repo.create(TradeGroup(
            serial_number=66, symbol="GLD", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        repo.add_ib_realized_pnl(trade.id, Decimal("5.25"))
        repo.add_ib_realized_pnl(trade.id, Decimal("-1.50"))
        repo.add_ib_realized_pnl(trade.id, Decimal("0.10"))
        fetched = repo.get_by_serial(66)
        assert fetched.ib_realized_pnl == Decimal("3.85")
        # realized_pnl (the bot/close-leg path) stays untouched.
        assert fetched.realized_pnl is None

    def test_add_ib_realized_pnl_does_not_clobber_realized_pnl(self, session_factory):
        """Bot trades write realized_pnl via update_pnl; the IB path
        writes ib_realized_pnl. They must not interact."""
        repo = TradeRepository(session_factory)
        trade = repo.create(TradeGroup(
            serial_number=67, symbol="GLD", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))
        repo.update_pnl(trade.id, Decimal("100.00"), Decimal("1.50"))
        repo.add_ib_realized_pnl(trade.id, Decimal("99.50"))
        fetched = repo.get_by_serial(67)
        assert fetched.realized_pnl == Decimal("100.00")
        assert fetched.ib_realized_pnl == Decimal("99.50")

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


class TestTransactionRepository:
    def _make_trade(self, session_factory, serial=1) -> TradeGroup:
        repo = TradeRepository(session_factory)
        return repo.create(TradeGroup(
            serial_number=serial, symbol="MSFT", direction="LONG",
            status=TradeStatus.OPEN, opened_at=_now(),
        ))

    def test_insert_and_get_by_ib_order_id(self, session_factory):
        """Insert a transaction event and retrieve it by ib_order_id."""
        trade = self._make_trade(session_factory)
        repo = TransactionRepository(session_factory)
        evt = TransactionEvent(
            ib_order_id=1001,
            action=TransactionAction.PLACE_ATTEMPT,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            leg_type=LegType.ENTRY, security_type="STK",
        )
        repo.insert(evt)
        rows = repo.get_by_ib_order_id(1001)
        assert len(rows) == 1
        assert rows[0].symbol == "MSFT"
        assert rows[0].action == TransactionAction.PLACE_ATTEMPT

    def test_get_latest_by_ib_order_id(self, session_factory):
        """get_latest_by_ib_order_id returns the most recent row."""
        repo = TransactionRepository(session_factory)
        repo.insert(TransactionEvent(
            ib_order_id=2001, action=TransactionAction.PLACE_ATTEMPT,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("5"), account_id="U1234567",
            requested_at=_now(),
        ))
        repo.insert(TransactionEvent(
            ib_order_id=2001, action=TransactionAction.PLACE_ACCEPTED,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("5"), account_id="U1234567",
            requested_at=_now(),
        ))
        latest = repo.get_latest_by_ib_order_id(2001)
        assert latest is not None
        assert latest.action == TransactionAction.PLACE_ACCEPTED

    def test_get_latest_by_ib_order_id_not_found(self, session_factory):
        """Returns None for unknown ib_order_id."""
        repo = TransactionRepository(session_factory)
        assert repo.get_latest_by_ib_order_id(99999) is None

    def test_get_open_orders_excludes_terminal(self, session_factory):
        """get_open_orders excludes orders whose latest row is terminal."""
        repo = TransactionRepository(session_factory)
        repo.insert(TransactionEvent(
            ib_order_id=3001, action=TransactionAction.PLACE_ACCEPTED,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), is_terminal=False,
        ))
        repo.insert(TransactionEvent(
            ib_order_id=3001, action=TransactionAction.FILLED,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), is_terminal=True,
        ))
        assert repo.get_open_orders() == []

    def test_get_for_trade(self, session_factory):
        """get_for_trade returns all transactions for a trade group."""
        trade = self._make_trade(session_factory)
        repo = TransactionRepository(session_factory)
        for action in [TransactionAction.PLACE_ATTEMPT, TransactionAction.PLACE_ACCEPTED]:
            repo.insert(TransactionEvent(
                ib_order_id=4001, action=action,
                symbol="MSFT", side="BUY", order_type="MID",
                quantity=Decimal("1"), account_id="U1234567",
                requested_at=_now(), trade_id=trade.id,
            ))
        rows = repo.get_for_trade(trade.id)
        assert len(rows) == 2

    def test_get_entry_fill(self, session_factory):
        """get_entry_fill returns the FILLED ENTRY transaction."""
        trade = self._make_trade(session_factory, serial=2)
        repo = TransactionRepository(session_factory)
        repo.insert(TransactionEvent(
            ib_order_id=5001, action=TransactionAction.FILLED,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            leg_type=LegType.ENTRY, is_terminal=True,
            ib_filled_qty=Decimal("10"), ib_avg_fill_price=Decimal("412.33"),
            commission=Decimal("1.00"),
        ))
        entry = repo.get_entry_fill(trade.id)
        assert entry is not None
        assert entry.ib_filled_qty == Decimal("10")
        assert entry.ib_avg_fill_price == Decimal("412.33")
        assert entry.commission == Decimal("1.00")

    def test_get_entry_fill_partial(self, session_factory):
        """get_entry_fill falls back to PARTIAL_FILL when no full FILLED exists."""
        trade = self._make_trade(session_factory, serial=20)
        repo = TransactionRepository(session_factory)
        repo.insert(TransactionEvent(
            ib_order_id=5050, action=TransactionAction.PARTIAL_FILL,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            leg_type=LegType.ENTRY,
            ib_filled_qty=Decimal("3"), ib_avg_fill_price=Decimal("100.00"),
        ))
        entry = repo.get_entry_fill(trade.id)
        assert entry is not None
        assert entry.action == TransactionAction.PARTIAL_FILL
        assert entry.ib_filled_qty == Decimal("3")

    def test_get_for_trade_serial(self, session_factory):
        """get_for_trade_serial returns all transactions for a serial number."""
        trade = self._make_trade(session_factory, serial=30)
        repo = TransactionRepository(session_factory)
        repo.insert(TransactionEvent(
            ib_order_id=6001, action=TransactionAction.PLACE_ATTEMPT,
            symbol="AAPL", side="BUY", order_type="MID",
            quantity=Decimal("5"), account_id="U1234567",
            requested_at=_now(), trade_serial=30, trade_id=trade.id,
        ))
        repo.insert(TransactionEvent(
            ib_order_id=6001, action=TransactionAction.FILLED,
            symbol="AAPL", side="BUY", order_type="MID",
            quantity=Decimal("5"), account_id="U1234567",
            requested_at=_now(), trade_serial=30, trade_id=trade.id,
        ))
        rows = repo.get_for_trade_serial(30)
        assert len(rows) == 2

    def test_get_filled_legs_includes_partial(self, session_factory):
        """get_filled_legs returns both FILLED and PARTIAL_FILL transactions."""
        trade = self._make_trade(session_factory, serial=31)
        repo = TransactionRepository(session_factory)
        repo.insert(TransactionEvent(
            ib_order_id=7001, action=TransactionAction.FILLED,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            leg_type=LegType.ENTRY, ib_filled_qty=Decimal("10"),
        ))
        repo.insert(TransactionEvent(
            ib_order_id=7002, action=TransactionAction.PARTIAL_FILL,
            symbol="MSFT", side="SELL", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            leg_type=LegType.CLOSE, ib_filled_qty=Decimal("5"),
        ))
        legs = repo.get_filled_legs(trade.id)
        assert len(legs) == 2
        actions = {l.action for l in legs}
        assert TransactionAction.FILLED in actions
        assert TransactionAction.PARTIAL_FILL in actions

    def test_get_open_for_trade(self, session_factory):
        """get_open_for_trade returns non-terminal legs for a trade."""
        trade = self._make_trade(session_factory, serial=32)
        repo = TransactionRepository(session_factory)
        # Open leg
        repo.insert(TransactionEvent(
            ib_order_id=8001, action=TransactionAction.PLACE_ACCEPTED,
            symbol="MSFT", side="SELL", order_type="LIMIT",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            leg_type=LegType.PROFIT_TAKER, is_terminal=False,
        ))
        # Terminal leg
        repo.insert(TransactionEvent(
            ib_order_id=8002, action=TransactionAction.FILLED,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            leg_type=LegType.ENTRY, is_terminal=True,
        ))
        open_legs = repo.get_open_for_trade(trade.id)
        assert len(open_legs) == 1
        assert open_legs[0].ib_order_id == 8001

    def test_get_trade_leg_summary(self, session_factory):
        """get_trade_leg_summary returns latest transaction per ib_order_id."""
        trade = self._make_trade(session_factory, serial=33)
        repo = TransactionRepository(session_factory)
        # Two events for same ib_order_id — should only get latest
        repo.insert(TransactionEvent(
            ib_order_id=9001, action=TransactionAction.PLACE_ACCEPTED,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            leg_type=LegType.ENTRY, is_terminal=False,
        ))
        repo.insert(TransactionEvent(
            ib_order_id=9001, action=TransactionAction.FILLED,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            leg_type=LegType.ENTRY, is_terminal=True,
        ))
        summary = repo.get_trade_leg_summary(trade.id)
        assert len(summary) == 1
        assert summary[0].action == TransactionAction.FILLED

    def test_has_unconfirmed_placements(self, session_factory):
        """has_unconfirmed_placements detects PLACE_ATTEMPT without PLACE_ACCEPTED."""
        trade = self._make_trade(session_factory, serial=34)
        repo = TransactionRepository(session_factory)
        repo.insert(TransactionEvent(
            ib_order_id=10001, action=TransactionAction.PLACE_ATTEMPT,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            correlation_id="corr-1",
        ))
        assert repo.has_unconfirmed_placements(trade.id) is True

        # Add PLACE_ACCEPTED — should now be confirmed
        repo.insert(TransactionEvent(
            ib_order_id=10001, action=TransactionAction.PLACE_ACCEPTED,
            symbol="MSFT", side="BUY", order_type="MID",
            quantity=Decimal("10"), account_id="U1234567",
            requested_at=_now(), trade_id=trade.id,
            correlation_id="corr-1",
        ))
        assert repo.has_unconfirmed_placements(trade.id) is False

    def test_get_by_correlation_id(self, session_factory):
        """get_by_correlation_id returns all transactions with a given correlation_id."""
        repo = TransactionRepository(session_factory)
        corr = "test-corr-abc"
        repo.insert(TransactionEvent(
            ib_order_id=11001, action=TransactionAction.PLACE_ATTEMPT,
            symbol="AAPL", side="BUY", order_type="MID",
            quantity=Decimal("5"), account_id="U1234567",
            requested_at=_now(), correlation_id=corr,
        ))
        repo.insert(TransactionEvent(
            ib_order_id=11001, action=TransactionAction.PLACE_ACCEPTED,
            symbol="AAPL", side="BUY", order_type="MID",
            quantity=Decimal("5"), account_id="U1234567",
            requested_at=_now(), correlation_id=corr,
        ))
        rows = repo.get_by_correlation_id(corr)
        assert len(rows) == 2
        assert all(r.correlation_id == corr for r in rows)


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
