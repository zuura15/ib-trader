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
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

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
