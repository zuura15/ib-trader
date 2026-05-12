"""Backtest the live chart_signal rules on 30 days of 3-min bars for
the four currently-running micro futures (MGC, MCL, MES, MNQ).

Each symbol's history is fetched directly from IB via
``fetch_history.fetch`` with a +50 client-id offset so the running
engine isn't disturbed. The new ``run_backtest_live`` function
mirrors the live rules: single position at a time across sides,
3rd-touch + fresh-pivot entry, exit on the earlier of line-breach
or trail (per-symbol ``trail_width_pct``), and a one-bar cooldown
after each round-trip.

Per-symbol multiplier matches the bot YAMLs:
  MGC = 10 oz/contract
  MCL = 100 bbl/contract
  MES = $5/index point
  MNQ = $2/index point

Pure read-only: never writes to the dev server, never modifies
SQLite, never publishes anything to Redis. Just fetches IB
historicals and prints summary tables.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402
from scripts.backtest.fetch_history import fetch  # noqa: E402

# (display_label, ib_local_symbol, multiplier, trail_width_pct,
#  commission_per_side_usd)
# Commission values per the operator's live broker statements:
#   MES, MNQ: $0.62/side → $1.24/round-trip
#   MGC:      $0.97/side → $1.94/round-trip
#   MCL:      $0.77/side → $1.54/round-trip
SYMBOLS = [
    ("MGC", "MGCM6", 10,  0.0002, 0.97),
    ("MCL", "MCLM6", 100, 0.0013, 0.77),
    ("MES", "MESM6", 5,   0.0003, 0.62),
    ("MNQ", "MNQM6", 2,   0.0002, 0.62),
]


def _summarize(label: str, trades: list[sr_backtest.Trade],
                multiplier: float, trail_pct: float,
                comm_per_side: float) -> dict:
    longs = [t for t in trades if t.side == "LONG"]
    shorts = [t for t in trades if t.side == "SHORT"]
    closed = [t for t in trades if t.exit_reason != "EOD"]
    by_reason: dict[str, int] = {}
    for t in trades:
        if t.exit_reason:
            by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    total = sum(t.pnl for t in trades)
    closed_pnl = sum(t.pnl for t in closed)
    # Each round-trip = 2 sides × ``comm_per_side``. Open trades (EOD)
    # have only paid the entry side, so subtract one side per open
    # trade.
    n_rt_sides = 2 * len(closed) + 1 * (len(trades) - len(closed))
    total_comm = n_rt_sides * comm_per_side
    wins = sum(1 for t in trades if t.pnl > 0)
    # Net wins after commission per trade: a trade with $14 gross
    # gain becomes a net win only if it exceeds 2×comm. Compute the
    # per-trade net distribution since gross win% lies above the
    # commission floor.
    net_wins = 0
    for t in trades:
        net = t.pnl - (2 * comm_per_side if t.exit_reason != "EOD"
                        else comm_per_side)
        if net > 0:
            net_wins += 1
    return {
        "label": label,
        "multiplier": multiplier,
        "trail_pct": trail_pct,
        "comm_per_side": comm_per_side,
        "n_trades": len(trades),
        "n_longs": len(longs),
        "n_shorts": len(shorts),
        "n_closed": len(closed),
        "wins": wins,
        "net_wins": net_wins,
        "win_rate": (wins / len(trades) * 100) if trades else 0.0,
        "net_win_rate": (net_wins / len(trades) * 100) if trades else 0.0,
        "total_pnl": total,
        "closed_pnl": closed_pnl,
        "total_comm": total_comm,
        "net_pnl": total - total_comm,
        "by_reason": by_reason,
        "first_t": trades[0].entry_t if trades else None,
        "last_t": (trades[-1].exit_t or trades[-1].entry_t) if trades else None,
    }


def _print_summary(s: dict) -> None:
    print()
    print(f"=== {s['label']} (mult={s['multiplier']}, "
          f"trail={s['trail_pct']*100:.2f}%, "
          f"comm=${s['comm_per_side']:.2f}/side) ===")
    print(f"  window:           {s['first_t']} → {s['last_t']}")
    print(f"  trades:           {s['n_trades']} "
          f"(longs={s['n_longs']}, shorts={s['n_shorts']})")
    print(f"  closed:           {s['n_closed']} "
          f"({s['n_trades'] - s['n_closed']} open at EOD)")
    print(f"  wins (gross):     {s['wins']:>4}  win-rate {s['win_rate']:.1f}%")
    print(f"  wins (net):       {s['net_wins']:>4}  win-rate {s['net_win_rate']:.1f}%")
    print(f"  gross P&L:        ${s['total_pnl']:>10.2f}")
    print(f"  commission:       ${s['total_comm']:>10.2f}")
    print(f"  NET P&L:          ${s['net_pnl']:>10.2f}")
    print(f"  exits by reason:  {s['by_reason']}")


async def _fetch_one(symbol: str, hours: int, cache_dir: Path) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    out_path = cache_dir / f"{symbol}_30d.json"
    if out_path.exists() and out_path.stat().st_size > 1000:
        print(f"using cached {out_path}", file=sys.stderr)
        return out_path
    print(f"fetching {hours}h of 3-min bars for {symbol}…", file=sys.stderr)
    bars = await fetch(symbol, "FUT", hours, "3 mins")
    out_path.write_text(json.dumps({"bars": bars}))
    print(f"  wrote {len(bars)} bars → {out_path}", file=sys.stderr)
    return out_path


def _load_bars(path: Path) -> list[sr_backtest.Bar]:
    raw = json.loads(path.read_text())
    bar_list = raw.get("bars", raw) if isinstance(raw, dict) else raw
    bars = [sr_backtest.Bar(
        t=b["ts"], open=b["open"], high=b["high"],
        low=b["low"], close=b["close"],
    ) for b in bar_list]
    bars.sort(key=lambda b: b.t)
    return bars


async def main_async() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--hours", type=int, default=720, help="30 days = 720h.")
    p.add_argument("--cache-dir", default="/tmp/chart_signal_30d",
                    help="Where to cache fetched bars.")
    p.add_argument("--print-trades", action="store_true",
                    help="Dump every trade row in addition to summary.")
    args = p.parse_args()

    cache_dir = Path(args.cache_dir)
    summaries = []
    for label, ib_sym, mult, trail, comm in SYMBOLS:
        path = await _fetch_one(ib_sym, args.hours, cache_dir)
        bars = _load_bars(path)
        if not bars:
            print(f"{label}: NO BARS — skipping", file=sys.stderr)
            continue
        sr_backtest.MULTIPLIER = float(mult)
        trades = sr_backtest.run_backtest_live(
            bars, trail_width_pct=trail, cooldown_bars=1,
        )
        summaries.append(_summarize(label, trades, mult, trail, comm))
        if args.print_trades:
            print(f"\n--- {label} trades ---")
            for tr in trades:
                e = tr.entry_t[:16].replace("T", " ")
                x = (tr.exit_t or "")[:16].replace("T", " ") if tr.exit_t else "open"
                print(f"  {tr.side:<6} {e}  →  {x:<16} "
                      f"entry={tr.entry_price:>10.2f} "
                      f"exit={(tr.exit_price or 0):>10.2f} "
                      f"pnl=${tr.pnl:>8.2f} "
                      f"touches={tr.line_touches} "
                      f"reason={tr.exit_reason}")

    for s in summaries:
        _print_summary(s)

    print()
    print("=== Aggregate ===")
    total = sum(s["total_pnl"] for s in summaries)
    comm = sum(s["total_comm"] for s in summaries)
    net = sum(s["net_pnl"] for s in summaries)
    n_total = sum(s["n_trades"] for s in summaries)
    n_wins = sum(s["wins"] for s in summaries)
    n_net_wins = sum(s["net_wins"] for s in summaries)
    print(f"  total trades:     {n_total}")
    print(f"  wins (gross):     {n_wins:>5} ({(n_wins / n_total * 100 if n_total else 0):.1f}%)")
    print(f"  wins (net):       {n_net_wins:>5} ({(n_net_wins / n_total * 100 if n_total else 0):.1f}%)")
    print(f"  gross P&L:        ${total:>10.2f}")
    print(f"  commission:       ${comm:>10.2f}")
    print(f"  NET P&L:          ${net:>10.2f}")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
