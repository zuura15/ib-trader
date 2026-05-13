"""Volume-rank correlation for shallow-right-leg pivots.

Hypothesis under test (operator question): does the volume rank of
the pivot bar (within the trailing 24-hour distribution) predict
the performance of a shallow-pivot trade?

For each baseline trade we compute:
  - right_leg / prior_bar_change           (the "shallow" metric)
  - pivot-bar volume percentile vs the trailing 24h (480 × 3-min)
Then bucket shallow trades (right-leg < 0.25) by volume quartile
and report win-rate + net P&L per quartile, per symbol + aggregate.

Uses the TRADES-feed 30d cache in /tmp/chart_signal_30d_trades/ so
volume is real (the BID_ASK cache reports -1 for every bar).
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
    ("MGC", "MGCM6", 10,  0.0002, 0.97),
    ("MCL", "MCLM6", 100, 0.0013, 0.77),
    ("MES", "MESM6", 5,   0.0003, 0.62),
    ("MNQ", "MNQM6", 2,   0.0002, 0.62),
]

CACHE_DIR = Path("/tmp/chart_signal_30d_trades")
RATIO_THRESHOLD = 0.25
TRAILING_HOURS = 24            # rolling volume window
BAR_SECONDS = 180              # 3-min bars
TRAILING_BARS = TRAILING_HOURS * 3600 // BAR_SECONDS  # = 480


def _load(ib_sym: str) -> tuple[list[sr_backtest.Bar], list[float]]:
    p = CACHE_DIR / f"{ib_sym}_30d.json"
    raw = json.loads(p.read_text())
    bar_list = raw.get("bars", raw) if isinstance(raw, dict) else raw
    bar_list.sort(key=lambda b: b["ts"])
    bars = [sr_backtest.Bar(
        t=b["ts"], open=b["open"], high=b["high"],
        low=b["low"], close=b["close"],
    ) for b in bar_list]
    volumes = [float(b.get("volume") or 0.0) for b in bar_list]
    return bars, volumes


def _trade_net(t, comm):
    sides = 1 if t.exit_reason == "EOD" else 2
    pnl = getattr(t, "_frozen_pnl", t.pnl)
    return pnl - sides * comm


def _legs_and_prior(bars, entry_idx, side):
    p = entry_idx - 1
    if p - 2 < 0 or p + 1 > len(bars) - 1:
        return None, None, None
    c = [b.close for b in bars]
    if side == "LONG":
        left  = c[p - 1] - c[p]
        right = c[p + 1] - c[p]
    else:
        left  = c[p] - c[p - 1]
        right = c[p] - c[p + 1]
    prior = abs(c[p - 1] - c[p - 2])
    return left, right, prior


def _volume_percentile(volumes, pivot_idx) -> float | None:
    """Rank ``volumes[pivot_idx]`` within the trailing 24h window
    (excluding the pivot bar itself). Returns 0.0..1.0 or None when
    there isn't enough history."""
    lo = max(0, pivot_idx - TRAILING_BARS)
    window = volumes[lo:pivot_idx]
    if len(window) < 30:           # too thin to rank meaningfully
        return None
    v = volumes[pivot_idx]
    if v <= 0:
        return None
    rank = sum(1 for w in window if w < v) / len(window)
    return rank


def main() -> int:
    if not CACHE_DIR.exists():
        print(f"missing cache {CACHE_DIR}", file=sys.stderr)
        return 1

    # Run baseline once per symbol; freeze pnl so we can re-iterate.
    cache: dict[str, tuple[list, list, list, float]] = {}
    for label, ib_sym, mult, trail, comm in SYMBOLS:
        bars, volumes = _load(ib_sym)
        if len(bars) < 100:
            print(f"{label}: too few bars", file=sys.stderr)
            continue
        sr_backtest.MULTIPLIER = float(mult)
        trades = sr_backtest.run_backtest_live(
            bars, trail_width_pct=trail, cooldown_bars=1,
        )
        for t in trades:
            t._frozen_pnl = t.pnl  # type: ignore[attr-defined]
        cache[label] = (bars, volumes, trades, comm)

    print(f"\n=== Shallow right-leg trades (ratio < {RATIO_THRESHOLD}), "
          f"bucketed by pivot-bar volume vs trailing {TRAILING_HOURS}h ===\n")
    print(f"{'sym':<5}{'q':<7}{'n':>6}{'win':>5}{'win%':>7}"
          f"{'gross$':>10}{'comm$':>9}{'NET$':>10}")
    print("-" * 60)

    # Aggregate per quartile across symbols.
    agg: dict[str, dict] = {}
    for label, ib_sym, mult, trail, comm in SYMBOLS:
        if label not in cache:
            continue
        bars, volumes, trades, comm = cache[label]
        # Bucket: percentile rank 0-25 / 25-50 / 50-75 / 75-100.
        buckets: dict[str, list] = {
            "Q1 (lo)": [], "Q2": [], "Q3": [], "Q4 (hi)": [], "n/a": [],
        }
        for t in trades:
            left, right, prior = _legs_and_prior(bars, t.entry_idx, t.side)
            if right is None or prior is None or prior <= 0:
                continue
            if right / prior >= RATIO_THRESHOLD:
                continue   # only shallow trades
            pct = _volume_percentile(volumes, t.entry_idx - 1)
            if pct is None:
                buckets["n/a"].append(t)
            elif pct < 0.25:
                buckets["Q1 (lo)"].append(t)
            elif pct < 0.50:
                buckets["Q2"].append(t)
            elif pct < 0.75:
                buckets["Q3"].append(t)
            else:
                buckets["Q4 (hi)"].append(t)

        for name in ("Q1 (lo)", "Q2", "Q3", "Q4 (hi)"):
            ts = buckets[name]
            n = len(ts)
            wins = sum(1 for t in ts
                       if getattr(t, "_frozen_pnl", t.pnl) > 0)
            gross = sum(getattr(t, "_frozen_pnl", t.pnl) for t in ts)
            sides_paid = sum(
                1 if t.exit_reason == "EOD" else 2 for t in ts
            )
            comm_total = sides_paid * comm
            net = gross - comm_total
            wr = (100 * wins / n) if n else 0.0
            print(f"{label:<5}{name:<7}{n:>6}{wins:>5}{wr:>6.1f}%"
                  f"{gross:>10.2f}{comm_total:>9.2f}{net:>10.2f}")
            a = agg.setdefault(name, {"n": 0, "wins": 0, "net": 0.0})
            a["n"] += n; a["wins"] += wins; a["net"] += net
        print()

    print("=== Aggregate across 4 symbols (shallow right-leg only) ===")
    print(f"{'q':<10}{'n':>6}{'win':>5}{'win%':>7}{'NET$':>11}")
    for name in ("Q1 (lo)", "Q2", "Q3", "Q4 (hi)"):
        a = agg.get(name, {"n": 0, "wins": 0, "net": 0.0})
        wr = (100 * a["wins"] / a["n"]) if a["n"] else 0.0
        print(f"{name:<10}{a['n']:>6}{a['wins']:>5}{wr:>6.1f}%"
              f"{a['net']:>11.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
