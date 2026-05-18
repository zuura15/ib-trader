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
            exit_time, symbol="STKTEST", direction="LONG"):
    """Default symbol is intentionally a fake STK ticker — it has no
    entry in ``ROUND_TRIP_MIN`` so the rollup's floor logic is a
    no-op. Tests targeting the floor behavior pass a futures symbol
    explicitly (MGCQ6, MESM6, MNQM6)."""
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
        are fully populated. (STKTEST symbol → floor inactive.)"""
        now = datetime.now(timezone.utc)
        for pnl, comm in [
            (9, 0.85), (38.5, 2.2), (0, 1.7), (6.5, 2.2),
            (0, 1.24), (63.75, 2.2), (-14, 1.7), (-10, 2.2),
            (-2, 1.7), (-12.5, 2.2), (0, 1.24),
        ]:
            _insert(repo, realized_pnl=pnl, commission=comm,
                    exit_time=now - timedelta(minutes=10))
        net = repo.sum_realized_pnl_last_hours(24.0)
        assert net == Decimal("59.82")


class TestSumRealizedPnlFloorsCommission:
    """The rollup floors each row's commission at the symbol's
    expected round-trip cost — protects the header from inflating
    when IB's ``commissionReport`` delivery lags one of the two legs
    (bot_trade.commission stores e.g. $0.97 on MGC instead of the
    expected $1.94 round-trip; the floor brings the display number
    in line with what IB actually charges)."""

    def test_partial_mgc_commission_floored_to_round_trip(self, repo):
        now = datetime.now(timezone.utc)
        _insert(repo, realized_pnl=20, commission=Decimal("0.97"),
                exit_time=now - timedelta(minutes=10), symbol="MGCQ6")
        # stored $0.97 < floor $1.94 → uses floor.
        # net = 20 - 1.94 = 18.06
        assert repo.sum_realized_pnl_last_hours(24.0) == Decimal("18.06")

    def test_stored_above_floor_used_as_is(self, repo):
        now = datetime.now(timezone.utc)
        _insert(repo, realized_pnl=50, commission=Decimal("2.50"),
                exit_time=now - timedelta(minutes=10), symbol="MGCQ6")
        # stored $2.50 > floor $1.94 → uses stored.
        # net = 50 - 2.50 = 47.50
        assert repo.sum_realized_pnl_last_hours(24.0) == Decimal("47.50")

    def test_mes_floor_distinct_from_mgc(self, repo):
        """MES floor is $1.24, MGC floor is $1.94 — per-symbol lookup
        not a single constant."""
        now = datetime.now(timezone.utc)
        _insert(repo, realized_pnl=10, commission=Decimal("0"),
                exit_time=now - timedelta(minutes=10), symbol="MESM6")
        # MES floor 1.24. net = 10 - 1.24 = 8.76
        assert repo.sum_realized_pnl_last_hours(24.0) == Decimal("8.76")

    def test_unknown_symbol_falls_back_to_zero_floor(self, repo):
        now = datetime.now(timezone.utc)
        _insert(repo, realized_pnl=10, commission=Decimal("0"),
                exit_time=now - timedelta(minutes=10), symbol="WEIRD")
        # No floor for WEIRD → stored 0 used as-is. net = 10 - 0 = 10
        assert repo.sum_realized_pnl_last_hours(24.0) == Decimal("10")

    def test_null_commission_uses_floor(self, repo):
        """A futures-symbol row with commission=None hits the floor,
        protecting the header during the brief window between
        bot_trade creation and the first commissionReport landing."""
        now = datetime.now(timezone.utc)
        _insert(repo, realized_pnl=8, commission=None,
                exit_time=now - timedelta(minutes=10), symbol="MNQM6")
        # MNQ floor 1.24. net = 8 - 1.24 = 6.76
        assert repo.sum_realized_pnl_last_hours(24.0) == Decimal("6.76")
