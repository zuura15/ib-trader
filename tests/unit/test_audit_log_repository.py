"""Unit tests for AuditLogRepository.

Covers the three event types (BAR_EVAL / ORDER_PLACED / TRADE_CLOSED),
their headline fields, and the read queries (filter by bot, time
bounds, paging via id cursor for SSE).
"""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.data.models import Base, AuditLog
from ib_trader.data.repositories.audit_log_repository import (
    AuditLogRepository,
    EVENT_BAR_EVAL, EVENT_ORDER_PLACED, EVENT_TRADE_CLOSED,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def audit_repo():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session_factory = scoped_session(factory)
    return AuditLogRepository(session_factory)


class TestInsertBarEval:
    def test_writes_full_headline(self, audit_repo):
        row = audit_repo.insert_bar_eval(
            bot_id="chart-bot-1", symbol="MGCM6",
            event_ts_utc=_now(),
            pivot_status="PIVOT_LOW", line_status="LINES_LONG",
            decision="FIRED·BUY",
            bar_close=Decimal("4666.30"),
            payload={"touches": 7, "slope": 0.65},
        )
        assert row.id is not None
        assert row.event_type == EVENT_BAR_EVAL
        assert row.pivot_status == "PIVOT_LOW"
        assert row.line_status == "LINES_LONG"
        assert row.decision == "FIRED·BUY"
        assert row.bar_close == Decimal("4666.30")
        assert row.payload_json is not None
        assert "touches" in row.payload_json

    def test_payload_is_optional(self, audit_repo):
        row = audit_repo.insert_bar_eval(
            bot_id="b", symbol="X", event_ts_utc=_now(),
            pivot_status="NO_PIVOT", line_status="LINES_NONE",
            decision="SKIP·no_new_pivot",
        )
        assert row.payload_json is None


class TestInsertOrderPlaced:
    def test_writes_entry_order(self, audit_repo):
        row = audit_repo.insert_order_placed(
            bot_id="b", symbol="MGCM6",
            event_ts_utc=_now(),
            decision="ORDER·BUY·entry",
            payload={"side": "BUY", "qty": "1"},
        )
        assert row.event_type == EVENT_ORDER_PLACED
        assert row.decision == "ORDER·BUY·entry"
        # Pivot/line headline fields are NULL for ORDER_PLACED.
        assert row.pivot_status is None
        assert row.line_status is None

    def test_writes_exit_order_with_reason(self, audit_repo):
        row = audit_repo.insert_order_placed(
            bot_id="b", symbol="MGCM6",
            event_ts_utc=_now(),
            decision="ORDER·SELL·exit·counter_line",
            payload={"side": "SELL", "exit_context": {"reason": "counter_line"}},
        )
        assert row.decision.endswith("counter_line")


class TestInsertTradeClosed:
    def test_writes_pnl_and_decision(self, audit_repo):
        row = audit_repo.insert_trade_closed(
            bot_id="b", symbol="MGCM6",
            event_ts_utc=_now(),
            decision="CLOSED·LONG·trail_stop",
            pnl_net=Decimal("-3.94"),
            payload={"entry_price": "4666.30", "exit_price": "4666.10"},
        )
        assert row.event_type == EVENT_TRADE_CLOSED
        assert row.pnl_net == Decimal("-3.94")
        assert row.bar_close is None  # not used for closures


class TestListRecent:
    def test_filters_by_bot(self, audit_repo):
        base = _now()
        for i, bot in enumerate(["bot-a", "bot-b", "bot-a"]):
            audit_repo.insert_bar_eval(
                bot_id=bot, symbol="X",
                event_ts_utc=base + timedelta(seconds=i),
                pivot_status="NONE", line_status="NONE",
                decision="HOLDING",
            )
        rows = audit_repo.list_recent(bot_id="bot-a", limit=10)
        assert len(rows) == 2
        assert all(r.bot_id == "bot-a" for r in rows)

    def test_filters_by_since(self, audit_repo):
        base = _now()
        cutoff = base + timedelta(seconds=30)
        for offset in (0, 10, 20, 40, 60):
            audit_repo.insert_bar_eval(
                bot_id="b", symbol="X",
                event_ts_utc=base + timedelta(seconds=offset),
                pivot_status="NONE", line_status="NONE",
                decision="HOLDING",
            )
        rows = audit_repo.list_recent(since=cutoff, limit=10)
        assert len(rows) == 2
        # SQLite stores naive datetimes — drop tzinfo on the cutoff
        # to make the comparison work in unit tests.
        cutoff_naive = cutoff.replace(tzinfo=None)
        for r in rows:
            r_ts = r.event_ts_utc.replace(tzinfo=None) \
                if r.event_ts_utc.tzinfo else r.event_ts_utc
            assert r_ts >= cutoff_naive

    def test_newest_first_ordering(self, audit_repo):
        base = _now()
        ids_inserted = []
        for offset in (0, 10, 20):
            r = audit_repo.insert_bar_eval(
                bot_id="b", symbol="X",
                event_ts_utc=base + timedelta(seconds=offset),
                pivot_status="NONE", line_status="NONE",
                decision="HOLDING",
            )
            ids_inserted.append(r.id)
        rows = audit_repo.list_recent(limit=10)
        # Newest first → reverse insertion order.
        assert [r.id for r in rows] == list(reversed(ids_inserted))


class TestStreamCursor:
    def test_latest_id_returns_max(self, audit_repo):
        assert audit_repo.latest_id() is None
        r1 = audit_repo.insert_bar_eval(
            bot_id="b", symbol="X", event_ts_utc=_now(),
            pivot_status="NONE", line_status="NONE", decision="HOLDING",
        )
        r2 = audit_repo.insert_bar_eval(
            bot_id="b", symbol="X", event_ts_utc=_now(),
            pivot_status="NONE", line_status="NONE", decision="HOLDING",
        )
        assert audit_repo.latest_id() == r2.id
        assert r2.id > r1.id

    def test_list_after_id_paged_forward(self, audit_repo):
        ids = []
        for _ in range(5):
            r = audit_repo.insert_bar_eval(
                bot_id="b", symbol="X", event_ts_utc=_now(),
                pivot_status="NONE", line_status="NONE",
                decision="HOLDING",
            )
            ids.append(r.id)
        after = audit_repo.list_after_id(ids[1], limit=10)
        # Returns rows with id > ids[1], in ascending order.
        assert [r.id for r in after] == ids[2:]
