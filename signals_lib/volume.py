"""Volume-based features."""

from __future__ import annotations

import pandas as pd


def add_volume_features(frame: pd.DataFrame, window: int = 20) -> pd.DataFrame:
    """Append relative volume and volume z-score features."""
    enriched = frame.copy()
    vol = enriched["volume"].astype(float)

    rolling_mean = vol.rolling(window=window, min_periods=window).mean()
    rolling_std = vol.rolling(window=window, min_periods=window).std(ddof=1)

    enriched["rel_volume"] = vol / rolling_mean.replace(0, float("nan"))
    enriched["volume_zscore"] = (vol - rolling_mean) / rolling_std.replace(0, float("nan"))

    return enriched
