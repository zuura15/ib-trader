"""Sweep ATR-scaled trailing stop across 30d of bars.

Reuses the cached bars at /tmp/chart_signal_30d/ and the live-rules
backtest. For each symbol, runs the baseline (fixed % trail) and a
series of ATR-multiplier configs to see whether scaling the trail by
realized vol beats the fixed-% trail. NET P&L uses the same
per-symbol commissions as run_30d_chart_signal.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402

# (display_label, ib_local_symbol, multiplier, base_trail_pct, comm_per_side)
SYMBOLS = [
    ("MGC", "MGCM6", 10,  0.0002, 0.97),
    ("MCL", "MCLM6", 100, 0.0013, 0.77),
    ("MES", "MESM6", 5,   0.0003, 0.62),
    ("MNQ", "MNQM6", 2,   0.0002, 0.62),
]

ATR_MULTS = [1.0, 1.5, 2.0, 2.5, 3.0, 4.0]


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
    print(f"{'sym':<5}{'config':<20}{'trades':>8}{'win%':>8}"
          f"{'gross$':>11}{'comm$':>10}{'NET$':>11}")
    print("-" * 73)

    agg: dict[str, dict] = {}
    for label, ib_sym, mult, base_trail, comm in SYMBOLS:
        bars = _load(ib_sym)
        if len(bars) < 100:
            print(f"{label}: too few bars — skip", file=sys.stderr)
            continue

        # Baseline: fixed % trail (live config).
        base = _run(bars, mult, comm,
                    trail_width_pct=base_trail, cooldown_bars=1)
        agg.setdefault("baseline", {"net": 0.0, "n": 0})
        agg["baseline"]["net"] += base["net"]
        agg["baseline"]["n"] += base["n"]
        print(f"{label:<5}{'baseline (% trail)':<20}"
              f"{base['n']:>8}{base['win_rate']:>7.1f}%"
              f"{base['gross']:>11.2f}{base['comm']:>10.2f}"
              f"{base['net']:>11.2f}")

        for k in ATR_MULTS:
            r = _run(bars, mult, comm,
                     trail_width_pct=base_trail, cooldown_bars=1,
                     atr_mult=k)
            key = f"atr × {k:.1f}"
            agg.setdefault(key, {"net": 0.0, "n": 0})
            agg[key]["net"] += r["net"]
            agg[key]["n"] += r["n"]
            print(f"{label:<5}{key:<20}"
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
