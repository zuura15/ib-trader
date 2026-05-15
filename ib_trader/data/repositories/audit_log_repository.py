"""Repository for ``audit_log`` — operator-facing event feed.

Three event types share the table:
  - ``BAR_EVAL``     — per-bar bot evaluation (entry decision or in-position)
  - ``ORDER_PLACED`` — bot submitted an order to IB
  - ``TRADE_CLOSED`` — round-trip summary on exit fill

Written by the bot runtime + strategy hooks. Read by ``GET /api/audit``
for the audit feed panel in the frontend.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from sqlalchemy.orm import scoped_session, Session

from ib_trader.data.models import AuditLog

logger = logging.getLogger(__name__)

EVENT_BAR_EVAL = "BAR_EVAL"
EVENT_ORDER_PLACED = "ORDER_PLACED"
EVENT_TRADE_CLOSED = "TRADE_CLOSED"


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _to_str(payload: Optional[dict]) -> Optional[str]:
    """Serialize payload to JSON; tolerates Decimal via ``default=str``."""
    if payload is None:
        return None
    return json.dumps(payload, default=str, separators=(",", ":"))


class AuditLogRepository:
    """SQLAlchemy repository for the ``audit_log`` table."""

    def __init__(self, session_factory: scoped_session) -> None:
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def insert_bar_eval(
        self,
        *,
        bot_id: str,
        symbol: str,
        event_ts_utc: datetime,
        pivot_status: str,
        line_status: str,
        decision: str,
        bar_close: Optional[Decimal] = None,
        payload: Optional[dict] = None,
    ) -> AuditLog:
        """Insert a BAR_EVAL row.

        ``pivot_status``: PIVOT_LOW / PIVOT_HIGH / NO_PIVOT / NONE
        ``line_status``:  LINES_LONG / LINES_SHORT / LINES_BOTH /
                          LINES_NONE / NONE
        ``decision``: FIRED·BUY / FIRED·SELL / FILTERED·<name> /
                      SKIP·<reason> / GATED·<gate> / HOLDING /
                      EXIT_FIRED·<reason>
        """
        row = AuditLog(
            bot_id=bot_id,
            symbol=symbol,
            event_ts_utc=event_ts_utc,
            event_type=EVENT_BAR_EVAL,
            pivot_status=pivot_status,
            line_status=line_status,
            decision=decision,
            bar_close=bar_close,
            payload_json=_to_str(payload),
            created_at=_now_utc(),
        )
        s = self._session()
        s.add(row)
        s.commit()
        return row

    def insert_order_placed(
        self,
        *,
        bot_id: str,
        symbol: str,
        event_ts_utc: datetime,
        decision: str,
        bar_close: Optional[Decimal] = None,
        payload: Optional[dict] = None,
    ) -> AuditLog:
        """Insert an ORDER_PLACED row.

        ``decision`` examples:
            ORDER·BUY·entry
            ORDER·SELL·exit·counter_line
            ORDER·BUY·exit·trail
        """
        row = AuditLog(
            bot_id=bot_id,
            symbol=symbol,
            event_ts_utc=event_ts_utc,
            event_type=EVENT_ORDER_PLACED,
            pivot_status=None,
            line_status=None,
            decision=decision,
            bar_close=bar_close,
            payload_json=_to_str(payload),
            created_at=_now_utc(),
        )
        s = self._session()
        s.add(row)
        s.commit()
        return row

    def insert_trade_closed(
        self,
        *,
        bot_id: str,
        symbol: str,
        event_ts_utc: datetime,
        decision: str,
        pnl_net: Optional[Decimal] = None,
        payload: Optional[dict] = None,
    ) -> AuditLog:
        """Insert a TRADE_CLOSED row.

        ``decision`` examples:
            CLOSED·LONG·counter_line
            CLOSED·SHORT·trail+line_breach
            CLOSED·LONG·force_quit
        """
        row = AuditLog(
            bot_id=bot_id,
            symbol=symbol,
            event_ts_utc=event_ts_utc,
            event_type=EVENT_TRADE_CLOSED,
            pivot_status=None,
            line_status=None,
            decision=decision,
            bar_close=None,
            pnl_net=pnl_net,
            payload_json=_to_str(payload),
            created_at=_now_utc(),
        )
        s = self._session()
        s.add(row)
        s.commit()
        return row

    def list_recent(
        self,
        *,
        bot_id: Optional[str] = None,
        since: Optional[datetime] = None,
        before: Optional[datetime] = None,
        limit: int = 100,
    ) -> list[AuditLog]:
        """Return audit rows newest-first.

        ``bot_id``: None = all bots. ``since``: lower bound (inclusive)
        on ``event_ts_utc``. ``before``: upper bound (exclusive) — use
        with the oldest row's ``event_ts_utc`` to page backwards.
        ``limit``: max rows returned, default 100.
        """
        q = self._session().query(AuditLog)
        if bot_id:
            q = q.filter(AuditLog.bot_id == bot_id)
        if since is not None:
            q = q.filter(AuditLog.event_ts_utc >= since)
        if before is not None:
            q = q.filter(AuditLog.event_ts_utc < before)
        return (
            q.order_by(AuditLog.event_ts_utc.desc(), AuditLog.id.desc())
            .limit(limit)
            .all()
        )

    def latest_id(self) -> Optional[int]:
        """Highest ``id`` currently in the table — used by the SSE
        stream to track what it has already pushed."""
        row = (
            self._session()
            .query(AuditLog.id)
            .order_by(AuditLog.id.desc())
            .limit(1)
            .first()
        )
        return row[0] if row else None

    def list_after_id(self, after_id: int, limit: int = 100) -> list[AuditLog]:
        """Return rows with id > ``after_id`` in insertion order.

        Used by the SSE stream to push new rows since the last cursor.
        """
        return (
            self._session()
            .query(AuditLog)
            .filter(AuditLog.id > after_id)
            .order_by(AuditLog.id.asc())
            .limit(limit)
            .all()
        )
