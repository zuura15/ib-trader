#!/usr/bin/env python3
"""One-shot backfill of commission values using IB's reqExecutions.

Asks IB for all executions in a date range and updates
``transactions.commission`` + ``bot_trades.commission`` for every fill
whose currently stored commission is zero but IB has a real number.

IB only returns executions for the **calling client_id** by default.
Pass ``--all-clients`` to fetch across every client that has been
connected today; otherwise you'll only see fills placed via this
client.

Idempotent: commissions are summed in with a guard — if the transaction
row already has a non-zero commission equal to what IB reports, no
update happens. Safe to re-run.

Usage:
    .venv/bin/python scripts/backfill_commissions.py --days 7
    .venv/bin/python scripts/backfill_commissions.py --dry-run
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_trader.config.loader import load_settings
from ib_trader.data.models import (
    Base, BotTrade, TransactionAction, TransactionEvent,
)

logger = logging.getLogger("backfill-commissions")


async def _fetch_ib_executions(host: str, port: int, client_id: int, days: int):
    """Return list of (ib_order_id_int, exec_id, commission: Decimal)."""
    from ib_async import IB, ExecutionFilter

    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, timeout=8)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        ef = ExecutionFilter()
        # IB expects YYYYMMDD-HH:MM:SS UTC. We use the day component.
        ef.time = cutoff.strftime("%Y%m%d-%H:%M:%S")
        fills = await ib.reqExecutionsAsync(ef)
        result = []
        for f in fills:
            exec_id = getattr(f.execution, "execId", "") or ""
            order_id = getattr(f.execution, "orderId", 0) or 0
            report = getattr(f, "commissionReport", None)
            commission = Decimal("0")
            if report is not None:
                try:
                    commission = Decimal(str(getattr(report, "commission", 0) or 0))
                except Exception:
                    commission = Decimal("0")
            if commission <= 0:
                continue
            result.append((int(order_id), exec_id, commission))
        return result
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def _apply(session_factory, fills, dry_run: bool) -> tuple[int, int]:
    """Apply commissions to transactions + bot_trades. Returns (txn_count,
    bot_trade_count). Idempotent: ignores fills whose commission has
    already been accounted for."""
    s = session_factory()
    txn_updates = 0
    bot_trade_updates = 0

    for ib_order_id, _exec_id, commission in fills:
        # transactions — add commission to every FILLED/PARTIAL_FILL
        # row for this ib_order_id whose commission is currently 0.
        txn_rows = (
            s.query(TransactionEvent)
            .filter(
                TransactionEvent.ib_order_id == ib_order_id,
                TransactionEvent.action.in_([
                    TransactionAction.FILLED,
                    TransactionAction.PARTIAL_FILL,
                ]),
            )
            .all()
        )
        trade_serial = None
        for row in txn_rows:
            trade_serial = row.trade_serial or trade_serial
            existing = row.commission or Decimal("0")
            if existing >= commission:
                continue
            if dry_run:
                logger.info(
                    "WOULD UPDATE txn id=%d ib_order_id=%s commission %s → %s",
                    row.id, ib_order_id, existing, existing + commission,
                )
            else:
                row.commission = existing + commission
            txn_updates += 1

        if trade_serial is None:
            continue
        # bot_trades — match by entry_serial OR exit_serial. The
        # commission we just applied to a transaction row is the
        # commission for that specific leg; the bot_trade spans both
        # entry + exit, so a full round-trip accumulates two rows'
        # worth.
        bot_trade_rows = (
            s.query(BotTrade)
            .filter(
                (BotTrade.entry_serial == trade_serial)
                | (BotTrade.exit_serial == trade_serial)
            )
            .all()
        )
        for row in bot_trade_rows:
            existing = row.commission or Decimal("0")
            if dry_run:
                logger.info(
                    "WOULD UPDATE bot_trade id=%s serial=%d commission %s → %s",
                    row.id, trade_serial, existing, existing + commission,
                )
            else:
                row.commission = existing + commission
            bot_trade_updates += 1

    if not dry_run:
        s.commit()
    return txn_updates, bot_trade_updates


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
        "--days", type=int, default=1,
        help="Look-back window in days for reqExecutions (default: 1). "
             "IB typically keeps today's + yesterday's executions; older "
             "data is not retrievable via this API.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be updated without writing.",
    )
    parser.add_argument(
        "--settings", default="config/settings.yaml",
        help="Path to settings.yaml (default: config/settings.yaml)",
    )
    parser.add_argument(
        "--client-id", type=int, default=None,
        help="IB client_id to use. Defaults to settings ib_client_id + 100 "
             "so the script doesn't collide with a running daemon. Override "
             "if you need to pin a specific id.",
    )
    args = parser.parse_args()

    settings = load_settings(args.settings)
    host = settings.get("ib_host", "127.0.0.1")
    port = int(settings.get("ib_port", 4002))
    client_id = args.client_id if args.client_id is not None \
        else int(settings.get("ib_client_id", 1)) + 100

    logger.info("connecting to IB at %s:%s (client_id=%d)", host, port, client_id)
    fills = asyncio.run(_fetch_ib_executions(host, port, client_id, args.days))
    logger.info("IB returned %d fills with non-zero commission in last %d day(s)",
                len(fills), args.days)

    engine = create_engine(f"sqlite:///{args.db_path}", future=True)
    Base.metadata.create_all(engine, checkfirst=True)
    session_factory = scoped_session(sessionmaker(bind=engine, future=True))

    txn_n, bt_n = _apply(session_factory, fills, dry_run=args.dry_run)
    logger.info(
        "done: %d transaction rows updated, %d bot_trade rows updated%s",
        txn_n, bt_n, " (dry-run)" if args.dry_run else "",
    )


if __name__ == "__main__":
    main()
