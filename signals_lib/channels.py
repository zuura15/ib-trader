"""Channel position and slope features."""

from __future__ import annotations

import numpy as np
import pandas as pd


def add_channel_features(frame: pd.DataFrame, window: int = 20, price_column: str = "close") -> pd.DataFrame:
    """Append channel position and slope features."""
    enriched = frame.copy()
    close = enriched[price_column].astype(float)

    rolling_high = enriched["high"].rolling(window=window, min_periods=window).max()
    rolling_low = enriched["low"].rolling(window=window, min_periods=window).min()
    channel_range = rolling_high - rolling_low

    # Position within the channel (0 = bottom, 1 = top)
    enriched["channel_position"] = (close - rolling_low) / channel_range.replace(0, float("nan"))

    # Linear regression slope of close over the window
    def _slope(values: np.ndarray) -> float:
        if len(values) < 2:
            return float("nan")
        x = np.arange(len(values), dtype=float)
        mask = ~np.isnan(values)
        if mask.sum() < 2:
            return float("nan")
        x_m, y_m = x[mask], values[mask]
        slope = np.polyfit(x_m, y_m, 1)[0]
        return slope / np.mean(np.abs(y_m)) if np.mean(np.abs(y_m)) > 0 else 0.0

    enriched["channel_slope"] = close.rolling(window=window, min_periods=window).apply(
        _slope, raw=True
    )

    return enriched
