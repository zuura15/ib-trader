"""Regression test for the 2026-06-08 reconciler multiplier bug.

Pre-fix, ``_maybe_close_trade_group`` computed ``realized_pnl =
exit_value - entry_value`` in price-points × qty units. For futures
(where the contract multiplier turns each point into N dollars), this
under-stated dollar P&L by the multiplier (e.g. NQ has $20/point, so
realized P&L was off by 20×). Operator hit this when a NQM6
profit-taker fired autonomously and the daemon reconciler wrote the
post-close P&L number.

The fix looks up ``multiplier`` from any non-null transaction row for
the trade (``execute_order`` writes it on the PLACE_ACCEPTED row at
place time) and applies it: ``(exit - entry) × multiplier``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

from ib_trader.config.context import AppContext
from ib_trader.daemon.reconciler import _maybe_close_trade_group
from ib_trader.data.models import (
    Base, LegType, TradeGroup, TradeStatus, TransactionAction,
    TransactionEvent,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.data.repository import (
    AlertRepository, ContractRepository, HeartbeatRepository,
    RepriceEventRepository, TradeRepository,
)
from ib_trader.engine.tracker import OrderTracker
from tests.conftest import MockIBClient


def _now() -> datetime:
    return datetime.now(timezone.utc)


@pytest.fixture
def ctx():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sf = scoped_session(sessionmaker(bind=engine))
    return AppContext(
        ib=MockIBClient(),
        trades=TradeRepository(sf),
        reprice_events=RepriceEventRepository(sf),
        contracts=ContractRepository(sf),
        heartbeats=HeartbeatRepository(sf),
        alerts=AlertRepository(sf),
        tracker=OrderTracker(),
        settings={},
        account_id="U1234567",
        transactions=TransactionRepository(sf),
    )


def _create_trade(ctx, symbol: str, direction: str) -> str:
    tg = TradeGroup(
        serial_number=ctx.trades.next_serial_number(),
        symbol=symbol, direction=direction,
        status=TradeStatus.OPEN, opened_at=_now(),
    )
    ctx.trades.create(tg)
    return tg.id


def _insert_leg(
    ctx, *, trade_id: str, leg_type: LegType, side: str,
    qty: Decimal, price: Decimal, action: TransactionAction,
    multiplier: str | None = None, ib_order_id: int = 0,
    is_terminal: bool = True,
) -> None:
    ctx.transactions.insert(TransactionEvent(
        ib_order_id=ib_order_id,
        action=action,
        symbol="NQM6",
        side=side,
        order_type="LIMIT",
        quantity=qty,
        account_id="U1234567",
        requested_at=_now(),
        is_terminal=is_terminal,
        trade_id=trade_id,
        leg_type=leg_type,
        ib_filled_qty=qty if action in (
            TransactionAction.FILLED, TransactionAction.PARTIAL_FILL,
        ) else None,
        ib_avg_fill_price=price if action in (
            TransactionAction.FILLED, TransactionAction.PARTIAL_FILL,
        ) else None,
        multiplier=multiplier,
    ))


class TestReconcilerMultiplier:
    def test_nq_long_close_applies_multiplier(self, ctx):
        """NQ entry 20000, PT exit 20010, qty 1, multiplier 20.
        Pre-fix realized_pnl = 10 (price-points × qty).
        Post-fix realized_pnl = 10 × 20 = 200 dollars."""
        trade_id = _create_trade(ctx, "NQM6", "LONG")
        # Entry placement carries multiplier (execute_order writes it)
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.ENTRY, side="BUY",
            qty=Decimal("1"), price=Decimal("0"),
            action=TransactionAction.PLACE_ACCEPTED,
            multiplier="20", ib_order_id=1001, is_terminal=False,
        )
        # Entry fill
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.ENTRY, side="BUY",
            qty=Decimal("1"), price=Decimal("20000"),
            action=TransactionAction.FILLED, ib_order_id=1001,
        )
        # PT fill
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.PROFIT_TAKER, side="SELL",
            qty=Decimal("1"), price=Decimal("20010"),
            action=TransactionAction.FILLED, ib_order_id=1002,
        )

        _maybe_close_trade_group(ctx, trade_id)

        tg = ctx.trades.get_by_serial(0)
        assert tg.status == TradeStatus.CLOSED
        assert tg.realized_pnl == Decimal("200")  # 10 × 20

    def test_gc_short_close_applies_multiplier(self, ctx):
        """GC entry 3500, PT exit 3490, qty 2, multiplier 100, SHORT.
        Pre-fix: entry - exit = (3500-3490) × 2 = 20. Post-fix: × 100 = 2000."""
        trade_id = _create_trade(ctx, "NQM6", "SHORT")
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.ENTRY, side="SELL",
            qty=Decimal("2"), price=Decimal("0"),
            action=TransactionAction.PLACE_ACCEPTED,
            multiplier="100", ib_order_id=2001, is_terminal=False,
        )
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.ENTRY, side="SELL",
            qty=Decimal("2"), price=Decimal("3500"),
            action=TransactionAction.FILLED, ib_order_id=2001,
        )
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.PROFIT_TAKER, side="BUY",
            qty=Decimal("2"), price=Decimal("3490"),
            action=TransactionAction.FILLED, ib_order_id=2002,
        )

        _maybe_close_trade_group(ctx, trade_id)

        tg = ctx.trades.get_by_serial(0)
        assert tg.realized_pnl == Decimal("2000")  # (3500-3490) × 2 × 100

    def test_stk_close_no_multiplier(self, ctx):
        """STK trades have no multiplier set on transactions → defaults
        to 1, so the price diff IS the dollar P&L. Verifies the fix
        doesn't regress the STK path."""
        trade_id = _create_trade(ctx, "AAPL", "LONG")
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.ENTRY, side="BUY",
            qty=Decimal("100"), price=Decimal("0"),
            action=TransactionAction.PLACE_ACCEPTED,
            multiplier=None, ib_order_id=3001, is_terminal=False,
        )
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.ENTRY, side="BUY",
            qty=Decimal("100"), price=Decimal("200"),
            action=TransactionAction.FILLED, ib_order_id=3001,
        )
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.CLOSE, side="SELL",
            qty=Decimal("100"), price=Decimal("201"),
            action=TransactionAction.FILLED, ib_order_id=3002,
        )

        _maybe_close_trade_group(ctx, trade_id)

        tg = ctx.trades.get_by_serial(0)
        # (201 - 200) × 100 × 1 = 100
        assert tg.realized_pnl == Decimal("100")

    def test_multiplier_picked_from_first_non_null_row(self, ctx):
        """Multiplier lookup walks transactions; the first non-null wins
        even if it's not the entry row. PT-only rows carrying multiplier
        (post-fix place_profit_taker writes) still satisfy."""
        trade_id = _create_trade(ctx, "NQM6", "LONG")
        # Entry row has no multiplier (legacy / old data shape)
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.ENTRY, side="BUY",
            qty=Decimal("1"), price=Decimal("20000"),
            action=TransactionAction.FILLED, ib_order_id=4001,
            multiplier=None,
        )
        # PT placement carries multiplier
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.PROFIT_TAKER, side="SELL",
            qty=Decimal("1"), price=Decimal("0"),
            action=TransactionAction.PLACE_ACCEPTED,
            multiplier="20", ib_order_id=4002, is_terminal=False,
        )
        # PT fill
        _insert_leg(
            ctx, trade_id=trade_id, leg_type=LegType.PROFIT_TAKER, side="SELL",
            qty=Decimal("1"), price=Decimal("20010"),
            action=TransactionAction.FILLED, ib_order_id=4002,
        )

        _maybe_close_trade_group(ctx, trade_id)

        tg = ctx.trades.get_by_serial(0)
        assert tg.realized_pnl == Decimal("200")
