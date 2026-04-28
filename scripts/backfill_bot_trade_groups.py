#!/usr/bin/env python3
"""One-shot migration: mark bot-owned trade_groups CLOSED and populate
realized_pnl from the bot_trades table.

Before the bot runtime wrote back to trade_groups, every bot-placed
entry and exit order left its trade_group as ``status=OPEN,
realized_pnl=NULL`` — the Trades panel then rendered $0 for every row.
This script retro-fixes those rows using the bot_trades table (which
records the round-trip with correct realized_pnl and entry_serial /
exit_serial links).

Rules (per row):
  - EXIT trade_group  → status=CLOSED, realized_pnl=bot_trade.realized_pnl,
    total_commission=bot_trade.commission, closed_at=bot_trade.exit_time
  - ENTRY trade_group → status=CLOSED, realized_pnl=0 (placeholder;
    aggregate by summing CLOSED rows), total_commission=0,
    closed_at=bot_trade.exit_time

Idempotent: only touches trade_groups currently at status=OPEN AND
realized_pnl is NULL — re-running is safe.

Usage:
    .venv/bin/python scripts/backfill_bot_trade_groups.py --dry-run
    .venv/bin/python scripts/backfill_bot_trade_groups.py
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
    Base, BotTrade, TradeGroup, TradeStatus,
)

logger = logging.getLogger("backfill-bot-trade-groups")


def _apply(session_factory, dry_run: bool) -> tuple[int, int, int]:
    """Return (scanned, entry_updates, exit_updates)."""
    s = session_factory()
    rows = s.query(BotTrade).filter(
        (BotTrade.entry_serial.isnot(None))
        | (BotTrade.exit_serial.isnot(None))
    ).all()

    entry_hits = 0
    exit_hits = 0

    for bt in rows:
        if bt.exit_serial is not None:
            tg = s.query(TradeGroup).filter(TradeGroup.serial_number == int(bt.exit_serial)).first()
            if tg is not None and tg.status == TradeStatus.OPEN and tg.realized_pnl is None:
                if dry_run:
                    logger.info(
                        "WOULD CLOSE exit tg serial=%s id=%s pnl=%s commission=%s",
                        bt.exit_serial, tg.id, bt.realized_pnl, bt.commission,
                    )
                else:
                    tg.realized_pnl = bt.realized_pnl or Decimal("0")
                    tg.total_commission = bt.commission or Decimal("0")
                    tg.status = TradeStatus.CLOSED
                    tg.closed_at = bt.exit_time
                exit_hits += 1

        if bt.entry_serial is not None:
            tg = s.query(TradeGroup).filter(TradeGroup.serial_number == int(bt.entry_serial)).first()
            if tg is not None and tg.status == TradeStatus.OPEN and tg.realized_pnl is None:
                if dry_run:
                    logger.info(
                        "WOULD CLOSE entry tg serial=%s id=%s pnl=0",
                        bt.entry_serial, tg.id,
                    )
                else:
                    tg.realized_pnl = Decimal("0")
                    tg.total_commission = Decimal("0")
                    tg.status = TradeStatus.CLOSED
                    tg.closed_at = bt.exit_time
                entry_hits += 1

    if not dry_run:
        s.commit()
    return len(rows), entry_hits, exit_hits


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

    scanned, entry_hits, exit_hits = _apply(session_factory, dry_run=args.dry_run)
    logger.info(
        "done: scanned %d bot_trades, closed %d entry trade_groups, %d exit trade_groups%s",
        scanned, entry_hits, exit_hits,
        " (dry-run)" if args.dry_run else "",
    )


if __name__ == "__main__":
    main()
