#!/usr/bin/env python3
"""Bot-trade analytics — symbol × entry-condition matrix.

Generates a cross-tab of bot round-trips from ``bot_trades`` (the
source of truth — one row per closed bot trade, always written by
the runtime's ``_handle_record_trade_closed``) joined with
``audit_log`` for the entry-condition tag.

Cell content:

    +$NET.NN  (n=N, max +$M, min −$L)

where N is the number of trades in that cell, M is the largest
single-trade gain (net), and L is the largest single-trade loss
(net).  Sign on M/L reflects direction.

Net P&L per trade matches the 24h header logic:

    net = realized_pnl - max(commission, ROUND_TRIP_MIN[symbol])

The ``max(...)`` floors the commission at the symbol's expected
round-trip cost — when IB's commissionReport delivery lags on one
leg the stored value can be partial (e.g. $0.97 on MGC vs the $1.94
floor); the rollup logic does the same so the matrix totals line up
with the header.

Entry condition (the column dimension) is derived from the
audit_log TRADE_CLOSED row's ``decision`` (4th ``·``-segment) or
``payload.entry_tag`` when present. Legacy trades that pre-date the
entry_tag commit (ee3d183, 2026-05-17) bucket as ``unknown``.

Compute is broken out as a pure function returning a plain dict so
the future GUI dashboard can re-use it via the same import.

Usage:

    # Default window: most-recent 3 PM PT mark → now.
    uv run python scripts/analytics/trade_matrix.py

    # Explicit window:
    uv run python scripts/analytics/trade_matrix.py \\
        --since "2026-05-17 22:00:00"

    # ISO with timezone (auto-converted to UTC):
    uv run python scripts/analytics/trade_matrix.py \\
        --since "2026-05-17T15:00:00-07:00"

    # CSV (one row per (symbol, tag) cell):
    uv run python scripts/analytics/trade_matrix.py --csv > matrix.csv
"""
from __future__ import annotations

import argparse
import csv
import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

# Default DB path — matches the prod/dev convention. Override with --db.
DB_PATH = Path("/home/zuura/projects/ib-trader/trader.db")

# Display order for entry-tag columns. Anything not in this list
# appears at the end, alphabetically. ``unknown`` is pinned last so
# the modern buckets read left-to-right cleanly.
TAG_ORDER = [
    "clean",
    "accel",
    "shoulder",
    "min_target",
    "far_from_pivot",
    "tight_triangle",
    "stale_line",
    "opposing_dominance",
]
_TAG_ORDER_INDEX = {t: i for i, t in enumerate(TAG_ORDER)}

PT = ZoneInfo("America/Los_Angeles")

# Mirror of ``ib_trader.data.commissions.ROUND_TRIP_MIN`` — copied
# here so the analytics script stays runnable as a standalone CLI
# without imposing the ib_trader package import path on every
# invocation. Keep in sync.
ROUND_TRIP_MIN: dict[str, Decimal] = {
    "MGCM6": Decimal("1.94"),
    "MESM6": Decimal("1.24"),
    "MNQM6": Decimal("1.24"),
}


def _floor_for(symbol: str) -> Decimal:
    return ROUND_TRIP_MIN.get(symbol, Decimal("0"))


# ---------------------------------------------------------------------------
# Pure compute layer — single function the dashboard can re-use.
# Returns the raw per-trade rows AND the aggregated matrix; cheaper
# than re-aggregating downstream.
# ---------------------------------------------------------------------------

@dataclass
class CellStats:
    """One cell of the (symbol, entry_tag) matrix."""

    net_pnl: Decimal = Decimal("0")
    n_trades: int = 0
    max_gain: Decimal | None = None  # highest single-trade net
    max_loss: Decimal | None = None  # lowest (= most-negative) single-trade net

    def add(self, pnl: Decimal) -> None:
        self.net_pnl += pnl
        self.n_trades += 1
        if self.max_gain is None or pnl > self.max_gain:
            self.max_gain = pnl
        if self.max_loss is None or pnl < self.max_loss:
            self.max_loss = pnl


@dataclass
class TradeRow:
    """Per-trade record after joining bot_trades with audit_log."""

    symbol: str
    direction: str
    entry_tag: str
    gross_pnl: Decimal
    commission_stored: Decimal
    commission_used: Decimal  # max(stored, floor)
    net_pnl: Decimal
    exit_time: datetime


def _parse_entry_tag(decision: str | None, payload_json: str | None) -> str:
    """Return the entry classification for a TRADE_CLOSED row.

    Decision format (post 2026-05-18): ``CLOSED·DIR·exit_reason·entry_tag``.
    Older rows lack the suffix; fall back to ``payload.entry_tag``
    if present, else ``"unknown"``.
    """
    parts = (decision or "").split("·")
    if len(parts) >= 4 and parts[3]:
        return parts[3]
    if payload_json:
        try:
            tag = json.loads(payload_json).get("entry_tag")
            if tag:
                return str(tag)
        except (ValueError, TypeError):
            pass
    return "unknown"


def _parse_ts(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(raw))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def compute(
    since_utc: datetime,
    until_utc: datetime | None = None,
    db_path: Path | None = None,
) -> tuple[
    dict[tuple[str, str], CellStats],
    list[TradeRow],
    int,
]:
    """Return ``(matrix, raw_rows, n_bot_trades_scanned)``.

    Reads from bot_trades (the source of truth — written by the
    runtime on every exit fill). Joins entry_tag from audit_log via
    (entry_serial, exit_serial). Trades whose audit row predates the
    entry_tag work or whose audit row never landed bucket as
    ``unknown`` so the operator sees the unclassified P&L explicitly
    rather than it silently vanishing from any column.
    """
    db_path = db_path or DB_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # Pull bot_trades in the window.
    if until_utc is None:
        cur.execute(
            "SELECT id, symbol, direction, "
            "       CAST(realized_pnl AS TEXT) AS realized_pnl, "
            "       CAST(commission   AS TEXT) AS commission, "
            "       entry_serial, exit_serial, exit_time "
            "FROM bot_trades "
            "WHERE exit_time >= ?",
            (since_utc.strftime("%Y-%m-%d %H:%M:%S"),),
        )
    else:
        cur.execute(
            "SELECT id, symbol, direction, "
            "       CAST(realized_pnl AS TEXT) AS realized_pnl, "
            "       CAST(commission   AS TEXT) AS commission, "
            "       entry_serial, exit_serial, exit_time "
            "FROM bot_trades "
            "WHERE exit_time >= ? AND exit_time < ?",
            (
                since_utc.strftime("%Y-%m-%d %H:%M:%S"),
                until_utc.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    bt_rows = cur.fetchall()
    n_scanned = len(bt_rows)

    # Build entry_tag map from audit_log. We key on
    # (entry_serial, exit_serial) which is present in the audit
    # payload for any row written after ee3d183. For older rows the
    # map will not contain a hit and we fall through to "unknown".
    if until_utc is None:
        cur.execute(
            "SELECT decision, payload_json "
            "FROM audit_log "
            "WHERE event_type = 'TRADE_CLOSED' "
            "  AND event_ts_utc >= ?",
            (since_utc.strftime("%Y-%m-%d %H:%M:%S"),),
        )
    else:
        cur.execute(
            "SELECT decision, payload_json "
            "FROM audit_log "
            "WHERE event_type = 'TRADE_CLOSED' "
            "  AND event_ts_utc >= ? AND event_ts_utc < ?",
            (
                since_utc.strftime("%Y-%m-%d %H:%M:%S"),
                until_utc.strftime("%Y-%m-%d %H:%M:%S"),
            ),
        )
    audit_rows = cur.fetchall()
    conn.close()

    tag_by_serials: dict[tuple[int | None, int | None], str] = {}
    for ar in audit_rows:
        tag = _parse_entry_tag(ar["decision"], ar["payload_json"])
        es = xs = None
        if ar["payload_json"]:
            try:
                p = json.loads(ar["payload_json"])
                es = p.get("entry_serial")
                xs = p.get("exit_serial")
            except (ValueError, TypeError):
                pass
        if es is not None or xs is not None:
            tag_by_serials[(es, xs)] = tag

    matrix: dict[tuple[str, str], CellStats] = defaultdict(CellStats)
    raw: list[TradeRow] = []

    for r in bt_rows:
        try:
            gross = Decimal(r["realized_pnl"] or "0")
            comm_stored = Decimal(r["commission"] or "0")
        except (ValueError, TypeError, ArithmeticError):
            continue
        symbol = str(r["symbol"])
        floor = _floor_for(symbol)
        comm_used = comm_stored if comm_stored >= floor else floor
        net = gross - comm_used
        tag = tag_by_serials.get(
            (r["entry_serial"], r["exit_serial"]),
            "unknown",
        )
        exit_t = _parse_ts(r["exit_time"]) or datetime.now(timezone.utc)
        row = TradeRow(
            symbol=symbol,
            direction=str(r["direction"] or ""),
            entry_tag=tag,
            gross_pnl=gross,
            commission_stored=comm_stored,
            commission_used=comm_used,
            net_pnl=net,
            exit_time=exit_t,
        )
        raw.append(row)
        matrix[(symbol, tag)].add(net)

    return matrix, raw, n_scanned


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _fmt_money(v: Decimal | None) -> str:
    if v is None:
        return "—"
    sign = "+" if v >= 0 else "−"
    return f"{sign}${abs(v):.2f}"


def _fmt_cell(c: CellStats) -> str:
    if c.n_trades == 0:
        return "—"
    return (
        f"{_fmt_money(c.net_pnl)} "
        f"(n={c.n_trades}, "
        f"max {_fmt_money(c.max_gain)}, "
        f"min {_fmt_money(c.max_loss)})"
    )


def _tag_sort_key(tag: str) -> tuple[int, str]:
    if tag == "unknown":
        return (10_000, tag)
    return (_TAG_ORDER_INDEX.get(tag, 5_000), tag)


def render_text(
    matrix: dict[tuple[str, str], CellStats],
    raw: list[TradeRow],
    since_utc: datetime,
    until_utc: datetime | None,
) -> str:
    if not matrix:
        return "No bot_trades rows in the window.\n"

    symbols = sorted({s for s, _ in matrix.keys()})
    tags = sorted({t for _, t in matrix.keys()}, key=_tag_sort_key)

    # Marginals — recomputed from raw so they're trade-accurate
    # (CellStats is already a reduce, can't be re-aggregated).
    sym_totals: dict[str, CellStats] = defaultdict(CellStats)
    tag_totals: dict[str, CellStats] = defaultdict(CellStats)
    grand = CellStats()
    for r in raw:
        sym_totals[r.symbol].add(r.net_pnl)
        tag_totals[r.entry_tag].add(r.net_pnl)
        grand.add(r.net_pnl)

    out: list[str] = []
    out.append("Bot-trade matrix — symbol × entry condition")
    until = until_utc or datetime.now(timezone.utc)
    out.append(
        f"  window:  {since_utc.astimezone(PT):%Y-%m-%d %H:%M %Z}"
        f"  →  {until.astimezone(PT):%Y-%m-%d %H:%M %Z}"
    )
    out.append(
        "  cell:    net  (n trades, max single gain, max single loss)"
    )
    out.append(
        "  net:     realized_pnl − max(stored_commission, symbol_floor)"
    )
    out.append("")

    sym_w = max(len(s) for s in symbols) + 2
    tag_w = max(len(t) for t in tags) + 2
    out.append(
        f"{'symbol'.ljust(sym_w)}{'condition'.ljust(tag_w)}cell"
    )
    out.append("-" * (sym_w + tag_w + 56))
    for sym in symbols:
        printed_any = False
        for tag in tags:
            cell = matrix.get((sym, tag))
            if cell is None or cell.n_trades == 0:
                continue
            printed_any = True
            out.append(
                f"{sym.ljust(sym_w)}{tag.ljust(tag_w)}{_fmt_cell(cell)}"
            )
        if printed_any:
            out.append(
                f"{''.ljust(sym_w)}{'(symbol total)'.ljust(tag_w)}"
                f"{_fmt_cell(sym_totals[sym])}"
            )
            out.append("")

    out.append("Per-condition totals:")
    for tag in tags:
        out.append(f"  {tag.ljust(tag_w)}{_fmt_cell(tag_totals[tag])}")
    out.append("")
    out.append(f"Grand total: {_fmt_cell(grand)}")
    return "\n".join(out) + "\n"


def render_csv(
    matrix: dict[tuple[str, str], CellStats],
    out_fh,
) -> None:
    """Emit one row per non-empty (symbol, tag) cell."""
    w = csv.writer(out_fh)
    w.writerow(
        ["symbol", "condition", "net_pnl", "n_trades",
         "max_gain", "max_loss"],
    )
    for (sym, tag), c in sorted(
        matrix.items(),
        key=lambda kv: (kv[0][0], _tag_sort_key(kv[0][1])),
    ):
        if c.n_trades == 0:
            continue
        w.writerow([
            sym, tag,
            f"{c.net_pnl:.4f}",
            c.n_trades,
            f"{c.max_gain:.4f}" if c.max_gain is not None else "",
            f"{c.max_loss:.4f}" if c.max_loss is not None else "",
        ])


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _default_since_utc() -> datetime:
    """Most-recent 3 PM PT mark, returned in UTC. PT handles DST."""
    now_pt = datetime.now(PT)
    today_3pm = datetime.combine(now_pt.date(), time(15, 0), tzinfo=PT)
    if now_pt < today_3pm:
        today_3pm -= timedelta(days=1)
    return today_3pm.astimezone(timezone.utc)


def _parse_since(arg: str | None) -> datetime:
    if arg is None:
        return _default_since_utc()
    s = arg.strip()
    if s == "today_3pm_pt":
        return _default_since_utc()
    try:
        dt = datetime.fromisoformat(s)
    except ValueError as e:
        raise SystemExit(f"--since: cannot parse {arg!r}: {e}")
    if dt.tzinfo is None:
        # Naive → assume UTC (matches the DB convention).
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description=(
            "Bot-trade analytics — symbol × entry-condition matrix. "
            "Sources: bot_trades (P&L) joined with audit_log "
            "(entry_tag)."
        ),
    )
    ap.add_argument(
        "--since",
        default=None,
        help=(
            "Window start (ISO, with or without timezone). "
            "Default: today's 3 PM PT (or yesterday's if before "
            "3 PM PT now)."
        ),
    )
    ap.add_argument(
        "--until",
        default=None,
        help="Optional window end (ISO). Default: now.",
    )
    ap.add_argument("--db", default=str(DB_PATH))
    ap.add_argument(
        "--csv", action="store_true",
        help="Emit machine-readable CSV instead of the text matrix.",
    )
    args = ap.parse_args(argv)

    since = _parse_since(args.since)
    until = _parse_since(args.until) if args.until else None

    matrix, raw, scanned = compute(since, until, Path(args.db))

    if args.csv:
        render_csv(matrix, sys.stdout)
    else:
        print(render_text(matrix, raw, since, until))
        # Quick coverage note so the operator knows how many trades
        # are unclassified (legacy / pre-entry_tag rows).
        n_unknown = sum(
            1 for r in raw if r.entry_tag == "unknown"
        )
        if n_unknown:
            print(
                f"({n_unknown} of {len(raw)} trades bucketed as "
                f"'unknown' — predate the entry_tag commit ee3d183)"
            )
        print(
            f"(scanned {scanned} bot_trades rows; "
            f"{len(raw)} had pnl populated)",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
