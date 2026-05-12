"""Sweep filter configurations across the 30-day cached bars.

Reuses ``/tmp/chart_signal_30d/<SYM>_30d.json`` produced by
``run_30d_chart_signal.py`` — no IB hits, pure CPU.

Each (symbol × config) combo runs ``run_backtest_live`` and prints
a one-line row. The goal: find any single filter or combination
that flips the aggregate P&L positive (or at least surfaces a per-
symbol winner that's robust enough to deploy).

Filter knobs explored:
  - RSI thresholds (long_max / short_min)
  - UTC time-of-day window (RTH overlap, futures active)
  - Trail width × 1 / × 3 / × 6 (giveback room)
  - Cooldown 1 vs 3 bars (re-entry rate)

Output is a wide table; you can ``less -S`` it on terminal.
"""
from __future__ import annotations

import json
import sys
from itertools import product
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402

# (display_label, ib_symbol, multiplier, baseline_trail)
SYMBOLS = [
    ("MGC", "MGCM6", 10,  0.0002),
    ("MCL", "MCLM6", 100, 0.0013),
    ("MES", "MESM6", 5,   0.0003),
    ("MNQ", "MNQM6", 2,   0.0002),
]


def _load(label: str, ib_sym: str) -> list[sr_backtest.Bar]:
    p = Path(f"/tmp/chart_signal_30d/{ib_sym}_30d.json")
    if not p.exists():
        print(f"missing {p} — run run_30d_chart_signal.py first",
              file=sys.stderr)
        return []
    raw = json.loads(p.read_text())
    bar_list = raw.get("bars", raw) if isinstance(raw, dict) else raw
    bars = [sr_backtest.Bar(
        t=b["ts"], open=b["open"], high=b["high"],
        low=b["low"], close=b["close"],
    ) for b in bar_list]
    bars.sort(key=lambda b: b.t)
    return bars


def _run(bars: list[sr_backtest.Bar], multiplier: float, **kwargs) -> dict:
    sr_backtest.MULTIPLIER = float(multiplier)
    trades = sr_backtest.run_backtest_live(bars, **kwargs)
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    total = sum(t.pnl for t in trades)
    avg = total / n if n else 0.0
    return {
        "n": n,
        "wins": wins,
        "win_rate": (wins / n * 100) if n else 0.0,
        "pnl": total,
        "avg": avg,
    }


# Filter combos to try. Each entry is a (label, kwargs) pair.
# Trail multipliers applied per-symbol on top of the YAML baseline.
def _configs(base_trail: float) -> list[tuple[str, dict]]:
    return [
        ("baseline",            dict(trail_width_pct=base_trail, cooldown_bars=1)),
        ("rsi-50/50",           dict(trail_width_pct=base_trail, cooldown_bars=1,
                                      rsi_long_max=50, rsi_short_min=50)),
        ("rsi-40/60",           dict(trail_width_pct=base_trail, cooldown_bars=1,
                                      rsi_long_max=40, rsi_short_min=60)),
        ("rsi-30/70",           dict(trail_width_pct=base_trail, cooldown_bars=1,
                                      rsi_long_max=30, rsi_short_min=70)),
        ("rth-utc-13-20",       dict(trail_width_pct=base_trail, cooldown_bars=1,
                                      utc_hour_range=(13, 20))),
        ("rth + rsi 40/60",     dict(trail_width_pct=base_trail, cooldown_bars=1,
                                      utc_hour_range=(13, 20),
                                      rsi_long_max=40, rsi_short_min=60)),
        ("trail × 3",           dict(trail_width_pct=base_trail * 3, cooldown_bars=1)),
        ("trail × 6",           dict(trail_width_pct=base_trail * 6, cooldown_bars=1)),
        ("trail × 3 + rsi 40/60", dict(trail_width_pct=base_trail * 3, cooldown_bars=1,
                                         rsi_long_max=40, rsi_short_min=60)),
        ("trail × 3 + rth",     dict(trail_width_pct=base_trail * 3, cooldown_bars=1,
                                      utc_hour_range=(13, 20))),
        ("cooldown 3 bars",     dict(trail_width_pct=base_trail, cooldown_bars=3)),
        ("cooldown 5 bars",     dict(trail_width_pct=base_trail, cooldown_bars=5)),
    ]


def main() -> int:
    # Header.
    print(f"{'symbol':<6}{'config':<24}"
          f"{'trades':>8}{'wins':>6}{'win%':>7}"
          f"{'pnl$':>10}{'avg$':>8}")
    print("-" * 70)

    per_symbol_best: dict[str, tuple[str, dict]] = {}
    per_config_total: dict[str, float] = {}

    for label, ib_sym, mult, base_trail in SYMBOLS:
        bars = _load(label, ib_sym)
        if not bars:
            continue
        best_pnl = -float("inf")
        best_cfg = None
        for cfg_name, kwargs in _configs(base_trail):
            res = _run(bars, mult, **kwargs)
            print(f"{label:<6}{cfg_name:<24}"
                  f"{res['n']:>8}{res['wins']:>6}"
                  f"{res['win_rate']:>6.1f}%"
                  f"{res['pnl']:>10.2f}{res['avg']:>8.2f}")
            per_config_total[cfg_name] = (
                per_config_total.get(cfg_name, 0.0) + res["pnl"]
            )
            if res["pnl"] > best_pnl:
                best_pnl = res["pnl"]
                best_cfg = (cfg_name, res)
        if best_cfg:
            per_symbol_best[label] = best_cfg
        print()

    print("=== Per-symbol best ===")
    for sym, (cfg, res) in per_symbol_best.items():
        print(f"  {sym:<6}{cfg:<26}P&L ${res['pnl']:>9.2f}  "
              f"({res['n']} trades, {res['win_rate']:.1f}% win)")

    print()
    print("=== Aggregate P&L by config (sum across 4 symbols) ===")
    for cfg, total in sorted(per_config_total.items(),
                              key=lambda x: -x[1]):
        flag = "  ✓" if total > 0 else ""
        print(f"  {cfg:<26}${total:>10.2f}{flag}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
