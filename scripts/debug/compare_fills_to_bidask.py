"""Compare each of today's actual bot fills against the BID_ASK bar
that covered the fill time.

For each entry / exit:
  bid  = bar.open  (IB BID_ASK convention: time-weighted avg bid)
  ask  = bar.close (time-weighted avg ask)
  mid  = (bid + ask) / 2
Reports per-side slippage = fill_price - mid (signed for direction).

Negative slippage on BUY entries = bought below mid (good).
Negative slippage on SELL exits  = sold below mid (bad).

The aggregate over today tells us whether BID_ASK-based simulation
is systematically optimistic or pessimistic vs reality.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest.fetch_history import fetch  # noqa: E402

DB_PATH = _REPO_ROOT / "trader.db"
BAR_SECONDS = 180


def _today_trades() -> list[dict]:
    """Bot trades whose entry happened in the last 30 hours (covers
    overnight session + dev/prod sync timing)."""
    conn = sqlite3.connect(str(DB_PATH))
    c = conn.cursor()
    c.execute("""
        SELECT symbol, direction, entry_price, entry_time,
               exit_price, exit_time, realized_pnl
        FROM bot_trades
        WHERE datetime(entry_time) >= datetime('now', '-30 hours')
        ORDER BY entry_time
    """)
    rows = []
    for r in c.fetchall():
        rows.append({
            "symbol": r[0], "direction": r[1],
            "entry_price": float(r[2]), "entry_time": r[3],
            "exit_price": float(r[4]) if r[4] is not None else None,
            "exit_time": r[5],
            "pnl": float(r[6]) if r[6] is not None else None,
        })
    return rows


async def _fetch_bidask(symbol: str, hours: int) -> list[dict]:
    """Fetch BID_ASK bars covering the last ``hours``. Cached under
    /tmp/today_bidask/ to keep reruns cheap."""
    cache = Path("/tmp/today_bidask")
    cache.mkdir(exist_ok=True)
    out = cache / f"{symbol}_{hours}h.json"
    if out.exists() and out.stat().st_size > 1000:
        return json.loads(out.read_text())["bars"]
    bars = await fetch(symbol, "FUT", hours, "3 mins",
                       what_to_show="BID_ASK")
    out.write_text(json.dumps({"bars": bars}))
    return bars


def _parse_db_time(s: str) -> datetime:
    """SQLite stores naive UTC. Add tz."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _bar_at(bars: list[dict], t: datetime) -> dict | None:
    """Bar whose slot covers ``t`` (bar.ts <= t < bar.ts + BAR_SECONDS)."""
    for b in bars:
        bt = datetime.fromisoformat(b["ts"].replace("Z", "+00:00"))
        if bt <= t < bt.fromtimestamp(bt.timestamp() + BAR_SECONDS,
                                      tz=timezone.utc):
            return b
    return None


def _leg_slip(side: str, action: str, fill: float, bid: float,
              ask: float) -> tuple[float, float, str]:
    """Return (slip_vs_mid, sign_label, qualitative).

    action ∈ {"open", "close"}; side ∈ {"LONG", "SHORT"}.
    """
    mid = (bid + ask) / 2
    slip = fill - mid
    # BUY = LONG-open or SHORT-close: paying ask is bad.
    is_buy = (action == "open" and side == "LONG") or \
             (action == "close" and side == "SHORT")
    if is_buy:
        qual = "good" if slip < 0 else "bad"
    else:
        qual = "good" if slip > 0 else "bad"
    return slip, "BUY" if is_buy else "SELL", qual


async def main() -> int:
    trades = _today_trades()
    if not trades:
        print("no recent trades")
        return 0
    print(f"loaded {len(trades)} trades")

    # Fetch BID_ASK per symbol. 36 hours covers entry+exit of every
    # trade with margin for the earliest fill in the window.
    syms = sorted({t["symbol"] for t in trades})
    print(f"fetching BID_ASK for {syms}…", file=sys.stderr)
    bars_by_sym: dict[str, list[dict]] = {}
    for s in syms:
        bars_by_sym[s] = await _fetch_bidask(s, 36)
        print(f"  {s}: {len(bars_by_sym[s])} bars", file=sys.stderr)

    print()
    print(f"{'symbol':<8}{'side':<6}{'action':<7}{'time':<20}"
          f"{'fill':>10}{'bid':>10}{'ask':>10}{'mid':>10}"
          f"{'slip_vs_mid':>13}")
    print("-" * 94)

    sums: dict[str, dict] = {
        "BUY":  {"n": 0, "slip": 0.0, "abs_slip": 0.0},
        "SELL": {"n": 0, "slip": 0.0, "abs_slip": 0.0},
    }
    # Time-bucket: RTH (06:30–13:00 PT), Afternoon (13:00–17:00 PT),
    # Evening (17:00+ PT). Each bucket tracks per-leg slippage and
    # per-leg counts so we can compare bid-ask quality across the
    # session split.
    def _bucket_of(local_dt) -> str:
        hm = local_dt.strftime("%H:%M")
        if "06:30" <= hm < "13:00":
            return "RTH"
        if "13:00" <= hm < "17:00":
            return "Afternoon"
        return "Evening"
    buckets: dict[str, dict] = {
        k: {"n": 0, "slip": 0.0, "abs_slip": 0.0, "pnl_actual": 0.0,
            "pnl_mid": 0.0, "trades": 0}
        for k in ("RTH", "Afternoon", "Evening")
    }
    pnl_actual_total = 0.0
    pnl_mid_total = 0.0

    for tr in trades:
        if tr["exit_price"] is None:
            continue
        bars = bars_by_sym[tr["symbol"]]
        e_t = _parse_db_time(tr["entry_time"])
        x_t = _parse_db_time(tr["exit_time"])
        e_bar = _bar_at(bars, e_t)
        x_bar = _bar_at(bars, x_t)
        if e_bar is None or x_bar is None:
            continue

        e_bid, e_ask = e_bar["open"], e_bar["close"]
        x_bid, x_ask = x_bar["open"], x_bar["close"]
        e_slip, e_dir, e_q = _leg_slip(
            tr["direction"], "open",  tr["entry_price"], e_bid, e_ask,
        )
        x_slip, x_dir, x_q = _leg_slip(
            tr["direction"], "close", tr["exit_price"],  x_bid, x_ask,
        )
        e_mid = (e_bid + e_ask) / 2
        x_mid = (x_bid + x_ask) / 2

        sign = 1 if tr["direction"] == "LONG" else -1
        pnl_actual = (tr["exit_price"] - tr["entry_price"]) * sign
        pnl_mid = (x_mid - e_mid) * sign
        pnl_actual_total += pnl_actual
        pnl_mid_total += pnl_mid

        print(f"{tr['symbol']:<8}{tr['direction']:<6}{'open':<7}"
              f"{e_t.strftime('%m-%d %H:%M:%S'):<20}"
              f"{tr['entry_price']:>10.4f}{e_bid:>10.4f}{e_ask:>10.4f}"
              f"{e_mid:>10.4f}{e_slip:>+10.4f} {e_dir}")
        print(f"{tr['symbol']:<8}{tr['direction']:<6}{'close':<7}"
              f"{x_t.strftime('%m-%d %H:%M:%S'):<20}"
              f"{tr['exit_price']:>10.4f}{x_bid:>10.4f}{x_ask:>10.4f}"
              f"{x_mid:>10.4f}{x_slip:>+10.4f} {x_dir}")
        sums[e_dir]["n"] += 1
        sums[e_dir]["slip"] += e_slip
        sums[e_dir]["abs_slip"] += abs(e_slip)
        sums[x_dir]["n"] += 1
        sums[x_dir]["slip"] += x_slip
        sums[x_dir]["abs_slip"] += abs(x_slip)

        # Time-bucket each leg by its LOCAL fill time.
        e_bucket = _bucket_of(e_t.astimezone())
        x_bucket = _bucket_of(x_t.astimezone())
        for b, slip in ((e_bucket, e_slip), (x_bucket, x_slip)):
            buckets[b]["n"] += 1
            buckets[b]["slip"] += slip
            buckets[b]["abs_slip"] += abs(slip)
        # Trade-level PnL goes into the ENTRY bucket so each trade is
        # counted once. Same convention as a "session" view.
        buckets[e_bucket]["trades"] += 1
        buckets[e_bucket]["pnl_actual"] += pnl_actual
        buckets[e_bucket]["pnl_mid"] += pnl_mid

    print()
    print("=== Slippage vs mid (BID_ASK bar) ===")
    for kind in ("BUY", "SELL"):
        s = sums[kind]
        if s["n"]:
            print(f"  {kind:<5}  n={s['n']:>3}  "
                  f"mean slip ${s['slip']/s['n']:+.4f}  "
                  f"mean |slip| ${s['abs_slip']/s['n']:.4f}")

    print()
    print(f"PnL with actual fills (price units): {pnl_actual_total:+.2f}")
    print(f"PnL if filled at mid both legs:       {pnl_mid_total:+.2f}")
    delta = pnl_actual_total - pnl_mid_total
    print(f"  Δ vs mid-fill simulation:           {delta:+.2f}   "
          f"({'bot did better' if delta > 0 else 'mid would have been better'})")

    print()
    print("=== Slippage by session window (local PT) ===")
    print(f"{'bucket':<11}{'legs':>5}{'mean slip$':>12}{'|slip|$':>11}"
          f"{'trades':>8}{'actual PnL':>13}{'mid PnL':>11}{'Δ':>10}")
    for name in ("RTH", "Afternoon", "Evening"):
        b = buckets[name]
        if b["n"] == 0:
            continue
        mean_slip = b["slip"] / b["n"]
        mean_abs = b["abs_slip"] / b["n"]
        delta_b = b["pnl_actual"] - b["pnl_mid"]
        print(f"{name:<11}{b['n']:>5}{mean_slip:>+12.4f}{mean_abs:>11.4f}"
              f"{b['trades']:>8}{b['pnl_actual']:>+13.2f}"
              f"{b['pnl_mid']:>+11.2f}{delta_b:>+10.2f}")
    print()
    print("Reading:")
    print("  |slip|$  = per-leg avg distance from bar mid (price units)")
    print("  Δ        = actual PnL − hypothetical mid-fill PnL "
          "(negative = bot underperformed mid)")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
