"""Sweep an intra-bar hard-stop multiplier on 30 d cached bars.

The live ``chart_signal`` strategy exits on bar-CLOSE trail breach,
which lets intra-bar dips slide and catches false reversals. An
optional hard-stop fires intra-bar when the bar's high/low (against
the position side) breaches ``HWM/LWM × (1 ± k × trail_width_pct)``
— a wider band than the trail itself, so normal wiggles still get
the close-only filter but a genuine $20-30 reversal against the
position kills it without waiting for the bar to close.

Sweeps ``hard_stop_mult ∈ {None, 2.0, 2.5, 3.0, 4.0}`` across the
four bots' cached 30 d bars and reports NET P&L. Same commissions
as ``run_30d_chart_signal.py``.

Pure read-only.
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

HARD_STOP_MULTS = [None, 2.0, 2.5, 3.0, 4.0]


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
    trades = sr_backtest.run_backtest_live(bars, **kwargs)
    n = len(trades)
    wins = sum(1 for t in trades if t.pnl > 0)
    gross = sum(t.pnl for t in trades)
    closed = [t for t in trades if t.exit_reason != "EOD"]
    n_sides = 2 * len(closed) + (n - len(closed))
    comm_total = n_sides * comm
    by_reason: dict[str, int] = {}
    for t in trades:
        if t.exit_reason:
            by_reason[t.exit_reason] = by_reason.get(t.exit_reason, 0) + 1
    return {
        "n": n,
        "wins": wins,
        "win_rate": (wins / n * 100) if n else 0.0,
        "gross": gross,
        "comm": comm_total,
        "net": gross - comm_total,
        "by_reason": by_reason,
    }


def main() -> int:
    print(f"{'sym':<5}{'config':<14}{'trades':>8}{'win%':>8}"
          f"{'gross$':>11}{'comm$':>10}{'NET$':>11}  reasons")
    print("-" * 90)

    agg: dict[str, dict] = {}
    for label, ib_sym, mult, base_trail, comm in SYMBOLS:
        bars = _load(ib_sym)
        if len(bars) < 100:
            print(f"{label}: too few bars — skip", file=sys.stderr)
            continue
        for hs in HARD_STOP_MULTS:
            r = _run(bars, mult, comm,
                     trail_width_pct=base_trail, cooldown_bars=1,
                     hard_stop_mult=hs)
            key = "baseline" if hs is None else f"hard × {hs:.1f}"
            agg.setdefault(key, {"net": 0.0, "n": 0, "wins": 0})
            agg[key]["net"] += r["net"]
            agg[key]["n"] += r["n"]
            agg[key]["wins"] += r["wins"]
            reasons = ",".join(f"{k}={v}" for k, v in r["by_reason"].items())
            print(f"{label:<5}{key:<14}"
                  f"{r['n']:>8}{r['win_rate']:>7.1f}%"
                  f"{r['gross']:>11.2f}{r['comm']:>10.2f}"
                  f"{r['net']:>11.2f}  {reasons}")
        print()

    print("=== Aggregate NET P&L by hard-stop multiplier (4 symbols, 30 d) ===")
    rows = sorted(agg.items(), key=lambda kv: kv[1]["net"], reverse=True)
    for name, v in rows:
        wr = (v["wins"] / v["n"] * 100) if v["n"] else 0.0
        print(f"  {name:<12} {v['n']:>6} trades  "
              f"win {wr:>5.1f}%   NET ${v['net']:>10.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
