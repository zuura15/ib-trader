"""Support/resistance fan detection — full-fidelity Python port of
``frontend/src/features/chart/supportResistance.ts``.

Used by:
  - ``ib_trader.bots.strategies.chart_signal`` (the live bot).
  - ``scripts/backtest/sr_backtest.py`` (the offline harness).

The frontend chart is the canonical implementation — the user tunes
``toleranceFraction`` / ``touchToleranceFraction`` by eye on a real
chart, then this file mirrors the exact same math so the bot's verdict
on every bar equals the chart's. Anything that changes the algorithm
here MUST be reproduced in the frontend file (or vice versa) and the
defaults below must match ``SR_DEFAULTS`` there.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

EPS = 1e-6

# Defaults — kept in lockstep with ``frontend/src/features/chart/
# supportResistance.ts:SR_DEFAULTS``. Two distinct tolerances:
#   * ``TOLERANCE_FRACTION`` (strict): the channel rule and post-P
#     break detection. A close on the wrong side by more than this
#     invalidates / breaks the line. 0.00005 ≈ $0.24 on gold at $4700.
#   * ``TOUCH_TOLERANCE_FRACTION`` (looser): the touch counter, used
#     to decide whether a third pivot upgrades a tentative 2-touch
#     fan member into a confirmed 3+-touch line. ≈ $0.95 on the same
#     gold price.
TOLERANCE_FRACTION = 0.00005
TOUCH_TOLERANCE_FRACTION = 0.0002
BREAK_STALE_BARS = 20
MIN_TOUCHES = 3   # 3rd-touch entry trigger

_PT = ZoneInfo("America/Los_Angeles")


@dataclass
class TrendLine:
    """One survived SR line. Mirrors the frontend's ``SRLine``
    interface. ``slope`` and ``intercept`` are in (bar-index, price)
    space; ``value_at(idx)`` is the line's price at any bar index."""
    type: str          # "support" | "resistance"
    from_idx: int      # frontend: fromIdx (== Q anchor)
    anchor_b_idx: int  # frontend: anchorBIdx (== P anchor)
    to_idx: int        # frontend: toIdx (== breakIdx or lastBarIdx)
    slope: float
    intercept: float
    touches: int
    break_idx: int | None

    def value_at(self, idx: int) -> float:
        return self.slope * idx + self.intercept


def find_pivot_lows(
    closes: list[float],
    min_pivot_strength: float = 0.0,
) -> list[int]:
    """Strict 1/1 local minima on the close polyline.

    ``min_pivot_strength`` (optional, price units): require BOTH
    neighbour-deltas to clear this threshold. With ``0`` it stays
    the bare 1/1 rule; with a positive value it rejects
    micro-blips where the central close is only fractionally below
    its neighbours."""
    out: list[int] = []
    for i in range(1, len(closes) - 1):
        v, l, r = closes[i], closes[i - 1], closes[i + 1]
        if v < l and v < r:
            if min_pivot_strength > 0:
                if (l - v) < min_pivot_strength or (r - v) < min_pivot_strength:
                    continue
            out.append(i)
    return out


def find_pivot_highs(
    closes: list[float],
    min_pivot_strength: float = 0.0,
) -> list[int]:
    """Strict 1/1 local maxima on the close polyline.

    ``min_pivot_strength`` (optional, price units): same semantics as
    ``find_pivot_lows`` — both neighbour-deltas must clear it."""
    out: list[int] = []
    for i in range(1, len(closes) - 1):
        v, l, r = closes[i], closes[i - 1], closes[i + 1]
        if v > l and v > r:
            if min_pivot_strength > 0:
                if (v - l) < min_pivot_strength or (v - r) < min_pivot_strength:
                    continue
            out.append(i)
    return out


def crosses_any(slope: float, intercept: float, from_idx: int,
                to_idx: int, others: list[TrendLine]) -> bool:
    """Does a candidate (slope, intercept) line cross any line in
    ``others`` within the overlap of their drawn ranges? Mirrors the
    frontend's ``crossesAny``. Each existing line's range is
    [from_idx, to_idx], where to_idx = break_idx or last_bar_idx —
    so a candidate crossing AFTER its anchor still gets rejected if
    the existing line's drawn range covers that x."""
    for e in others:
        lo = max(from_idx, e.from_idx)
        hi = min(to_idx, e.to_idx)
        if lo >= hi:
            continue
        dm = slope - e.slope
        if abs(dm) < EPS:
            continue
        x = (e.intercept - intercept) / dm
        if lo + EPS < x < hi - EPS:
            return True
    return False


def earliest_crossing(slope: float, intercept: float, from_idx: int,
                      to_idx: int, others: list[TrendLine]) -> int | None:
    """First integer x in [from_idx, to_idx] where the candidate
    crosses any line in ``others``. Used to set ``break_idx`` on the
    post-P extension when the line gets cut off by another fan member
    before it would break against the polyline. Mirrors the frontend's
    ``earliestCrossing``."""
    earliest: int | None = None
    for e in others:
        lo = max(from_idx, e.from_idx)
        hi = min(to_idx, e.to_idx)
        if lo > hi:
            continue
        dm = slope - e.slope
        if abs(dm) < EPS:
            continue
        x = (e.intercept - intercept) / dm
        if x < lo - EPS or x > hi + EPS:
            continue
        # Frontend uses Math.ceil so a fractional x rounds up to the
        # next integer bar that the line "lands on" first.
        import math
        idx = int(math.ceil(x))
        if from_idx <= idx <= to_idx:
            if earliest is None or idx < earliest:
                earliest = idx
    return earliest


def _detect_one_side(closes: list[float], pivots: list[int],
                     type_: str, *,
                     tolerance_fraction: float,
                     touch_tolerance_fraction: float,
                     break_stale_bars: int,
                     near_touch_tolerance_fraction: float | None = None,
                     ) -> list[TrendLine]:
    """Per-side fan detection. ``type_`` is "support" or "resistance".
    Direct port of the frontend's ``detectOneSide``.

    ``near_touch_tolerance_fraction`` (optional): when set and > the
    base ``touch_tolerance_fraction``, additional pivots within this
    looser band count as further touches — but only after a line has
    already accumulated ``MIN_TOUCHES`` strict touches. Used by the
    backtest harness to evaluate the 4th-and-later "near touch on an
    established trendline" rule without altering live behavior."""
    out: list[TrendLine] = []
    last_bar_idx = len(closes) - 1
    if last_bar_idx < 2 or len(pivots) < 2:
        return out
    avg_close = sum(closes) / max(1, len(closes))
    tol = max(EPS, avg_close * tolerance_fraction)
    touch_tol = max(tol, avg_close * touch_tolerance_fraction)
    near_touch_tol: float | None = None
    if (near_touch_tolerance_fraction is not None
            and near_touch_tolerance_fraction > touch_tolerance_fraction):
        near_touch_tol = max(touch_tol,
                              avg_close * near_touch_tolerance_fraction)

    def violates(close_v: float, line_v: float) -> bool:
        return (close_v < line_v - tol) if type_ == "support" \
            else (close_v > line_v + tol)

    # Walk pivots newest → oldest.
    for pi in range(len(pivots) - 1, 0, -1):
        P = pivots[pi]

        # Build the fan of (Q, P) candidates.
        candidates: list[tuple[int, float]] = []
        for qi in range(pi - 1, -1, -1):
            q = pivots[qi]
            slope = (closes[P] - closes[q]) / (P - q)
            candidates.append((q, slope))

        # Steepest first. Support → DESC by slope; resistance → ASC.
        # Tiebreak by older Q first so coincident-line dedup keeps
        # the longest member of a collinear set.
        if type_ == "support":
            candidates.sort(key=lambda c: (-c[1], c[0]))
        else:
            candidates.sort(key=lambda c: (c[1], c[0]))

        for q, slope in candidates:
            intercept = closes[P] - slope * P

            # Channel rule against the chart polyline between Q and P.
            valid = True
            for i in range(q + 1, P):
                if violates(closes[i], slope * i + intercept):
                    valid = False
                    break
            if not valid:
                continue

            # Channel rule against already-emitted lines between Q and P.
            if crosses_any(slope, intercept, q, P, out):
                continue

            # Post-P break detection on the polyline.
            break_idx: int | None = None
            for i in range(P + 1, last_bar_idx + 1):
                if violates(closes[i], slope * i + intercept):
                    break_idx = i
                    break

            # Post-P break detection against existing lines —
            # whichever comes first wins.
            x_cross = earliest_crossing(slope, intercept, P + 1,
                                        last_bar_idx, out)
            if x_cross is not None and (break_idx is None or x_cross < break_idx):
                break_idx = x_cross

            if break_idx is not None and (last_bar_idx - break_idx) > break_stale_bars:
                continue

            # Coincident-line dedup (same slope AND intercept within
            # EPS). Three or more collinear pivots produce identical
            # lines through every (Q, P) pair; without this guard the
            # output would carry overlapping duplicates.
            coincident = any(
                abs(e.slope - slope) < EPS and abs(e.intercept - intercept) < EPS
                for e in out
            )
            if coincident:
                continue

            # Touch counting — looser tolerance than channel/break,
            # range covers q..last_bar_idx so post-P retests count.
            # Mirrors the frontend: count ALL pivots within touch_tol
            # of the line, then min-cap at 2 (Q and P always count by
            # construction).
            #
            # Optional 4th+ near-touch rule (``near_touch_tol``):
            # once the line accumulates ``MIN_TOUCHES`` strict
            # touches, further pivots within the wider ``near_touch_tol``
            # band also count. The first three touches stay strict —
            # so line construction quality is unchanged; only the
            # confirmation count widens. ``None`` leaves the legacy
            # single-tolerance behavior in place.
            strict_touches = 0
            loose_touches = 0
            for piv_idx in pivots:
                if piv_idx < q or piv_idx > last_bar_idx:
                    continue
                line_at = slope * piv_idx + intercept
                delta = abs(closes[piv_idx] - line_at)
                if delta <= touch_tol:
                    strict_touches += 1
                    loose_touches += 1
                elif near_touch_tol is not None and delta <= near_touch_tol:
                    loose_touches += 1
            if near_touch_tol is not None and strict_touches >= MIN_TOUCHES:
                touches = loose_touches
            else:
                touches = strict_touches
            if touches < 2:
                touches = 2

            out.append(TrendLine(
                type=type_,
                from_idx=q,
                anchor_b_idx=P,
                to_idx=break_idx if break_idx is not None else last_bar_idx,
                slope=slope,
                intercept=intercept,
                touches=touches,
                break_idx=break_idx,
            ))

    return out


def detect_support_resistance(
    closes: list[float],
    *,
    tolerance_fraction: float = TOLERANCE_FRACTION,
    touch_tolerance_fraction: float = TOUCH_TOLERANCE_FRACTION,
    break_stale_bars: int = BREAK_STALE_BARS,
    near_touch_tolerance_fraction: float | None = None,
) -> list[TrendLine]:
    """Top-level: run support and resistance fan detection on the
    same close series and return all surviving lines from both sides.
    Each side has its own ``out`` array internally (a support and a
    resistance can cross — triangle apex / converging channel — which
    is visually correct)."""
    if len(closes) < 3:
        return []
    lows = find_pivot_lows(closes)
    highs = find_pivot_highs(closes)
    out = _detect_one_side(
        closes, lows, "support",
        tolerance_fraction=tolerance_fraction,
        touch_tolerance_fraction=touch_tolerance_fraction,
        break_stale_bars=break_stale_bars,
        near_touch_tolerance_fraction=near_touch_tolerance_fraction,
    )
    out.extend(_detect_one_side(
        closes, highs, "resistance",
        tolerance_fraction=tolerance_fraction,
        touch_tolerance_fraction=touch_tolerance_fraction,
        break_stale_bars=break_stale_bars,
        near_touch_tolerance_fraction=near_touch_tolerance_fraction,
    ))
    return out


def detect_lines(closes: list[float], up_to: int, type_: str,
                 *,
                 tolerance_fraction: float = TOLERANCE_FRACTION,
                 touch_tolerance_fraction: float = TOUCH_TOLERANCE_FRACTION,
                 break_stale_bars: int = BREAK_STALE_BARS,
                 near_touch_tolerance_fraction: float | None = None,
                 min_pivot_strength: float = 0.0,
                 ) -> list[TrendLine]:
    """Compatibility shim used by ``chart_signal`` and the backtest.
    Returns only lines of one side (matches the old single-side
    function), slicing the input to ``closes[:up_to+1]`` first to
    preserve the "no look-ahead" guarantee the backtest relies on.

    ``min_pivot_strength`` (optional, price units) filters out
    micro-pivots whose smaller-side delta is below the threshold.
    See ``find_pivot_lows`` / ``find_pivot_highs``."""
    if up_to < 2:
        return []
    sub = closes[: up_to + 1]
    pivots = (
        find_pivot_lows(sub, min_pivot_strength=min_pivot_strength)
        if type_ == "support"
        else find_pivot_highs(sub, min_pivot_strength=min_pivot_strength)
    )
    return _detect_one_side(
        sub, pivots, type_,
        tolerance_fraction=tolerance_fraction,
        touch_tolerance_fraction=touch_tolerance_fraction,
        break_stale_bars=break_stale_bars,
        near_touch_tolerance_fraction=near_touch_tolerance_fraction,
    )


@dataclass
class Wedge:
    """A converging support/resistance pair forming a triangle/wedge.

    ``apex_bars_ahead`` is the integer number of bars from ``last_idx``
    to the apex (the point where the two lines intersect). Computed
    via ``floor(apex_idx_float - last_idx)`` so an apex landing
    half-way through the current bar reports as ``0``.

    ``overlap_start_idx`` is the leftmost bar where both lines were
    defined — used as the left edge of the wedge trapezoid the
    frontend renders.
    """
    support: TrendLine
    resistance: TrendLine
    apex_idx_float: float    # exact intersection point in bar-index space
    apex_bars_ahead: int     # floor(apex_idx_float - last_idx)
    overlap_start_idx: int   # max(support.from_idx, resistance.from_idx)


def find_wedges(
    supports: list[TrendLine],
    resistances: list[TrendLine],
    last_idx: int,
    *,
    max_apex_bars_ahead: int = 200,
    min_overlap_bars: int = 5,
    include_broken: bool = False,
) -> list[Wedge]:
    """Find converging (support, resistance) pairs forming a wedge.

    Canonical wedge detection — used by:
      - the bot's tight-triangle entry filter (see chart_signal.py)
      - the chart frontend's wedge-shading overlay (via API)
      - the backtest harness (so simulation matches live)

    Rules (mirror the operator's visual intuition):
      - Pair must be converging: ``support.slope > resistance.slope``
        (the support line catches up to the resistance from below).
      - Apex must lie in the future: ``apex_idx_float > last_idx``.
      - Apex must be within ``max_apex_bars_ahead`` (the wedge is
        "imminent enough to matter" — far-future apexes are usually
        artefacts of nearly-parallel lines).
      - The two lines must overlap on the x-axis by at least
        ``min_overlap_bars`` so the wedge has actual width.
      - At ``overlap_start_idx`` resistance must sit above support
        (real wedge shape, not crossed lines).

    By default broken lines are excluded — once a line is breached
    its "active" wedge interpretation is gone. Set ``include_broken``
    if you want to include them anyway (the chart sometimes does
    when the operator has "show broken" toggled on).

    Returns a list of ``Wedge``, sorted by ``apex_bars_ahead`` ascending
    (innermost wedge first), so ``[0]`` is the nearest apex.
    """
    out: list[Wedge] = []
    s_pool = [
        s for s in supports
        if include_broken or s.break_idx is None
    ]
    r_pool = [
        r for r in resistances
        if include_broken or r.break_idx is None
    ]
    for s in s_pool:
        for r in r_pool:
            d_slope = s.slope - r.slope
            if d_slope <= 1e-9:           # parallel or diverging
                continue
            apex_idx_float = (r.intercept - s.intercept) / d_slope
            if apex_idx_float <= last_idx:
                continue
            if apex_idx_float - last_idx > max_apex_bars_ahead:
                continue
            overlap_start = max(s.from_idx, r.from_idx)
            overlap_end = min(s.to_idx, r.to_idx)
            if overlap_end - overlap_start < min_overlap_bars:
                continue
            r_at_start = r.slope * overlap_start + r.intercept
            s_at_start = s.slope * overlap_start + s.intercept
            if r_at_start <= s_at_start:  # crossed, not a wedge
                continue
            apex_bars_ahead = int(apex_idx_float - last_idx)
            out.append(Wedge(
                support=s, resistance=r,
                apex_idx_float=apex_idx_float,
                apex_bars_ahead=apex_bars_ahead,
                overlap_start_idx=overlap_start,
            ))
    out.sort(key=lambda w: w.apex_bars_ahead)
    return out


def in_futures_deadzone(now: datetime) -> bool:
    """``True`` when ``now`` falls inside the futures maintenance window
    (14:00-15:00 Pacific). Naive datetimes are treated as server-local
    (the canonical zone per CLAUDE.md); aware datetimes are converted
    to PT first."""
    if now.tzinfo is None:
        local = now
    else:
        local = now.astimezone(_PT)
    return 14 <= local.hour < 15
