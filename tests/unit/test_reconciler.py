"""Unit tests for the transaction-based reconciliation.

Verifies:
- Orders present in transactions (non-terminal) but absent from IB open orders
  → RECONCILED row written, WARNING alert emitted
- Orders present in both → no action, no alert
- Orders present in IB but not in transactions → no action
- Empty transactions table → no errors, no alerts
"""
import pytest
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import (
    Base, TransactionAction, TransactionEvent, AlertSeverity,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.data.repository import (
    TradeRepository, RepriceEventRepository,
    ContractRepository, HeartbeatRepository, AlertRepository,
)
from ib_trader.config.context import AppContext
from ib_trader.engine.tracker import OrderTracker
from ib_trader.daemon.reconciler import run_transaction_reconciliation
from tests.conftest import MockIBClient


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def recon_ctx():
    """Create an AppContext with in-memory DB, mock IB, and transactions repo."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    sf = scoped_session(factory)

    mock_ib = MockIBClient()

    return AppContext(
        ib=mock_ib,
        trades=TradeRepository(sf),
        reprice_events=RepriceEventRepository(sf),
        contracts=ContractRepository(sf),
        heartbeats=HeartbeatRepository(sf),
        alerts=AlertRepository(sf),
        tracker=OrderTracker(),
        settings={
            "max_order_size_shares": 10,
            "reconciliation_interval_seconds": 3600,
        },
        account_id="U1234567",
        transactions=TransactionRepository(sf),
    )


def _insert_open_txn(ctx, ib_order_id, symbol="MSFT"):
    """Insert a non-terminal PLACE_ACCEPTED transaction."""
    evt = TransactionEvent(
        ib_order_id=ib_order_id,
        action=TransactionAction.PLACE_ACCEPTED,
        symbol=symbol,
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("1"),
        account_id="U1234567",
        requested_at=_now(),
        is_terminal=False,
    )
    ctx.transactions.insert(evt)


class TestTransactionReconciliation:
    """Tests for run_transaction_reconciliation()."""

    @pytest.mark.asyncio
    async def test_discrepancy_writes_discrepancy_row_and_warning(self, recon_ctx):
        """Order in transactions but not in IB → DISCREPANCY row + WARNING alert."""
        _insert_open_txn(recon_ctx, ib_order_id=1000, symbol="AAPL")

        # Mock IB returns no open orders
        recon_ctx.ib._open_orders_response = []

        result = await run_transaction_reconciliation(recon_ctx)

        assert result["discrepancies"] == 1
        assert 1000 in result["details"]

        # Check DISCREPANCY row was written (non-terminal — does not auto-heal)
        rows = recon_ctx.transactions.get_by_ib_order_id(1000)
        discrepancies = [r for r in rows if r.action == TransactionAction.DISCREPANCY]
        assert len(discrepancies) == 1
        assert discrepancies[0].ib_status == "NOT_FOUND_IN_IB"
        assert discrepancies[0].is_terminal is False

        # Check WARNING alert was created
        alerts = recon_ctx.alerts.get_open()
        assert len(alerts) == 1
        assert alerts[0].severity == AlertSeverity.WARNING
        assert "1000" in alerts[0].message
        assert "AAPL" in alerts[0].message

    @pytest.mark.asyncio
    async def test_order_present_in_both_no_action(self, recon_ctx):
        """Order in both transactions and IB → no action, no alert."""
        _insert_open_txn(recon_ctx, ib_order_id=2000)

        # Mock IB returns this order as open
        async def mock_get_open_orders():
            return [{"ib_order_id": "2000", "symbol": "MSFT", "side": "BUY",
                     "qty": Decimal("1"), "status": "Submitted"}]
        recon_ctx.ib.get_open_orders = mock_get_open_orders

        result = await run_transaction_reconciliation(recon_ctx)

        assert result["discrepancies"] == 0
        assert recon_ctx.alerts.get_open() == []

    @pytest.mark.asyncio
    async def test_external_order_in_ib_no_action(self, recon_ctx):
        """Order in IB but not in transactions → no action."""
        # No transactions inserted
        async def mock_get_open_orders():
            return [{"ib_order_id": "9999", "symbol": "GOOG", "side": "SELL",
                     "qty": Decimal("5"), "status": "Submitted"}]
        recon_ctx.ib.get_open_orders = mock_get_open_orders

        result = await run_transaction_reconciliation(recon_ctx)

        assert result["discrepancies"] == 0
        assert recon_ctx.alerts.get_open() == []

    @pytest.mark.asyncio
    async def test_empty_transactions_no_errors(self, recon_ctx):
        """Empty transactions table → no errors, no alerts."""
        result = await run_transaction_reconciliation(recon_ctx)

        assert result["discrepancies"] == 0
        assert recon_ctx.alerts.get_open() == []

    @pytest.mark.asyncio
    async def test_terminal_order_not_flagged(self, recon_ctx):
        """Terminal orders in transactions are not flagged as discrepancies."""
        # Insert open then terminal
        evt1 = TransactionEvent(
            ib_order_id=3000,
            action=TransactionAction.PLACE_ACCEPTED,
            symbol="TSLA",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("1"),
            account_id="U1234567",
            requested_at=_now(),
            is_terminal=False,
        )
        recon_ctx.transactions.insert(evt1)

        evt2 = TransactionEvent(
            ib_order_id=3000,
            action=TransactionAction.FILLED,
            symbol="TSLA",
            side="BUY",
            order_type="LIMIT",
            quantity=Decimal("1"),
            account_id="U1234567",
            requested_at=_now(),
            is_terminal=True,
        )
        recon_ctx.transactions.insert(evt2)

        result = await run_transaction_reconciliation(recon_ctx)
        assert result["discrepancies"] == 0

    @pytest.mark.asyncio
    async def test_no_transactions_repo(self, recon_ctx):
        """When transactions repo is None, reconciliation is skipped."""
        recon_ctx.transactions = None
        result = await run_transaction_reconciliation(recon_ctx)
        assert result["discrepancies"] == 0
