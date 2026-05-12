"""Walk-forward validation of the filter sweep.

Pipeline:
  1. Load each symbol's cached 30d bars.
  2. Split chronologically into IN-SAMPLE (first half) and
     OUT-OF-SAMPLE (second half).
  3. Sweep configs on IN-SAMPLE only. Pick the highest-P&L config.
  4. Run that ONE config on OUT-OF-SAMPLE and report.
  5. If OOS P&L tracks IS P&L → robust. If OOS craters → curve-fit.

Same config set as ``sweep_30d.py``. Pure CPU, no IB hits.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402

SYMBOLS = [
    ("MGC", "MGCM6", 10,  0.0002),
    ("MCL", "MCLM6", 100, 0.0013),
    ("MES", "MESM6", 5,   0.0003),
    ("MNQ", "MNQM6", 2,   0.0002),
]


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


def _run(bars: list[sr_backtest.Bar], multiplier: float, **kwargs) -> dict:
    sr_backtest.MULTIPLIER = float(multiplier)
    trades = sr_backtest.run_backtest_live(bars, **kwargs)
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    return {
        "n": n,
        "wins": wins,
        "win_rate": (wins / n * 100) if n else 0.0,
        "pnl": sum(t.pnl for t in trades),
    }


def _configs(base_trail: float) -> list[tuple[str, dict]]:
    return [
        ("baseline",             dict(trail_width_pct=base_trail, cooldown_bars=1)),
        ("rsi-50/50",            dict(trail_width_pct=base_trail, cooldown_bars=1,
                                       rsi_long_max=50, rsi_short_min=50)),
        ("rsi-40/60",            dict(trail_width_pct=base_trail, cooldown_bars=1,
                                       rsi_long_max=40, rsi_short_min=60)),
        ("rth (utc 13-20)",      dict(trail_width_pct=base_trail, cooldown_bars=1,
                                       utc_hour_range=(13, 20))),
        ("rth + rsi 40/60",      dict(trail_width_pct=base_trail, cooldown_bars=1,
                                       utc_hour_range=(13, 20),
                                       rsi_long_max=40, rsi_short_min=60)),
        ("trail × 3",            dict(trail_width_pct=base_trail * 3, cooldown_bars=1)),
        ("trail × 6",            dict(trail_width_pct=base_trail * 6, cooldown_bars=1)),
        ("trail × 3 + rsi 40/60", dict(trail_width_pct=base_trail * 3, cooldown_bars=1,
                                         rsi_long_max=40, rsi_short_min=60)),
        ("trail × 3 + rth",      dict(trail_width_pct=base_trail * 3, cooldown_bars=1,
                                       utc_hour_range=(13, 20))),
        ("cooldown 3 bars",      dict(trail_width_pct=base_trail, cooldown_bars=3)),
        ("cooldown 5 bars",      dict(trail_width_pct=base_trail, cooldown_bars=5)),
    ]


def main() -> int:
    print(f"{'sym':<5}{'config':<26}"
          f"{'IS trades':>10}{'IS win%':>9}{'IS pnl':>10}"
          f"{'OOS trades':>11}{'OOS win%':>10}{'OOS pnl':>10}"
          f"{'verdict':>12}")
    print("-" * 100)

    is_total = 0.0
    oos_total = 0.0
    is_baseline_total = 0.0
    oos_baseline_total = 0.0

    for label, ib_sym, mult, base_trail in SYMBOLS:
        bars = _load(ib_sym)
        if len(bars) < 100:
            continue
        mid = len(bars) // 2
        in_sample = bars[:mid]
        oos = bars[mid:]
        cfgs = _configs(base_trail)

        # Always run baseline on both halves as a comparator.
        is_base = _run(in_sample, mult, **cfgs[0][1])
        oos_base = _run(oos, mult, **cfgs[0][1])
        is_baseline_total += is_base["pnl"]
        oos_baseline_total += oos_base["pnl"]

        # Fit on IS — pick highest P&L (skip configs with < 10 trades
        # since they're noise).
        best_name = None
        best_pnl = -float("inf")
        best_kwargs = None
        for cfg_name, kwargs in cfgs:
            res = _run(in_sample, mult, **kwargs)
            if res["n"] < 10:
                continue
            if res["pnl"] > best_pnl:
                best_pnl = res["pnl"]
                best_name = cfg_name
                best_kwargs = kwargs

        if best_kwargs is None:
            print(f"{label:<5}{'<no IS configs>':<26}")
            continue

        # Re-run IS for the chosen config (we already have it; recompute for clarity).
        is_best = _run(in_sample, mult, **best_kwargs)
        oos_best = _run(oos, mult, **best_kwargs)

        is_total += is_best["pnl"]
        oos_total += oos_best["pnl"]

        # Verdict: did the OOS P&L track the same sign as IS?
        if oos_best["pnl"] > 0:
            verdict = "ROBUST"
        elif is_best["pnl"] > 0 and oos_best["pnl"] < 0:
            verdict = "OVERFIT"
        else:
            verdict = "WEAK"
        print(f"{label:<5}{best_name:<26}"
              f"{is_best['n']:>10}{is_best['win_rate']:>8.1f}%"
              f"{is_best['pnl']:>10.2f}"
              f"{oos_best['n']:>11}{oos_best['win_rate']:>9.1f}%"
              f"{oos_best['pnl']:>10.2f}"
              f"{verdict:>12}")
        print(f"{label:<5}{'baseline (comparator)':<26}"
              f"{is_base['n']:>10}{is_base['win_rate']:>8.1f}%"
              f"{is_base['pnl']:>10.2f}"
              f"{oos_base['n']:>11}{oos_base['win_rate']:>9.1f}%"
              f"{oos_base['pnl']:>10.2f}")
        print()

    print("=== Aggregate ===")
    print(f"  IS  (fit-window) total P&L,  per-symbol best: ${is_total:>10.2f}")
    print(f"  OOS (held-out)   total P&L,  per-symbol best: ${oos_total:>10.2f}")
    print(f"  IS  baseline                              :   ${is_baseline_total:>10.2f}")
    print(f"  OOS baseline                              :   ${oos_baseline_total:>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
