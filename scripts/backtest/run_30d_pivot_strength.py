"""Sweep ``min_pivot_strength`` across 30 d of cached bars.

Filters micro-pivots whose smaller-side close-delta is below ``k ×
tick_size``. Visually-flat blips like the 2026-05-12 MGCM6 18:33
pivot ($0.60 / 6 ticks) routinely satisfy the strict 1/1 rule and
fire dud entries; the threshold rejects them at the find_pivot_*
level so both line construction and the freshness gate ignore
them.

Sweeps ``min_pivot_strength_ticks ∈ {0, 2, 3, 5, 7, 10}`` per
symbol. Same commissions as ``run_30d_chart_signal.py``.

Pure read-only.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402

# (label, ib_symbol, multiplier, base_trail_pct, commission/side,
#  tick_size $)
SYMBOLS = [
    ("MGC", "MGCM6", 10,  0.0002, 0.97, 0.10),
    ("MCL", "MCLM6", 100, 0.0013, 0.77, 0.01),
    ("MES", "MESM6", 5,   0.0003, 0.62, 0.25),
    ("MNQ", "MNQM6", 2,   0.0002, 0.62, 0.25),
]

TICKS_SWEEP = [0, 2, 3, 5, 7, 10]


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


def _run(bars, mult, comm, **kwargs) -> dict:
    sr_backtest.MULTIPLIER = float(mult)
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
    print(f"{'sym':<5}{'config':<14}{'trades':>8}{'win%':>8}"
          f"{'gross$':>11}{'comm$':>10}{'NET$':>11}")
    print("-" * 76)

    agg: dict[str, dict] = {}
    for label, ib_sym, mult, base_trail, comm, tick in SYMBOLS:
        bars = _load(ib_sym)
        if len(bars) < 100:
            print(f"{label}: too few bars — skip", file=sys.stderr)
            continue
        for n_ticks in TICKS_SWEEP:
            min_strength = float(n_ticks) * tick
            r = _run(bars, mult, comm,
                     trail_width_pct=base_trail, cooldown_bars=1,
                     min_pivot_strength=min_strength)
            key = "baseline" if n_ticks == 0 else f"≥{n_ticks}t (${min_strength:.2f})"
            agg.setdefault(key, {"net": 0.0, "n": 0, "wins": 0})
            agg[key]["net"] += r["net"]
            agg[key]["n"] += r["n"]
            agg[key]["wins"] += r["wins"]
            print(f"{label:<5}{key:<14}"
                  f"{r['n']:>8}{r['win_rate']:>7.1f}%"
                  f"{r['gross']:>11.2f}{r['comm']:>10.2f}"
                  f"{r['net']:>11.2f}")
        print()

    print("=== Aggregate NET P&L by min-pivot-strength (4 symbols, 30 d) ===")
    rows = sorted(agg.items(), key=lambda kv: kv[1]["net"], reverse=True)
    for name, v in rows:
        wr = (v["wins"] / v["n"] * 100) if v["n"] else 0.0
        print(f"  {name:<16} {v['n']:>6} trades  "
              f"win {wr:>5.1f}%   NET ${v['net']:>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
