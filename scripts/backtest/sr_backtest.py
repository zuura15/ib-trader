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

# Repo-root sys.path hop so this script runs as a standalone too.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from ib_trader.signals.sr_fan import (  # noqa: E402
    EPS,
    TOLERANCE_FRACTION,
    TOUCH_TOLERANCE_FRACTION,
    BREAK_STALE_BARS,
    MIN_TOUCHES,
    TrendLine as Line,
    crosses_any,
    detect_lines,
    find_pivot_highs,
    find_pivot_lows,
)

# Per-instrument $-per-point multiplier. STK=1, MGC=10, MES=5, ES=50.
MULTIPLIER = 10
RSI_PERIOD = 14   # matches frontend RSI_DEFAULTS
ATR_PERIOD = 14   # standard Wilder ATR length


def compute_atr(bars: list["Bar"], period: int = ATR_PERIOD) -> list[float | None]:
    """Wilder's ATR. Returns one value per bar — None until ``period``
    true ranges have been observed. True Range uses the prior close,
    so out[0] is always None."""
    out: list[float | None] = [None] * len(bars)
    if len(bars) < period + 1:
        return out
    trs: list[float] = []
    for i in range(1, len(bars)):
        prev_c = bars[i - 1].close
        b = bars[i]
        trs.append(max(b.high - b.low,
                       abs(b.high - prev_c),
                       abs(b.low - prev_c)))
    # Seed with simple average of first ``period`` TRs (Wilder seed).
    seed = sum(trs[:period]) / period
    out[period] = seed
    atr = seed
    for i in range(period, len(trs)):
        atr = (atr * (period - 1) + trs[i]) / period
        out[i + 1] = atr
    return out


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
    # 'BREAK' (line breach), 'TRAIL' (trailing dip/rise),
    # 'BOTH' (both gates fired on same bar), 'EOD' (end of data).
    exit_reason: str | None = None

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


def run_backtest_live(
    bars: list[Bar],
    *,
    trail_width_pct: float = 0.0003,
    cooldown_bars: int = 1,
    history_bars: int = 40,
    rsi_long_max: float | None = None,
    rsi_short_min: float | None = None,
    utc_hour_range: tuple[int, int] | None = None,
    atr_mult: float | None = None,
    atr_period: int = ATR_PERIOD,
    near_touch_tolerance_fraction: float | None = None,
    hard_stop_mult: float | None = None,
    min_pivot_strength: float = 0.0,
    entry_min_pivot_height: float = 0.0,
    shallow_right_leg_threshold: float | None = None,
    shoulder_filter: bool = False,
    tight_triangle_block_distance: int = 0,
    exit_on_counter_trend: bool = False,
    counter_trend_buffer_pct: float = 0.0001,
    exit_on_triangle_rejection: bool = False,
    triangle_min_touches: int = 2,
    triangle_reentry_watch_bars: int = 4,
    min_target_filter: bool = False,
    counter_line_exit: bool = False,
    counter_line_bars1m: list["Bar"] | None = None,
    counter_line_hold_seconds: float = 10.0,
) -> list[Trade]:
    """Mirrors current chart_signal live rules:
    - Single position at a time, EITHER side.
    - Entry: 3rd-touch on uptrending support (long) OR downtrending
      resistance (short), with the just-confirmed pivot at t-1 ON the
      line. Tie → prefer long.
    - Exit: bar close beyond the entry line OR bar close past the
      water mark by trail_width_pct, whichever fires first.
    - After exit, ``cooldown_bars`` bars must pass before the next
      entry (matches the live runtime's ``cooldown_until`` on the
      doc post round-trip).

    The original ``run_backtest`` runs independent per-side passes
    that can hold long and short simultaneously — kept for backward
    compatibility but doesn't match the live bot's one-position
    semantic.
    """
    closes = [b.close for b in bars]
    avg = sum(closes) / max(1, len(closes))
    tol = max(EPS, avg * TOLERANCE_FRACTION)
    rsi = compute_rsi(closes)
    # ATR-scaled trail uses the bar-by-bar ATR series. None until
    # ``atr_period`` bars have elapsed.
    atr_series = compute_atr(bars, atr_period) if atr_mult is not None else None

    # Pre-compute UTC-second timestamps for counter_line tick-approx.
    # The opposing line is projected to each 1-min bar's elapsed time
    # in 3-min-bar units, so the slope/intercept (in 3-min-bar coords)
    # works directly.
    from datetime import datetime as _dt
    def _iso_to_sec(s: str) -> float:
        s2 = s.replace("Z", "+00:00")
        try:
            return _dt.fromisoformat(s2).timestamp()
        except ValueError:
            return _dt.strptime(s2.split(".")[0],
                                "%Y-%m-%dT%H:%M:%S").timestamp()
    bar3m_secs: list[float] = []
    bar1m_secs: list[float] = []
    if counter_line_exit and counter_line_bars1m:
        bar3m_secs = [_iso_to_sec(b.t) for b in bars]
        bar1m_secs = [_iso_to_sec(b.t) for b in counter_line_bars1m]

    trades: list[Trade] = []
    open_trade: Trade | None = None
    cooldown_until_idx = -1
    hwm = -float('inf')
    lwm = float('inf')
    # Monotonic pointer into 1-min bars — only used when counter_line
    # tick-approx is active. Advances forward each 3-min iteration.
    cl_1m_cursor = 0
    # Armed-state for the touch state machine. ``None`` = no active
    # touch; dict = { "line_val": float } captures the line being
    # touched. Persists across 1-min bars within the same 3-min
    # iteration so a touch on minute N can be confirmed on minute N+1.
    cl_touch_armed: dict | None = None
    # Re-entry watch state — populated when a triangle-rejection exit
    # fires so the next few bars can re-enter if price actually breaks
    # the rejected line. Cleared when the watch window expires or a
    # re-entry triggers.
    watch_until_idx = -1
    watch_line_slope: float = 0.0
    watch_line_intercept: float = 0.0
    watch_side: str = ""              # "LONG" / "SHORT" — original direction
    watch_entry_line_slope: float = 0.0
    watch_entry_line_intercept: float = 0.0
    watch_entry_anchor_b_idx: int = 0
    watch_entry_touches: int = 0

    for t in range(2, len(bars) - 1):
        if open_trade is not None:
            line_at_t = (
                open_trade.line_slope * t + open_trade.line_intercept
            )
            if atr_series is not None:
                atr_t = atr_series[t]
                # During warm-up (ATR not yet defined) fall back to the
                # % trail. Avoids skipping the early-window trades that
                # would otherwise have no exit gate.
                trail_delta = (atr_mult * atr_t) if atr_t is not None else None
            else:
                trail_delta = None
            # Optional intra-bar hard stop. Fires when bar.low (long)
            # or bar.high (short) breaches HWM/LWM ×
            # (1 ± hard_stop_mult × trail_width_pct) — i.e. a wider
            # tick-by-tick band than the close-only trail. Catches
            # rapid mid-bar reversals that the bar-close trail
            # waits 1-3 min to confirm.
            hard_stop_fired = False
            hard_stop_price = 0.0
            if open_trade.side == 'LONG':
                hwm = max(hwm, closes[t])
                if hard_stop_mult is not None and hard_stop_mult > 0 \
                        and trail_width_pct > 0:
                    hard_pct = trail_width_pct * hard_stop_mult
                    hard_stop_price = hwm * (1 - hard_pct)
                    if bars[t].low < hard_stop_price:
                        hard_stop_fired = True
                if trail_delta is not None:
                    trail_stop = hwm - trail_delta
                else:
                    trail_stop = (hwm * (1 - trail_width_pct)
                                  if trail_width_pct > 0 else -float('inf'))
                breach_line = closes[t] < line_at_t - tol
                breach_trail = closes[t] < trail_stop
            else:
                lwm = min(lwm, closes[t])
                if hard_stop_mult is not None and hard_stop_mult > 0 \
                        and trail_width_pct > 0:
                    hard_pct = trail_width_pct * hard_stop_mult
                    hard_stop_price = lwm * (1 + hard_pct)
                    if bars[t].high > hard_stop_price:
                        hard_stop_fired = True
                if trail_delta is not None:
                    trail_stop = lwm + trail_delta
                else:
                    trail_stop = (lwm * (1 + trail_width_pct)
                                  if trail_width_pct > 0 else float('inf'))
                breach_line = closes[t] > line_at_t + tol
                breach_trail = closes[t] > trail_stop
            if hard_stop_fired:
                # Exit at the hard-stop price — assumes a stop-loss
                # order would have filled there (no slippage modeled).
                open_trade.exit_t = bars[t].t
                open_trade.exit_idx = t
                open_trade.exit_price = hard_stop_price
                open_trade.exit_reason = "HARDSTOP"
                trades.append(open_trade)
                cooldown_until_idx = t + cooldown_bars
                open_trade = None
                hwm = -float('inf')
                lwm = float('inf')
                continue

            # Counter-trend rejection exit. For LONG: any resistance
            # line (incl. 2-touch tentative) detected in the trailing
            # window; if bar.high reached the line within ``touch_tol``
            # AND bar.close came back from that high by more than the
            # ``counter_trend_buffer_pct``, exit at bar.close. Mirrors
            # an intra-bar tick rejection without needing tick data —
            # the bar's range captures the "approached and rejected"
            # pattern. SHORT side: same logic on support lines.
            ct_fired = False
            if exit_on_counter_trend:
                win_start_ct = max(0, t - history_bars + 1)
                slice_ct = closes[win_start_ct:t]   # exclude bar t
                if len(slice_ct) >= 4:
                    avg_ct = sum(slice_ct) / len(slice_ct)
                    touch_tol_ct = max(EPS, avg_ct * TOUCH_TOLERANCE_FRACTION)
                    if open_trade.side == 'LONG':
                        ct_lines = detect_lines(
                            slice_ct,
                            up_to=len(slice_ct) - 1,
                            type_='resistance',
                        )
                        bar_high = bars[t].high
                        bar_close = bars[t].close
                        for cl in ct_lines:
                            line_val = (cl.intercept
                                        + cl.slope * (len(slice_ct) - 1
                                                      + 1))  # project to t
                            buf_amt = max(EPS, line_val * counter_trend_buffer_pct)
                            if bar_high >= line_val - touch_tol_ct \
                                    and bar_close < bar_high - buf_amt:
                                ct_fired = True
                                break
                    else:   # SHORT
                        ct_lines = detect_lines(
                            slice_ct,
                            up_to=len(slice_ct) - 1,
                            type_='support',
                        )
                        bar_low = bars[t].low
                        bar_close = bars[t].close
                        for cl in ct_lines:
                            line_val = (cl.intercept
                                        + cl.slope * (len(slice_ct) - 1 + 1))
                            buf_amt = max(EPS, line_val * counter_trend_buffer_pct)
                            if bar_low <= line_val + touch_tol_ct \
                                    and bar_close > bar_low + buf_amt:
                                ct_fired = True
                                break
            if ct_fired:
                open_trade.exit_t = bars[t].t
                open_trade.exit_idx = t
                open_trade.exit_price = bars[t].close
                open_trade.exit_reason = "COUNTER_TREND"
                trades.append(open_trade)
                cooldown_until_idx = t + cooldown_bars
                open_trade = None
                hwm = -float('inf')
                lwm = float('inf')
                continue

            # Counter-line tick-approx (chart_signal.py EXIT_COUNTER_LINE).
            # Refreshes opposing-side line set at every 3-min bar close;
            # for 1-min sub-bars inside [bars[t-1].t, bars[t].t) checks
            # touch + close-confirm = "rejection that didn't break through".
            # Fires exit at the offending 1-min bar's close. Approximates
            # the live rule (10s hold on tick stream) at 1-min granularity
            # — 1-min > 10s so any "touched AND closed near the line"
            # bar implies the touch persisted past the threshold.
            cl_fired = False
            cl_exit_price = 0.0
            cl_exit_ts = ""
            if (counter_line_exit and counter_line_bars1m
                    and t >= 2 and len(bar1m_secs) > 0):
                win_start_cl = max(0, t - history_bars + 1)
                slice_cl_closes = closes[win_start_cl:t + 1]
                if len(slice_cl_closes) >= 4:
                    opp_type = ('resistance' if open_trade.side == 'LONG'
                                else 'support')
                    opp_lines = detect_lines(
                        slice_cl_closes,
                        up_to=len(slice_cl_closes) - 1,
                        type_=opp_type,
                        near_touch_tolerance_fraction=near_touch_tolerance_fraction,
                    )
                    # Live rule: 2+ touches, unbroken. detect_lines
                    # excludes broken by default; touches >= 2 is the
                    # minimum the live bot accepts (caches the best by
                    # touch count).
                    opp_lines = [l for l in opp_lines if l.touches >= 2]
                    if opp_lines:
                        # Highest-touch wins (matches live).
                        opp = max(opp_lines, key=lambda l: l.touches)
                        # Convert slope/intercept to absolute (full-bar
                        # frame, t-relative). win_start_cl is the slice
                        # origin in absolute bar coords.
                        abs_slope = opp.slope
                        abs_intercept = (opp.intercept
                                         - opp.slope * win_start_cl)
                        # 1-min bars in [bars[t-1].t_sec, bars[t].t_sec).
                        # Advance cursor to the first 1-min bar with
                        # t_sec >= bars[t-1].t_sec; iterate until t_sec
                        # >= bars[t].t_sec.
                        prev_3m_sec = bar3m_secs[t - 1]
                        cur_3m_sec = bar3m_secs[t]
                        # Skip-ahead the cursor.
                        while (cl_1m_cursor < len(bar1m_secs)
                               and bar1m_secs[cl_1m_cursor] < prev_3m_sec):
                            cl_1m_cursor += 1
                        i = cl_1m_cursor
                        while (i < len(bar1m_secs)
                               and bar1m_secs[i] < cur_3m_sec):
                            sb = counter_line_bars1m[i]
                            sec = bar1m_secs[i]
                            # Elapsed 3-min-bar units from win_start.
                            elapsed_3m = ((sec - bar3m_secs[win_start_cl])
                                          / 180.0)
                            line_val = abs_intercept + abs_slope * (
                                win_start_cl + elapsed_3m
                            )
                            # Touch tolerance — counter_line uses the
                            # TIGHT TOUCH_TOLERANCE_FRACTION (live default
                            # 0.0002 = 0.02%) for the "is mid currently
                            # touching the line" check, NOT the looser
                            # near_touch_tolerance_fraction (0.001) which
                            # is only for line DETECTION. The wider tol
                            # would over-fire by ~5x at touch time.
                            slice_avg = (sum(slice_cl_closes)
                                         / len(slice_cl_closes))
                            touch_tol = max(
                                EPS, slice_avg * TOUCH_TOLERANCE_FRACTION,
                            )
                            # Armed-then-confirmed state machine — the
                            # cleanest 1-min approximation of the live
                            # "arm at first touch, confirm at +10s"
                            # logic. On bar N, if price touched the line
                            # without breaking through: arm. On bar
                            # N+1 (~60s later, >> 10s), confirm if
                            # still touching → exit; else disarm.
                            if open_trade.side == 'LONG':
                                # Opposing = resistance above.
                                touched_now = (
                                    sb.high >= line_val - touch_tol
                                    and sb.close <= line_val + touch_tol
                                )
                                breakout_now = (
                                    sb.close > line_val + touch_tol
                                )
                            else:
                                # Opposing = support below.
                                touched_now = (
                                    sb.low <= line_val + touch_tol
                                    and sb.close >= line_val - touch_tol
                                )
                                breakout_now = (
                                    sb.close < line_val - touch_tol
                                )
                            if cl_touch_armed is not None:
                                # Confirm-or-clear pass.
                                if breakout_now:
                                    cl_touch_armed = None
                                elif touched_now:
                                    cl_fired = True
                                    cl_exit_price = sb.close
                                    cl_exit_ts = sb.t
                                    cl_touch_armed = None
                                    break
                                else:
                                    cl_touch_armed = None
                            elif touched_now and not breakout_now:
                                cl_touch_armed = {"line": line_val}
                            i += 1
                        # Keep cursor advanced to cur_3m_sec for next iter
                        # so we don't re-scan past sub-bars.
                        cl_1m_cursor = i
            if cl_fired:
                open_trade.exit_t = cl_exit_ts
                open_trade.exit_idx = t
                open_trade.exit_price = cl_exit_price
                open_trade.exit_reason = "COUNTER_LINE"
                trades.append(open_trade)
                cooldown_until_idx = t + cooldown_bars
                open_trade = None
                hwm = -float('inf')
                lwm = float('inf')
                continue

            # Triangle-rejection exit. Fires when:
            #   1. We're inside a converging triangle: an opposing-side
            #      line exists AND the entry-side line + opposing
            #      line converge (s.slope > r.slope).
            #   2. The bar's wick reached the adjacent side within
            #      ``touch_tol``.
            #   3. The bar closed on the wrong side of the line
            #      (failed to clear — close < resistance for LONG,
            #      close > support for SHORT).
            # Uses ``triangle_min_touches`` to gate the opposing line
            # quality (2 = any 2-touch tentative; 3 = confirmed).
            tri_fired = False
            if exit_on_triangle_rejection:
                win_start_tr = max(0, t - history_bars + 1)
                slice_tr = closes[win_start_tr:t]
                if len(slice_tr) >= 4:
                    avg_tr = sum(slice_tr) / len(slice_tr)
                    touch_tol_tr = max(EPS, avg_tr * TOUCH_TOLERANCE_FRACTION)
                    if open_trade.side == 'LONG':
                        adj_lines = [
                            l for l in detect_lines(
                                slice_tr,
                                up_to=len(slice_tr) - 1,
                                type_='resistance',
                            ) if l.touches >= triangle_min_touches
                        ]
                        sup_lines = detect_lines(
                            slice_tr,
                            up_to=len(slice_tr) - 1,
                            type_='support',
                        )
                        has_converging_pair = False
                        for sl in sup_lines:
                            for rl in adj_lines:
                                if sl.slope - rl.slope > 1e-9:
                                    has_converging_pair = True
                                    break
                            if has_converging_pair:
                                break
                        if has_converging_pair:
                            bar_high = bars[t].high
                            bar_close = bars[t].close
                            t_in_slice = len(slice_tr) - 1 + 1
                            for rl in adj_lines:
                                line_val = rl.intercept + rl.slope * t_in_slice
                                if bar_high >= line_val - touch_tol_tr \
                                        and bar_close < line_val:
                                    tri_fired = True
                                    break
                    else:  # SHORT
                        adj_lines = [
                            l for l in detect_lines(
                                slice_tr,
                                up_to=len(slice_tr) - 1,
                                type_='support',
                            ) if l.touches >= triangle_min_touches
                        ]
                        res_lines = detect_lines(
                            slice_tr,
                            up_to=len(slice_tr) - 1,
                            type_='resistance',
                        )
                        has_converging_pair = False
                        for sl in adj_lines:
                            for rl in res_lines:
                                if sl.slope - rl.slope > 1e-9:
                                    has_converging_pair = True
                                    break
                            if has_converging_pair:
                                break
                        if has_converging_pair:
                            bar_low = bars[t].low
                            bar_close = bars[t].close
                            t_in_slice = len(slice_tr) - 1 + 1
                            for sl in adj_lines:
                                line_val = sl.intercept + sl.slope * t_in_slice
                                if bar_low <= line_val + touch_tol_tr \
                                        and bar_close > line_val:
                                    tri_fired = True
                                    break
            if tri_fired:
                # Capture the entry-line params + the rejected line
                # BEFORE we clear open_trade, so the re-entry watch
                # can rebuild a continuation trade if price breaks.
                if triangle_reentry_watch_bars > 0:
                    watch_until_idx = t + triangle_reentry_watch_bars
                    watch_side = open_trade.side
                    watch_entry_line_slope = open_trade.line_slope
                    watch_entry_line_intercept = open_trade.line_intercept
                    watch_entry_anchor_b_idx = open_trade.line_anchor_b_idx
                    watch_entry_touches = open_trade.line_touches
                    # The line we got rejected at — find the one that
                    # actually triggered the exit and stash its slope/
                    # intercept (window-absolute).
                    if open_trade.side == 'LONG':
                        rej_lines = adj_lines
                    else:
                        rej_lines = adj_lines
                    bar_high = bars[t].high
                    bar_low = bars[t].low
                    bar_close = bars[t].close
                    win_start_local = max(0, t - history_bars + 1)
                    slice_len = t - win_start_local
                    for cl in rej_lines:
                        line_val = (cl.intercept
                                    + cl.slope * (slice_len - 1 + 1))
                        if open_trade.side == 'LONG':
                            if bar_high >= line_val - touch_tol_tr \
                                    and bar_close < line_val:
                                watch_line_slope = cl.slope
                                # Convert slice-local intercept to
                                # absolute (t-frame) so future bar
                                # idx t' projects correctly:
                                # line_at_t' = slope*(t' - win_start) + intercept
                                #            = slope*t' + (intercept - slope*win_start)
                                watch_line_intercept = (
                                    cl.intercept - cl.slope * win_start_local
                                )
                                break
                        else:
                            if bar_low <= line_val + touch_tol_tr \
                                    and bar_close > line_val:
                                watch_line_slope = cl.slope
                                watch_line_intercept = (
                                    cl.intercept - cl.slope * win_start_local
                                )
                                break
                open_trade.exit_t = bars[t].t
                open_trade.exit_idx = t
                open_trade.exit_price = bars[t].close
                open_trade.exit_reason = "TRIANGLE_REJECT"
                trades.append(open_trade)
                cooldown_until_idx = t + cooldown_bars
                open_trade = None
                hwm = -float('inf')
                lwm = float('inf')
                continue
            if breach_line or breach_trail:
                # Live fills happen within ~1-5s of bar close, not a
                # full bar later. Approximate as the trigger bar's
                # close price (matches the strategy's mid-order fill
                # near the bar boundary). Modeling exit at bar t+1's
                # mid baked in a full bar of slippage on every trade
                # — caught 2026-05-12 when live results showed
                # winners ~$30 vs backtest losers ~$14.
                open_trade.exit_t = bars[t].t
                open_trade.exit_idx = t
                open_trade.exit_price = bars[t].close
                if breach_line and breach_trail:
                    open_trade.exit_reason = "BOTH"
                elif breach_trail:
                    open_trade.exit_reason = "TRAIL"
                else:
                    open_trade.exit_reason = "BREAK"
                trades.append(open_trade)
                cooldown_until_idx = t + cooldown_bars
                open_trade = None
                hwm = -float('inf')
                lwm = float('inf')
            continue

        # Re-entry watch — if a triangle-rejection exit just fired
        # within the last ``triangle_reentry_watch_bars`` bars, check
        # whether THIS bar broke through the rejected line. If yes,
        # rebuild the continuation trade using the original entry-line
        # params (so existing breach/trail logic applies unchanged).
        if watch_side and watch_until_idx >= t:
            line_val = watch_line_slope * t + watch_line_intercept
            broke_through = (
                (watch_side == 'LONG' and bars[t].close > line_val)
                or (watch_side == 'SHORT' and bars[t].close < line_val)
            )
            if broke_through:
                open_trade = Trade(
                    side=watch_side,
                    entry_t=bars[t].t,
                    entry_idx=t,
                    entry_price=bars[t].close,
                    line_slope=watch_entry_line_slope,
                    line_intercept=watch_entry_line_intercept,
                    line_anchor_b_idx=watch_entry_anchor_b_idx,
                    line_touches=watch_entry_touches,
                )
                hwm = bars[t].close if watch_side == 'LONG' else -float('inf')
                lwm = bars[t].close if watch_side == 'SHORT' else float('inf')
                watch_until_idx = -1
                watch_side = ""
                cooldown_until_idx = -1  # break-out bypasses cooldown
                continue
        elif watch_side and watch_until_idx < t:
            # Watch expired without a break — reset.
            watch_until_idx = -1
            watch_side = ""

        # Cooldown after a recent exit.
        if t <= cooldown_until_idx:
            continue

        # Time-of-day filter (UTC hour ∈ [lo, hi)) — None = always.
        if utc_hour_range is not None:
            ts = bars[t].t  # ISO-8601 string, e.g. 2026-05-12T14:30:00+00:00
            try:
                hour = int(ts[11:13])
            except (ValueError, IndexError):
                hour = -1
            lo, hi = utc_hour_range
            if not (lo <= hour < hi):
                continue

        # Try both sides; the just-confirmed pivot at t-1 must be on
        # the chosen line.
        new_low = closes[t - 1] < closes[t - 2] and closes[t - 1] < closes[t]
        new_high = closes[t - 1] > closes[t - 2] and closes[t - 1] > closes[t]

        # Asymmetric entry-only filter: reject the new-pivot entry
        # trigger when the pivot's height vs its immediate neighbors
        # is below ``entry_min_pivot_height`` (absolute price units).
        # Height = smaller of the two adjacent legs. Tiny-blip pivots
        # both visually clutter the chart AND tend to be followed by
        # a big counter-move that smacks the trail. Crucially this
        # gate does NOT propagate into line construction — SR lines
        # still form on every strict 1/1 pivot, so the *exit* side
        # (line-breach + trail) is unaffected. Only the new-bar
        # ``new_low / new_high`` entry latch is suppressed.
        if entry_min_pivot_height > 0.0 and (new_low or new_high):
            if new_low:
                height = min(
                    closes[t - 2] - closes[t - 1],
                    closes[t] - closes[t - 1],
                )
            else:
                height = min(
                    closes[t - 1] - closes[t - 2],
                    closes[t - 1] - closes[t],
                )
            if height < entry_min_pivot_height:
                new_low = False
                new_high = False

        # Sliding history window: matches live chart_signal's 2h
        # ``/engine/history`` fetch (history_bars = 40 × 3min). Each
        # candidate's detect_lines runs on this window, not on the
        # cumulative slice. Anchor_b_idx is relative to the window,
        # so the just-confirmed-pivot check uses the window's
        # last-but-one index.
        win_start = max(0, t - history_bars + 1)
        slice_closes = closes[win_start:t + 1]
        if len(slice_closes) < 4:
            continue
        window_last = len(slice_closes) - 1
        window_new_pivot = window_last - 1

        # Window-local tolerances for the strict-then-loose entry gate.
        # ``touch_tol_w`` mirrors ``_has_new_touch``'s strict band in
        # the live bot; ``loose_tol_w`` (when set) widens the new
        # pivot's accept band — but ONLY when the line already holds
        # MIN_TOUCHES strict touches from older pivots.
        avg_slice = sum(slice_closes) / max(1, len(slice_closes))
        touch_tol_w = max(EPS, avg_slice * TOUCH_TOLERANCE_FRACTION)
        loose_tol_w: float | None = None
        if (near_touch_tolerance_fraction is not None
                and near_touch_tolerance_fraction > TOUCH_TOLERANCE_FRACTION):
            loose_tol_w = max(touch_tol_w,
                              avg_slice * near_touch_tolerance_fraction)

        side_supports = find_pivot_lows(
            slice_closes, min_pivot_strength=min_pivot_strength,
        )
        side_resists = find_pivot_highs(
            slice_closes, min_pivot_strength=min_pivot_strength,
        )

        def _accept(line, side_pivots) -> bool:
            """Entry gate: did the just-confirmed pivot land on this
            line? Strict tol always; loose tol only when the line
            already has MIN_TOUCHES strict touches from OLDER pivots
            (so the loose acceptance is a 4th+ near touch on an
            already-established trendline, not a relaxed first
            confirmation)."""
            if window_new_pivot not in side_pivots:
                return False
            line_at_new = line.intercept + line.slope * window_new_pivot
            delta_new = abs(slice_closes[window_new_pivot] - line_at_new)
            # Count strict touches from older pivots only — Q and P
            # are pivots and always on the line within EPS, so they
            # contribute when they're not the new pivot.
            strict_old = 0
            for piv in side_pivots:
                if piv == window_new_pivot or piv < line.from_idx \
                        or piv > window_last:
                    continue
                if abs(slice_closes[piv]
                       - (line.intercept + line.slope * piv)) <= touch_tol_w:
                    strict_old += 1
            if delta_new <= touch_tol_w:
                return (strict_old + 1) >= MIN_TOUCHES
            if loose_tol_w is not None and delta_new <= loose_tol_w:
                return strict_old >= MIN_TOUCHES
            return False

        long_line = None
        if new_low:
            if rsi_long_max is None or (
                rsi[t - 1] is not None and rsi[t - 1] <= rsi_long_max
            ):
                for line in detect_lines(
                    slice_closes, up_to=window_last, type_='support',
                    near_touch_tolerance_fraction=near_touch_tolerance_fraction,
                    min_pivot_strength=min_pivot_strength,
                ):
                    if line.slope > 0 and _accept(line, side_supports):
                        long_line = line
                        break

        short_line = None
        if new_high:
            if rsi_short_min is None or (
                rsi[t - 1] is not None and rsi[t - 1] >= rsi_short_min
            ):
                for line in detect_lines(
                    slice_closes, up_to=window_last, type_='resistance',
                    near_touch_tolerance_fraction=near_touch_tolerance_fraction,
                    min_pivot_strength=min_pivot_strength,
                ):
                    if line.slope < 0 and _accept(line, side_resists):
                        short_line = line
                        break

        # Bad-shoulder filter — mirrors chart_signal.py's live filter.
        # LONG  rejects when right_shoulder ≤ left_shoulder.
        # SHORT rejects when right_shoulder ≥ left_shoulder.
        if shoulder_filter and window_new_pivot >= 1:
            left_shoulder = slice_closes[window_new_pivot - 1]
            right_shoulder = slice_closes[window_last]
            if long_line is not None and right_shoulder <= left_shoulder:
                long_line = None
            if short_line is not None and right_shoulder >= left_shoulder:
                short_line = None

        # Shallow-right-leg filter — mirrors chart_signal.py's live
        # entry gate. Drops the entry when the bar AFTER the pivot
        # bar (rise-out for LONG / drop-out for SHORT) is less than
        # ``shallow_right_leg_threshold`` × the bar immediately
        # before the pivot bar. Done in window-relative coords:
        # pivot is at window_new_pivot, the bar after = window_last,
        # the bar before the pivot bar = window_new_pivot - 1, and
        # the bar BEFORE THAT = window_new_pivot - 2 (we need both
        # to compute prior-bar net height).
        if (shallow_right_leg_threshold is not None
                and window_new_pivot >= 2):
            prior_change = abs(
                slice_closes[window_new_pivot - 1]
                - slice_closes[window_new_pivot - 2]
            )
            if prior_change > 0:
                if long_line is not None:
                    rl = (slice_closes[window_last]
                          - slice_closes[window_new_pivot])
                    if rl / prior_change < shallow_right_leg_threshold:
                        long_line = None
                if short_line is not None:
                    rl = (slice_closes[window_new_pivot]
                          - slice_closes[window_last])
                    if rl / prior_change < shallow_right_leg_threshold:
                        short_line = None

        chosen_line = None
        chosen_side = None
        if long_line and short_line:
            if short_line.touches > long_line.touches:
                chosen_line, chosen_side = short_line, 'SHORT'
            else:
                chosen_line, chosen_side = long_line, 'LONG'
        elif long_line:
            chosen_line, chosen_side = long_line, 'LONG'
        elif short_line:
            chosen_line, chosen_side = short_line, 'SHORT'

        if chosen_line is None:
            continue

        # Tight-triangle entry block. Mirrors the live bot's
        # ``entry_triangle_block_distance`` config (chart_signal.py).
        # When set, detect all converging support/resistance pairs in
        # the same window and skip the entry if the nearest apex is
        # within ``tight_triangle_block_distance`` bars. Rationale:
        # entering just before a wedge resolves exposes the position
        # to a breakout going either way.
        if tight_triangle_block_distance > 0:
            from ib_trader.signals.sr_fan import find_wedges
            # Both sides already detected for the entry path; reuse
            # them. ``find_wedges`` defaults exclude broken lines —
            # matches the live bot path.
            tt_supports = detect_lines(
                slice_closes, up_to=window_last, type_='support',
                near_touch_tolerance_fraction=near_touch_tolerance_fraction,
                min_pivot_strength=min_pivot_strength,
            )
            tt_resistances = detect_lines(
                slice_closes, up_to=window_last, type_='resistance',
                near_touch_tolerance_fraction=near_touch_tolerance_fraction,
                min_pivot_strength=min_pivot_strength,
            )
            wedges = find_wedges(
                tt_supports, tt_resistances, window_last,
            )
            if wedges and wedges[0].apex_bars_ahead \
                    <= tight_triangle_block_distance:
                continue

        # Min-target entry filter (FILTER_MIN_TARGET). Mirrors the
        # live bot's gate: if the opposing wedge edge is closer than
        # the worst-case stop distance, the trade has bad R:R. Only
        # applies when a wedge exists.
        if min_target_filter:
            from ib_trader.signals.sr_fan import find_wedges
            mt_supports = detect_lines(
                slice_closes, up_to=window_last, type_='support',
                near_touch_tolerance_fraction=near_touch_tolerance_fraction,
                min_pivot_strength=min_pivot_strength,
            )
            mt_resistances = detect_lines(
                slice_closes, up_to=window_last, type_='resistance',
                near_touch_tolerance_fraction=near_touch_tolerance_fraction,
                min_pivot_strength=min_pivot_strength,
            )
            mt_wedges = find_wedges(
                mt_supports, mt_resistances, window_last,
            )
            if mt_wedges:
                nearest = mt_wedges[0]
                entry_price = slice_closes[window_last]
                trail_dist = abs(entry_price) * trail_width_pct
                if chosen_side == 'LONG':
                    opp = nearest.resistance
                else:
                    opp = nearest.support
                opp_at = opp.intercept + opp.slope * window_last
                target_dist = max(
                    0.0,
                    (opp_at - entry_price) if chosen_side == 'LONG'
                    else (entry_price - opp_at),
                )
                line_at = (
                    chosen_line.intercept + chosen_line.slope * window_last
                )
                line_breach_dist = abs(entry_price - line_at)
                stop_dist = max(trail_dist, line_breach_dist)
                if target_dist < stop_dist:
                    continue

        # Live fires entry order immediately on bar close and fills
        # within ~1-5s at market mid — well inside the same bar.
        # Approximate as the trigger bar's close. Modeling entry at
        # bar t+1's mid baked in a full bar of slippage (same fix as
        # the exit side above).
        # Convert the line's slope/intercept from window-relative to
        # absolute (bar-index) frame so the exit-check projection
        # ``slope * t + intercept`` works against absolute t.
        abs_slope = chosen_line.slope
        abs_intercept = chosen_line.intercept - chosen_line.slope * win_start
        open_trade = Trade(
            side=chosen_side,
            entry_t=bars[t].t,
            entry_idx=t,
            entry_price=bars[t].close,
            line_slope=abs_slope,
            line_intercept=abs_intercept,
            line_anchor_b_idx=chosen_line.anchor_b_idx + win_start,
            line_touches=chosen_line.touches,
        )
        if chosen_side == 'LONG':
            hwm = bars[t].close
        else:
            lwm = bars[t].close
        # Reset counter_line armed state at every entry — touches from
        # a prior position must not bleed into the new one.
        cl_touch_armed = None

    if open_trade is not None:
        open_trade.exit_t = bars[-1].t
        open_trade.exit_idx = len(bars) - 1
        open_trade.exit_price = bars[-1].close
        open_trade.exit_reason = "EOD"
        trades.append(open_trade)

    return trades


def run_backtest_momentum(
    bars: list[Bar],
    *,
    breakout_bars: int = 10,
    trail_width_pct: float = 0.0003,
    cooldown_bars: int = 1,
    utc_hour_range: tuple[int, int] | None = None,
    atr_mult: float | None = None,
    atr_period: int = ATR_PERIOD,
) -> list[Trade]:
    """Donchian-channel breakout strategy. Single position at a time.

    Entry:
      * LONG  when close[t] > max(close[t - breakout_bars : t]).
      * SHORT when close[t] < min(close[t - breakout_bars : t]).
      First breakout wins; opposite-side breakouts during a position
      are ignored (no reversals).

    Exit:
      * Trailing-stop at HWM × (1 − trail_width_pct) for longs and
        LWM × (1 + trail_width_pct) for shorts. ``atr_mult`` overrides
        the % trail with ``k × ATR`` when set.
      * EOD on the final bar; open trades flagged EOD in the
        summary.

    Fill model mirrors ``run_backtest_live``: entry and exit at the
    trigger bar's close (no phantom +1-bar slippage). Cooldown bars
    apply after each round-trip just like chart_signal."""
    if len(bars) < breakout_bars + 3:
        return []
    closes = [b.close for b in bars]
    atr_series = (
        compute_atr(bars, atr_period) if atr_mult is not None else None
    )

    trades: list[Trade] = []
    open_trade: Trade | None = None
    cooldown_until_idx = -1
    hwm = -float('inf')
    lwm = float('inf')

    for t in range(breakout_bars, len(bars)):
        # Exit gate on the open position.
        if open_trade is not None:
            if atr_series is not None:
                atr_t = atr_series[t]
                trail_delta = (atr_mult * atr_t) if atr_t is not None else None
            else:
                trail_delta = None
            if open_trade.side == 'LONG':
                hwm = max(hwm, closes[t])
                if trail_delta is not None:
                    trail_stop = hwm - trail_delta
                else:
                    trail_stop = (hwm * (1 - trail_width_pct)
                                  if trail_width_pct > 0 else -float('inf'))
                breach = closes[t] < trail_stop
            else:
                lwm = min(lwm, closes[t])
                if trail_delta is not None:
                    trail_stop = lwm + trail_delta
                else:
                    trail_stop = (lwm * (1 + trail_width_pct)
                                  if trail_width_pct > 0 else float('inf'))
                breach = closes[t] > trail_stop
            if breach:
                open_trade.exit_t = bars[t].t
                open_trade.exit_idx = t
                open_trade.exit_price = bars[t].close
                open_trade.exit_reason = "TRAIL"
                trades.append(open_trade)
                cooldown_until_idx = t + cooldown_bars
                open_trade = None
                hwm = -float('inf')
                lwm = float('inf')
            continue

        if t <= cooldown_until_idx:
            continue

        if utc_hour_range is not None:
            ts = bars[t].t
            try:
                hour = int(ts[11:13])
            except (ValueError, IndexError):
                hour = -1
            lo, hi = utc_hour_range
            if not (lo <= hour < hi):
                continue

        # Donchian-channel triggers — prior-bar lookback so close[t]
        # vs the highs/lows that existed BEFORE this bar formed.
        window_lo = t - breakout_bars
        window_hi = t  # exclusive: closes[window_lo : window_hi]
        prior = closes[window_lo:window_hi]
        if not prior:
            continue
        prior_high = max(prior)
        prior_low = min(prior)
        side: str | None = None
        if closes[t] > prior_high:
            side = 'LONG'
        elif closes[t] < prior_low:
            side = 'SHORT'
        if side is None:
            continue

        # Entry at the trigger bar's close — matches live-style fill
        # near bar boundary; no +1-bar phantom slippage.
        open_trade = Trade(
            side=side,
            entry_t=bars[t].t,
            entry_idx=t,
            entry_price=bars[t].close,
            line_slope=0.0,
            line_intercept=0.0,
            line_anchor_b_idx=t,
            line_touches=breakout_bars,
        )
        if side == 'LONG':
            hwm = bars[t].close
        else:
            lwm = bars[t].close

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
