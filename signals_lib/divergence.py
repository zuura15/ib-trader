"""Pivot-based RSI divergence detection."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Pivot:
    index: int
    value: float


def _is_pivot_low(series: pd.Series, index: int, left: int, right: int) -> bool:
    window = series.iloc[index - left : index + right + 1]
    center = series.iloc[index]
    return center == window.min() and (window == center).sum() == 1


def _is_pivot_high(series: pd.Series, index: int, left: int, right: int) -> bool:
    window = series.iloc[index - left : index + right + 1]
    center = series.iloc[index]
    return center == window.max() and (window == center).sum() == 1


def _collect_pivots(series: pd.Series, left: int, right: int, pivot_type: str) -> list[Pivot]:
    pivots: list[Pivot] = []
    detector = _is_pivot_low if pivot_type == "low" else _is_pivot_high
    for index in range(left, len(series) - right):
        value = series.iloc[index]
        if pd.isna(value):
            continue
        if detector(series, index, left, right):
            pivots.append(Pivot(index=index, value=float(value)))
    return pivots


def _nearest_pivot(pivots: list[Pivot], target_index: int, max_distance: int) -> Pivot | None:
    candidates = [p for p in pivots if abs(p.index - target_index) <= max_distance]
    if not candidates:
        return None
    return min(candidates, key=lambda p: abs(p.index - target_index))


def _find_matching_pivot(
    pivots: list[Pivot], target_index: int, max_distance: int, before_index: int | None = None,
) -> Pivot | None:
    candidates = [
        p for p in pivots
        if abs(p.index - target_index) <= max_distance and (before_index is None or p.index < before_index)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda p: abs(p.index - target_index))


def add_rsi_divergence(
    frame: pd.DataFrame,
    price_column: str = "close",
    rsi_column: str = "rsi",
    left_span: int = 3,
    right_span: int = 3,
    max_alignment_bars: int = 12,
    max_pivot_lookback: int = 5,
) -> pd.DataFrame:
    """Append bullish and bearish RSI divergence flags."""
    enriched = frame.copy()
    enriched["bullish_divergence"] = False
    enriched["bearish_divergence"] = False
    label_index = enriched.index.to_list()

    price = enriched[price_column].astype(float).reset_index(drop=True)
    rsi = pd.to_numeric(enriched[rsi_column], errors="coerce").reset_index(drop=True)

    logger.info("  Collecting pivots on %d rows...", len(price))
    t0 = time.time()
    price_lows = _collect_pivots(price, left_span, right_span, "low")
    price_highs = _collect_pivots(price, left_span, right_span, "high")
    rsi_lows = _collect_pivots(rsi, left_span, right_span, "low")
    rsi_highs = _collect_pivots(rsi, left_span, right_span, "high")
    logger.info("  Found %d price lows, %d price highs, %d RSI lows, %d RSI highs (%.1fs)",
                len(price_lows), len(price_highs), len(rsi_lows), len(rsi_highs), time.time() - t0)

    for current_idx, current_price in enumerate(price_lows):
        current_rsi = _nearest_pivot(rsi_lows, current_price.index, max_alignment_bars)
        if current_rsi is None:
            continue
        for previous_price in reversed(price_lows[max(0, current_idx - max_pivot_lookback) : current_idx]):
            previous_rsi = _find_matching_pivot(
                rsi_lows, previous_price.index, max_alignment_bars, before_index=current_rsi.index,
            )
            if previous_rsi is None:
                continue
            if current_price.value < previous_price.value and current_rsi.value > previous_rsi.value:
                enriched.loc[label_index[current_price.index], "bullish_divergence"] = True
                break

    for current_idx, current_price in enumerate(price_highs):
        current_rsi = _nearest_pivot(rsi_highs, current_price.index, max_alignment_bars)
        if current_rsi is None:
            continue
        for previous_price in reversed(price_highs[max(0, current_idx - max_pivot_lookback) : current_idx]):
            previous_rsi = _find_matching_pivot(
                rsi_highs, previous_price.index, max_alignment_bars, before_index=current_rsi.index,
            )
            if previous_rsi is None:
                continue
            if current_price.value > previous_price.value and current_rsi.value < previous_rsi.value:
                enriched.loc[label_index[current_price.index], "bearish_divergence"] = True
                break

    return enriched
