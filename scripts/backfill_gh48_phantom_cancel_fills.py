#!/usr/bin/env python3
"""Backfill missing FILLED transaction rows for GH #48 phantom-cancel victims.

Symptom (pre-fix): a synthetic ``Cancelled`` status produced by ib_async
for IB error 462 ("Cannot change to the new Time in Force.DAY") set
``tracker.is_canceled`` immediately, which short-circuited the strategy's
``_cancel_and_await_resolution`` loop on its first iteration. The strategy
wrote a CANCELLED transaction row and marked the trade group CLOSED before
the actual fills landed 18-120 seconds later. Those fills flowed through
the global ``on_fill`` → OrderLedger path (so position state stayed
correct), but they never wrote a FILLED row in ``transactions`` because
the strategy that owned that responsibility had already exited.

Result: trade groups in CLOSED status with no entry-fill row → trades
panel shows ``—`` for strategy / qty / price / commission / pnl despite
the underlying order having actually filled at IB.

Recovery: the engine logs preserve every fill via ``FILL_RELAYED`` lines
keyed by ``ib_order_id``. This script:

  1. Finds CLOSED trade groups whose entry leg has a CANCELLED row but no
     FILLED / PARTIAL_FILL row, AND a known ib_order_id from the
     PLACE_ACCEPTED row.
  2. Scans engine logs (current + rotated + .gz) for ``FILL_RELAYED``
     entries matching that ib_order_id.
  3. Sums per-fill quantity, computes weighted-avg fill price.
  4. Appends a single FILLED transaction row tagged with
     ``correlation_id = "backfill-gh48"`` so it is identifiable and
     reversible.

The append-only contract is preserved: the existing CANCELLED row stays
as historical truth of what the engine *thought* happened. ``get_entry_fill``
prefers FILLED, so the trades panel renders the right data immediately.

Idempotent: if a FILLED row tagged ``backfill-gh48`` already exists for
a trade, the trade is skipped.

Usage:
    .venv/bin/python scripts/backfill_gh48_phantom_cancel_fills.py            # dry-run
    .venv/bin/python scripts/backfill_gh48_phantom_cancel_fills.py --apply    # write
    .venv/bin/python scripts/backfill_gh48_phantom_cancel_fills.py --apply --serial 376
"""
from __future__ import annotations

import argparse
import gzip
import json
import logging
import re
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_trader.data.models import (  # noqa: E402
    Base, LegType, TradeGroup, TransactionAction, TransactionEvent,
)
from ib_trader.data.repositories.transaction_repository import (  # noqa: E402
    TransactionRepository,
)

logger = logging.getLogger("backfill-gh48")

BACKFILL_TAG = "backfill-gh48"


def _open_log(path: Path):
    """Open a log file, transparently handling .gz."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("rt", encoding="utf-8", errors="replace")


def _scan_fills(log_dir: Path) -> dict[int, list[tuple[Decimal, Decimal]]]:
    """Return {ib_order_id: [(qty, price), ...]} from FILL_RELAYED log lines."""
    fills: dict[int, list[tuple[Decimal, Decimal]]] = {}
    pattern = re.compile(r'"event":\s*"FILL_RELAYED"')
    log_files = sorted(log_dir.glob("ib_trader.log*"))
    for path in log_files:
        try:
            with _open_log(path) as fh:
                for line in fh:
                    if "FILL_RELAYED" not in line:
                        continue
                    if not pattern.search(line):
                        continue
                    try:
                        rec = json.loads(line)
                    except (json.JSONDecodeError, ValueError):
                        continue
                    try:
                        ib_id = int(rec["ib_order_id"])
                        qty = Decimal(str(rec["qty"]))
                        price = Decimal(str(rec["price"]))
                    except (KeyError, ValueError, TypeError):
                        continue
                    fills.setdefault(ib_id, []).append((qty, price))
        except OSError as exc:
            logger.warning("could not read %s: %s", path, exc)
    return fills


def _find_victim_trades(session) -> list[tuple[TradeGroup, TransactionEvent]]:
    """Trade groups whose ENTRY leg has CANCELLED but no FILLED/PARTIAL_FILL.

    Returns (trade_group, place_accepted_txn) pairs — the PLACE_ACCEPTED row
    carries the ib_order_id we need to match log lines.
    """
    victims: list[tuple[TradeGroup, TransactionEvent]] = []
    trades = session.query(TradeGroup).order_by(TradeGroup.serial_number).all()
    for tg in trades:
        entry_txns = (
            session.query(TransactionEvent)
            .filter(
                TransactionEvent.trade_id == tg.id,
                TransactionEvent.leg_type == LegType.ENTRY,
            )
            .order_by(TransactionEvent.id.asc())
            .all()
        )
        if not entry_txns:
            continue
        actions = {t.action for t in entry_txns}
        if TransactionAction.FILLED in actions or TransactionAction.PARTIAL_FILL in actions:
            continue
        if TransactionAction.CANCELLED not in actions:
            continue
        # Already backfilled?
        if any(
            t.correlation_id == BACKFILL_TAG and t.action == TransactionAction.FILLED
            for t in entry_txns
        ):
            continue
        accepted = next(
            (t for t in entry_txns if t.action == TransactionAction.PLACE_ACCEPTED),
            None,
        )
        if accepted is None or accepted.ib_order_id is None:
            continue
        victims.append((tg, accepted))
    return victims


def _aggregate(fills: list[tuple[Decimal, Decimal]]) -> tuple[Decimal, Decimal]:
    """Sum qty and compute weighted-avg price across a list of fills."""
    total_qty = Decimal("0")
    notional = Decimal("0")
    for qty, price in fills:
        total_qty += qty
        notional += qty * price
    avg = (notional / total_qty).quantize(Decimal("0.0001")) if total_qty > 0 else Decimal("0")
    return total_qty, avg


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="trader.db", help="SQLite DB path")
    parser.add_argument(
        "--logs-dir", default="logs", help="Directory containing ib_trader.log*",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Actually write the FILLED rows. Default is dry-run.",
    )
    parser.add_argument(
        "--serial", type=int, default=None,
        help="Restrict to a single trade serial (useful for testing).",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_path = Path(args.db_path).resolve()
    logs_dir = Path(args.logs_dir).resolve()
    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        return 2
    if not logs_dir.is_dir():
        logger.error("logs dir not found: %s", logs_dir)
        return 2

    logger.info("scanning %s for FILL_RELAYED events…", logs_dir)
    fills_by_order = _scan_fills(logs_dir)
    logger.info("found fills for %d distinct ib_order_ids", len(fills_by_order))

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine, future=True))
    txns = TransactionRepository(Session)

    session = Session()
    try:
        victims = _find_victim_trades(session)
    finally:
        Session.remove()

    if args.serial is not None:
        victims = [(tg, acc) for tg, acc in victims if tg.serial_number == args.serial]

    logger.info("found %d trade group(s) with phantom-cancel pattern", len(victims))

    written = 0
    skipped_no_fills = 0
    for tg, accepted in victims:
        ib_id = int(accepted.ib_order_id)
        fills = fills_by_order.get(ib_id, [])
        if not fills:
            logger.info(
                "  skip serial=%s symbol=%s ib_order_id=%s — no FILL_RELAYED in logs",
                tg.serial_number, tg.symbol, ib_id,
            )
            skipped_no_fills += 1
            continue
        total_qty, avg_price = _aggregate(fills)
        logger.info(
            "  serial=%s symbol=%s side=%s ib_order_id=%s qty=%s avg=%s (%d fills)",
            tg.serial_number, tg.symbol, accepted.side, ib_id,
            total_qty, avg_price, len(fills),
        )
        if not args.apply:
            continue
        event = TransactionEvent(
            ib_order_id=ib_id,
            ib_perm_id=accepted.ib_perm_id,
            action=TransactionAction.FILLED,
            symbol=accepted.symbol,
            side=accepted.side,
            order_type=accepted.order_type,
            quantity=accepted.quantity,
            limit_price=accepted.limit_price,
            account_id=accepted.account_id,
            ib_status="Filled",
            ib_filled_qty=total_qty,
            ib_avg_fill_price=avg_price,
            trade_serial=tg.serial_number,
            requested_at=datetime.now(timezone.utc),
            ib_responded_at=datetime.now(timezone.utc),
            is_terminal=True,
            trade_id=tg.id,
            leg_type=LegType.ENTRY,
            commission=None,
            price_placed=accepted.price_placed,
            correlation_id=BACKFILL_TAG,
            security_type=accepted.security_type,
            expiry=accepted.expiry,
            strike=accepted.strike,
            right=accepted.right,
        )
        txns.insert(event)
        written += 1

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info(
        "[%s] candidates=%d written=%d skipped_no_fills=%d",
        mode, len(victims), written, skipped_no_fills,
    )
    if not args.apply and victims:
        logger.info("re-run with --apply to write the rows")
    return 0


if __name__ == "__main__":
    sys.exit(main())
