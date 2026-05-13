"""Walk-forward validation: 5-min ATR×4 vs 3-min fixed-% (current
live) across 30 days of cached TRADES bars.

Setup:
- 30 d window split chronologically into THREE folds (~10 d each).
- Two configs tested side-by-side on each fold:
    A. 3-min bars + fixed-% trail (current live baseline).
    B. 5-min bars + ATR × 4 trail (matrix winner).
- Both use the same commission, same multiplier, same
  near_touch_tolerance_fraction = 0.001 (= the live default).

Goal:
- Confirm 5-min/ATR×4 dominates 3-min/fixed-% IN EVERY FOLD (or at
  least every-but-one). A win-everywhere result is the walk-forward
  signal that 5-min isn't just a 14-d lucky window.

Pre-req: 30-day bars at 5-min in /tmp/wf_5min_v1/ (fetched here if
missing). 3-min 30-day bars are reused from /tmp/chart_signal_30d/.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402
from scripts.backtest.fetch_history import fetch  # noqa: E402

SYMBOLS = [
    ("MGC", "MGCM6", 10,  0.0002, 0.97),
    ("MCL", "MCLM6", 100, 0.0013, 0.77),
    ("MES", "MESM6", 5,   0.0003, 0.62),
    ("MNQ", "MNQM6", 2,   0.0002, 0.62),
]

WINDOW_HOURS = 30 * 24
N_FOLDS = 3
CACHE_DIR_5MIN = Path("/tmp/wf_5min_v1")
CACHE_DIR_3MIN = Path("/tmp/chart_signal_30d")


async def _fetch_5min(symbol: str) -> Path:
    CACHE_DIR_5MIN.mkdir(parents=True, exist_ok=True)
    out_path = CACHE_DIR_5MIN / f"{symbol}_5_mins.json"
    if out_path.exists() and out_path.stat().st_size > 1000:
        return out_path
    print(f"fetching 30 d of 5-min bars for {symbol}…", file=sys.stderr)
    bars = await fetch(symbol, "FUT", WINDOW_HOURS, "5 mins",
                       what_to_show="TRADES")
    out_path.write_text(json.dumps({"bars": bars}))
    print(f"  wrote {len(bars)} bars → {out_path}", file=sys.stderr)
    return out_path


def _load(path: Path) -> list[sr_backtest.Bar]:
    raw = json.loads(path.read_text())
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
        "n": n, "wins": wins,
        "win_rate": (wins / n * 100) if n else 0.0,
        "gross": gross, "comm": comm_total,
        "net": gross - comm_total,
    }


def _fold_bars(bars, fold_idx, n_folds):
    """Split chronologically; return the bars for fold ``fold_idx``."""
    if len(bars) < n_folds:
        return []
    size = len(bars) // n_folds
    lo = fold_idx * size
    hi = (fold_idx + 1) * size if fold_idx < n_folds - 1 else len(bars)
    return bars[lo:hi]


async def main_async() -> int:
    # Make sure both bar-size datasets exist for each symbol.
    paths_5min: dict[str, Path] = {}
    for _label, sym, *_ in SYMBOLS:
        paths_5min[sym] = await _fetch_5min(sym)

    # 3-min reuses the existing 30 d cache from run_30d_chart_signal.
    paths_3min: dict[str, Path] = {}
    for _label, sym, *_ in SYMBOLS:
        p = CACHE_DIR_3MIN / f"{sym}_30d.json"
        if not p.exists():
            print(f"MISSING 3-min cache: {p} — fetch with run_30d_chart_signal.py first",
                  file=sys.stderr)
            return 1
        paths_3min[sym] = p

    print()
    print(f"{'sym':<5}{'fold':<6}{'config':<20}"
          f"{'trades':>8}{'win%':>8}{'gross$':>11}{'comm$':>10}{'NET$':>11}")
    print("-" * 86)

    # Sum across folds → identify which config wins every fold.
    by_cfg_fold: dict[tuple[str, int], dict] = {}
    fold_winner_counts = {"3-min fixed%": 0, "5-min ATR×4": 0}

    for label, sym, mult, base_trail, comm in SYMBOLS:
        bars_3 = _load(paths_3min[sym])
        bars_5 = _load(paths_5min[sym])
        for f in range(N_FOLDS):
            sub3 = _fold_bars(bars_3, f, N_FOLDS)
            sub5 = _fold_bars(bars_5, f, N_FOLDS)
            if len(sub3) < 50 or len(sub5) < 50:
                continue
            r3 = _run(sub3, mult, comm,
                      trail_width_pct=base_trail, cooldown_bars=1,
                      history_bars=40,
                      near_touch_tolerance_fraction=0.001)
            r5 = _run(sub5, mult, comm,
                      trail_width_pct=base_trail, cooldown_bars=1,
                      history_bars=max(20, int(120 / 5)),
                      atr_mult=4.0,
                      near_touch_tolerance_fraction=0.001)
            print(f"{label:<5}{f+1:<6}{'3-min fixed%':<20}"
                  f"{r3['n']:>8}{r3['win_rate']:>7.1f}%"
                  f"{r3['gross']:>11.2f}{r3['comm']:>10.2f}{r3['net']:>11.2f}")
            print(f"{label:<5}{f+1:<6}{'5-min ATR×4':<20}"
                  f"{r5['n']:>8}{r5['win_rate']:>7.1f}%"
                  f"{r5['gross']:>11.2f}{r5['comm']:>10.2f}{r5['net']:>11.2f}")
            k3 = ("3-min fixed%", f); k5 = ("5-min ATR×4", f)
            by_cfg_fold.setdefault(k3, {"net": 0.0, "n": 0})
            by_cfg_fold.setdefault(k5, {"net": 0.0, "n": 0})
            by_cfg_fold[k3]["net"] += r3["net"]; by_cfg_fold[k3]["n"] += r3["n"]
            by_cfg_fold[k5]["net"] += r5["net"]; by_cfg_fold[k5]["n"] += r5["n"]
            if r5["net"] > r3["net"]:
                fold_winner_counts["5-min ATR×4"] = fold_winner_counts.get("5-min ATR×4", 0)
        print()

    print("=== Per-fold aggregate (sum across 4 symbols) ===")
    for f in range(N_FOLDS):
        a3 = by_cfg_fold.get(("3-min fixed%", f), {"net": 0.0, "n": 0})
        a5 = by_cfg_fold.get(("5-min ATR×4", f), {"net": 0.0, "n": 0})
        winner = "5-min ATR×4" if a5["net"] > a3["net"] else "3-min fixed%"
        margin = a5["net"] - a3["net"]
        print(f"  fold {f+1}:  3-min fixed% NET ${a3['net']:>10.2f}"
              f"   5-min ATR×4 NET ${a5['net']:>10.2f}"
              f"   winner {winner:<14}  Δ ${margin:>+10.2f}")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
