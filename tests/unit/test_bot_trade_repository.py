"""Unit tests for BotTradeRepository.

Focused on ``sum_realized_pnl_last_hours`` — the query that feeds the
header "Realized P&L · 24h" chip. The header must report NET P&L
(realized_pnl minus commission) so it matches what IB shows; gross
diverges by ~$1-3 per round-trip on micro futures and the operator
loses trust in the number.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import Base, BotTrade
from ib_trader.data.repositories.bot_trade_repository import BotTradeRepository


@pytest.fixture
def repo():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session_factory = scoped_session(factory)
    return BotTradeRepository(session_factory)


def _insert(repo: BotTradeRepository, *, realized_pnl, commission,
            exit_time, symbol="MGCM6", direction="LONG"):
    s = repo._session()
    row = BotTrade(
        id=str(uuid.uuid4()),
        bot_id="chart-bot-1",
        bot_name="test",
        symbol=symbol,
        direction=direction,
        entry_price=Decimal("4540.0"),
        entry_qty=Decimal("1"),
        entry_time=exit_time - timedelta(seconds=30),
        exit_price=Decimal("4542.0"),
        exit_qty=Decimal("1"),
        exit_time=exit_time,
        realized_pnl=Decimal(str(realized_pnl)),
        commission=Decimal(str(commission)) if commission is not None else None,
        trail_reset_count=0,
        duration_seconds=30,
        created_at=datetime.now(timezone.utc),
    )
    s.add(row)
    s.commit()
    return row


class TestSumRealizedPnlLastHours:
    def test_subtracts_commission_from_each_trade(self, repo):
        now = datetime.now(timezone.utc)
        _insert(repo, realized_pnl=20, commission=2,
                exit_time=now - timedelta(hours=1))
        _insert(repo, realized_pnl=10, commission=1,
                exit_time=now - timedelta(hours=2))
        # net = (20-2) + (10-1) = 27
        assert repo.sum_realized_pnl_last_hours(24.0) == Decimal("27")

    def test_treats_null_commission_as_zero(self, repo):
        """Commission backfill is asynchronous — IB delivers
        ``commissionReport`` after ``execDetails`` and the bot_trades
        row may briefly have ``commission = NULL``. The rollup must
        not return NULL/error in that window; just undercount until
        the report lands."""
        now = datetime.now(timezone.utc)
        _insert(repo, realized_pnl=15, commission=None,
                exit_time=now - timedelta(hours=1))
        # null commission → counts as 0 → net = 15 - 0 = 15
        assert repo.sum_realized_pnl_last_hours(24.0) == Decimal("15")

    def test_excludes_trades_outside_window(self, repo):
        now = datetime.now(timezone.utc)
        _insert(repo, realized_pnl=100, commission=5,
                exit_time=now - timedelta(hours=25))
        _insert(repo, realized_pnl=8, commission=1,
                exit_time=now - timedelta(hours=1))
        # only the recent trade counts: 8 - 1 = 7
        assert repo.sum_realized_pnl_last_hours(24.0) == Decimal("7")

    def test_empty_window_returns_zero(self, repo):
        assert repo.sum_realized_pnl_last_hours(24.0) == Decimal("0")

    def test_negative_pnl_with_commission(self, repo):
        """A losing trade pays both the loss AND the commission, so
        net should be more negative than gross. Caught the inverse
        sign bug in an earlier draft of the query."""
        now = datetime.now(timezone.utc)
        _insert(repo, realized_pnl=-10, commission=2,
                exit_time=now - timedelta(minutes=30))
        # net = -10 - 2 = -12
        assert repo.sum_realized_pnl_last_hours(24.0) == Decimal("-12")

    def test_matches_observed_prod_gap(self, repo):
        """Regression for 2026-05-17: prod showed gross $79.25 vs IB
        net ~$61. The query must produce the net once commissions
        are fully populated."""
        now = datetime.now(timezone.utc)
        for pnl, comm in [
            (9, 0.85), (38.5, 2.2), (0, 1.7), (6.5, 2.2),
            (0, 1.24), (63.75, 2.2), (-14, 1.7), (-10, 2.2),
            (-2, 1.7), (-12.5, 2.2), (0, 1.24),
        ]:
            _insert(repo, realized_pnl=pnl, commission=comm,
                    exit_time=now - timedelta(minutes=10))
        # gross 79.25, total commission 19.43, net 59.82 — close to
        # IB's $61 (the residual gap is timing on when the next
        # commissionReport lands vs the snapshot).
        net = repo.sum_realized_pnl_last_hours(24.0)
        assert net == Decimal("59.82")
