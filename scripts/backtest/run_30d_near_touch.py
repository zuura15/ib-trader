"""Sweep near-touch tolerance across 30d of cached bars.

Rule under test: once a trendline has accumulated MIN_TOUCHES strict
touches from older pivots, additional pivots within a widened
``near_touch_tolerance_fraction`` band count as further touches —
i.e. the entry gate accepts a 4th/5th pivot that is close-but-not-
exactly-on an established line. The first three touches stay strict
so line construction quality is unchanged.

Compares the live baseline (``near_touch=None``) against multipliers
of ``TOUCH_TOLERANCE_FRACTION`` (1×, 2×, 3×, 5×). Uses the same
cached bars and commissions as ``run_30d_chart_signal.py`` so the
numbers are apples-to-apples vs the production live-rules baseline.

Pure read-only: never touches the dev or prod boxes.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402
from ib_trader.signals.sr_fan import TOUCH_TOLERANCE_FRACTION  # noqa: E402

# (display_label, ib_local_symbol, multiplier, base_trail_pct, comm_per_side)
SYMBOLS = [
    ("MGC", "MGCM6", 10,  0.0002, 0.97),
    ("MCL", "MCLM6", 100, 0.0013, 0.77),
    ("MES", "MESM6", 5,   0.0003, 0.62),
    ("MNQ", "MNQM6", 2,   0.0002, 0.62),
]

# Multipliers of TOUCH_TOLERANCE_FRACTION (= 0.0002). 1.0 collapses
# to the strict baseline (loose tol == strict tol → the loose branch
# in sr_fan/_accept is inactive). 2x ≈ $1.89 on MGC, 5x ≈ $4.72.
NEAR_MULTIPLIERS = [1.0, 2.0, 3.0, 5.0]


def _load(ib_sym: str) -> list[sr_backtest.Bar]:
    p = Path(f"/tmp/chart_signal_30d/{ib_sym}_30d.json")
    raw = json.loads(p.read_text())
    bar_list = raw.get("bars", raw) if isinstance(raw, dict) else raw
    bars = [sr_backtest.Bar(
        t=b["ts"], open=b["open"], high=b["high"],
        low=b["low"], close=b["close"],
    ) for b in bar_list]
    bars.sort(key=lambda b: b.t)
    return bars


def _run(bars, multiplier, comm, **kwargs) -> dict:
    sr_backtest.MULTIPLIER = float(multiplier)
    trades = sr_backtest.run_backtest_live(bars, **kwargs)
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    gross = sum(t.pnl for t in trades)
    closed = [t for t in trades if t.exit_reason != "EOD"]
    n_sides = 2 * len(closed) + (n - len(closed))
    comm_total = n_sides * comm
    return {
        "n": n,
        "wins": wins,
        "win_rate": (wins / n * 100) if n else 0.0,
        "gross": gross,
        "comm": comm_total,
        "net": gross - comm_total,
    }


def main() -> int:
    print(f"{'sym':<5}{'config':<22}{'trades':>8}{'win%':>8}"
          f"{'gross$':>11}{'comm$':>10}{'NET$':>11}")
    print("-" * 75)

    agg: dict[str, dict] = {}
    for label, ib_sym, mult, base_trail, comm in SYMBOLS:
        bars = _load(ib_sym)
        if len(bars) < 100:
            print(f"{label}: too few bars — skip", file=sys.stderr)
            continue

        # Baseline = strict gate (no widening).
        base = _run(bars, mult, comm,
                    trail_width_pct=base_trail, cooldown_bars=1,
                    near_touch_tolerance_fraction=None)
        agg.setdefault("baseline (strict)", {"net": 0.0, "n": 0})
        agg["baseline (strict)"]["net"] += base["net"]
        agg["baseline (strict)"]["n"] += base["n"]
        print(f"{label:<5}{'baseline (strict)':<22}"
              f"{base['n']:>8}{base['win_rate']:>7.1f}%"
              f"{base['gross']:>11.2f}{base['comm']:>10.2f}"
              f"{base['net']:>11.2f}")

        for m in NEAR_MULTIPLIERS:
            frac = TOUCH_TOLERANCE_FRACTION * m
            r = _run(bars, mult, comm,
                     trail_width_pct=base_trail, cooldown_bars=1,
                     near_touch_tolerance_fraction=frac)
            key = f"near × {m:.1f}"
            agg.setdefault(key, {"net": 0.0, "n": 0})
            agg[key]["net"] += r["net"]
            agg[key]["n"] += r["n"]
            print(f"{label:<5}{key:<22}"
                  f"{r['n']:>8}{r['win_rate']:>7.1f}%"
                  f"{r['gross']:>11.2f}{r['comm']:>10.2f}"
                  f"{r['net']:>11.2f}")
        print()

    print("=== Aggregate NET P&L by config (across 4 symbols) ===")
    rows = sorted(agg.items(), key=lambda kv: kv[1]["net"], reverse=True)
    for name, v in rows:
        print(f"  {name:<22}{v['n']:>6} trades   NET ${v['net']:>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
