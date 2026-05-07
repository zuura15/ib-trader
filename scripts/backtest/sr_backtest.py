"""Backtest the support/resistance fan algorithm on historical bars.

Usage:
    uv run python scripts/backtest/sr_backtest.py [--bars PATH]

Loads bars from JSON (default ``/tmp/mgc7d.json``), walks chronologically
with no look-ahead, and simulates trades per the user's spec:

    Entry  - 3rd-touch + bounce on an uptrending support line.
              Specifically: at bar t, support line anchored at t-1 has
              touches >= 3 and slope > 0, AND close[t] > close[t-1].
              Open 1 contract at t+1 mid (open + close)/2 of that bar.
    Exit   - close[k] < line(k) - tolerance.  Exit at k+1 mid.
    Tol    - avgClose * 0.0005 (matches frontend default).

Output: per-trade log + summary stats. No commission / slippage.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

EPS = 1e-6
TOLERANCE_FRACTION = 0.0005
BREAK_STALE_BARS = 20
MIN_TOUCHES = 3   # spec: 3rd-touch entry trigger
# Per-instrument $-per-point multiplier. STK=1, MGC=10, MES=5, ES=50.
MULTIPLIER = 10
RSI_PERIOD = 14   # matches frontend RSI_DEFAULTS


def compute_rsi(closes: list[float], period: int = RSI_PERIOD) -> list[float | None]:
    """Wilder's RSI — port of frontend ``computeRsi``. Returns one
    value per close (None until the period is filled)."""
    out: list[float | None] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gain_sum = loss_sum = 0.0
    for i in range(1, period + 1):
        diff = closes[i] - closes[i - 1]
        if diff > 0:
            gain_sum += diff
        else:
            loss_sum -= diff
    avg_gain = gain_sum / period
    avg_loss = loss_sum / period
    out[period] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    for i in range(period + 1, len(closes)):
        diff = closes[i] - closes[i - 1]
        gain = diff if diff > 0 else 0.0
        loss = -diff if diff < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = 100.0 if avg_loss == 0 else 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)
    return out


@dataclass
class Bar:
    t: str          # ISO timestamp
    open: float
    high: float
    low: float
    close: float

    @property
    def mid(self) -> float:
        # BID_ASK feed: open=avg_bid, close=avg_ask. Mid = (bid+ask)/2.
        return (self.open + self.close) / 2.0


@dataclass
class Line:
    from_idx: int
    anchor_b_idx: int
    slope: float
    intercept: float
    touches: int
    break_idx: int | None

    def value_at(self, idx: int) -> float:
        return self.slope * idx + self.intercept


def find_pivot_lows(closes: list[float]) -> list[int]:
    """Strict 1/1 local minima on the close polyline."""
    out: list[int] = []
    for i in range(1, len(closes) - 1):
        v, l, r = closes[i], closes[i - 1], closes[i + 1]
        if v < l and v < r:
            out.append(i)
    return out


def find_pivot_highs(closes: list[float]) -> list[int]:
    """Strict 1/1 local maxima on the close polyline."""
    out: list[int] = []
    for i in range(1, len(closes) - 1):
        v, l, r = closes[i], closes[i - 1], closes[i + 1]
        if v > l and v > r:
            out.append(i)
    return out


def crosses_any(slope: float, intercept: float, from_idx: int,
                to_idx: int, others: list[Line]) -> bool:
    for e in others:
        lo = max(from_idx, e.from_idx)
        hi = min(to_idx, e.anchor_b_idx if e.break_idx is None else e.break_idx)
        # Use end-of-drawn-range; here we use anchor_b_idx as a proxy
        # since lines extend to lastBarIdx in the live algo. For
        # backtest fidelity we use the real upper bound (caller
        # passes to_idx).
        if lo >= hi:
            continue
        dm = slope - e.slope
        if abs(dm) < EPS:
            continue
        x = (e.intercept - intercept) / dm
        if lo + EPS < x < hi - EPS:
            return True
    return False


def detect_lines(closes: list[float], up_to: int,
                 type_: str) -> list[Line]:
    """Run the iterative-fan SR detection on closes[0..up_to] and
    return all surviving lines for one side (``type_`` = 'support' or
    'resistance'). No look-ahead beyond ``up_to``.
    """
    if up_to < 2:
        return []
    sub = closes[: up_to + 1]
    if type_ == 'support':
        pivots = find_pivot_lows(sub)
    else:
        pivots = find_pivot_highs(sub)
    if len(pivots) < 2:
        return []
    last_idx = up_to
    avg = sum(sub) / len(sub)
    tol = max(EPS, avg * TOLERANCE_FRACTION)

    def violates(close_v: float, line_v: float) -> bool:
        return (close_v < line_v - tol) if type_ == 'support' \
            else (close_v > line_v + tol)

    out: list[Line] = []
    for pi in range(len(pivots) - 1, 0, -1):
        P = pivots[pi]
        candidates: list[tuple[int, float]] = []
        for qi in range(pi - 1, -1, -1):
            Q = pivots[qi]
            slope = (sub[P] - sub[Q]) / (P - Q)
            candidates.append((Q, slope))
        # Steepest first. Support → DESC (steep up first); resistance
        # → ASC (steep down first). Tiebreak older Q first.
        if type_ == 'support':
            candidates.sort(key=lambda x: (-x[1], x[0]))
        else:
            candidates.sort(key=lambda x: (x[1], x[0]))

        for Q, slope in candidates:
            intercept = sub[P] - slope * P
            # Channel rule against polyline.
            valid = True
            for i in range(Q + 1, P):
                if violates(sub[i], slope * i + intercept):
                    valid = False
                    break
            if not valid:
                continue
            # Cross-rejection against already-emitted lines (same side).
            if crosses_any(slope, intercept, Q, P, out):
                continue
            # Post-P break detection on the chart polyline.
            break_idx: int | None = None
            for i in range(P + 1, last_idx + 1):
                if violates(sub[i], slope * i + intercept):
                    break_idx = i
                    break
            if break_idx is not None and (last_idx - break_idx) > BREAK_STALE_BARS:
                continue

            # Touch counting.
            touch_end = break_idx if break_idx is not None else last_idx
            touches = 2
            for p in pivots:
                if p == Q or p == P or p < Q or p > touch_end:
                    continue
                if abs(sub[p] - (slope * p + intercept)) <= tol:
                    touches += 1
            if touches < 2:
                continue

            # Coincident-line dedup.
            coincident = any(
                abs(e.slope - slope) < EPS and abs(e.intercept - intercept) < EPS
                for e in out
            )
            if coincident:
                continue

            out.append(Line(
                from_idx=Q, anchor_b_idx=P, slope=slope,
                intercept=intercept, touches=touches, break_idx=break_idx,
            ))

    return out


@dataclass
class Trade:
    side: str   # 'LONG' (off support) or 'SHORT' (off resistance)
    entry_t: str
    entry_idx: int
    entry_price: float
    line_slope: float
    line_intercept: float
    line_anchor_b_idx: int
    line_touches: int
    exit_t: str | None = None
    exit_idx: int | None = None
    exit_price: float | None = None
    exit_reason: str | None = None  # 'BREAK' | 'EOD'

    @property
    def pnl(self) -> float:
        if self.exit_price is None:
            return 0.0
        sign = 1 if self.side == 'LONG' else -1
        return (self.exit_price - self.entry_price) * sign * MULTIPLIER

    @property
    def bars_held(self) -> int:
        if self.exit_idx is None:
            return 0
        return self.exit_idx - self.entry_idx


def run_backtest(bars: list[Bar], direction: str,
                 rsi_long_max: float | None = None,
                 rsi_short_min: float | None = None) -> list[Trade]:
    """Walk bars chronologically and simulate trades.

    direction:
      'long'  - enter long on uptrending support 3rd-touch + bounce up;
                exit on close below the line.
      'short' - mirror: enter short on downtrending resistance 3rd-touch
                + reject down; exit on close above the line.
      'both'  - run both passes independently, merge trades.

    For 'both' the long and short positions can overlap in time. The
    simulator is two-pass to keep bookkeeping simple — call once per
    side and merge the trade lists.
    """
    if direction == 'both':
        return (
            run_backtest(bars, 'long', rsi_long_max, rsi_short_min)
            + run_backtest(bars, 'short', rsi_long_max, rsi_short_min)
        )

    closes = [b.close for b in bars]
    avg = sum(closes) / max(1, len(closes))
    tol = max(EPS, avg * TOLERANCE_FRACTION)
    side = 'LONG' if direction == 'long' else 'SHORT'
    line_type = 'support' if direction == 'long' else 'resistance'
    rsi = compute_rsi(closes)

    trades: list[Trade] = []
    open_trade: Trade | None = None

    # Optimisation: SR detection (O(P²·B)) is too slow to call on
    # every bar in a 3000+ bar window. Detect only when a NEW
    # confirmed pivot at t-1 has a bounce-confirmation at t — that's
    # the only condition that could trigger a fresh entry. Between
    # pivots we only need to evaluate break-detection on the open
    # trade's stored line, which is O(1).
    for t in range(2, len(bars) - 1):
        # Exit check on the open trade.
        if open_trade is not None:
            line_at_t = (
                open_trade.line_slope * t + open_trade.line_intercept
            )
            if direction == 'long':
                broken = closes[t] < line_at_t - tol
            else:
                broken = closes[t] > line_at_t + tol
            if broken:
                exit_idx = min(t + 1, len(bars) - 1)
                open_trade.exit_t = bars[exit_idx].t
                open_trade.exit_idx = exit_idx
                open_trade.exit_price = bars[exit_idx].mid
                open_trade.exit_reason = "BREAK"
                trades.append(open_trade)
                open_trade = None
                continue
            # If holding, no entry consideration this bar.
            continue

        # Entry-side fast filter: require a new confirmed pivot at
        # t-1 plus bounce/reject at t before paying for detection.
        if direction == 'long':
            new_pivot = closes[t - 1] < closes[t - 2] and closes[t - 1] < closes[t]
            bounce = closes[t] > closes[t - 1]
        else:
            new_pivot = closes[t - 1] > closes[t - 2] and closes[t - 1] > closes[t]
            bounce = closes[t] < closes[t - 1]
        if not (new_pivot and bounce):
            continue

        # RSI filter — applied at the signal bar t. Use RSI at the
        # pivot bar (t-1) since that's the touch-point that triggered
        # the bounce. Skip the candidate if RSI doesn't meet the
        # threshold; the backtest's "no entry" is fully equivalent
        # to never seeing this bar.
        if direction == 'long' and rsi_long_max is not None:
            r = rsi[t - 1]
            if r is None or r > rsi_long_max:
                continue
        if direction == 'short' and rsi_short_min is not None:
            r = rsi[t - 1]
            if r is None or r < rsi_short_min:
                continue

        lines = detect_lines(closes, up_to=t, type_=line_type)
        for line in lines:
            if line.anchor_b_idx != t - 1:
                continue
            if direction == 'long' and line.slope <= 0:
                continue
            if direction == 'short' and line.slope >= 0:
                continue
            if line.touches < MIN_TOUCHES:
                continue
            entry_idx = t + 1
            if entry_idx >= len(bars):
                break
            open_trade = Trade(
                side=side,
                entry_t=bars[entry_idx].t,
                entry_idx=entry_idx,
                entry_price=bars[entry_idx].mid,
                line_slope=line.slope,
                line_intercept=line.intercept,
                line_anchor_b_idx=line.anchor_b_idx,
                line_touches=line.touches,
            )
            break

    if open_trade is not None:
        open_trade.exit_t = bars[-1].t
        open_trade.exit_idx = len(bars) - 1
        open_trade.exit_price = bars[-1].close
        open_trade.exit_reason = "EOD"
        trades.append(open_trade)

    return trades


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", default="/tmp/mgc7d.json",
                        help="Path to bars JSON (frontend /api/history shape)")
    parser.add_argument("--multiplier", type=float, default=10.0,
                        help="$-per-point. STK=1, MGC=10, MES=5, ES=50.")
    parser.add_argument("--direction", choices=["long", "short", "both"],
                        default="long")
    parser.add_argument("--rsi-long-max", type=float, default=None,
                        help="Skip long entries if RSI > this at signal bar.")
    parser.add_argument("--rsi-short-min", type=float, default=None,
                        help="Skip short entries if RSI < this at signal bar.")
    args = parser.parse_args()
    global MULTIPLIER
    MULTIPLIER = args.multiplier

    path = Path(args.bars)
    if not path.exists():
        print(f"missing bars file: {path}", file=sys.stderr)
        return 1

    raw = json.loads(path.read_text())
    if isinstance(raw, dict) and "error" in raw:
        print(f"upstream error: {raw['error']}", file=sys.stderr)
        return 2
    if isinstance(raw, dict) and "detail" in raw:
        print(f"upstream detail: {raw['detail']}", file=sys.stderr)
        return 2
    bar_list = raw.get("bars", raw) if isinstance(raw, dict) else raw
    if not isinstance(bar_list, list) or not bar_list:
        print(f"unexpected shape in {path}", file=sys.stderr)
        return 3

    bars = [Bar(t=b["ts"], open=b["open"], high=b["high"],
                low=b["low"], close=b["close"]) for b in bar_list]
    bars.sort(key=lambda b: b.t)

    print(f"loaded {len(bars)} bars from {bars[0].t} to {bars[-1].t}")
    trades = run_backtest(
        bars, args.direction,
        rsi_long_max=args.rsi_long_max,
        rsi_short_min=args.rsi_short_min,
    )
    trades.sort(key=lambda x: x.entry_t)

    print()
    print(f"Trades (direction={args.direction}, multiplier={MULTIPLIER}):")
    print(f"{'side':<6}{'entry':<25}{'exit':<25}{'entry$':>10}{'exit$':>10}"
          f"{'pnl$':>10}{'bars':>6}{'touches':>9}{'reason':>8}")
    total_pnl = 0.0
    wins = 0
    losses = 0
    open_pnl = 0.0
    for tr in trades:
        total_pnl += tr.pnl
        if tr.exit_reason == 'EOD':
            open_pnl += tr.pnl
        if tr.pnl > 0:
            wins += 1
        elif tr.pnl < 0:
            losses += 1
        e = tr.entry_t[11:16]
        x = (tr.exit_t or "")[11:16] if tr.exit_t else "open"
        e_d = tr.entry_t[:10]
        x_d = (tr.exit_t or "")[:10] if tr.exit_t else ""
        print(f"{tr.side:<6}{e_d} {e:<14}{x_d} {x:<14}"
              f"{tr.entry_price:>10.2f}"
              f"{(tr.exit_price or 0):>10.2f}"
              f"{tr.pnl:>10.2f}"
              f"{tr.bars_held:>6}"
              f"{tr.line_touches:>9}"
              f"{(tr.exit_reason or ''):>8}")

    print()
    n = len(trades)
    closed = [t for t in trades if t.exit_reason == 'BREAK']
    closed_n = len(closed)
    closed_wins = sum(1 for t in closed if t.pnl > 0)
    closed_pnl = sum(t.pnl for t in closed)
    print("Summary:")
    print(f"  trades:           {n}")
    print(f"  wins:             {wins}")
    print(f"  losses:           {losses}")
    print(f"  win rate (all):   {(wins / n * 100 if n else 0):.1f}%")
    print(f"  total P&L:        ${total_pnl:.2f}")
    print(f"  closed-only P&L:  ${closed_pnl:.2f}  ({closed_n} trades, {(closed_wins / closed_n * 100 if closed_n else 0):.1f}% win)")
    print(f"  open ride P&L:    ${open_pnl:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
