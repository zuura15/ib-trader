"""Unit tests for the commission reconciler.

Covers:
  - BotTradeRepository.find_undercommissioned_trades — per-symbol
    threshold + ACTIVE_STATES bot skip.
  - BotTradeRepository.update_commission_if_higher — idempotent.
  - daemon.reconciler.run_commission_reconciliation end-to-end:
    transactions-sourced backfill writes the higher value,
    WARNING fires for stragglers past the 24h horizon.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import (
    Base, BotTrade, TransactionAction, TransactionEvent,
)
from ib_trader.data.repositories.bot_trade_repository import (
    BotTradeRepository,
)
from ib_trader.data.repositories.transaction_repository import (
    TransactionRepository,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def session_factory():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return scoped_session(factory)


@pytest.fixture
def repo(session_factory):
    return BotTradeRepository(session_factory)


@pytest.fixture
def txn_repo(session_factory):
    return TransactionRepository(session_factory)


def _insert_trade(repo, *, symbol="MGCM6", commission=Decimal("0"),
                   exit_time=None, entry_serial=None, exit_serial=None,
                   bot_id="chart-bot-1"):
    s = repo._session()
    exit_t = exit_time or _now()
    row = BotTrade(
        id=str(uuid.uuid4()),
        bot_id=bot_id,
        bot_name="test",
        symbol=symbol,
        direction="LONG",
        entry_price=Decimal("4540"),
        entry_qty=Decimal("1"),
        entry_time=exit_t - timedelta(seconds=30),
        exit_price=Decimal("4542"),
        exit_qty=Decimal("1"),
        exit_time=exit_t,
        realized_pnl=Decimal("20"),
        commission=commission,
        trail_reset_count=0,
        duration_seconds=30,
        entry_serial=entry_serial,
        exit_serial=exit_serial,
        created_at=exit_t,
    )
    s.add(row)
    s.commit()
    return row


def _insert_txn(txn_repo, *, ib_order_id, trade_serial, commission,
                 requested_at=None):
    evt = TransactionEvent(
        ib_order_id=ib_order_id,
        action=TransactionAction.FILLED,
        symbol="MGCM6",
        side="BUY",
        order_type="LIMIT",
        quantity=Decimal("1"),
        account_id="U1",
        trade_serial=trade_serial,
        commission=commission,
        requested_at=requested_at or _now(),
        is_terminal=True,
    )
    txn_repo.insert(evt)


class TestFindUndercommissioned:
    def test_returns_rows_below_per_symbol_floor(self, repo):
        # MGC floor is $1.94 — a row at $0 is undercommissioned.
        _insert_trade(repo, symbol="MGCM6", commission=Decimal("0"))
        # MES floor is $1.24 — $1.94 is OVER the MES floor, no candidate.
        _insert_trade(repo, symbol="MESM6", commission=Decimal("1.94"))
        candidates = repo.find_undercommissioned_trades(24.0)
        assert len(candidates) == 1
        assert candidates[0].symbol == "MGCM6"

    def test_skips_bots_in_active_state(self, repo):
        _insert_trade(repo, symbol="MGCM6", commission=Decimal("0"),
                       bot_id="chart-bot-1")
        _insert_trade(repo, symbol="MGCM6", commission=Decimal("0"),
                       bot_id="chart-bot-2")
        result = repo.find_undercommissioned_trades(
            24.0, skip_bot_ids={"chart-bot-1"},
        )
        assert len(result) == 1
        assert result[0].bot_id == "chart-bot-2"

    def test_ignores_unknown_symbols(self, repo):
        """A symbol with no entry in ROUND_TRIP_MIN gets no threshold
        and is excluded from the candidate set (commission >= 0
        always passes)."""
        _insert_trade(repo, symbol="UNKNOWN", commission=Decimal("0"))
        assert repo.find_undercommissioned_trades(24.0) == []

    def test_excludes_rows_outside_window(self, repo):
        old = _now() - timedelta(hours=48)
        _insert_trade(repo, symbol="MGCM6", commission=Decimal("0"),
                       exit_time=old)
        assert repo.find_undercommissioned_trades(24.0) == []


class TestUpdateCommissionIfHigher:
    def test_writes_when_strictly_higher(self, repo):
        row = _insert_trade(repo, commission=Decimal("0.97"))
        assert repo.update_commission_if_higher(row.id, Decimal("1.94")) is True
        # Re-fetch to confirm.
        fresh = repo._session().query(BotTrade).filter(BotTrade.id == row.id).first()
        assert fresh.commission == Decimal("1.94")

    def test_skips_when_equal_or_lower(self, repo):
        """Idempotent: a sweep that finds the same value shouldn't
        rewrite the row (avoids spurious BOT_TRADE_COMMISSION_BACKFILLED
        log spam on every reconciler tick once the row is settled)."""
        row = _insert_trade(repo, commission=Decimal("1.94"))
        assert repo.update_commission_if_higher(row.id, Decimal("1.94")) is False
        assert repo.update_commission_if_higher(row.id, Decimal("0.50")) is False
        fresh = repo._session().query(BotTrade).filter(BotTrade.id == row.id).first()
        assert fresh.commission == Decimal("1.94")

    def test_missing_row_returns_false(self, repo):
        assert (
            repo.update_commission_if_higher("nonexistent", Decimal("1.0"))
            is False
        )


class TestRunCommissionReconciliation:
    """End-to-end: empty bot_trade.commission + populated transactions
    → reconciler backfills the row from the transactions sum.

    AppContext is mocked to the minimum interface the reconciler
    touches: ``bot_trades``, ``transactions``, ``redis``. Redis is
    mocked to return ``None`` for any bot doc (so no bot is treated as
    active and the skip-set is empty)."""

    @pytest.mark.asyncio
    async def test_backfills_from_transactions(
        self, repo, txn_repo, session_factory,
    ):
        from ib_trader.daemon.reconciler import run_commission_reconciliation
        row = _insert_trade(
            repo, symbol="MGCM6", commission=Decimal("0"),
            entry_serial=100, exit_serial=101,
        )
        _insert_txn(txn_repo, ib_order_id=9000, trade_serial=100,
                     commission=Decimal("0.97"),
                     requested_at=row.exit_time - timedelta(seconds=5))
        _insert_txn(txn_repo, ib_order_id=9001, trade_serial=101,
                     commission=Decimal("0.97"),
                     requested_at=row.exit_time + timedelta(seconds=5))

        ctx = MagicMock()
        ctx.bot_trades = repo
        ctx.transactions = txn_repo
        ctx.redis = MagicMock()
        # StateStore.get is awaited; return empty doc → no active bots.
        # Patch via the module path the reconciler uses.
        from ib_trader.redis import state as state_mod
        orig_get = state_mod.StateStore.get
        state_mod.StateStore.get = AsyncMock(return_value=None)
        try:
            result = await run_commission_reconciliation(ctx)
        finally:
            state_mod.StateStore.get = orig_get

        assert result["backfilled"] == 1
        fresh = repo._session().query(BotTrade).filter(
            BotTrade.id == row.id,
        ).first()
        assert fresh.commission == Decimal("1.94")

    @pytest.mark.asyncio
    async def test_warns_on_straggler_past_24h(
        self, repo, txn_repo,
    ):
        """A row older than 24h with commission still below the floor
        and no transactions to backfill from fires a
        COMMISSION_DELIVERY_GAP WARNING."""
        from ib_trader.daemon.reconciler import run_commission_reconciliation
        old = _now() - timedelta(hours=30)
        _insert_trade(
            repo, symbol="MGCM6", commission=Decimal("0"),
            entry_serial=200, exit_serial=201,
            exit_time=old,
        )
        # Intentionally no matching transactions — straggler.

        ctx = MagicMock()
        ctx.bot_trades = repo
        ctx.transactions = txn_repo
        ctx.redis = MagicMock()
        from ib_trader.redis import state as state_mod
        orig_get = state_mod.StateStore.get
        state_mod.StateStore.get = AsyncMock(return_value=None)
        # Patch log_and_alert to record calls without touching Redis.
        import ib_trader.daemon.reconciler as recon_mod
        orig_alert = recon_mod.log_and_alert
        recon_mod.log_and_alert = AsyncMock()
        try:
            result = await run_commission_reconciliation(
                ctx, lookback_hours=48.0,
            )
            assert result["warned"] == 1
            recon_mod.log_and_alert.assert_called_once()
            kwargs = recon_mod.log_and_alert.call_args.kwargs
            assert kwargs["trigger"] == "COMMISSION_DELIVERY_GAP"
            assert kwargs["severity"] == "WARNING"
        finally:
            state_mod.StateStore.get = orig_get
            recon_mod.log_and_alert = orig_alert
