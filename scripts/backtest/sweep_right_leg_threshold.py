"""Threshold sweep for the right-leg shallow-pivot entry filter.

For each candidate threshold T, drop any entry where
``right_leg / prior_bar_change < T``. Report:
  - n_filtered (entries killed)
  - sh_net     (NET of the killed entries — negative ⇒ filter helps)
  - all_net    (baseline total)
  - filt_net   (all_net − sh_net = NET with filter on)

Reads 30d TRADES cache. Read-only.
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

THRESHOLDS = [0.0, 0.10, 0.20, 0.25, 0.33, 0.50, 0.75, 1.00, 1.50]


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


def _right_over_prior(bars, entry_idx, side):
    p = entry_idx - 1
    if p - 2 < 0 or p + 1 > len(bars) - 1:
        return None
    c = [b.close for b in bars]
    if side == "LONG":
        right = c[p + 1] - c[p]
    else:
        right = c[p] - c[p + 1]
    prior = abs(c[p - 1] - c[p - 2])
    if prior <= 0:
        return None
    return right / prior


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

    print(f"{'threshold':<12}{'n_total':>10}{'n_killed':>11}"
          f"{'killed_NET$':>14}{'kept_NET$':>13}{'edge_Δ$':>11}")
    print("-" * 71)

    n_total = sum(len(c[1]) for c in cache.values())
    base_net = 0.0
    for label, (_, trades, comm) in cache.items():
        for t in trades:
            base_net += _trade_net(t, comm)
    print(f"{'baseline':<12}{n_total:>10}{0:>11}{0.00:>14.2f}"
          f"{base_net:>13.2f}{0.00:>11.2f}")

    for T in THRESHOLDS:
        n_killed = 0
        killed_net = 0.0
        kept_net = 0.0
        for label, (bars, trades, comm) in cache.items():
            for t in trades:
                ratio = _right_over_prior(bars, t.entry_idx, t.side)
                net = _trade_net(t, comm)
                if ratio is None:
                    kept_net += net   # can't classify → keep
                    continue
                if ratio < T:
                    n_killed += 1
                    killed_net += net
                else:
                    kept_net += net
        edge = kept_net - base_net
        print(f"{T:<12.2f}{n_total:>10}{n_killed:>11}"
              f"{killed_net:>14.2f}{kept_net:>13.2f}{edge:>+11.2f}")

    print()
    print("Reading:")
    print("  killed_NET$ negative ⇒ filter discards a losing bucket "
          "(good).")
    print("  edge_Δ$ = kept_NET − baseline; bigger is better.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
