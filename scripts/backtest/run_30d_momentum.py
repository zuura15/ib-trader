"""Sweep a Donchian-breakout momentum strategy on cached 30 d bars.

Mirrors ``run_30d_chart_signal.py`` so the NET P&L numbers are
directly comparable: same symbols, same commissions, same fill
model (entry/exit at the trigger bar's close).

Strategy: single position at a time; LONG when ``close > prior
N-bar high``, SHORT when ``close < prior N-bar low``. Exit on the
same trail-stop band the live bots use. Cooldown 1 bar after each
round-trip.

Sweeps:
  breakout_bars   ∈ {6, 10, 15, 20, 30}
  trail_width_pct → each symbol's live config value (matches the
                     chart_signal bot's per-symbol YAML).

Pure read-only — touches no live boxes, no Redis, no SQLite.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402

# (display_label, ib_local_symbol, multiplier, trail_width_pct,
#  commission_per_side_usd)
SYMBOLS = [
    ("MGC", "MGCM6", 10,  0.0002, 0.97),
    ("MCL", "MCLM6", 100, 0.0013, 0.77),
    ("MES", "MESM6", 5,   0.0003, 0.62),
    ("MNQ", "MNQM6", 2,   0.0002, 0.62),
]

BREAKOUT_BARS_SWEEP = [6, 10, 15, 20, 30]


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
    trades = sr_backtest.run_backtest_momentum(bars, **kwargs)
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    gross = sum(t.pnl for t in trades)
    closed = [t for t in trades if t.exit_reason != "EOD"]
    n_sides = 2 * len(closed) + (n - len(closed))
    comm_total = n_sides * comm
    return {
        "n": n,
        "longs": sum(1 for t in trades if t.side == "LONG"),
        "shorts": sum(1 for t in trades if t.side == "SHORT"),
        "wins": wins,
        "win_rate": (wins / n * 100) if n else 0.0,
        "gross": gross,
        "comm": comm_total,
        "net": gross - comm_total,
    }


def main() -> int:
    print(f"{'sym':<5}{'config':<14}{'trades':>8}{'L/S':>9}{'win%':>8}"
          f"{'gross$':>11}{'comm$':>10}{'NET$':>11}")
    print("-" * 76)

    agg: dict[str, dict] = {}
    for label, ib_sym, mult, trail, comm in SYMBOLS:
        bars = _load(ib_sym)
        if len(bars) < 100:
            print(f"{label}: too few bars — skip", file=sys.stderr)
            continue
        for n_brk in BREAKOUT_BARS_SWEEP:
            r = _run(bars, mult, comm,
                     breakout_bars=n_brk,
                     trail_width_pct=trail,
                     cooldown_bars=1)
            key = f"brk={n_brk}"
            agg.setdefault(key, {"net": 0.0, "n": 0, "wins": 0})
            agg[key]["net"] += r["net"]
            agg[key]["n"] += r["n"]
            agg[key]["wins"] += r["wins"]
            print(f"{label:<5}{key:<14}"
                  f"{r['n']:>8}"
                  f"{r['longs']:>4}/{r['shorts']:<3}"
                  f"{r['win_rate']:>7.1f}%"
                  f"{r['gross']:>11.2f}{r['comm']:>10.2f}"
                  f"{r['net']:>11.2f}")
        print()

    print("=== Aggregate NET P&L by breakout window (4 symbols, 30 d) ===")
    rows = sorted(agg.items(), key=lambda kv: kv[1]["net"], reverse=True)
    for name, v in rows:
        wr = (v["wins"] / v["n"] * 100) if v["n"] else 0.0
        print(f"  {name:<10} {v['n']:>6} trades  "
              f"win {wr:>5.1f}%   NET ${v['net']:>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
