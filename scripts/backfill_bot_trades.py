#!/usr/bin/env python3
"""One-shot backfill of the bot_trades table from historical bot_events.

Walks the ``bot_events`` table (event_type='FILL'), pairs each bot's
BUY fills with its next SELL fill (same bot_id + symbol, ordered by
recorded_at), and inserts a ``bot_trades`` row for each pair.

Why bot_events and not transactions? The IB orderRef that identifies
bot ownership is never persisted to SQLite — it only lives in IB's
memory and in the Redis order:updates stream. But the bot runtime
writes one FILL row per terminal order to ``bot_events`` with the
bot_id stamped directly, so that's the authoritative source for
"which fill belonged to which bot".

Idempotent: skips any pair whose (bot_id, entry_time, exit_time) is
already present in bot_trades. Safe to re-run.

Historical caveats:
- ``trail_reset_count`` is 0 for backfilled rows — the counter was
  introduced in the same commit as the bot_trades table; prior runs
  never tracked HWM ratchets.
- ``entry_serial`` / ``exit_serial`` are whatever the bot_event row
  stored (often 0 for older runs — ignored for identity purposes).
- Fills with qty=0 or price=0 are skipped (the bot runtime emits
  a FILL audit row on every dispatch, including non-terminal
  placeholders that never got real data).
- If a symbol has unmatched BUYs (never sold), they're left as open
  positions — those belong in the live ``bots`` panel, not in
  completed ``bot_trades``.

Usage:
    .venv/bin/python scripts/backfill_bot_trades.py
    .venv/bin/python scripts/backfill_bot_trades.py --dry-run
    .venv/bin/python scripts/backfill_bot_trades.py --db-path trader.db
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Optional

import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import scoped_session, sessionmaker

# Repo root on path so we can import ib_trader.*
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ib_trader.data.models import Base, BotEvent, BotTrade

logger = logging.getLogger("backfill")

# "BUY 10.0 QQQ @ 648.00 (serial=42)"  or  "SELL 15 F @ 12.69"
_MESSAGE_RE = re.compile(
    r"^(?P<side>BUY|SELL)\s+(?P<qty>-?\d+(?:\.\d+)?)\s+(?P<symbol>\w+)\s+@\s+(?P<price>-?\d+(?:\.\d+)?)"
)


def _load_bot_registry(repo_root: Path) -> dict[str, str]:
    """Return a map of bot_id → bot_name from the YAML configs.

    Backfilled rows record the bot_id from the bot_events row directly;
    we only need the registry for display names.
    """
    registry: dict[str, str] = {}
    bots_dir = repo_root / "config" / "bots"
    for bot_yaml in bots_dir.glob("*.yaml"):
        try:
            with open(bot_yaml) as f:
                bot_cfg = yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning("failed to parse %s: %s", bot_yaml, e)
            continue
        bot_id = bot_cfg.get("id")
        if bot_id:
            registry[bot_id] = bot_cfg.get("name") or ""
    return registry


def _parse_fill(ev: BotEvent) -> Optional[dict]:
    """Return a normalized fill dict or None if the row is unusable.

    Shape: {side: 'BUY'|'SELL', qty: Decimal, symbol: str, price: Decimal,
            commission: Decimal, recorded_at: datetime}
    """
    msg = ev.message or ""
    match = _MESSAGE_RE.match(msg)
    if not match:
        return None
    side = match.group("side")
    try:
        # Historical BUY rows sometimes carried a negative qty (a bug in
        # the audit format). Take the absolute value and trust ``side``
        # as the authoritative direction.
        qty = abs(Decimal(match.group("qty")))
        price = Decimal(match.group("price"))
    except Exception:
        return None
    if qty <= 0 or price <= 0:
        return None
    symbol = match.group("symbol")

    # Commission lives in payload_json.
    commission = Decimal("0")
    if ev.payload_json:
        try:
            payload = json.loads(ev.payload_json)
            c = payload.get("commission")
            if c is not None:
                commission = Decimal(str(c))
        except Exception:
            pass

    ts = ev.recorded_at
    if ts is not None and ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return {
        "side": side,
        "qty": qty,
        "symbol": symbol,
        "price": price,
        "commission": commission,
        "recorded_at": ts,
        "trade_serial": ev.trade_serial or 0,
    }


def _sig_key(dt: Optional[datetime]) -> str:
    """Stable signature string for a datetime across naive/aware round-trips.

    SQLite strips tzinfo on reload (BotTrade rows read back are naive),
    while newly-constructed fills from bot_events are aware. Normalize
    both to a tz-naive microsecond-precision ISO string.
    """
    if dt is None:
        return ""
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat(timespec="microseconds")


def _existing_signatures(session) -> set[tuple[str, str, str]]:
    """Return set of (bot_id, entry_time_key, exit_time_key) already present.

    Time-based identity works because bot_events timestamps have ~µs
    resolution and a single bot can't have two round-trips with the
    same (entry_time, exit_time).
    """
    sigs = set()
    for row in session.query(BotTrade).all():
        if row.entry_time and row.exit_time:
            sigs.add((
                row.bot_id,
                _sig_key(row.entry_time),
                _sig_key(row.exit_time),
            ))
    return sigs


def backfill(db_path: str, repo_root: Path, dry_run: bool = False) -> int:
    engine = create_engine(f"sqlite:///{db_path}", future=True)
    Base.metadata.create_all(engine, checkfirst=True)
    session_factory = scoped_session(sessionmaker(bind=engine, future=True))
    session = session_factory()

    registry = _load_bot_registry(repo_root)
    logger.info("loaded %d bot(s) from YAML configs", len(registry))
    existing = _existing_signatures(session)
    logger.info("existing bot_trades rows: %d", len(existing))

    # Pull FILLs and bucket by (bot_id, symbol) after parsing.
    events = (
        session.query(BotEvent)
        .filter(BotEvent.event_type == "FILL")
        .order_by(BotEvent.recorded_at.asc(), BotEvent.id.asc())
        .all()
    )
    buckets: dict[tuple[str, str], list[tuple[BotEvent, dict]]] = defaultdict(list)
    for ev in events:
        fill = _parse_fill(ev)
        if fill is None:
            continue
        buckets[(ev.bot_id, fill["symbol"])].append((ev, fill))

    inserted = 0
    skipped_exists = 0
    unmatched_entries = 0
    now = datetime.now(timezone.utc)

    for (bot_id, symbol), rows in buckets.items():
        pending: Optional[dict] = None
        for _ev, fill in rows:
            if fill["side"] == "BUY":
                pending = fill
                continue
            # SELL — close out pending entry.
            if pending is None:
                continue
            entry_time = pending["recorded_at"]
            exit_time = fill["recorded_at"]
            if entry_time is None or exit_time is None:
                pending = None
                continue
            signature = (bot_id, _sig_key(entry_time), _sig_key(exit_time))
            if signature in existing:
                skipped_exists += 1
                pending = None
                continue
            entry_price = pending["price"]
            exit_price = fill["price"]
            qty = min(pending["qty"], fill["qty"])
            realized_pnl = (exit_price - entry_price) * qty
            total_commission = pending["commission"] + fill["commission"]
            duration = int((exit_time - entry_time).total_seconds())

            row = BotTrade(
                bot_id=bot_id,
                bot_name=registry.get(bot_id) or None,
                symbol=symbol,
                direction="LONG",
                entry_price=entry_price,
                entry_qty=pending["qty"],
                entry_time=entry_time,
                exit_price=exit_price,
                exit_qty=fill["qty"],
                exit_time=exit_time,
                realized_pnl=realized_pnl,
                commission=total_commission,
                trail_reset_count=0,
                duration_seconds=duration,
                entry_serial=pending["trade_serial"] or None,
                exit_serial=fill["trade_serial"] or None,
                created_at=now,
            )
            if dry_run:
                logger.info(
                    "WOULD INSERT bot=%s symbol=%s entry=%s@%s exit=%s@%s "
                    "pnl=%s dur=%ds",
                    registry.get(bot_id, bot_id[:8]), symbol,
                    pending["qty"], entry_price,
                    fill["qty"], exit_price, realized_pnl, duration,
                )
            else:
                session.add(row)
                existing.add(signature)
            inserted += 1
            pending = None
        if pending is not None:
            unmatched_entries += 1

    if not dry_run:
        session.commit()

    logger.info(
        "done: %d inserted, %d skipped (already exists), %d open "
        "positions left unmatched",
        inserted, skipped_exists, unmatched_entries,
    )
    return inserted


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path", default="trader.db",
        help="SQLite DB path (default: trader.db in repo root)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be inserted without writing.",
    )
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    backfill(args.db_path, repo_root, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
