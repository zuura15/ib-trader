"""Shoulder-asymmetry analysis.

Operator's visual cue for "good" vs "bad" pivots:
  pivot LOW (LONG entry):  right shoulder bar must close HIGHER
                            than the left shoulder bar (post-pivot
                            close > pre-pivot close).
  pivot HIGH (SHORT entry): right shoulder bar must close LOWER
                            than the left shoulder bar.

In bar indices with pivot at p = entry_idx − 1:
  left  = close[p−1]    (bar BEFORE the pivot bar)
  right = close[p+1]    (bar AFTER the pivot bar = entry bar's close)
  LONG  good_shoulder  : right > left
  SHORT good_shoulder  : right < left

Buckets baseline backtest trades into good-shoulder vs bad-shoulder
and reports trade count, win rate, NET P&L per bucket. Pure read-
only on the TRADES 30d cache.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402

CACHE_DIR = Path("/tmp/chart_signal_30d_trades")

SYMBOLS = [
    ("MGC", "MGCM6", 10,  0.0002, 0.97),
    ("MCL", "MCLM6", 100, 0.0013, 0.77),
    ("MES", "MESM6", 5,   0.0003, 0.62),
    ("MNQ", "MNQM6", 2,   0.0002, 0.62),
]


def _load(ib_sym: str) -> list[sr_backtest.Bar]:
    raw = json.loads((CACHE_DIR / f"{ib_sym}_30d.json").read_text())
    bar_list = raw.get("bars", raw)
    bar_list.sort(key=lambda b: b["ts"])
    return [sr_backtest.Bar(
        t=b["ts"], open=b["open"], high=b["high"],
        low=b["low"], close=b["close"],
    ) for b in bar_list]


def _trade_net(t, comm):
    sides = 1 if t.exit_reason == "EOD" else 2
    return getattr(t, "_frozen_pnl", t.pnl) - sides * comm


def _is_good_shoulder(bars, entry_idx, side) -> bool | None:
    p = entry_idx - 1
    if p - 1 < 0 or p + 1 > len(bars) - 1:
        return None
    left = bars[p - 1].close
    right = bars[p + 1].close
    if side == "LONG":
        return right > left
    else:
        return right < left


def main() -> int:
    cache = {}
    for label, ib_sym, mult, trail, comm in SYMBOLS:
        bars = _load(ib_sym)
        sr_backtest.MULTIPLIER = float(mult)
        trades = sr_backtest.run_backtest_live(
            bars, trail_width_pct=trail, cooldown_bars=1,
        )
        for t in trades:
            t._frozen_pnl = t.pnl
        cache[label] = (bars, trades, comm)

    print(f"{'sym':<5}{'bucket':<10}{'n':>6}{'win':>5}{'win%':>7}"
          f"{'gross$':>11}{'comm$':>10}{'NET$':>11}{'$/trade':>10}")
    print("-" * 75)

    agg = {"good": {"n": 0, "wins": 0, "net": 0.0},
           "bad":  {"n": 0, "wins": 0, "net": 0.0}}

    for label, ib_sym, mult, trail, comm in SYMBOLS:
        bars, trades, comm = cache[label]
        buckets = {"good": [], "bad": []}
        for t in trades:
            good = _is_good_shoulder(bars, t.entry_idx, t.side)
            if good is None:
                continue
            buckets["good" if good else "bad"].append(t)
        for name in ("good", "bad"):
            ts = buckets[name]
            n = len(ts)
            wins = sum(1 for t in ts
                       if getattr(t, "_frozen_pnl", t.pnl) > 0)
            gross = sum(getattr(t, "_frozen_pnl", t.pnl) for t in ts)
            sides_paid = sum(1 if t.exit_reason == "EOD" else 2 for t in ts)
            comm_total = sides_paid * comm
            net = gross - comm_total
            wr = 100 * wins / n if n else 0.0
            per = net / n if n else 0.0
            print(f"{label:<5}{name:<10}{n:>6}{wins:>5}{wr:>6.1f}%"
                  f"{gross:>11.2f}{comm_total:>10.2f}"
                  f"{net:>11.2f}{per:>+10.3f}")
            a = agg[name]
            a["n"] += n; a["wins"] += wins; a["net"] += net
        print()

    print("=== Aggregate ===")
    for name in ("good", "bad"):
        a = agg[name]
        wr = 100 * a["wins"] / a["n"] if a["n"] else 0.0
        per = a["net"] / a["n"] if a["n"] else 0.0
        print(f"  {name:<10}{a['n']:>6}{a['wins']:>5}"
              f"{wr:>6.1f}%   NET ${a['net']:>10.2f}   "
              f"${per:>+7.3f}/trade")

    total = agg["good"]["n"] + agg["bad"]["n"]
    if total:
        print(f"  good = {100*agg['good']['n']/total:.1f}% of all trades")
        print(f"  if FILTER (drop bad-shoulder) → NET ${agg['good']['net']:.2f}"
              f"   vs baseline ${agg['good']['net']+agg['bad']['net']:.2f}"
              f"   (Δ ${-agg['bad']['net']:+.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
