"""1-year backtest: baseline (state as of 9 PM PT 5/13/2026) vs current.

Baseline filters (shipped + live as of 5/13 evening):
- near_touch_tolerance_fraction = 0.001 (5× TOUCH_TOLERANCE_FRACTION)
- shoulder_filter per-symbol (MGC/MES on, MNQ off)
- tight_triangle_block_distance = 2 (live default)

Current adds:
- min_target_filter = True   (NEW, ported into sr_backtest.py)
- counter_line_exit = bar-approx fallback via exit_on_counter_trend
  (when 1-min bars unavailable; tick-approx is a separate runner).

Reads /tmp/long_history/{SYMBOL}_{YYYYMM}_3min_trades.json.

Reports per-contract, per-symbol aggregate, win-rate dimensions,
and monthly P&L curve. MCL not in long_history yet — analysis covers
MGC, MES, MNQ.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from scripts.backtest import sr_backtest  # noqa: E402

HIST_DIR = Path("/tmp/long_history")
HIST_1MIN_DIR = Path("/tmp/long_history_1min")

# (label, multiplier, trail_pct, comm_per_side, shoulder_on)
SYMBOLS = [
    ("MGC", 10, 0.0002, 0.97, True),
    ("MES", 5, 0.0003, 0.62, True),
    ("MNQ", 2, 0.0002, 0.62, False),
]
NEAR_TOUCH_FRAC = 0.001
TRIANGLE_BLOCK = 2


def _load(path: Path) -> list[sr_backtest.Bar]:
    raw = json.loads(path.read_text())
    bar_list = raw.get("bars", raw)
    bar_list.sort(key=lambda b: b["ts"])
    return [sr_backtest.Bar(t=b["ts"], open=b["open"], high=b["high"],
                            low=b["low"], close=b["close"]) for b in bar_list]


def _run_one(bars, trail, shoulder, *, min_target=False,
             counter_line=False, bars1m=None):
    return sr_backtest.run_backtest_live(
        bars,
        trail_width_pct=trail,
        cooldown_bars=1,
        near_touch_tolerance_fraction=NEAR_TOUCH_FRAC,
        shoulder_filter=shoulder,
        tight_triangle_block_distance=TRIANGLE_BLOCK,
        min_target_filter=min_target,
        counter_line_exit=counter_line,
        counter_line_bars1m=bars1m,
    )


def _summarize(trades, comm):
    n = len(trades)
    if n == 0:
        return None
    wins = sum(1 for t in trades if t.pnl > 0)
    losses = sum(1 for t in trades if t.pnl < 0)
    flats = n - wins - losses
    gross = sum(t.pnl for t in trades)
    sides = sum(1 if t.exit_reason == "EOD" else 2 for t in trades)
    comm_total = sides * comm
    net = gross - comm_total
    win_pnls = [t.pnl for t in trades if t.pnl > 0]
    loss_pnls = [t.pnl for t in trades if t.pnl < 0]
    avg_win = sum(win_pnls) / len(win_pnls) if win_pnls else 0
    avg_loss = sum(loss_pnls) / len(loss_pnls) if loss_pnls else 0
    profit_factor = (sum(win_pnls) / -sum(loss_pnls)) if loss_pnls else float("inf")
    # Max drawdown on equity curve
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for tr in sorted(trades, key=lambda x: x.exit_t or x.entry_t):
        equity += tr.pnl - 2 * comm
        if equity > peak: peak = equity
        dd = peak - equity
        if dd > max_dd: max_dd = dd
    return {
        "n": n, "wins": wins, "losses": losses, "flats": flats,
        "gross": gross, "comm": comm_total, "net": net,
        "win_rate": 100 * wins / n,
        "avg_win": avg_win, "avg_loss": avg_loss,
        "profit_factor": profit_factor,
        "max_dd": max_dd,
    }


def _monthly_pnl(trades, comm):
    """Aggregate net P&L per calendar month."""
    out: dict[str, float] = defaultdict(float)
    for tr in trades:
        ts = tr.exit_t or tr.entry_t
        ym = ts[:7]
        sides = 1 if tr.exit_reason == "EOD" else 2
        out[ym] += tr.pnl - sides * comm
    return dict(sorted(out.items()))


def main():
    sym_summaries = {}
    sym_trades_baseline = {}
    sym_trades_current = {}
    print("Per-contract NET P&L "
          "(baseline | +min_target | +min_target+counter_line):")
    print(f"{'sym':<5}{'contract':<10}{'baseline':>12}{'+MT':>11}"
          f"{'+MT+CL':>11}{'Δ_cur':>10}{'n_base':>8}{'n_cur':>8}{'cl_fires':>9}")
    print("-" * 90)
    for label, mult, trail, comm, shoulder in SYMBOLS:
        files = sorted(HIST_DIR.glob(f"{label}_*_3min_*.json"))
        if not files:
            print(f"{label}: no history files in {HIST_DIR}", file=sys.stderr)
            continue
        sr_backtest.MULTIPLIER = float(mult)
        all_base = []
        all_cur = []
        for f in files:
            expiry = f.stem.split("_")[1]
            bars = _load(f)
            if len(bars) < 200:
                continue
            # 1-min bars cache for this contract — for counter_line
            # tick-approx. Skipped if no 1-min file (counter_line falls
            # back to disabled).
            bars1m = None
            p1m = HIST_1MIN_DIR / f"{label}_{expiry}_1min_trades.json"
            if p1m.exists():
                bars1m = _load(p1m)
            base = _run_one(bars, trail, shoulder)
            mt_only = _run_one(bars, trail, shoulder, min_target=True)
            cur = _run_one(
                bars, trail, shoulder, min_target=True,
                counter_line=(bars1m is not None), bars1m=bars1m,
            )
            base_net = sum(t.pnl for t in base) - 2 * comm * len(base)
            mt_net = sum(t.pnl for t in mt_only) - 2 * comm * len(mt_only)
            cur_net = sum(t.pnl for t in cur) - 2 * comm * len(cur)
            cl_fires = sum(1 for t in cur if t.exit_reason == "COUNTER_LINE")
            mark = "" if bars1m else " *no-1m"
            print(f"{label:<5}{expiry:<10}{base_net:>+12.0f}{mt_net:>+11.0f}"
                  f"{cur_net:>+11.0f}{cur_net - base_net:>+10.0f}"
                  f"{len(base):>8}{len(cur):>8}{cl_fires:>9}{mark}")
            all_base.extend(base)
            all_cur.extend(cur)
        sym_trades_baseline[label] = (all_base, comm)
        sym_trades_current[label] = (all_cur, comm)
        sym_summaries[label] = {
            "baseline": _summarize(all_base, comm),
            "current":  _summarize(all_cur, comm),
        }
    print()

    print("=== Per-symbol aggregate (1-yr) ===")
    print(f"{'sym':<5}{'config':<14}{'trades':>8}{'win%':>7}{'avg_win':>9}"
          f"{'avg_loss':>10}{'PF':>6}{'gross$':>11}{'NET$':>11}{'maxDD$':>10}")
    print("-" * 95)
    for label, *_ in SYMBOLS:
        s = sym_summaries.get(label)
        if not s:
            continue
        for cfg_name, key in (("baseline", "baseline"), ("+MT+CL", "current")):
            stat = s[key]
            if not stat:
                continue
            pf = stat["profit_factor"]
            pf_s = f"{pf:.2f}" if pf != float("inf") else "∞"
            print(f"{label:<5}{cfg_name:<14}{stat['n']:>8}{stat['win_rate']:>6.1f}%"
                  f"{stat['avg_win']:>+9.1f}{stat['avg_loss']:>+10.1f}"
                  f"{pf_s:>6}{stat['gross']:>+11.0f}"
                  f"{stat['net']:>+11.0f}{stat['max_dd']:>10.0f}")
        print()

    # Filter delta
    print("=== Filter Δ (current NET − baseline NET) ===")
    for label, *_ in SYMBOLS:
        s = sym_summaries.get(label)
        if not s or not s["baseline"] or not s["current"]:
            continue
        b = s["baseline"]["net"]
        c = s["current"]["net"]
        print(f"  {label:<5}  baseline ${b:>+10.0f}   "
              f"current ${c:>+10.0f}   Δ ${c - b:>+8.0f}  "
              f"({s['current']['n'] - s['baseline']['n']:+d} trades, "
              f"{s['current']['win_rate'] - s['baseline']['win_rate']:+.1f}pp win-rate)")
    print()

    # Monthly P&L curve, aggregated across all symbols
    print("=== Monthly NET P&L curve (aggregated MGC + MES + MNQ) ===")
    months = sorted(set().union(
        *[_monthly_pnl(t, c).keys() for t, c in sym_trades_baseline.values()],
        *[_monthly_pnl(t, c).keys() for t, c in sym_trades_current.values()],
    ))
    print(f"{'month':<10}{'baseline':>14}{'current':>14}{'Δ':>12}")
    print("-" * 50)
    cum_base = 0.0
    cum_cur = 0.0
    for ym in months:
        b_total = 0.0
        c_total = 0.0
        for label, *_ in SYMBOLS:
            t_b, comm = sym_trades_baseline.get(label, (None, 0))
            t_c, _ = sym_trades_current.get(label, (None, 0))
            if t_b:
                b_total += _monthly_pnl(t_b, comm).get(ym, 0)
            if t_c:
                c_total += _monthly_pnl(t_c, comm).get(ym, 0)
        cum_base += b_total
        cum_cur += c_total
        print(f"{ym:<10}{b_total:>+14.2f}{c_total:>+14.2f}{c_total-b_total:>+12.2f}")
    print("-" * 50)
    print(f"{'cumulative':<10}{cum_base:>+14.2f}{cum_cur:>+14.2f}{cum_cur-cum_base:>+12.2f}")


if __name__ == "__main__":
    main()
