#!/usr/bin/env python3
"""Recompute ``trade_groups.realized_pnl`` for FUT closes that were
written before the multiplier-aware fix landed.

The bug: ``_handle_close_fill`` / ``_handle_close_partial`` in
``engine/order.py`` computed realized P&L as
``(exit - entry) * qty * direction - commission`` without the contract
multiplier. For futures that under-reported the dollar value by the
multiplier factor (MES 5x, MGC 10x, ZN 20x, etc.). STK paths are
unaffected (multiplier = 1).

This script re-derives the correct value from ``transactions`` rows:

  * Picks the first FILLED ENTRY fill per trade_group (mirrors
    ``ctx.transactions.get_entry_fill`` — the engine's own behaviour).
  * Sums ``(exit - entry) * qty * multiplier * direction - commission``
    over every FILLED CLOSE / PROFIT_TAKER leg.
  * Compares against the stored ``realized_pnl``. If they differ by more
    than $0.01, prints (and on --apply writes) the corrected value.

Only touches trade_groups where the close legs carry security_type='FUT'
and a multiplier > 1. STK and unknown-multiplier rows are skipped.

IB's own realized P&L (``trade_groups.ib_realized_pnl``, populated by
the commission callback when available) is *not* touched — that field
holds IB's authoritative round-trip and is independent of our formula.

Idempotent: re-running after --apply finds nothing to change.

Usage:
    .venv/bin/python scripts/backfill_fut_realized_pnl_multiplier.py            # dry-run
    .venv/bin/python scripts/backfill_fut_realized_pnl_multiplier.py --apply
    .venv/bin/python scripts/backfill_fut_realized_pnl_multiplier.py --db-path /path/to/trader.db
"""
from __future__ import annotations

import argparse
import logging
import sys
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_trader.data.models import (
    Base, LegType, TradeGroup, TransactionAction, TransactionEvent,
)

logger = logging.getLogger("backfill-fut-pnl-multiplier")

# A penny is below the noise floor of Decimal serialisation round-trips
# through SQLite, so anything tighter than this is almost certainly the
# same number rendered twice.
_EPSILON = Decimal("0.01")


def _entry_fill(session, trade_id: str) -> TransactionEvent | None:
    """First FILLED entry txn — mirrors ``ctx.transactions.get_entry_fill``."""
    return (
        session.query(TransactionEvent)
        .filter(
            TransactionEvent.trade_id == trade_id,
            TransactionEvent.leg_type == LegType.ENTRY,
            TransactionEvent.action == TransactionAction.FILLED,
        )
        .order_by(TransactionEvent.id.asc())
        .first()
    )


def _close_fills(session, trade_id: str) -> list[TransactionEvent]:
    """All FILLED close/PT fills for the trade group, ordered chronologically."""
    return (
        session.query(TransactionEvent)
        .filter(
            TransactionEvent.trade_id == trade_id,
            TransactionEvent.leg_type.in_([LegType.CLOSE, LegType.PROFIT_TAKER]),
            TransactionEvent.action.in_([
                TransactionAction.FILLED,
                TransactionAction.PARTIAL_FILL,
            ]),
        )
        .order_by(TransactionEvent.id.asc())
        .all()
    )


def _multiplier_for(close_legs: list[TransactionEvent]) -> Decimal | None:
    """Return multiplier from the close fills (any of them; should agree).

    Returns None if no FUT row carries a usable multiplier.
    """
    for leg in close_legs:
        sec = (leg.security_type or "").upper()
        if sec != "FUT":
            continue
        m = leg.multiplier
        if not m:
            continue
        try:
            mult = Decimal(str(m))
        except Exception:
            continue
        if mult > 0:
            return mult
    return None


def _recompute(entry: TransactionEvent, closes: list[TransactionEvent],
               multiplier: Decimal) -> Decimal:
    """Mirror engine/order.py post-fix formula."""
    entry_price = (
        Decimal(str(entry.ib_avg_fill_price))
        if entry.ib_avg_fill_price is not None else Decimal("0")
    )
    direction = Decimal("1") if entry.side == "BUY" else Decimal("-1")
    total = Decimal("0")
    for leg in closes:
        qty = leg.ib_filled_qty
        if qty is None or qty == 0:
            continue
        price = leg.ib_avg_fill_price
        if price is None:
            continue
        commission = leg.commission or Decimal("0")
        leg_pnl = (
            (Decimal(str(price)) - entry_price)
            * Decimal(str(qty))
            * multiplier
            * direction
            - Decimal(str(commission))
        )
        total += leg_pnl
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="trader.db")
    parser.add_argument("--apply", action="store_true",
                        help="Write corrections (default is dry-run).")
    parser.add_argument("--serial", type=int, default=None,
                        help="Limit to a single trade serial for spot-check.")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_path = Path(args.db_path).resolve()
    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        return 2

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine, future=True))

    candidates: list[tuple[TradeGroup, Decimal, Decimal]] = []
    skipped_stk = 0
    skipped_no_multiplier = 0
    skipped_no_entry = 0
    skipped_no_close = 0
    skipped_already_correct = 0

    session = Session()
    try:
        q = session.query(TradeGroup).filter(TradeGroup.realized_pnl.isnot(None))
        if args.serial is not None:
            q = q.filter(TradeGroup.serial_number == args.serial)

        for tg in q.order_by(TradeGroup.serial_number.asc()).all():
            closes = _close_fills(session, tg.id)
            if not closes:
                skipped_no_close += 1
                continue

            # Determine multiplier; skip STK and rows with no usable mult.
            multiplier = _multiplier_for(closes)
            if multiplier is None:
                # All FILLED close legs are STK or have no multiplier — nothing to fix.
                skipped_stk += 1
                continue
            if multiplier == Decimal("1"):
                skipped_no_multiplier += 1
                continue

            entry = _entry_fill(session, tg.id)
            if entry is None or entry.ib_avg_fill_price is None:
                skipped_no_entry += 1
                continue

            corrected = _recompute(entry, closes, multiplier)
            existing = Decimal(str(tg.realized_pnl))
            delta = corrected - existing
            if abs(delta) < _EPSILON:
                skipped_already_correct += 1
                continue

            candidates.append((tg, existing, corrected))
            logger.info(
                "  serial=%s symbol=%s mult=%s  %s  ->  %s  (Δ %+.2f)",
                tg.serial_number, tg.symbol, multiplier,
                f"${existing}", f"${corrected}", float(delta),
            )
    finally:
        Session.remove()

    logger.info(
        "scanned: candidates=%d skipped_already_correct=%d skipped_stk=%d "
        "skipped_no_multiplier=%d skipped_no_entry=%d skipped_no_close=%d",
        len(candidates), skipped_already_correct, skipped_stk,
        skipped_no_multiplier, skipped_no_entry, skipped_no_close,
    )

    if not args.apply:
        logger.info("[DRY-RUN] no rows written. Re-run with --apply to commit.")
        return 0

    session = Session()
    try:
        written = 0
        for tg_stale, _existing, corrected in candidates:
            tg = session.query(TradeGroup).filter(TradeGroup.id == tg_stale.id).one()
            tg.realized_pnl = corrected
            written += 1
        session.commit()
        logger.info("[APPLY] wrote %d trade_groups", written)
    finally:
        Session.remove()

    return 0


if __name__ == "__main__":
    sys.exit(main())
