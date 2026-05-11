"""Fast "why didn't the bot fire" lookup.

Usage:
    uv run scripts/debug/explain.py SYMBOL [TIME] [--window MIN] [--bot-id ID]

Examples:
    uv run scripts/debug/explain.py MGCM6 21:36
    uv run scripts/debug/explain.py MES 07:51 --window 10
    uv run scripts/debug/explain.py --bot-id chart-bot-1 22:03

What it does (in <1s for 99% of cases):
  1. Resolves SYMBOL → bot_id via the YAML registry.
  2. Pulls every ``bot_events`` row for that bot in [TIME-window, TIME+window].
     The ``ix_bot_events_bot_recorded`` index makes this a hash-lookup.
  3. Tail-greps ``logs/ib_trader.log`` for the same time window for
     IB errors, FSM transitions, and HTTP failures keyed to the symbol.
  4. Reads the latest bar from Redis ``bar:<symbol>:5s`` so you can
     tell whether the engine is even seeing ticks.
  5. Prints a single consolidated report — bot state, what the SR
     algorithm saw on each bar (BAR rows), any rejected orders.

Designed for Claude (and humans) to answer "the bot didn't fire at
X" questions without grepping through 60+MB of logs by hand.
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
    """Tail-grep ``logs/ib_trader.log`` for symbol + window. Returns
    full JSON lines whose timestamp falls in the range and that mention
    the symbol OR the bot's id OR an IB error. Skips the noisy
    ``IB_THROTTLED``/HTTP-noise lines by default."""
    if not LOG_PATH.exists():
        return []
    lo_iso = lo.astimezone(PT).strftime("%Y-%m-%dT%H:%M")
    hi_iso = hi.astimezone(PT).strftime("%Y-%m-%dT%H:%M")
    # rg for speed when available; fall back to grep.
    pattern = "|".join((
        re.escape(symbol),
        r"IB_ERROR",
        r"BOT_STARTUP_FORCED_OFF",
        r"FSM_TRANSITION",
        r"SUBSCRIBE_BARS_FAILED",
        r"UNSUBSCRIBE_BARS_FAILED",
        r"WARMUP_BARS_PUBLISHED",
        r"BOT_AUTOSTARTED",
        r"BOT_TASK_STARTED",
        r"STRATEGY_BOT_STARTED",
    ))
    grep_cmd = ["grep", "-E", pattern, str(LOG_PATH)]
    try:
        out = subprocess.check_output(grep_cmd, text=True, errors="replace")
    except subprocess.CalledProcessError:
        return []
    keep: list[str] = []
    for line in out.splitlines():
        # Tight time filter — line starts with `{"timestamp": "ISO".
        m = re.search(r'"timestamp":\s*"([0-9T:.\-+]+)"', line)
        if not m:
            continue
        ts = m.group(1)
        # ISO comparisons string-wise work because of fixed-width
        # ISO 8601 with the same offset.
        if ts[: len(lo_iso)] < lo_iso or ts[: len(hi_iso)] > hi_iso:
            continue
        # Drop a couple of common noise events we never want.
        if '"event": "IB_THROTTLED"' in line:
            continue
        if '"HTTP Request' in line:
            continue
        if '"DEBUG"' in line:
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


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Fast troubleshooting for missed signals / orders.",
    )
    ap.add_argument("symbol", help="Contract symbol or bot id (e.g. MGCM6, chart-bot-1).")
    ap.add_argument("time", nargs="?", default=None,
                    help='Time (PT) — "HH:MM" / "HH:MM:SS" / "YYYY-MM-DD HH:MM". Default: now.')
    ap.add_argument("--window", type=int, default=15,
                    help="Minutes ± around TIME to search. Default 15.")
    ap.add_argument("--bot-id", default=None,
                    help="Explicit bot id (skips registry lookup).")
    args = ap.parse_args()

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
