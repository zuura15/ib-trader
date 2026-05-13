"""Sweep the triangle-rejection exit + re-entry-watch combo.

Vary:
  triangle_min_touches    ∈ {2, 3}
  reentry_watch_bars      ∈ {2, 4, 6, 8, 12}

Run against 15-mo BID_ASK cache. Report per-symbol + aggregate.
"""
from __future__ import annotations

import json
import os
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402

HIST_DIR = Path(os.environ.get(
    "IB_LONG_HIST_DIR", "/tmp/long_history_bidask",
))

SYMBOLS = [
    ("MGC", 10,  0.0002, 0.97),
    ("MES",  5,  0.0003, 0.62),
    ("MNQ",  2,  0.0002, 0.62),
]

NEAR_TOUCH_FRAC = 0.001
TOUCH_VARIANTS = [2, 3]
WATCH_VARIANTS = [2, 4, 6, 8, 12]


def _load(path: Path) -> list[sr_backtest.Bar]:
    raw = json.loads(path.read_text())
    bar_list = raw.get("bars", raw)
    bar_list.sort(key=lambda b: b["ts"])
    return [sr_backtest.Bar(
        t=b["ts"], open=b["open"], high=b["high"],
        low=b["low"], close=b["close"],
    ) for b in bar_list]


def _stats(trades, comm):
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    gross = sum(t.pnl for t in trades)
    sides = sum(1 if t.exit_reason == "EOD" else 2 for t in trades)
    return n, wins, gross, gross - sides * comm


def main() -> int:
    print(f"using history dir: {HIST_DIR}")
    print()
    sym_baseline: dict[str, float] = {}
    grid: dict[tuple[int, int], dict[str, float]] = defaultdict(dict)

    for label, mult, trail, comm in SYMBOLS:
        files = sorted(HIST_DIR.glob(f"{label}_*_3min_*.json"))
        if not files:
            continue
        sr_backtest.MULTIPLIER = float(mult)
        # Baseline once per symbol.
        total_base = 0.0
        for f in files:
            bars = _load(f)
            if len(bars) < 200:
                continue
            base = sr_backtest.run_backtest_live(
                bars,
                trail_width_pct=trail,
                cooldown_bars=1,
                near_touch_tolerance_fraction=NEAR_TOUCH_FRAC,
            )
            _, _, _, net = _stats(base, comm)
            total_base += net
        sym_baseline[label] = total_base
        print(f"{label}: baseline NET = ${total_base:>11.2f}")

        for tt in TOUCH_VARIANTS:
            for wb in WATCH_VARIANTS:
                total = 0.0
                for f in files:
                    bars = _load(f)
                    if len(bars) < 200:
                        continue
                    tri = sr_backtest.run_backtest_live(
                        bars,
                        trail_width_pct=trail,
                        cooldown_bars=1,
                        near_touch_tolerance_fraction=NEAR_TOUCH_FRAC,
                        exit_on_triangle_rejection=True,
                        triangle_min_touches=tt,
                        triangle_reentry_watch_bars=wb,
                    )
                    _, _, _, net = _stats(tri, comm)
                    total += net
                grid[(tt, wb)][label] = total
                delta = total - total_base
                print(f"  touches={tt}  watch={wb:>2}b  NET ${total:>10.2f}"
                      f"   Δ ${delta:>+8.2f}")
        print()

    print("=== Aggregate (NET sum across MGC + MES + MNQ) ===")
    base_agg = sum(sym_baseline.values())
    print(f"baseline                NET ${base_agg:>11.2f}")
    rows = []
    for (tt, wb), per_sym in grid.items():
        agg = sum(per_sym.values())
        rows.append((tt, wb, agg, agg - base_agg))
    rows.sort(key=lambda r: r[3], reverse=True)
    for tt, wb, agg, delta in rows:
        print(f"  touches={tt}  watch={wb:>2}b  NET ${agg:>11.2f}"
              f"   Δ ${delta:>+9.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
