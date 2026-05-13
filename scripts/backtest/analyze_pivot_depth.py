"""Analytical: for each baseline trade in the 30d backtest, compute
the just-confirmed pivot's depth vs the immediate prior bar change.

Definitions (pivot at idx p = entry_idx - 1):
  depth          = price drop into the pivot
                   LONG:  closes[p-1] - closes[p]   (low; positive)
                   SHORT: closes[p]   - closes[p-1] (high; positive)
  prior_change   = |closes[p-1] - closes[p-2]|   (immediate bar before)
  ratio          = depth / prior_change

Report counts of trades with ratio < 0.25 (the "shallow pivot vs
prior momentum" bucket the operator asked about), split by
win/loss/total, per symbol + aggregate. Also shows expected NET if
we had filtered them out.

Read-only against the cached 30d TRADES bars in
/tmp/chart_signal_30d/. Does not change any live config.
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

RATIO_THRESHOLD = 0.25

# Local-time window filter applied to every trade's entry timestamp.
# ``None`` disables filtering. Window is inclusive on both ends.
# PDT is in effect right now (May), so 9 PM-11:59 PM PST per the
# operator's request maps to UTC 04:00-06:59 the following day.
from datetime import datetime, timezone, timedelta  # noqa: E402

PDT_OFFSET_HOURS = -7  # PST/PDT — adjust if you re-run in winter.
WINDOW_LOCAL = None  # full 24h — set ("21:00", "23:59") to restrict


def _in_window(iso_ts: str) -> bool:
    """True if iso_ts (UTC) falls inside ``WINDOW_LOCAL`` once shifted
    to PDT. Returns True when WINDOW_LOCAL is None (= no filter)."""
    if WINDOW_LOCAL is None:
        return True
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone(timezone(timedelta(hours=PDT_OFFSET_HOURS)))
    hm = local.strftime("%H:%M")
    return WINDOW_LOCAL[0] <= hm <= WINDOW_LOCAL[1]


def _load(ib_sym: str) -> list[sr_backtest.Bar]:
    # Switched to TRADES cache (real volume + executable prints).
    p = Path(f"/tmp/chart_signal_30d_trades/{ib_sym}_30d.json")
    raw = json.loads(p.read_text())
    bar_list = raw.get("bars", raw) if isinstance(raw, dict) else raw
    bars = [sr_backtest.Bar(
        t=b["ts"], open=b["open"], high=b["high"],
        low=b["low"], close=b["close"],
    ) for b in bar_list]
    bars.sort(key=lambda b: b.t)
    return bars


def _legs_and_prior(bars, entry_idx, side):
    """Return (left, right, prior) in price units.

    Pivot is at idx p = entry_idx - 1.
      left  = move INTO the pivot   (bar p-1 → p)
      right = move OUT of the pivot (bar p   → p+1)
      prior = |move of bar BEFORE the pivot bar| (bar p-2 → p-1)
    All positive for a strict 1/1 pivot of the matching side.
    """
    p = entry_idx - 1
    if p - 2 < 0 or p + 1 > len(bars) - 1:
        return None, None, None
    c = [b.close for b in bars]
    if side == "LONG":
        left  = c[p - 1] - c[p]    # drop into the pivot low
        right = c[p + 1] - c[p]    # rise out of the pivot low
    else:
        left  = c[p] - c[p - 1]    # rise into the pivot high
        right = c[p] - c[p + 1]    # drop out of the pivot high
    prior = abs(c[p - 1] - c[p - 2])
    return left, right, prior


def _trade_net(t, comm):
    sides = 1 if t.exit_reason == "EOD" else 2
    pnl = getattr(t, "_frozen_pnl", t.pnl)
    return pnl - sides * comm


def _classify_block(label_def, bars, trades, comm, depth_of):
    """Run one depth-definition pass; return per-symbol stats dict."""
    # Filter to the local-time window if WINDOW_LOCAL is set.
    trades = [t for t in trades if _in_window(t.entry_t)]
    n_total = len(trades)
    n_win = sum(1 for t in trades if getattr(t, "_frozen_pnl", t.pnl) > 0)
    all_net = sum(_trade_net(t, comm) for t in trades)
    n_shallow = n_sh_win = 0
    sh_net = 0.0
    filt_net = 0.0
    for t in trades:
        left, right, prior = _legs_and_prior(bars, t.entry_idx, t.side)
        if left is None or prior is None or prior <= 0:
            filt_net += _trade_net(t, comm)
            continue
        depth = depth_of(left, right)
        ratio = depth / prior
        if ratio < RATIO_THRESHOLD:
            n_shallow += 1
            sh_net += _trade_net(t, comm)
            if getattr(t, "_frozen_pnl", t.pnl) > 0:
                n_sh_win += 1
        else:
            filt_net += _trade_net(t, comm)
    return {
        "label": label_def,
        "n_total": n_total, "n_win": n_win,
        "n_shallow": n_shallow, "n_sh_win": n_sh_win,
        "n_sh_loss": n_shallow - n_sh_win,
        "sh_net": sh_net, "all_net": all_net, "filt_net": filt_net,
    }


DEFS = [
    ("left  (drop-in)",   lambda l, r: l),
    ("right (rise-out)",  lambda l, r: r),
    ("avg   (L+R)/2",     lambda l, r: 0.5 * (l + r)),
]


def main() -> int:
    # Trade.pnl reads the module-global MULTIPLIER at access time, so
    # we have to snapshot pnls per-symbol while the right multiplier
    # is in force. Freeze them onto each Trade as ``_frozen_pnl``.
    cache: dict[str, tuple[list, list, float]] = {}
    for label, ib_sym, mult, trail, comm in SYMBOLS:
        bars = _load(ib_sym)
        sr_backtest.MULTIPLIER = float(mult)
        trades = sr_backtest.run_backtest_live(
            bars, trail_width_pct=trail, cooldown_bars=1,
        )
        for t in trades:
            t._frozen_pnl = t.pnl  # type: ignore[attr-defined]
        cache[label] = (bars, trades, comm)

    if WINDOW_LOCAL is not None:
        print(f"** Time filter: entries in {WINDOW_LOCAL[0]}–"
              f"{WINDOW_LOCAL[1]} PDT only **")

    for def_label, depth_fn in DEFS:
        print(f"\n=== depth = {def_label}  (ratio < {RATIO_THRESHOLD}) ===")
        print(f"{'sym':<5}{'trades':>8}{'win':>6}{'shallow':>9}"
              f"{'sh-win':>8}{'sh-loss':>9}{'sh-net$':>11}"
              f"{'all-net$':>11}{'filt-net$':>12}")
        print("-" * 79)
        tot = {"n_total": 0, "n_win": 0, "n_shallow": 0,
               "n_sh_win": 0, "n_sh_loss": 0,
               "sh_net": 0.0, "all_net": 0.0, "filt_net": 0.0}
        for label, _, _, _, _ in SYMBOLS:
            bars, trades, comm = cache[label]
            r = _classify_block(def_label, bars, trades, comm, depth_fn)
            print(f"{label:<5}{r['n_total']:>8}{r['n_win']:>6}"
                  f"{r['n_shallow']:>9}{r['n_sh_win']:>8}{r['n_sh_loss']:>9}"
                  f"{r['sh_net']:>11.2f}{r['all_net']:>11.2f}"
                  f"{r['filt_net']:>12.2f}")
            for k in ("n_total", "n_win", "n_shallow",
                      "n_sh_win", "n_sh_loss"):
                tot[k] += r[k]
            for k in ("sh_net", "all_net", "filt_net"):
                tot[k] += r[k]
        print("-" * 79)
        print(f"{'TOT':<5}{tot['n_total']:>8}{tot['n_win']:>6}"
              f"{tot['n_shallow']:>9}{tot['n_sh_win']:>8}{tot['n_sh_loss']:>9}"
              f"{tot['sh_net']:>11.2f}{tot['all_net']:>11.2f}"
              f"{tot['filt_net']:>12.2f}")
        sh_wr = (100 * tot['n_sh_win'] / max(1, tot['n_shallow']))
        print(f"  shallow trades: {tot['n_shallow']}/{tot['n_total']}  "
              f"({100*tot['n_shallow']/max(1,tot['n_total']):.1f}%)   "
              f"shallow win-rate {sh_wr:.1f}%   "
              f"if filtered NET ${tot['filt_net']:.2f}   "
              f"(Δ ${tot['filt_net'] - tot['all_net']:+.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
