"""Close-price trend detection for tight-range price action.

Uses only the close price series (line graph) to detect peaks and valleys,
then checks if they form an ascending pattern. Works better than OHLC
sawtooth detection in tight ranges where bar highs/lows are too close
together to form distinct swing points.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def add_close_trend_features(
    frame: pd.DataFrame,
    peak_window: int = 2,
    trend_points: int = 2,
    price_column: str = "close",
) -> pd.DataFrame:
    """Detect ascending trend from close-price peaks and valleys.

    A peak is a close that's higher than `peak_window` bars on each side.
    A valley is a close that's lower than `peak_window` bars on each side.
    A rising trend requires `trend_points` ascending valleys AND
    `trend_points` ascending peaks.

    With peak_window=2, a valley is confirmed 2 bars after it occurs
    (same look-ahead discipline as sawtooth).

    Args:
        frame: DataFrame with close price column.
        peak_window: Bars on each side to confirm a peak/valley (default 2).
        trend_points: Minimum ascending points to confirm trend (default 2).
        price_column: Column name for close price.

    Adds columns:
        close_trend_up: 1.0 if ascending trend detected, 0.0 otherwise.
        close_valley_bars_ago: Bars since the most recent confirmed valley.
        close_near_valley: 1.0 if price is within 0.15% of the last valley.
        close_trend_strength: How many consecutive ascending valleys (0, 1, 2, 3...).
    """
    enriched = frame.copy()
    close = enriched[price_column].astype(float).values
    n = len(close)

    # Step 1: Detect peaks and valleys on close prices
    is_peak = np.zeros(n, dtype=bool)
    is_valley = np.zeros(n, dtype=bool)

    for i in range(peak_window, n - peak_window):
        window = close[i - peak_window: i + peak_window + 1]
        center = close[i]

        if center == window.max() and np.sum(window == center) == 1:
            is_peak[i] = True
        if center == window.min() and np.sum(window == center) == 1:
            is_valley[i] = True

    # Step 2: Build features with confirmation delay
    close_trend_up = np.zeros(n, dtype=float)
    close_valley_bars_ago = np.full(n, np.nan)
    close_near_valley = np.zeros(n, dtype=float)
    close_trend_strength = np.zeros(n, dtype=float)

    confirmed_peaks: list[tuple[int, float]] = []
    confirmed_valleys: list[tuple[int, float]] = []
    max_keep = trend_points + 3

    for i in range(n):
        # Confirmation delay: a peak/valley at bar j is confirmed at bar j + peak_window
        confirm_bar = i - peak_window
        if confirm_bar >= 0:
            if is_peak[confirm_bar]:
                confirmed_peaks.append((confirm_bar, close[confirm_bar]))
                if len(confirmed_peaks) > max_keep:
                    confirmed_peaks = confirmed_peaks[-max_keep:]
            if is_valley[confirm_bar]:
                confirmed_valleys.append((confirm_bar, close[confirm_bar]))
                if len(confirmed_valleys) > max_keep:
                    confirmed_valleys = confirmed_valleys[-max_keep:]

        # Check ascending trend
        if len(confirmed_peaks) >= trend_points and len(confirmed_valleys) >= trend_points:
            last_peaks = [v for _, v in confirmed_peaks[-trend_points:]]
            peaks_ascending = all(
                last_peaks[j] > last_peaks[j - 1] for j in range(1, len(last_peaks))
            )

            last_valleys = [v for _, v in confirmed_valleys[-trend_points:]]
            valleys_ascending = all(
                last_valleys[j] > last_valleys[j - 1] for j in range(1, len(last_valleys))
            )

            if peaks_ascending and valleys_ascending:
                close_trend_up[i] = 1.0

                # Count consecutive ascending valleys for strength
                all_vals = [v for _, v in confirmed_valleys]
                strength = 0
                for j in range(len(all_vals) - 1, 0, -1):
                    if all_vals[j] > all_vals[j - 1]:
                        strength += 1
                    else:
                        break
                close_trend_strength[i] = strength

        # Distance from most recent confirmed valley
        if confirmed_valleys:
            last_valley_idx, last_valley_val = confirmed_valleys[-1]
            close_valley_bars_ago[i] = i - last_valley_idx

            # Near valley: within 0.15% of valley price
            if last_valley_val > 0:
                pct_above = (close[i] - last_valley_val) / last_valley_val
                close_near_valley[i] = 1.0 if 0 <= pct_above < 0.0015 else 0.0

    enriched["close_trend_up"] = close_trend_up
    enriched["close_valley_bars_ago"] = close_valley_bars_ago
    enriched["close_near_valley"] = close_near_valley
    enriched["close_trend_strength"] = close_trend_strength

    n_trend = int(close_trend_up.sum())
    n_valleys = len(confirmed_valleys)
    logger.info("Close trend: %d bars in uptrend, %d valleys detected", n_trend, n_valleys)

    return enriched
