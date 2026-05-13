"""Backtest current chart_signal config (with the shipped shallow-
right-leg filter at T=0.5) across the full 15-month history.

Per-contract: run the backtest on that contract's data alone (chart_
signal is intraday-bounded; running per-contract avoids artificial
gaps at rollovers). Aggregate across contracts → per-symbol totals.

Reads /tmp/long_history/{SYMBOL}_{YYYYMM}_3min_trades.json.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402

import os  # noqa: E402

# Default = BID_ASK cache (matches current limit-mid execution); set
# IB_LONG_HIST_DIR to override for TRADES (or any future variant).
HIST_DIR = Path(os.environ.get(
    "IB_LONG_HIST_DIR", "/tmp/long_history_bidask",
))

# Same per-symbol config as run_30d_chart_signal.py + the live
# strategy defaults from chart_signal.yaml.
SYMBOLS = [
    # (label, root, multiplier, base_trail_pct, comm_per_side)
    ("MGC", 10,  0.0002, 0.97),
    ("MES",  5,  0.0003, 0.62),
    ("MNQ",  2,  0.0002, 0.62),
]

NEAR_TOUCH_FRAC = 0.001          # 5 × TOUCH_TOLERANCE_FRACTION
# Per the 30d study, MGC/MES benefit from the bad-shoulder filter
# but MNQ inverts the rule. Per-symbol on/off matches the per-bot
# YAML overrides (entry_shoulder_filter_enabled on chart-bot-4).
SHOULDER_BY_SYMBOL = {"MGC": True, "MES": True, "MNQ": False}


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
    comm_total = sides * comm
    return n, wins, gross, comm_total, gross - comm_total


def main() -> int:
    print(f"{'sym':<5}{'contract':<12}{'config':<14}{'trades':>8}"
          f"{'win%':>7}{'gross$':>11}{'comm$':>10}{'NET$':>11}")
    print("-" * 78)

    sym_totals: dict[str, dict] = defaultdict(
        lambda: {"baseline": {"n": 0, "w": 0, "g": 0.0, "c": 0.0, "net": 0.0},
                 "filtered": {"n": 0, "w": 0, "g": 0.0, "c": 0.0, "net": 0.0}},
    )

    print(f"using history dir: {HIST_DIR}", flush=True)
    for label, mult, trail, comm in SYMBOLS:
        # Match either *_3min_trades.json or *_3min_bidask.json.
        files = sorted(HIST_DIR.glob(f"{label}_*_3min_*.json"))
        if not files:
            print(f"{label}: no history files", file=sys.stderr)
            continue
        sr_backtest.MULTIPLIER = float(mult)
        for f in files:
            expiry = f.stem.split("_")[1]
            bars = _load(f)
            if len(bars) < 200:
                continue

            # Baseline: live tolerance, no shallow filter.
            base = sr_backtest.run_backtest_live(
                bars,
                trail_width_pct=trail,
                cooldown_bars=1,
                near_touch_tolerance_fraction=NEAR_TOUCH_FRAC,
            )
            n, w, g, c, net = _stats(base, comm)
            wr = 100 * w / n if n else 0.0
            print(f"{label:<5}{expiry:<12}{'baseline':<14}"
                  f"{n:>8}{wr:>6.1f}%{g:>11.2f}{c:>10.2f}{net:>11.2f}")
            t = sym_totals[label]["baseline"]
            t["n"] += n; t["w"] += w; t["g"] += g
            t["c"] += c; t["net"] += net

            # Triangle-rejection exit: only fires when in a converging
            # triangle AND wick tags the adjacent side AND close fails
            # to break through. min_touches=2 (any tentative line).
            tri = sr_backtest.run_backtest_live(
                bars,
                trail_width_pct=trail,
                cooldown_bars=1,
                near_touch_tolerance_fraction=NEAR_TOUCH_FRAC,
                exit_on_triangle_rejection=True,
                triangle_min_touches=3,
            )
            n, w, g, c, net = _stats(tri, comm)
            wr = 100 * w / n if n else 0.0
            print(f"{label:<5}{expiry:<12}{'+ tri (3t)':<14}"
                  f"{n:>8}{wr:>6.1f}%{g:>11.2f}{c:>10.2f}{net:>11.2f}")
            t = sym_totals[label]["filtered"]
            t["n"] += n; t["w"] += w; t["g"] += g
            t["c"] += c; t["net"] += net
        print()

    print("=== Per-symbol aggregate across all contracts ===")
    print(f"{'sym':<5}{'config':<14}{'trades':>8}{'win%':>7}"
          f"{'gross$':>11}{'comm$':>10}{'NET$':>11}{'$/trade':>10}")
    print("-" * 70)
    for label, *_ in SYMBOLS:
        for kind in ("baseline", "filtered"):
            t = sym_totals[label][kind]
            n = t["n"]
            if n == 0:
                continue
            wr = 100 * t["w"] / n
            per = t["net"] / n
            cfg = "baseline" if kind == "baseline" else "+ triangle (2t)"
            print(f"{label:<5}{cfg:<14}{n:>8}{wr:>6.1f}%"
                  f"{t['g']:>11.2f}{t['c']:>10.2f}{t['net']:>11.2f}"
                  f"{per:>+10.3f}")
        print()

    # Filter delta per symbol.
    print("=== Filter Δ (filtered NET − baseline NET) ===")
    for label, *_ in SYMBOLS:
        b = sym_totals[label]["baseline"]["net"]
        f = sym_totals[label]["filtered"]["net"]
        print(f"  {label:<5}  baseline ${b:>+10.2f}   "
              f"filtered ${f:>+10.2f}   Δ ${f - b:>+8.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
