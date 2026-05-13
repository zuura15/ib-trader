"""Sweep ASYMMETRIC entry-only pivot-strength filter.

Rule under test: reject the new-bar entry trigger when the just-
confirmed pivot's height (smaller adjacent leg) is below threshold,
but keep ALL pivots in line construction so the *exit* side (line
breach + trailing dip) is unchanged. This is different from the
earlier ``run_30d_pivot_strength.py`` sweep, which propagated
``min_pivot_strength`` into ``find_pivot_lows/_highs`` and therefore
suppressed lines themselves — net loss every threshold.

Thresholds are expressed as a fraction of average price so they
auto-scale across MGC/MCL/MES/MNQ. e.g. 0.0001 ≈ $0.47 on MGC,
$0.58 on MES, $0.007 on MCL, $2.20 on MNQ.

Compares the live baseline (no filter) against several heights.
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

# Fractions of average price for the entry-pivot height floor.
# Smaller leg < height_fraction × avg_price ⇒ entry suppressed.
HEIGHT_FRACTIONS = [
    0.00005, 0.0001, 0.00015, 0.0002, 0.0003, 0.0005, 0.001, 0.0015,
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


def _avg_price(bars) -> float:
    if not bars:
        return 0.0
    return sum(b.close for b in bars) / len(bars)


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
    print(f"{'sym':<5}{'config':<28}{'trades':>8}{'win%':>8}"
          f"{'gross$':>11}{'comm$':>10}{'NET$':>11}")
    print("-" * 81)

    agg: dict[str, dict] = {}
    per_symbol_best: dict[str, tuple[str, float]] = {}

    for label, ib_sym, mult, base_trail, comm in SYMBOLS:
        bars = _load(ib_sym)
        if len(bars) < 100:
            print(f"{label}: too few bars — skip", file=sys.stderr)
            continue
        avg = _avg_price(bars)

        # Baseline = no entry-pivot filter.
        base = _run(bars, mult, comm,
                    trail_width_pct=base_trail, cooldown_bars=1,
                    entry_min_pivot_height=0.0)
        agg.setdefault("baseline (no filter)", {"net": 0.0, "n": 0})
        agg["baseline (no filter)"]["net"] += base["net"]
        agg["baseline (no filter)"]["n"] += base["n"]
        per_symbol_best[label] = ("baseline (no filter)", base["net"])
        print(f"{label:<5}{'baseline (no filter)':<28}"
              f"{base['n']:>8}{base['win_rate']:>7.1f}%"
              f"{base['gross']:>11.2f}{base['comm']:>10.2f}"
              f"{base['net']:>11.2f}")

        for frac in HEIGHT_FRACTIONS:
            height = frac * avg
            r = _run(bars, mult, comm,
                     trail_width_pct=base_trail, cooldown_bars=1,
                     entry_min_pivot_height=height)
            key = f"h≥{frac*1e4:.2f}bp (${height:.3f})"
            agg_key = f"h≥{frac*1e4:.2f}bp"
            agg.setdefault(agg_key, {"net": 0.0, "n": 0})
            agg[agg_key]["net"] += r["net"]
            agg[agg_key]["n"] += r["n"]
            if r["net"] > per_symbol_best[label][1]:
                per_symbol_best[label] = (key, r["net"])
            print(f"{label:<5}{key:<28}"
                  f"{r['n']:>8}{r['win_rate']:>7.1f}%"
                  f"{r['gross']:>11.2f}{r['comm']:>10.2f}"
                  f"{r['net']:>11.2f}")
        print()

    print("=== Aggregate NET P&L by config (across 4 symbols) ===")
    rows = sorted(agg.items(), key=lambda kv: kv[1]["net"], reverse=True)
    for name, v in rows:
        print(f"  {name:<28}{v['n']:>6} trades   NET ${v['net']:>10.2f}")

    print()
    print("=== Per-symbol best ===")
    for sym, (cfg, net) in per_symbol_best.items():
        print(f"  {sym:<5}{cfg:<32}NET ${net:>10.2f}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
