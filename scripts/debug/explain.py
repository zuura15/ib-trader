"""Fast "why didn't the bot fire" / "why did this order do X" lookup.

Two modes:

  Bot/symbol mode (original):
    uv run scripts/debug/explain.py SYMBOL [TIME] [--window MIN] [--bot-id ID]

    1. Resolves SYMBOL → bot_id via the YAML registry.
    2. Pulls every ``bot_events`` row for that bot in [TIME-window, TIME+window].
    3. Greps ``logs/ib_trader.log`` for the same window keyed by symbol.
    4. Reads the latest bar from Redis ``bar:<symbol>:5s``.

  Order mode (added 2026-06-02 — see feedback_db_first_for_order_investigations):
    uv run scripts/debug/explain.py --order IB_ORDER_ID
    uv run scripts/debug/explain.py --serial TRADE_SERIAL

    1. Pulls every ``transactions`` row for that order, chronological.
       Single indexed query — 50 ms vs minutes of full-log grep.
    2. Derives the incident window from the first/last txn timestamps
       (±2 min padding by default; override with --window).
    3. Greps ``logs/ib_trader.log`` ONLY for that window keyed by the
       ib_order_id — typically 30-50 lines instead of thousands.

Designed for Claude (and humans) to answer both "the bot didn't fire
at X" and "what happened with order #X" questions without burning
multiple SSH round-trips on full-log grep.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "trader.db"
LOG_PATH = ROOT / "logs" / "ib_trader.log"
PT = ZoneInfo("America/Los_Angeles")


def _resolve_bot_id(symbol_or_id: str) -> tuple[str, str]:
    """Return ``(bot_id, symbol)`` for the given argument.

    Accepts either an exact bot id (``chart-bot-1``) or a contract
    symbol (``MGCM6`` / ``MGC`` / ``MNQM6``). Symbol match is fuzzy:
    case-insensitive substring against any of the bot's configured
    symbols. Falls through to the unchanged value when nothing
    matches, so a typo at least produces a reasonable error later.
    """
    import sys
    sys.path.insert(0, str(ROOT))
    from ib_trader.bots.config_loader import load_all_bots

    defs = load_all_bots(ROOT / "config" / "bots")
    arg = symbol_or_id.upper()
    for d in defs:
        if d.id.upper() == arg:
            return d.id, d.config.get("symbol", "")
    for d in defs:
        cfg_sym = str(d.config.get("symbol", "")).upper()
        if cfg_sym and (cfg_sym == arg or arg in cfg_sym or cfg_sym in arg):
            return d.id, d.config.get("symbol", "")
    return symbol_or_id, symbol_or_id


def _parse_time(s: str | None) -> datetime:
    """Parse a time/datetime argument as PT.

    Accepts:
      - ``"HH:MM"`` (today at HH:MM PT)
      - ``"HH:MM:SS"`` (today at HH:MM:SS PT)
      - ``"YYYY-MM-DD HH:MM"`` (explicit date in PT)
      - ``None`` → now
    """
    if not s:
        return datetime.now().astimezone(PT)
    s = s.strip()
    today = datetime.now(PT).date()
    fmts = ("%H:%M", "%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S")
    for fmt in fmts:
        try:
            t = datetime.strptime(s, fmt)
            if "%Y" not in fmt:
                # Time-only input — strptime returns year=1900, which
                # turns the window into a fixed-date 1900 lookup. Stitch
                # in today's PT date instead.
                t = datetime.combine(today, t.time())
            return t.replace(tzinfo=PT)
        except ValueError:
            continue
    raise SystemExit(f"can't parse time: {s!r} (try HH:MM or YYYY-MM-DD HH:MM)")


def _query_bot_events(bot_id: str, lo: datetime, hi: datetime) -> list[tuple]:
    """Return rows in [lo, hi] for ``bot_id``. Tries PT-naive first
    (matches the new ``_now_utc`` → local writes) then UTC-naive (old
    rows). Both naive variants come back side-by-side."""
    con = sqlite3.connect(DB_PATH)
    rows: list[tuple] = []
    lo_pt = lo.astimezone(PT).strftime("%Y-%m-%d %H:%M:%S")
    hi_pt = hi.astimezone(PT).strftime("%Y-%m-%d %H:%M:%S")
    lo_utc = lo.astimezone(tz=None).utctimetuple()
    from datetime import timezone as _tz
    lo_utc_s = lo.astimezone(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
    hi_utc_s = hi.astimezone(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")
    sql = (
        "SELECT recorded_at, event_type, message FROM bot_events "
        "WHERE bot_id = ? AND ("
        "  (recorded_at >= ? AND recorded_at <= ?) OR "
        "  (recorded_at >= ? AND recorded_at <= ?)"
        ") ORDER BY recorded_at ASC"
    )
    rows = con.execute(sql, (bot_id, lo_pt, hi_pt, lo_utc_s, hi_utc_s)).fetchall()
    con.close()
    return rows


def _grep_logs(symbol: str, lo: datetime, hi: datetime) -> list[str]:
    """Look up log events from the ``log_events`` SQLite index instead
    of grepping the raw ``ib_trader.log``. The index is refreshed
    on every invocation (tails new lines since the last call —
    typically a few ms after the first warm-up build). Falls back to
    raw grep if the index can't be built."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import log_index  # type: ignore
        log_index.refresh()
        lo_iso = lo.astimezone(PT).isoformat()
        hi_iso = hi.astimezone(PT).isoformat()
        rows = log_index.query(lo_iso, hi_iso, symbol=symbol)
        # Re-emit as JSON strings so the existing print loop below
        # can keep using ``json.loads``. Cheap — these are already-
        # parsed payloads we're re-stringifying.
        return [payload for (_ts, _ev, _sym, _bot, payload) in rows]
    except Exception as e:  # noqa: BLE001
        print(f"[log_index] fallback to raw grep ({e})", file=sys.stderr)
    if not LOG_PATH.exists():
        return []
    lo_iso = lo.astimezone(PT).strftime("%Y-%m-%dT%H:%M")
    hi_iso = hi.astimezone(PT).strftime("%Y-%m-%dT%H:%M")
    pattern = "|".join((
        re.escape(symbol),
        r"IB_ERROR",
        r"BOT_STARTUP_FORCED_OFF",
        r"FSM_TRANSITION",
    ))
    try:
        out = subprocess.check_output(
            ["grep", "-E", pattern, str(LOG_PATH)],
            text=True, errors="replace",
        )
    except subprocess.CalledProcessError:
        return []
    keep: list[str] = []
    for line in out.splitlines():
        m = re.search(r'"timestamp":\s*"([0-9T:.\-+]+)"', line)
        if not m:
            continue
        ts = m.group(1)
        if ts[: len(lo_iso)] < lo_iso or ts[: len(hi_iso)] > hi_iso:
            continue
        if '"event": "IB_THROTTLED"' in line or '"HTTP Request' in line:
            continue
        keep.append(line)
    return keep


def _query_order_transactions(
    *, ib_order_id: int | None = None, trade_serial: int | None = None,
) -> list[dict]:
    """Return all transactions rows for an order, chronological.

    Caller supplies exactly one of ``ib_order_id`` or ``trade_serial``.
    The transactions table has indices on both, so this is a hash-
    lookup regardless of which key is used.
    """
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    if ib_order_id is not None:
        rows = con.execute(
            "SELECT id, action, symbol, side, order_type, quantity, "
            "limit_price, ib_status, ib_filled_qty, ib_avg_fill_price, "
            "commission, ib_order_id, trade_serial, leg_type, "
            "ib_responded_at, requested_at, is_terminal "
            "FROM transactions WHERE ib_order_id = ? "
            "ORDER BY id ASC",
            (ib_order_id,),
        ).fetchall()
    elif trade_serial is not None:
        rows = con.execute(
            "SELECT id, action, symbol, side, order_type, quantity, "
            "limit_price, ib_status, ib_filled_qty, ib_avg_fill_price, "
            "commission, ib_order_id, trade_serial, leg_type, "
            "ib_responded_at, requested_at, is_terminal "
            "FROM transactions WHERE trade_serial = ? "
            "ORDER BY id ASC",
            (trade_serial,),
        ).fetchall()
    else:
        rows = []
    con.close()
    return [dict(r) for r in rows]


def _grep_logs_for_order(
    ib_order_id: int, lo: datetime, hi: datetime,
) -> list[str]:
    """Grep ``ib_trader.log`` keyed by ``ib_order_id`` within [lo, hi].

    Uses ``log_index`` if available (already-parsed payloads). Falls
    back to raw grep on the log file. Drops the periodic noise that
    pollutes order timelines (CANCEL_SETTLE_HEARTBEAT, OPEN_ORDERS_RAW,
    IB_THROTTLED, raw <<<-prefixed bytes from ib_async)."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import log_index  # type: ignore
        log_index.refresh()
        lo_iso = lo.astimezone(PT).isoformat()
        hi_iso = hi.astimezone(PT).isoformat()
        # log_index doesn't support a free-form id filter; pull the
        # window then post-filter for ib_order_id appearance.
        rows = log_index.query(lo_iso, hi_iso)
        id_s = str(ib_order_id)
        return [payload for (_ts, _ev, _sym, _bot, payload) in rows
                if id_s in payload]
    except Exception as e:  # noqa: BLE001
        print(f"[log_index] fallback to raw grep ({e})", file=sys.stderr)

    if not LOG_PATH.exists():
        return []
    lo_iso = lo.astimezone(PT).strftime("%Y-%m-%dT%H:%M")
    hi_iso = hi.astimezone(PT).strftime("%Y-%m-%dT%H:%M")
    try:
        out = subprocess.check_output(
            ["grep", "-F", str(ib_order_id), str(LOG_PATH)],
            text=True, errors="replace",
        )
    except subprocess.CalledProcessError:
        return []
    keep: list[str] = []
    NOISE = (
        '"event": "CANCEL_SETTLE_HEARTBEAT"',
        '"event": "OPEN_ORDERS_RAW"',
        '"event": "IB_THROTTLED"',
        '"message": "<<< ',
        '"message": ">>> ',
    )
    for line in out.splitlines():
        m = re.search(r'"timestamp":\s*"([0-9T:.\-+]+)"', line)
        if not m:
            continue
        ts = m.group(1)
        if ts[: len(lo_iso)] < lo_iso or ts[: len(hi_iso)] > hi_iso:
            continue
        if any(n in line for n in NOISE):
            continue
        keep.append(line)
    return keep


def _redis_bar_tail(symbol: str) -> str | None:
    """Read the most recent bar from ``bar:{symbol}:5s`` if Redis is
    up. Useful to confirm the engine is publishing live ticks."""
    cli = ROOT / ".local" / "bin" / "redis-cli"
    if not cli.exists():
        return None
    try:
        depth = subprocess.check_output(
            [str(cli), "xlen", f"bar:{symbol}:5s"], text=True,
        ).strip()
        latest = subprocess.check_output(
            [str(cli), "xrevrange", f"bar:{symbol}:5s", "+", "-", "count", "1"],
            text=True,
        ).strip()
    except subprocess.CalledProcessError:
        return None
    # Pull the ts field from the latest entry.
    m = re.search(r'"(\d{4}-\d{2}-\d{2}T[^"]+)"', latest)
    latest_ts = m.group(1) if m else "?"
    return f"bar:{symbol}:5s depth={depth} latest={latest_ts}"


def _explain_order(
    *, ib_order_id: int | None, trade_serial: int | None,
    window_minutes: int,
) -> int:
    """Order-mode report: structured transactions first, narrow log grep
    second. See module docstring."""
    txns = _query_order_transactions(
        ib_order_id=ib_order_id, trade_serial=trade_serial,
    )
    label = (
        f"ib_order_id={ib_order_id}" if ib_order_id is not None
        else f"trade_serial={trade_serial}"
    )
    if not txns:
        print(f"=== explain order {label} ===")
        print(f"  no transactions found in DB for {label}")
        return 1

    # Derive window from txn timestamps (use ib_responded_at when
    # present, fall back to requested_at for PLACE_ATTEMPT etc.).
    def _ts_of(row: dict) -> str:
        return row.get("ib_responded_at") or row.get("requested_at") or ""

    ts_strings = sorted(_ts_of(r) for r in txns if _ts_of(r))
    first_ts = ts_strings[0] if ts_strings else None
    last_ts = ts_strings[-1] if ts_strings else None

    resolved_order_id = ib_order_id or txns[0]["ib_order_id"]
    sym = txns[0]["symbol"]
    side = txns[0]["side"]
    serial = txns[0]["trade_serial"]

    print(f"=== explain order {label} ({sym} {side} serial=#{serial}) ===")
    print(f"  first txn: {first_ts}")
    print(f"  last  txn: {last_ts}")
    print(f"  {len(txns)} transactions in DB:")
    for r in txns:
        ts = _ts_of(r) or "?"
        action = r["action"]
        qty_filled = r.get("ib_filled_qty")
        avg_px = r.get("ib_avg_fill_price")
        ib_status = r.get("ib_status") or "—"
        terminal = "T" if r.get("is_terminal") else " "
        detail_bits = []
        if qty_filled:
            detail_bits.append(f"filled={qty_filled}")
        if avg_px:
            detail_bits.append(f"avg=${avg_px}")
        if r.get("commission"):
            detail_bits.append(f"comm=${r['commission']}")
        if r.get("leg_type"):
            detail_bits.append(f"leg={r['leg_type']}")
        detail = "  ".join(detail_bits)
        print(f"  {ts}  [{terminal}] {action:18s} status={ib_status:14s} {detail}")

    if resolved_order_id and first_ts and last_ts:
        from datetime import timezone as _tz
        # DB stores timestamps as ISO strings WITHOUT timezone (engine
        # writes UTC-naive via _now_utc().replace(tzinfo=None) in the
        # repository layer). Python's fromisoformat on a tz-less string
        # produces a NAIVE datetime; .astimezone() would then assume
        # local-tz which is wrong. Explicitly attach UTC, then convert.
        def _to_pt(s: str) -> datetime:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=_tz.utc)
            return dt.astimezone(PT)
        try:
            lo_dt = _to_pt(first_ts)
            hi_dt = _to_pt(last_ts)
        except Exception:  # noqa: BLE001
            lo_dt = datetime.now(PT) - timedelta(minutes=window_minutes)
            hi_dt = datetime.now(PT)
        lo = lo_dt - timedelta(minutes=window_minutes)
        hi = hi_dt + timedelta(minutes=window_minutes)
        log_lines = _grep_logs_for_order(int(resolved_order_id), lo, hi)
        print(f"\n  [ib_trader.log] {len(log_lines)} line(s) "
              f"keyed by ib_order_id={resolved_order_id} "
              f"in [{lo.strftime('%Y-%m-%d %H:%M:%S %Z')}, "
              f"{hi.strftime('%H:%M:%S %Z')}] ±{window_minutes}m:")
        for ln in log_lines[:200]:
            try:
                doc = json.loads(ln)
            except json.JSONDecodeError:
                print(f"  {ln[:200]}")
                continue
            ts = doc.get("timestamp", "?")
            ev = doc.get("event") or doc.get("level") or "—"
            bits = {
                k: v for k, v in doc.items()
                if k not in ("timestamp", "event", "level", "message")
                and not isinstance(v, (dict, list))
            }
            bit_str = " ".join(f"{k}={v}" for k, v in list(bits.items())[:6])
            print(f"  {ts}  {ev:28s}  {bit_str}")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fast troubleshooting for missed signals / orders.",
    )
    ap.add_argument("symbol", nargs="?", default=None,
                    help="Contract symbol or bot id (e.g. MGCM6, chart-bot-1). "
                         "Omit when using --order or --serial.")
    ap.add_argument("time", nargs="?", default=None,
                    help='Time (PT) — "HH:MM" / "HH:MM:SS" / "YYYY-MM-DD HH:MM". Default: now.')
    ap.add_argument("--window", type=int, default=15,
                    help="Minutes ± around TIME (or around the order's txn window). Default 15.")
    ap.add_argument("--bot-id", default=None,
                    help="Explicit bot id (skips registry lookup).")
    ap.add_argument("--order", type=int, default=None,
                    help="ib_order_id — switches to order-mode (DB-first lookup).")
    ap.add_argument("--serial", type=int, default=None,
                    help="trade_serial — switches to order-mode (DB-first lookup).")
    args = ap.parse_args()

    # Order mode short-circuits the bot/symbol resolution.
    if args.order is not None or args.serial is not None:
        if args.order is not None and args.serial is not None:
            print("error: --order and --serial are mutually exclusive",
                  file=sys.stderr)
            return 2
        return _explain_order(
            ib_order_id=args.order, trade_serial=args.serial,
            window_minutes=args.window,
        )

    if args.symbol is None:
        ap.error("symbol required unless --order or --serial is given")

    bot_id, symbol = (
        (args.bot_id, args.symbol) if args.bot_id
        else _resolve_bot_id(args.symbol)
    )
    pivot = _parse_time(args.time)
    lo = pivot - timedelta(minutes=args.window)
    hi = pivot + timedelta(minutes=args.window)

    print(f"=== explain {symbol} (bot_id={bot_id}) "
          f"@ {pivot.strftime('%Y-%m-%d %H:%M:%S %Z')} ±{args.window}m ===")

    rb = _redis_bar_tail(symbol)
    if rb:
        print(f"[redis] {rb}")

    events = _query_bot_events(bot_id, lo, hi)
    print(f"[bot_events] {len(events)} row(s) in window:")
    for ts, etype, msg in events:
        print(f"  {ts}  {etype:14s}  {msg[:200]}")

    log_lines = _grep_logs(symbol, lo, hi)
    print(f"[ib_trader.log] {len(log_lines)} non-noise line(s) in window:")
    for ln in log_lines[:200]:
        # Show just the event token + key fields, not the full JSON.
        try:
            doc = json.loads(ln)
        except json.JSONDecodeError:
            print(f"  {ln[:200]}")
            continue
        ts = doc.get("timestamp", "?")
        ev = doc.get("event") or doc.get("level") or "—"
        bits = {k: v for k, v in doc.items()
                if k not in ("timestamp", "event", "level", "message")
                and not isinstance(v, (dict, list))}
        bit_str = " ".join(f"{k}={v}" for k, v in list(bits.items())[:6])
        print(f"  {ts}  {ev:24s}  {bit_str}")

    if not events and not log_lines:
        print("\n  (no activity recorded in window — bot was probably OFF, "
              "or process restarted around then.)")

    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
