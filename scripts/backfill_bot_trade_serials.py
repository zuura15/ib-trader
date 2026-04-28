#!/usr/bin/env python3
"""One-shot migration: populate ``bot_trades.entry_serial`` /
``exit_serial`` for historical rows that were written before the
runtime started resolving serials at close time.

Every bot_trade maps to exactly one entry fill and one exit fill in
the ``transactions`` table. The link went missing because the bot's
state doc never populated ``"serial"`` — so both columns landed as
NULL and the commission callback's ``add_commission_by_serial``
couldn't find anything to update.

Matching heuristic (SQLite-only, no IB required):
  - Entry: FILLED where symbol == bot_trade.symbol, side == BUY
    (LONG direction), quantity ≈ entry_qty, requested_at within
    ±MATCH_WINDOW of entry_time.
  - Exit: same shape, side == SELL.

After populating serials, the user can re-run
``backfill_commissions.py`` and the commissions already in
transactions.commission will flow into bot_trades via the existing
``add_commission_by_serial`` path. Idempotent — only writes rows
where serials are currently NULL.

Usage:
    .venv/bin/python scripts/backfill_bot_trade_serials.py
    .venv/bin/python scripts/backfill_bot_trade_serials.py --dry-run
"""
from __future__ import annotations

import argparse
import logging
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_trader.data.models import (
    Base, BotTrade, TransactionAction, TransactionEvent,
)

logger = logging.getLogger("backfill-bot-trade-serials")

# How far before/after bot_trade.entry_time or exit_time we look for
# a matching transaction. IB fills usually land within a few seconds of
# the order placement; a 60 s window is generous.
_MATCH_WINDOW = timedelta(seconds=60)


def _find_matching_serial(
    s, symbol: str, side: str, qty: Decimal, when,
):
    """Return the trade_serial of the closest FILLED transaction
    matching (symbol, side, ~qty) within ±_MATCH_WINDOW of ``when``,
    or None. Prefers the row whose requested_at is closest to ``when``.
    """
    if when is None:
        return None
    lo = when - _MATCH_WINDOW
    hi = when + _MATCH_WINDOW
    # quantity tolerance: 1% or 1 share, whichever is larger
    tol = max(qty * Decimal("0.01"), Decimal("1"))
    q_lo = qty - tol
    q_hi = qty + tol
    candidates = (
        s.query(TransactionEvent)
        .filter(
            TransactionEvent.symbol == symbol,
            TransactionEvent.side == side,
            TransactionEvent.action.in_([
                TransactionAction.FILLED,
                TransactionAction.PARTIAL_FILL,
            ]),
            TransactionEvent.requested_at.between(lo, hi),
            TransactionEvent.quantity.between(q_lo, q_hi),
            TransactionEvent.trade_serial.isnot(None),
        )
        .all()
    )
    if not candidates:
        return None
    # Closest in time
    best = min(candidates, key=lambda r: abs((r.requested_at - when).total_seconds()))
    return best.trade_serial


def _backfill(session_factory, dry_run: bool) -> tuple[int, int, int]:
    """Walk bot_trades with NULL serials and try to populate them.
    Returns (scanned, matched_entry, matched_exit)."""
    s = session_factory()
    rows = (
        s.query(BotTrade)
        .filter((BotTrade.entry_serial.is_(None)) | (BotTrade.exit_serial.is_(None)))
        .all()
    )
    scanned = len(rows)
    entry_hits = 0
    exit_hits = 0

    for row in rows:
        direction = (row.direction or "LONG").upper()
        entry_side = "BUY" if direction == "LONG" else "SELL"
        exit_side = "SELL" if direction == "LONG" else "BUY"

        new_entry = row.entry_serial
        new_exit = row.exit_serial
        if new_entry is None:
            matched = _find_matching_serial(
                s, row.symbol, entry_side, row.entry_qty, row.entry_time,
            )
            if matched is not None:
                new_entry = matched
                entry_hits += 1
        if new_exit is None:
            matched = _find_matching_serial(
                s, row.symbol, exit_side, row.exit_qty, row.exit_time,
            )
            if matched is not None:
                new_exit = matched
                exit_hits += 1

        if new_entry == row.entry_serial and new_exit == row.exit_serial:
            continue
        if dry_run:
            logger.info(
                "WOULD UPDATE bot_trade id=%s symbol=%s entry_serial %s→%s exit_serial %s→%s",
                row.id, row.symbol, row.entry_serial, new_entry,
                row.exit_serial, new_exit,
            )
        else:
            row.entry_serial = new_entry
            row.exit_serial = new_exit
    if not dry_run:
        s.commit()
    return scanned, entry_hits, exit_hits


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path", default="trader.db",
        help="SQLite DB path (default: trader.db)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be updated without writing.",
    )
    args = parser.parse_args()

    engine = create_engine(f"sqlite:///{args.db_path}", future=True)
    Base.metadata.create_all(engine, checkfirst=True)
    session_factory = scoped_session(sessionmaker(bind=engine, future=True))

    scanned, entry_hits, exit_hits = _backfill(session_factory, dry_run=args.dry_run)
    logger.info(
        "done: scanned %d bot_trades, matched %d entry_serials, "
        "%d exit_serials%s",
        scanned, entry_hits, exit_hits,
        " (dry-run)" if args.dry_run else "",
    )


if __name__ == "__main__":
    main()
