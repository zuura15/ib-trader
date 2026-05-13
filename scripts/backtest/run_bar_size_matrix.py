"""Sweep ``chart_signal`` across (symbol × bar_size × trail-mode)
to see whether 3-min is actually the most profitable bar size.

Matrix:
  symbol  ∈ {MGC, MCL, MES, MNQ}
  bar_size ∈ {1, 2, 3, 4, 5} min
  trail    ∈ {fixed % (live config), ATR × 4 (per the prior ATR sweep)}

Data: 14 days of TRADES bars per (symbol, bar_size). Smaller bar
sizes scale linearly — 1-min × 14d ≈ 20k bars/symbol vs 3-min ×
30d ≈ 14k. Plenty of data for the strict-pivot + 3-touch
heuristics.

Per-symbol tunables (history_bars, break_stale_bars) are scaled by
bar_size so the time-window reach is bar-size-independent:
  history_bars      = 2 h / bar_minutes  (e.g. 120 for 1-min, 40 for 3-min)
  break_stale_bars  = scaled inside sr_fan's defaults (kept at 20
                       bars; equivalent to 20 min on 1-min, 100 min
                       on 5-min — same in-window tolerance regardless).

Pure read-only on the daemon; uses fetch_history with TRADES so the
data matches what live ``/engine/history`` returns. Caches each
(symbol, bar_size) in ``/tmp/bar_matrix_v2/``.
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

# (label, ib_symbol, multiplier, trail_pct, comm_per_side)
SYMBOLS = [
    ("MGC", "MGCM6", 10,  0.0002, 0.97),
    ("MCL", "MCLM6", 100, 0.0013, 0.77),
    ("MES", "MESM6", 5,   0.0003, 0.62),
    ("MNQ", "MNQM6", 2,   0.0002, 0.62),
]

BAR_SIZES = [
    ("1 min",  1),
    ("2 mins", 2),
    ("3 mins", 3),
    ("4 mins", 4),
    ("5 mins", 5),
]

HORIZON_HOURS = 14 * 24  # 14 days
CACHE_DIR = Path("/tmp/bar_matrix_v2")


async def _fetch_one(symbol: str, bar_size: str, hours: int) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{symbol}_{bar_size.replace(' ', '_')}.json"
    out_path = CACHE_DIR / fname
    if out_path.exists() and out_path.stat().st_size > 1000:
        print(f"  cached {out_path}", file=sys.stderr)
        return out_path
    print(f"  fetching {hours} h of {bar_size} bars for {symbol}…",
          file=sys.stderr)
    bars = await fetch(symbol, "FUT", hours, bar_size, what_to_show="TRADES")
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


def _run(bars, mult, comm, trail_pct, atr_mult, history_bars) -> dict:
    sr_backtest.MULTIPLIER = float(mult)
    trades = sr_backtest.run_backtest_live(
        bars,
        trail_width_pct=trail_pct,
        cooldown_bars=1,
        history_bars=history_bars,
        atr_mult=atr_mult,
        near_touch_tolerance_fraction=0.001,
    )
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


async def main_async() -> int:
    # Fetch everything first (sequential to be IB-rate-limit-safe).
    paths: dict[tuple[str, str], Path] = {}
    for _label, sym, *_ in SYMBOLS:
        print(f"--- {sym} ---", file=sys.stderr)
        for bar_label, _ in BAR_SIZES:
            p = await _fetch_one(sym, bar_label, HORIZON_HOURS)
            paths[(sym, bar_label)] = p

    # Backtest matrix.
    print(f"\n{'sym':<5}{'bar':<7}{'trail':<10}"
          f"{'trades':>8}{'win%':>7}{'gross$':>11}{'comm$':>10}{'NET$':>11}")
    print("-" * 80)

    # Aggregates by (bar, trail) across symbols.
    agg: dict[tuple[str, str], dict] = {}
    # Also per-symbol best across configs.
    per_sym_best: dict[str, dict] = {}

    for label, ib_sym, mult, trail_pct, comm in SYMBOLS:
        for bar_label, bar_minutes in BAR_SIZES:
            bars = _load(paths[(ib_sym, bar_label)])
            if len(bars) < 100:
                print(f"{label:<5}{bar_label:<7}  too few bars — skip",
                      file=sys.stderr)
                continue
            history_bars = max(20, int(120 / bar_minutes))
            for trail_kind, trail_kwargs in (
                ("fixed%", {"trail_pct": trail_pct, "atr_mult": None}),
                ("ATR×4",  {"trail_pct": trail_pct, "atr_mult": 4.0}),
            ):
                r = _run(bars, mult, comm,
                         history_bars=history_bars,
                         **trail_kwargs)
                key = (bar_label, trail_kind)
                a = agg.setdefault(key, {"net": 0.0, "n": 0, "wins": 0})
                a["net"] += r["net"]; a["n"] += r["n"]; a["wins"] += r["wins"]
                # Track per-symbol best.
                ps_key = (label,)
                ps = per_sym_best.setdefault(ps_key,
                                              {"net": -1e18, "cfg": "—"})
                if r["net"] > ps["net"]:
                    ps["net"] = r["net"]
                    ps["cfg"] = f"{bar_label} / {trail_kind}"
                print(f"{label:<5}{bar_label:<7}{trail_kind:<10}"
                      f"{r['n']:>8}{r['win_rate']:>6.1f}%"
                      f"{r['gross']:>11.2f}{r['comm']:>10.2f}"
                      f"{r['net']:>11.2f}")
        print()

    print("=== Aggregate NET by (bar, trail), 4 symbols × 14 d ===")
    rows = sorted(agg.items(), key=lambda kv: kv[1]["net"], reverse=True)
    for (bar, trail), v in rows:
        wr = (v["wins"] / v["n"] * 100) if v["n"] else 0.0
        print(f"  {bar:<7}{trail:<8}  {v['n']:>6} trades  "
              f"win {wr:>5.1f}%   NET ${v['net']:>10.2f}")

    print("\n=== Per-symbol best config ===")
    for (sym,), v in per_sym_best.items():
        print(f"  {sym}: {v['cfg']}   NET ${v['net']:.2f}")
    return 0


def main() -> int:
    return asyncio.run(main_async())


if __name__ == "__main__":
    sys.exit(main())
