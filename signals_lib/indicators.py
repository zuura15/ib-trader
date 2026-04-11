"""Core technical indicators: RSI and Bollinger Bands."""

from __future__ import annotations

import numpy as np
import pandas as pd


# ---------------------------------------------------------------------------
# RSI (Wilder smoothing)
# ---------------------------------------------------------------------------

def compute_rsi(close: pd.Series, window: int = 14) -> pd.Series:
    """Compute Wilder RSI for a closing price series."""
    if window < 1:
        raise ValueError("RSI window must be at least 1.")

    price = close.astype(float)
    delta = price.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)

    rsi = pd.Series(np.nan, index=price.index, dtype=float)
    if len(price) <= window:
        return rsi

    initial_avg_gain = gains.iloc[1 : window + 1].mean()
    initial_avg_loss = losses.iloc[1 : window + 1].mean()

    avg_gain = initial_avg_gain
    avg_loss = initial_avg_loss
    rsi.iloc[window] = _rsi_value(avg_gain, avg_loss)

    for index in range(window + 1, len(price)):
        avg_gain = ((avg_gain * (window - 1)) + gains.iloc[index]) / window
        avg_loss = ((avg_loss * (window - 1)) + losses.iloc[index]) / window
        rsi.iloc[index] = _rsi_value(avg_gain, avg_loss)

    return rsi


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


def add_rsi(frame: pd.DataFrame, window: int = 14, price_column: str = "close") -> pd.DataFrame:
    """Append an RSI column to a DataFrame."""
    enriched = frame.copy()
    enriched["rsi"] = compute_rsi(enriched[price_column].astype(float), window=window)
    return enriched


# ---------------------------------------------------------------------------
# Bollinger Bands
# ---------------------------------------------------------------------------

def add_bollinger_bands(
    frame: pd.DataFrame,
    window: int = 20,
    std_dev: float = 2.0,
    price_column: str = "close",
) -> pd.DataFrame:
    """Append Bollinger Band columns to a DataFrame."""
    enriched = frame.copy()
    price = enriched[price_column].astype(float)

    rolling_mean = price.rolling(window=window, min_periods=window).mean()
    rolling_std = price.rolling(window=window, min_periods=window).std(ddof=1)

    upper = rolling_mean + std_dev * rolling_std
    lower = rolling_mean - std_dev * rolling_std
    band_range = upper - lower
    safe_middle = rolling_mean.mask(np.isclose(rolling_mean, 0.0), np.nan)
    safe_range = band_range.mask(np.isclose(band_range, 0.0), np.nan)

    enriched["bb_middle"] = rolling_mean
    enriched["bb_upper"] = upper
    enriched["bb_lower"] = lower
    enriched["bb_bandwidth"] = band_range / safe_middle
    enriched["bb_percent_b"] = (price - lower) / safe_range
    return enriched
