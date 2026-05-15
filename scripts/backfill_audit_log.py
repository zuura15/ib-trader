"""Backfill ``audit_log`` from ``bot_events`` for the operator audit feed.

Default cutoff: 2026-05-13 22:00 UTC (= 3 PM PT 5/13, the operator's
chosen "look back to here" point on first deploy).

Strategy:
  - Per bot, walk events in chronological order grouped by minute.
  - For each minute that has a ``BAR`` event, synthesize one BAR_EVAL
    row from BAR + same-minute SKIP/SIGNAL/EXIT_CHECK.
  - For each ORDER event, synthesize one ORDER_PLACED row.
  - For each CLOSED event, synthesize one TRADE_CLOSED row.

Idempotent: deletes prior rows in the backfill window for each bot
before re-inserting. Safe to re-run.

Usage:
    uv run python scripts/backfill_audit_log.py [--cutoff ISO]

The ``--cutoff`` is interpreted as **UTC** and matches against
``bot_events.recorded_at`` directly (recorded_at is server-local PT;
we convert PT → UTC inside the script).
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

# Make ib_trader importable.
_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from ib_trader.data.models import AuditLog, BotEvent, BotTrade
from ib_trader.data.repositories.audit_log_repository import (
    AuditLogRepository,
    EVENT_BAR_EVAL, EVENT_ORDER_PLACED, EVENT_TRADE_CLOSED,
)
from ib_trader.data.repository import create_db_engine, create_session_factory
from sqlalchemy import and_

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("backfill_audit_log")


# bot_events.recorded_at is server-local (PT); audit_log.event_ts_utc is UTC.
def _pt_to_utc(naive_dt: datetime) -> datetime:
    """Treat ``naive_dt`` as PT and shift to UTC. PT = UTC-7 (PDT)
    during May; for production sweeps spanning DST flips, swap to
    a zoneinfo-aware conversion."""
    pt_offset = timedelta(hours=-7)
    return (naive_dt - pt_offset).replace(tzinfo=timezone.utc)


def _safe_json_loads(s: str | None) -> dict:
    if not s:
        return {}
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        return {}


def _derive_pivot_line_decision(
    bar_payload: dict,
    skip_msg: str | None,
    skip_payload: dict | None,
    signal_msg: str | None,
    order_side: str | None,
    exit_msg: str | None,
    exit_payload: dict | None,
) -> tuple[str, str, str]:
    """Mirror chart_signal._synthesize_bar_eval for backfill rows."""
    line_status = "LINES_NONE"
    blt = int((bar_payload or {}).get("best_long_touches") or 0)
    bst = int((bar_payload or {}).get("best_short_touches") or 0)
    if blt > 0 and bst > 0:
        line_status = "LINES_BOTH"
    elif blt > 0:
        line_status = "LINES_LONG"
    elif bst > 0:
        line_status = "LINES_SHORT"

    pivot_status = "NONE"
    decision = ""
    if order_side in ("BUY", "SELL"):
        decision = f"FIRED·{order_side}"
        pivot_status = "PIVOT_LOW" if order_side == "BUY" else "PIVOT_HIGH"
    elif exit_msg:
        # Exit fire (in-position bar).
        if ":" in exit_msg:
            short = exit_msg.split(":", 1)[1].strip()[:30]
        else:
            short = (exit_payload or {}).get("exit_type", "")
        decision = f"EXIT_FIRED·{short}" if short else "EXIT_FIRED·unknown"
        direction = (exit_payload or {}).get("direction", "")
        pivot_status = "PIVOT_LOW" if direction == "short" else "PIVOT_HIGH"
    elif skip_msg:
        filt = (skip_payload or {}).get("filter")
        if filt:
            decision = f"FILTERED·{filt}"
            side = (skip_payload or {}).get("direction", "")
            if side == "long":
                pivot_status = "PIVOT_LOW"
            elif side == "short":
                pivot_status = "PIVOT_HIGH"
        else:
            m = (skip_msg or "").lower()
            if "cooldown" in m:
                decision = "GATED·cooldown"
            elif "deadzone" in m:
                decision = "GATED·deadzone"
            elif "too old" in m:
                decision = "SKIP·stale_bar"
            elif "no new pivot" in m:
                decision = "SKIP·no_new_pivot"
                pivot_status = "NO_PIVOT"
            else:
                decision = "SKIP·other"
    else:
        # No SKIP, no order, no exit → bar was processed silently.
        decision = "HOLDING"
    return pivot_status, line_status, decision


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument(
        "--cutoff", default="2026-05-13T22:00:00Z",
        help="UTC cutoff (ISO 8601). Events with recorded_at-as-UTC >= "
             "this value are backfilled.",
    )
    p.add_argument(
        "--db", default="sqlite:///trader.db",
        help="SQLAlchemy DB URL. Default reads ./trader.db.",
    )
    args = p.parse_args()

    cutoff_utc = datetime.fromisoformat(args.cutoff.replace("Z", "+00:00"))
    cutoff_pt_naive = (cutoff_utc.astimezone(timezone.utc)
                        + timedelta(hours=-7)).replace(tzinfo=None)
    log.info(
        "cutoff UTC=%s → recorded_at PT >= %s",
        cutoff_utc.isoformat(), cutoff_pt_naive.isoformat(),
    )

    engine = create_db_engine(args.db)
    factory = create_session_factory(engine)
    session = factory()
    audit = AuditLogRepository(factory)

    # Wipe prior rows in window (idempotent).
    deleted = (
        session.query(AuditLog)
        .filter(AuditLog.event_ts_utc >= cutoff_utc)
        .delete(synchronize_session=False)
    )
    session.commit()
    log.info("cleared %d existing audit rows >= cutoff", deleted)

    # Pull bot_events with bot info. Group by bot.
    rows = (
        session.query(BotEvent)
        .filter(BotEvent.recorded_at >= cutoff_pt_naive)
        .order_by(BotEvent.bot_id, BotEvent.recorded_at, BotEvent.id)
        .all()
    )
    log.info("scanning %d bot_event rows", len(rows))

    by_bot: dict[str, list[BotEvent]] = defaultdict(list)
    for r in rows:
        by_bot[r.bot_id].append(r)

    # Discover each bot's symbol (from one of its BAR or SIGNAL events).
    # bot_events doesn't carry symbol — fall back to the bot config.
    from ib_trader.data.repositories.bot_repository import BotRepository
    bot_repo = BotRepository(factory)
    bot_symbols: dict[str, str] = {}
    for bot_id in by_bot.keys():
        b = bot_repo.get(bot_id)
        if b is not None:
            try:
                cfg = json.loads(b.config_json or "{}")
            except Exception:  # noqa: BLE001
                cfg = {}
            bot_symbols[bot_id] = (cfg.get("symbol")
                                    or cfg.get("strategy_config", {})
                                    .get("symbol", ""))
        else:
            bot_symbols[bot_id] = ""

    total_bar = 0
    total_order = 0
    total_closed = 0
    for bot_id, evts in by_bot.items():
        symbol = bot_symbols.get(bot_id, "")
        if not symbol:
            log.warning("bot %s has no symbol — skipping", bot_id)
            continue

        # Pass 1 — group events at the same 3-min bar close for BAR_EVAL.
        # Pass 2 — emit ORDER_PLACED and TRADE_CLOSED rows in event order.
        bar_buckets: dict[datetime, list[BotEvent]] = defaultdict(list)
        order_evts: list[BotEvent] = []
        closed_evts: list[BotEvent] = []
        for e in evts:
            if e.event_type == "BAR":
                bar_buckets[e.recorded_at][:] = [e]
            elif e.event_type in ("SKIP", "SIGNAL", "EXIT_CHECK"):
                # Attach to most recent same-minute BAR bucket if any.
                # We look up the BAR with the same minute as this event.
                # If none, the event stands alone (e.g. EXIT_CHECK can
                # fire between bars).
                key = next(
                    (k for k in bar_buckets.keys()
                     if k.replace(second=0, microsecond=0)
                     == e.recorded_at.replace(second=0, microsecond=0)),
                    None,
                )
                if key is not None:
                    bar_buckets[key].append(e)
            elif e.event_type == "ORDER":
                order_evts.append(e)
            elif e.event_type == "CLOSED":
                closed_evts.append(e)

        # Emit BAR_EVAL rows.
        for bar_ts, bucket in bar_buckets.items():
            bar_evt = bucket[0]
            bar_payload = _safe_json_loads(bar_evt.payload_json)
            # Bar close — from BAR message format "bar close=4666.20 ..."
            bar_close = None
            try:
                bc = bar_evt.message.split("bar close=", 1)[1].split()[0]
                bar_close = Decimal(bc)
            except Exception:  # noqa: BLE001
                pass

            skip_evt = next((b for b in bucket[1:] if b.event_type == "SKIP"), None)
            signal_evt = next((b for b in bucket[1:] if b.event_type == "SIGNAL"), None)
            exit_evt = next(
                (b for b in bucket[1:]
                 if b.event_type == "EXIT_CHECK"
                 and "TRAILING_STOP" in (b.message or "")),
                None,
            )
            # Was an entry order placed at the same minute? Pull the
            # earliest matching ORDER event (within 30s of bar close)
            # to attribute the FIRED decision back to the bar.
            same_minute_order = next(
                (o for o in order_evts
                 if abs((o.recorded_at - bar_ts).total_seconds()) < 30
                 and o.event_type == "ORDER"),
                None,
            )
            order_side = None
            if same_minute_order is not None:
                # ORDER message: "BUY MGCM6 submitted (#26129)" or
                # "SELL MGCM6 ..."
                m = same_minute_order.message or ""
                first = m.split()[0] if m else ""
                if first in ("BUY", "SELL"):
                    order_side = first

            pivot_status, line_status, decision = _derive_pivot_line_decision(
                bar_payload,
                skip_evt.message if skip_evt else None,
                _safe_json_loads(skip_evt.payload_json) if skip_evt else None,
                signal_evt.message if signal_evt else None,
                order_side,
                exit_evt.message if exit_evt else None,
                _safe_json_loads(exit_evt.payload_json) if exit_evt else None,
            )

            payload: dict = {"bar": bar_payload}
            if skip_evt:
                payload["skip"] = {
                    "message": skip_evt.message,
                    **_safe_json_loads(skip_evt.payload_json),
                }
            if signal_evt:
                payload["signal"] = {
                    "message": signal_evt.message,
                    **_safe_json_loads(signal_evt.payload_json),
                }
            if exit_evt:
                payload["exit"] = {
                    "message": exit_evt.message,
                    **_safe_json_loads(exit_evt.payload_json),
                }

            audit.insert_bar_eval(
                bot_id=bot_id, symbol=symbol,
                event_ts_utc=_pt_to_utc(bar_ts),
                pivot_status=pivot_status,
                line_status=line_status,
                decision=decision,
                bar_close=bar_close,
                payload=payload,
            )
            total_bar += 1

        # Emit ORDER_PLACED rows.
        for o in order_evts:
            m = o.message or ""
            first = m.split()[0] if m else ""
            if first not in ("BUY", "SELL"):
                continue
            # Find preceding EXIT_CHECK TRAILING_STOP (within ~5s
            # before the order) for exit-leg reason attribution.
            trailing = None
            for e in evts:
                if (e.event_type == "EXIT_CHECK"
                        and "TRAILING_STOP" in (e.message or "")
                        and o.recorded_at >= e.recorded_at >= (
                            o.recorded_at - timedelta(seconds=5)
                        )):
                    trailing = e
            is_exit = trailing is not None
            reason_suffix = ""
            if is_exit and trailing.message:
                from ib_trader.bots.middleware import _normalize_exit_reason
                short = _normalize_exit_reason(trailing.message)
                if short:
                    reason_suffix = f"·{short}"
            leg = "exit" if is_exit else "entry"
            decision = f"ORDER·{first}·{leg}{reason_suffix}"
            audit.insert_order_placed(
                bot_id=bot_id, symbol=symbol,
                event_ts_utc=_pt_to_utc(o.recorded_at),
                decision=decision,
                payload={
                    "message": o.message,
                    **_safe_json_loads(o.payload_json),
                    "trailing_stop_message": (
                        trailing.message if trailing else None
                    ),
                },
            )
            total_order += 1

        # closed_evts not used — bot_events doesn't carry CLOSED rows.
        # We read TRADE_CLOSED from the bot_trades table below instead.
        _ = closed_evts

    # TRADE_CLOSED — synthesize from bot_trades (authoritative round-trip
    # record). The CLOSED LogSignal in chart_signal.py never makes it
    # into bot_events historically (LoggingMiddleware suppression or a
    # legacy gap), so we read from the closed-trades table.
    # bot_trades.exit_time is naive UTC (datetime.now(timezone.utc) at write
    # time, tz stripped by SQLite). Compare against cutoff_utc as naive UTC.
    cutoff_utc_naive = cutoff_utc.replace(tzinfo=None)
    trades = (
        session.query(BotTrade)
        .filter(BotTrade.exit_time >= cutoff_utc_naive)
        .order_by(BotTrade.exit_time)
        .all()
    )
    for t in trades:
        # Duration from entry to exit, in seconds.
        duration_s = None
        if t.entry_time and t.exit_time:
            try:
                duration_s = (t.exit_time - t.entry_time).total_seconds()
            except Exception:  # noqa: BLE001
                pass
        net_pnl = t.realized_pnl
        if net_pnl is not None and t.commission is not None:
            try:
                net_pnl = Decimal(str(t.realized_pnl)) - Decimal(str(t.commission))
            except Exception:  # noqa: BLE001
                pass
        decision = f"CLOSED·{t.direction}·backfilled"
        # bot_trades.exit_time/entry_time are already UTC (written via
        # ``datetime.now(timezone.utc)`` in the runtime). The naive
        # SQLite storage keeps them tz-stripped; treat as UTC directly,
        # NOT as PT — _pt_to_utc would shift them 7h into the future.
        ts_naive_utc = t.exit_time or t.entry_time
        ts_utc = ts_naive_utc.replace(tzinfo=timezone.utc) \
            if ts_naive_utc and ts_naive_utc.tzinfo is None else ts_naive_utc
        audit.insert_trade_closed(
            bot_id=t.bot_id, symbol=t.symbol,
            event_ts_utc=ts_utc,
            decision=decision,
            pnl_net=net_pnl,
            payload={
                "direction": t.direction,
                "entry_price": str(t.entry_price),
                "exit_price": str(t.exit_price) if t.exit_price else None,
                "entry_qty": str(t.entry_qty),
                "entry_time": t.entry_time.isoformat() if t.entry_time else None,
                "exit_time": t.exit_time.isoformat() if t.exit_time else None,
                "realized_pnl": str(t.realized_pnl) if t.realized_pnl is not None else None,
                "commission": str(t.commission) if t.commission is not None else None,
                "duration_seconds": duration_s,
                "entry_serial": t.entry_serial,
                "exit_serial": t.exit_serial,
            },
        )
        total_closed += 1

    log.info(
        "backfilled: %d BAR_EVAL, %d ORDER_PLACED, %d TRADE_CLOSED",
        total_bar, total_order, total_closed,
    )
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
