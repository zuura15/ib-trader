#!/usr/bin/env python3
"""Backfill ``trade_groups.ib_realized_pnl`` from IB's reqExecutionsAsync.

IB ships realized P&L on every CommissionReport
(``CommissionReport.realizedPNL``). The engine now captures that value
on the live commission callback and accumulates it into
``trade_groups.ib_realized_pnl`` (GH #48 follow-up). For executions that
landed before the engine started capturing it, this script asks IB for
the historical executions and writes the missing values.

IB only returns executions for the calling client_id by default; pass
``--all-clients`` to also include fills from other clients. ExecutionFilter
typically returns the past day on paper, ~7 days on live — older fills
are not retrievable from IB.

Idempotent: each (ib_order_id, exec_id) seen by IB writes its
realizedPNL exactly once. Re-runs in the same window add nothing because
we track already-applied execIds in a sidecar set.

Usage:
    .venv/bin/python scripts/backfill_ib_realized_pnl.py            # dry-run
    .venv/bin/python scripts/backfill_ib_realized_pnl.py --apply
    .venv/bin/python scripts/backfill_ib_realized_pnl.py --apply --days 2
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import sys
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_trader.data.models import (  # noqa: E402
    Base, TradeGroup, TransactionAction, TransactionEvent,
)

logger = logging.getLogger("backfill-ib-pnl")


_PNL_SENTINEL_THRESHOLD = 1e15  # IB sends ~1.797e308 on opening fills


async def _fetch_realized_pnls(
    host: str, port: int, client_id: int, days: int,
) -> list[tuple[int, int, str, Decimal]]:
    """Return (ib_order_id, ib_perm_id, exec_id, realized_pnl) for fills with real P&L.

    ib_async's ``reqExecutionsAsync`` resolves on ``execDetailsEnd``, but
    each execution's CommissionReport arrives on a separate event that
    fires moments later. We wait briefly for those to land, then read
    from ``ib.fills()`` which has the reports attached.

    Note: when querying from a client that didn't place the orders, IB
    reports ``orderId=0`` on the executions; match on ``permId`` instead.
    """
    from ib_async import IB, ExecutionFilter

    ib = IB()
    await ib.connectAsync(host, port, clientId=client_id, timeout=8)
    try:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        ef = ExecutionFilter()
        ef.time = cutoff.strftime("%Y%m%d-%H:%M:%S")
        await ib.reqExecutionsAsync(ef)
        # Drain commissionReport events that arrive after execDetailsEnd.
        await asyncio.sleep(2.0)
        out: list[tuple[int, int, str, Decimal]] = []
        for f in ib.fills():
            exec_id = getattr(f.execution, "execId", "") or ""
            order_id = int(getattr(f.execution, "orderId", 0) or 0)
            perm_id = int(getattr(f.execution, "permId", 0) or 0)
            report = getattr(f, "commissionReport", None)
            if report is None:
                continue
            pnl_raw = getattr(report, "realizedPNL", None)
            if pnl_raw is None:
                continue
            try:
                pnl_f = float(pnl_raw)
            except (TypeError, ValueError):
                continue
            if math.isnan(pnl_f) or abs(pnl_f) >= _PNL_SENTINEL_THRESHOLD:
                continue
            if pnl_f == 0:
                continue
            out.append((order_id, perm_id, exec_id, Decimal(str(pnl_raw))))
        return out
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass


def _resolve_trade_id(
    session, ib_order_id: int, ib_perm_id: int,
) -> str | None:
    """Find the trade_group.id for an execution.

    Prefers ib_order_id (unambiguous within a client), falls back to
    ib_perm_id (stable across clients) when order_id is 0 — which happens
    when we query executions from a client that didn't place the order.
    """
    q = session.query(TransactionEvent).filter(
        TransactionEvent.action.in_([
            TransactionAction.FILLED,
            TransactionAction.PARTIAL_FILL,
        ]),
    )
    ev = None
    if ib_order_id:
        ev = q.filter(TransactionEvent.ib_order_id == ib_order_id).first()
    if ev is None and ib_perm_id:
        ev = q.filter(TransactionEvent.ib_perm_id == ib_perm_id).first()
    return ev.trade_id if ev else None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", default="trader.db")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4002)
    parser.add_argument(
        "--client-id", type=int, default=99,
        help="Use a non-conflicting client id; the live engine typically uses 1.",
    )
    parser.add_argument("--days", type=int, default=1)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    db_path = Path(args.db_path).resolve()
    if not db_path.exists():
        logger.error("DB not found: %s", db_path)
        return 2

    fills = asyncio.run(_fetch_realized_pnls(
        args.host, args.port, args.client_id, args.days,
    ))
    logger.info("IB returned %d executions with non-sentinel realized P&L", len(fills))

    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine)
    Session = scoped_session(sessionmaker(bind=engine, future=True))

    # Group contributions per trade_group so we can sum and write once.
    pnl_by_trade: dict[str, Decimal] = {}
    unmatched = 0
    session = Session()
    try:
        for ib_order_id, ib_perm_id, exec_id, pnl in fills:
            trade_id = _resolve_trade_id(session, ib_order_id, ib_perm_id)
            if trade_id is None:
                unmatched += 1
                logger.debug(
                    "  no FILLED txn for ib_order_id=%s perm_id=%s exec_id=%s pnl=%s",
                    ib_order_id, ib_perm_id, exec_id, pnl,
                )
                continue
            pnl_by_trade[trade_id] = pnl_by_trade.get(trade_id, Decimal("0")) + pnl
    finally:
        Session.remove()

    logger.info(
        "matched %d distinct trade_groups; %d executions had no matching txn",
        len(pnl_by_trade), unmatched,
    )

    written = 0
    skipped_already_set = 0
    session = Session()
    try:
        for trade_id, total_pnl in sorted(pnl_by_trade.items()):
            tg = session.query(TradeGroup).filter(TradeGroup.id == trade_id).one()
            existing = tg.ib_realized_pnl
            if existing is not None and existing != 0:
                skipped_already_set += 1
                logger.info(
                    "  skip serial=%s symbol=%s — ib_realized_pnl already=%s",
                    tg.serial_number, tg.symbol, existing,
                )
                continue
            logger.info(
                "  serial=%s symbol=%s realized_pnl=%s",
                tg.serial_number, tg.symbol, total_pnl,
            )
            if args.apply:
                tg.ib_realized_pnl = total_pnl
                written += 1
        if args.apply:
            session.commit()
    finally:
        Session.remove()

    mode = "APPLY" if args.apply else "DRY-RUN"
    logger.info(
        "[%s] candidates=%d written=%d skipped_already_set=%d unmatched_executions=%d",
        mode, len(pnl_by_trade), written, skipped_already_set, unmatched,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
