"""Fuzzy SR / channel / curve detection — Layer-2 indicator pack.

Sits next to ``sr_fan.py`` (which stays the canonical strict-3-touch
detector) so we can A/B them on the same bar history without breaking
the live bot. Used by:

  - ``ib_trader.bots.strategies.fuzzy_signal`` (the experimental bot).
  - The ``/api/sr/fuzzy`` endpoint that the chart fetches for overlays.

Three detectors live here:

  1. ``find_pivots_scored``    — scipy ``find_peaks`` with prominence /
     distance / width parameters; emits (idx, price, prominence) so the
     line fitters can weight pivots by structural significance instead
     of treating every 1/1 extremum equally.
  2. ``fit_fuzzy_lines``       — RANSAC linear fits across rolling
     pivot windows. ``inlier_mask_`` from the regressor is the
     "respected by N pivots" signal the operator described.
  3. ``find_parallel_channels`` — given fuzzy lines, pair S+R with
     similar slope into a channel band the price oscillates within.

Curves (polynomial / parabolic) are scaffolded via ``fit_curve_arc``
but disabled by default — wire them in once linear + channels prove out.

Dependencies: ``scikit-learn`` (RANSAC + Huber), ``scipy`` (find_peaks),
``numpy``. No pandas dependency — keeps this importable in lightweight
contexts. Callers pass plain numpy arrays.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

import numpy as np
from scipy.signal import find_peaks, peak_prominences  # noqa: F401
from sklearn.linear_model import RANSACRegressor


# ---------------------------------------------------------------------------
# Defaults — tunable from the bot config or the API endpoint.
# ---------------------------------------------------------------------------

# scipy.find_peaks prominence — fraction of (max - min) of the close
# polyline in the lookback window. 0.002 ≈ a $58 swing on a 29k MNQ bar
# series. Anything smaller is noise; anything bigger keeps only the
# structural pivots.
PIVOT_PROMINENCE_FRACTION = 0.002

# Minimum distance between pivots (in bars). Default 2 lets every other
# bar qualify; bump up to enforce "no pivots within N bars of each other".
PIVOT_MIN_DISTANCE_BARS = 2

# RANSAC residual threshold — how far (in price units) a pivot can sit
# from the candidate line and still count as an inlier. Expressed as a
# fraction of the rolling price range; converted to absolute units when
# fitting. 0.0008 ≈ $23 on MNQ at 29k.
RANSAC_RESIDUAL_FRACTION = 0.0008

# Minimum pivot inlier count for a fuzzy line to be reported. 3 keeps
# the same floor as the strict 3-touch fan; raise to 4-5 to be picky.
MIN_INLIERS = 3

# Rolling window for RANSAC fits, in bars. The fit runs on pivots
# inside [bar_idx - WINDOW, bar_idx]; a smaller window catches local
# structure (cleaner lines, faster decay); a larger one rewards
# multi-hour respect.
RANSAC_WINDOW_BARS = 80

# Channel pairing: two lines pair into a channel iff their slopes
# differ by less than this fraction-of-range and they're on opposite
# sides (one support, one resistance) and roughly overlap in time.
CHANNEL_SLOPE_TOLERANCE = 0.25  # 25% of the larger slope

# Channel must span at least this many bars to be reported (filters
# tiny coincidental parallels).
CHANNEL_MIN_SPAN_BARS = 15


# ---------------------------------------------------------------------------
# Dataclasses — what the chart / bot consumes.
# ---------------------------------------------------------------------------

@dataclass
class ScoredPivot:
    """One pivot with structural significance."""
    idx: int
    price: float
    kind: str               # "high" | "low"
    prominence: float       # scipy's prominence value, in price units
    width: float            # bars at half-prominence
    rank: float             # 0..1, normalized prominence within the window


@dataclass
class FuzzyLine:
    """A RANSAC-fitted line through scored pivots. ``inlier_count`` is the
    "respected by N pivots" score — higher = more structural confidence."""
    type: str               # "support" | "resistance"
    slope: float            # price per bar
    intercept: float        # price at bar idx 0
    from_idx: int           # leftmost inlier index
    to_idx: int             # rightmost inlier index
    inlier_count: int
    inlier_idxs: list[int]  # indices of pivots the line fits
    residual_threshold: float  # the price-unit tolerance used in the fit
    score: float            # composite quality score, 0..1
    age_bars: int           # duration the line has been "respected"

    def value_at(self, idx: int | float) -> float:
        return self.slope * float(idx) + self.intercept


@dataclass
class Channel:
    """A parallel S+R pair the price oscillates within."""
    support: FuzzyLine
    resistance: FuzzyLine
    width_at_mid: float     # price-distance between the two lines at midpoint
    slope_diff: float       # absolute slope difference (price/bar)
    span_bars: int          # time span where both lines are valid
    score: float            # composite, 0..1


@dataclass
class FuzzyDetection:
    """Top-level container returned by the entrypoint."""
    pivots: list[ScoredPivot] = field(default_factory=list)
    lines: list[FuzzyLine] = field(default_factory=list)
    channels: list[Channel] = field(default_factory=list)
    config: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pivot scoring — scipy.signal.find_peaks with prominence/width.
# ---------------------------------------------------------------------------

def find_pivots_scored(
    closes: Sequence[float],
    *,
    prominence_fraction: float = PIVOT_PROMINENCE_FRACTION,
    min_distance_bars: int = PIVOT_MIN_DISTANCE_BARS,
) -> list[ScoredPivot]:
    """Detect pivot highs and lows with prominence + width scoring.

    Returns pivots sorted by idx. ``prominence`` is scipy's vertical
    drop on either side until a higher peak is hit; ``width`` is the
    bar count at half-prominence (filters noisy tight wiggles).
    """
    arr = np.asarray(closes, dtype=float)
    if arr.size < 3:
        return []

    price_range = float(arr.max() - arr.min())
    if price_range <= 0:
        return []
    abs_prominence = prominence_fraction * price_range

    # Highs
    high_idxs, high_props = find_peaks(
        arr, prominence=abs_prominence, distance=min_distance_bars,
    )
    # Lows — invert the signal.
    low_idxs, low_props = find_peaks(
        -arr, prominence=abs_prominence, distance=min_distance_bars,
    )

    # Width (bars at half-prominence). ``peak_widths`` returns widths
    # in fractional-bar units; we keep as-is.
    from scipy.signal import peak_widths
    high_widths = peak_widths(arr, high_idxs, rel_height=0.5)[0] if len(high_idxs) else np.array([])
    low_widths = peak_widths(-arr, low_idxs, rel_height=0.5)[0] if len(low_idxs) else np.array([])

    out: list[ScoredPivot] = []
    max_prom_high = float(high_props["prominences"].max()) if len(high_idxs) else 1.0
    max_prom_low = float(low_props["prominences"].max()) if len(low_idxs) else 1.0
    max_prom = max(max_prom_high, max_prom_low, 1e-9)

    for i, (idx, prom) in enumerate(zip(high_idxs, high_props["prominences"])):
        out.append(ScoredPivot(
            idx=int(idx), price=float(arr[idx]), kind="high",
            prominence=float(prom),
            width=float(high_widths[i]) if i < len(high_widths) else 0.0,
            rank=float(prom) / max_prom,
        ))
    for i, (idx, prom) in enumerate(zip(low_idxs, low_props["prominences"])):
        out.append(ScoredPivot(
            idx=int(idx), price=float(arr[idx]), kind="low",
            prominence=float(prom),
            width=float(low_widths[i]) if i < len(low_widths) else 0.0,
            rank=float(prom) / max_prom,
        ))
    out.sort(key=lambda p: p.idx)
    return out


# ---------------------------------------------------------------------------
# Fuzzy line fitting — RANSAC across rolling pivot windows.
# ---------------------------------------------------------------------------

def _ransac_fit_pivots(
    pivot_idxs: np.ndarray,
    pivot_prices: np.ndarray,
    residual_threshold: float,
    min_samples: int = 2,
    max_trials: int = 100,
) -> tuple[float, float, np.ndarray] | None:
    """Run a single RANSAC fit. Returns (slope, intercept, inlier_mask)
    or None if the regressor fails (degenerate, too few points)."""
    if len(pivot_idxs) < max(min_samples, 2):
        return None
    X = pivot_idxs.reshape(-1, 1).astype(float)
    y = pivot_prices.astype(float)
    try:
        reg = RANSACRegressor(
            residual_threshold=residual_threshold,
            min_samples=min_samples,
            max_trials=max_trials,
            random_state=42,
        )
        reg.fit(X, y)
    except Exception:
        return None
    slope = float(reg.estimator_.coef_[0])
    intercept = float(reg.estimator_.intercept_)
    inlier_mask = reg.inlier_mask_.astype(bool)
    return slope, intercept, inlier_mask


def fit_fuzzy_lines(
    closes: Sequence[float],
    pivots: list[ScoredPivot],
    *,
    residual_fraction: float = RANSAC_RESIDUAL_FRACTION,
    min_inliers: int = MIN_INLIERS,
    window_bars: int = RANSAC_WINDOW_BARS,
) -> list[FuzzyLine]:
    """For each pivot kind (high → resistance, low → support), run RANSAC
    on rolling pivot windows and emit lines that pass the inlier floor.

    The same pivot can appear in multiple lines (different windows can
    fit through different inlier subsets). We dedup at the end on
    (slope, intercept) within a small tolerance.
    """
    if not pivots or len(closes) < 4:
        return []
    arr = np.asarray(closes, dtype=float)
    price_range = float(arr.max() - arr.min())
    if price_range <= 0:
        return []
    residual_threshold = residual_fraction * price_range

    lines_by_type: dict[str, list[FuzzyLine]] = {"support": [], "resistance": []}

    for kind, line_type in (("low", "support"), ("high", "resistance")):
        kind_pivots = [p for p in pivots if p.kind == kind]
        if len(kind_pivots) < min_inliers:
            continue
        idxs = np.array([p.idx for p in kind_pivots])
        prices = np.array([p.price for p in kind_pivots])

        last_bar = len(arr) - 1
        # Walk rolling windows ending at each pivot's idx; the right
        # edge of the latest window is the latest bar so a freshly
        # forming line is considered.
        window_ends = sorted(set(list(idxs) + [last_bar]))
        seen: set[tuple[int, int]] = set()
        for end in window_ends:
            lo = end - window_bars
            mask = (idxs >= lo) & (idxs <= end)
            if mask.sum() < min_inliers:
                continue
            sub_idxs = idxs[mask]
            sub_prices = prices[mask]
            fit = _ransac_fit_pivots(
                sub_idxs, sub_prices,
                residual_threshold=residual_threshold,
                min_samples=2,
            )
            if fit is None:
                continue
            slope, intercept, inlier_mask = fit
            n_inliers = int(inlier_mask.sum())
            if n_inliers < min_inliers:
                continue
            inlier_pivot_idxs = sub_idxs[inlier_mask].astype(int).tolist()
            key = (int(round(slope * 1e4)), int(round(intercept * 1e2)))
            if key in seen:
                continue
            seen.add(key)
            from_idx = min(inlier_pivot_idxs)
            to_idx = max(inlier_pivot_idxs)
            # Composite score — heavier on inlier count, lighter on age.
            age = to_idx - from_idx
            score = min(1.0, (n_inliers / 6.0) * 0.7
                        + (min(age, window_bars) / window_bars) * 0.3)
            lines_by_type[line_type].append(FuzzyLine(
                type=line_type,
                slope=slope,
                intercept=intercept,
                from_idx=from_idx,
                to_idx=to_idx,
                inlier_count=n_inliers,
                inlier_idxs=inlier_pivot_idxs,
                residual_threshold=residual_threshold,
                score=score,
                age_bars=age,
            ))

    # Keep the top-N by score per type so the chart doesn't get spaghetti.
    out: list[FuzzyLine] = []
    for line_type, lines in lines_by_type.items():
        lines.sort(key=lambda L: L.score, reverse=True)
        out.extend(lines[:5])
    return out


# ---------------------------------------------------------------------------
# Channels — pair S+R with similar slope.
# ---------------------------------------------------------------------------

def find_parallel_channels(
    lines: list[FuzzyLine],
    *,
    slope_tolerance: float = CHANNEL_SLOPE_TOLERANCE,
    min_span_bars: int = CHANNEL_MIN_SPAN_BARS,
) -> list[Channel]:
    """Pair each support with each resistance whose slope is within
    ``slope_tolerance``; emit those that overlap in time for at least
    ``min_span_bars``."""
    supports = [L for L in lines if L.type == "support"]
    resistances = [L for L in lines if L.type == "resistance"]
    out: list[Channel] = []
    for s in supports:
        for r in resistances:
            denom = max(abs(s.slope), abs(r.slope), 1e-6)
            slope_diff = abs(s.slope - r.slope)
            if slope_diff / denom > slope_tolerance:
                continue
            overlap_lo = max(s.from_idx, r.from_idx)
            overlap_hi = min(s.to_idx, r.to_idx)
            span = overlap_hi - overlap_lo
            if span < min_span_bars:
                continue
            mid = (overlap_lo + overlap_hi) / 2.0
            width_at_mid = abs(r.value_at(mid) - s.value_at(mid))
            score = min(1.0,
                        (1.0 - slope_diff / denom) * 0.5
                        + min(span, 60) / 60 * 0.3
                        + ((s.score + r.score) / 2.0) * 0.2)
            out.append(Channel(
                support=s, resistance=r,
                width_at_mid=width_at_mid,
                slope_diff=slope_diff,
                span_bars=span,
                score=score,
            ))
    out.sort(key=lambda c: c.score, reverse=True)
    return out[:5]


# ---------------------------------------------------------------------------
# Top-level entrypoint.
# ---------------------------------------------------------------------------

def detect_fuzzy(
    closes: Sequence[float],
    *,
    prominence_fraction: float = PIVOT_PROMINENCE_FRACTION,
    min_distance_bars: int = PIVOT_MIN_DISTANCE_BARS,
    residual_fraction: float = RANSAC_RESIDUAL_FRACTION,
    min_inliers: int = MIN_INLIERS,
    window_bars: int = RANSAC_WINDOW_BARS,
    slope_tolerance: float = CHANNEL_SLOPE_TOLERANCE,
    min_span_bars: int = CHANNEL_MIN_SPAN_BARS,
) -> FuzzyDetection:
    """Run pivot scoring → fuzzy line fit → channel pairing on a single
    close polyline. Pure function; no side effects."""
    pivots = find_pivots_scored(
        closes,
        prominence_fraction=prominence_fraction,
        min_distance_bars=min_distance_bars,
    )
    lines = fit_fuzzy_lines(
        closes, pivots,
        residual_fraction=residual_fraction,
        min_inliers=min_inliers,
        window_bars=window_bars,
    )
    channels = find_parallel_channels(
        lines,
        slope_tolerance=slope_tolerance,
        min_span_bars=min_span_bars,
    )
    return FuzzyDetection(
        pivots=pivots, lines=lines, channels=channels,
        config={
            "prominence_fraction": prominence_fraction,
            "min_distance_bars": min_distance_bars,
            "residual_fraction": residual_fraction,
            "min_inliers": min_inliers,
            "window_bars": window_bars,
            "slope_tolerance": slope_tolerance,
            "min_span_bars": min_span_bars,
        },
    )


# ---------------------------------------------------------------------------
# Polynomial / parabolic curve fit (scaffold — wired but off by default).
# ---------------------------------------------------------------------------

def fit_curve_arc(
    closes: Sequence[float],
    pivots: list[ScoredPivot],
    *,
    degree: int = 2,
    kind: str = "low",
    min_pivots: int = 4,
) -> tuple[np.ndarray, list[int]] | None:
    """Fit a polynomial of ``degree`` (default 2 = parabola) through
    pivots of the given kind. Returns (coefficients, pivot_idxs) or
    None if not enough pivots. Coefficients are highest-degree-first
    per numpy.polyfit convention.

    Not yet wired into ``detect_fuzzy`` — call directly when we want
    to A/B parabolic support against linear RANSAC.
    """
    kind_pivots = [p for p in pivots if p.kind == kind]
    if len(kind_pivots) < min_pivots:
        return None
    idxs = np.array([p.idx for p in kind_pivots], dtype=float)
    prices = np.array([p.price for p in kind_pivots], dtype=float)
    coeffs = np.polyfit(idxs, prices, deg=degree)
    return coeffs, [p.idx for p in kind_pivots]
