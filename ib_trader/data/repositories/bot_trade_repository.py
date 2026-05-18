"""Repository for bot_trades — one row per bot entry-to-exit round-trip.

Written by the bot runner when ``_handle_record_trade_closed`` fires on
a terminal exit fill. Read by ``GET /api/bot-trades`` for the Bot Trades
panel in the frontend.

The schema is the synthesis layer over the raw orders + transactions
tables — each bot execution shows up here as a single record with
entry/exit data, realized P&L, duration, and trail-reset count pulled
from the bot's state doc.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import scoped_session, Session

from ib_trader.data.models import BotTrade

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


class BotTradeRepository:
    """SQLAlchemy repository for the ``bot_trades`` table."""

    def __init__(self, session_factory: scoped_session) -> None:
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def create(self, trade: BotTrade) -> BotTrade:
        """Insert a bot trade row."""
        s = self._session()
        if trade.created_at is None:
            trade.created_at = _now_utc()
        s.add(trade)
        s.commit()
        return trade

    def get(self, trade_id: str) -> Optional[BotTrade]:
        """Return the trade with the given id, or None."""
        return self._session().query(BotTrade).filter(BotTrade.id == trade_id).first()

    def list_all(self, limit: int = 500) -> list[BotTrade]:
        """Return most-recent-first list of bot trades."""
        return (
            self._session()
            .query(BotTrade)
            .order_by(BotTrade.created_at.desc())
            .limit(limit)
            .all()
        )

    def list_for_bot(self, bot_id: str, limit: int = 500) -> list[BotTrade]:
        """Return most-recent-first list filtered to a single bot."""
        return (
            self._session()
            .query(BotTrade)
            .filter(BotTrade.bot_id == bot_id)
            .order_by(BotTrade.created_at.desc())
            .limit(limit)
            .all()
        )

    def sum_realized_pnl_last_hours(self, hours: float = 24.0) -> Decimal:
        """Return SUM(realized_pnl - commission) for trades closed in
        the last ``hours`` (rolling window). The header "Realized P&L"
        reads this and must match what IB reports — gross P&L drifts
        ~$1-3 per round-trip on micro futures, which on a busy day
        adds up to a visible $15-30 inflation vs IB.

        Commission is summed per-trade across both legs (entry + exit)
        in ``bot_trades.commission`` and is populated asynchronously
        by ``add_commission_by_serial`` when IB delivers
        ``commissionReport``. ``COALESCE`` defends against rows whose
        commission backfill hasn't landed yet — those will undercount
        until the report arrives but never error.
        """
        since = _now_utc() - timedelta(hours=hours)
        result = (
            self._session()
            .query(func.coalesce(
                func.sum(
                    BotTrade.realized_pnl
                    - func.coalesce(BotTrade.commission, 0)
                ),
                0,
            ))
            .filter(BotTrade.exit_time >= since)
            .scalar()
        )
        if result is None:
            return Decimal("0")
        if isinstance(result, Decimal):
            return result
        return Decimal(str(result))

    def find_undercommissioned_trades(
        self, hours: float = 24.0,
        *,
        skip_bot_ids: set[str] | None = None,
    ) -> list[BotTrade]:
        """Return closed rows in the last ``hours`` whose stored
        commission is BELOW the symbol-specific round-trip floor
        (``ib_trader.data.commissions.ROUND_TRIP_MIN``). Symbols with
        no expected floor are excluded by definition (threshold = 0,
        so commission >= 0 always passes).

        ``skip_bot_ids`` filters out bots whose live FSM state means
        a fresh fill might still be in flight — the caller passes the
        set of bot_ids currently in ``lifecycle.ACTIVE_STATES`` so
        the reconciler doesn't race the live commission backfill.

        Caller is expected to do the per-row backfill via
        ``update_commission_if_higher``. Returns the raw BotTrade
        objects so the caller can read entry/exit serials and
        exit_time for the time-bounded transactions sum.
        """
        from ib_trader.data.commissions import ROUND_TRIP_MIN

        if not ROUND_TRIP_MIN:
            return []
        since = _now_utc() - timedelta(hours=hours)
        # Pull every closed row in window for the symbols we know about
        # and filter in Python — per-symbol threshold via SQL CASE is
        # noisier than this and the candidate set is small (hundreds
        # of rows/day, not millions).
        skip = skip_bot_ids or set()
        rows = (
            self._session()
            .query(BotTrade)
            .filter(
                BotTrade.exit_time >= since,
                BotTrade.symbol.in_(list(ROUND_TRIP_MIN.keys())),
            )
            .all()
        )
        out: list[BotTrade] = []
        for row in rows:
            if row.bot_id in skip:
                continue
            floor = ROUND_TRIP_MIN.get(row.symbol, Decimal("0"))
            current = row.commission or Decimal("0")
            if current < floor:
                out.append(row)
        return out

    def update_commission_if_higher(
        self, trade_id: str, new_value: Decimal,
    ) -> bool:
        """Idempotent UPDATE — writes ``new_value`` only when it
        strictly exceeds the current stored value. Returns True iff
        the row was changed.

        Safe to call from a periodic sweep even when the live
        ``add_commission_by_serial`` path has already populated the
        commission: a sweep value equal-or-lower is a no-op.
        """
        if new_value is None:
            return False
        if not isinstance(new_value, Decimal):
            new_value = Decimal(str(new_value))
        s = self._session()
        row = (
            s.query(BotTrade)
            .filter(BotTrade.id == trade_id)
            .first()
        )
        if row is None:
            return False
        current = row.commission or Decimal("0")
        if new_value <= current:
            return False
        row.commission = new_value
        s.commit()
        return True

    def add_commission_by_serial(
        self, serial: int, delta: Decimal,
    ) -> int:
        """Accumulate ``delta`` into the commission of every bot_trade row
        matching ``serial`` as either entry_serial or exit_serial. Returns
        the number of rows updated.

        Commission arrives asynchronously (IB delivers ``commissionReport``
        after ``execDetails``). By the time it lands, the round-trip may
        already have been closed out into a bot_trades row — this method
        updates it in place.
        """
        if delta is None:
            return 0
        if not isinstance(delta, Decimal):
            delta = Decimal(str(delta))
        s = self._session()
        rows = (
            s.query(BotTrade)
            .filter(
                (BotTrade.entry_serial == serial)
                | (BotTrade.exit_serial == serial)
            )
            .all()
        )
        updated = 0
        for row in rows:
            existing = row.commission or Decimal("0")
            row.commission = existing + delta
            updated += 1
        if updated:
            s.commit()
        return updated
