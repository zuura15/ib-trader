"""Internal Bar Strength (IBS) and ultra-short RSI features."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def add_ibs_features(
    frame: pd.DataFrame,
    rsi_short_window: int = 2,
) -> pd.DataFrame:
    """Append IBS and RSI(2) features.

    IBS = (close - low) / (high - low). Measures where the bar closed in its range.
    RSI(2) = ultra-short RSI that catches extreme oversold on very recent bars.
    """
    enriched = frame.copy()
    high = enriched["high"].astype(float)
    low = enriched["low"].astype(float)
    close = enriched["close"].astype(float)

    bar_range = high - low
    safe_range = bar_range.replace(0, np.nan)
    enriched["ibs"] = (close - low) / safe_range

    # RSI(2)
    delta = close.diff()
    gains = delta.clip(lower=0.0)
    losses = -delta.clip(upper=0.0)

    rsi2 = pd.Series(np.nan, index=enriched.index, dtype=float)
    if len(close) > rsi_short_window:
        avg_gain = gains.iloc[1 : rsi_short_window + 1].mean()
        avg_loss = losses.iloc[1 : rsi_short_window + 1].mean()

        for i in range(rsi_short_window, len(close)):
            if i > rsi_short_window:
                avg_gain = ((avg_gain * (rsi_short_window - 1)) + gains.iloc[i]) / rsi_short_window
                avg_loss = ((avg_loss * (rsi_short_window - 1)) + losses.iloc[i]) / rsi_short_window
            if avg_loss == 0:
                rsi2.iloc[i] = 100.0 if avg_gain > 0 else 50.0
            else:
                rs = avg_gain / avg_loss
                rsi2.iloc[i] = 100 - (100 / (1 + rs))

    enriched["rsi2"] = rsi2

    # Consecutive down bars
    down = (close < close.shift(1)).astype(int)
    consec_down = down.groupby((down != down.shift()).cumsum()).cumsum()
    enriched["consec_down_bars"] = consec_down

    return enriched
