"""Sawtooth trend detection: uptrending structure with bounce identification.

IMPORTANT: Swing points require `swing_window` bars on each side for confirmation.
A swing low at bar `i` is only confirmed at bar `i + swing_window`. All downstream
features respect this delay to avoid look-ahead bias.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

logger = logging.getLogger(__name__)


def add_sawtooth_features(
    frame: pd.DataFrame,
    swing_window: int = 5,
    trend_swings: int = 3,
) -> pd.DataFrame:
    """Detect sawtooth uptrend structure and bounce-off-dip signals.

    Identifies swing highs and swing lows (with confirmation delay), checks
    whether recent confirmed swings form higher-highs + higher-lows, and
    flags bars where price has bounced up after a confirmed swing low in
    an uptrend.

    Args:
        frame: DataFrame with OHLC data
        swing_window: Bars on each side to confirm a swing point (default 5)
        trend_swings: Minimum number of swing pairs to confirm trend (default 3)
    """
    enriched = frame.copy()
    high = enriched["high"].astype(float).values
    low = enriched["low"].astype(float).values
    close = enriched["close"].astype(float).values
    n = len(high)

    # Step 1: Identify swing highs and swing lows
    swing_high_at = np.zeros(n, dtype=bool)
    swing_low_at = np.zeros(n, dtype=bool)

    for i in range(swing_window, n - swing_window):
        window_high = high[i - swing_window : i + swing_window + 1]
        if high[i] == window_high.max() and np.sum(window_high == high[i]) == 1:
            swing_high_at[i] = True

        window_low = low[i - swing_window : i + swing_window + 1]
        if low[i] == window_low.min() and np.sum(window_low == low[i]) == 1:
            swing_low_at[i] = True

    # Step 2: Build features with confirmation delay
    sawtooth_uptrend = np.zeros(n, dtype=bool)
    at_local_low = np.zeros(n, dtype=bool)
    bounce_after_dip = np.zeros(n, dtype=bool)
    bars_since_swing_low = np.full(n, np.nan)

    confirmed_swing_highs: list[tuple[int, float]] = []
    confirmed_swing_lows: list[tuple[int, float]] = []
    max_keep = trend_swings + 2

    for i in range(n):
        # A swing at bar j is confirmed at bar j + swing_window
        confirm_bar = i - swing_window
        if confirm_bar >= 0:
            if swing_high_at[confirm_bar]:
                confirmed_swing_highs.append((confirm_bar, high[confirm_bar]))
                if len(confirmed_swing_highs) > max_keep:
                    confirmed_swing_highs = confirmed_swing_highs[-max_keep:]
            if swing_low_at[confirm_bar]:
                confirmed_swing_lows.append((confirm_bar, low[confirm_bar]))
                if len(confirmed_swing_lows) > max_keep:
                    confirmed_swing_lows = confirmed_swing_lows[-max_keep:]

        # Sawtooth uptrend check
        if len(confirmed_swing_highs) >= trend_swings and len(confirmed_swing_lows) >= trend_swings:
            last_highs = [v for _, v in confirmed_swing_highs[-trend_swings:]]
            highs_ascending = all(last_highs[j] > last_highs[j - 1] for j in range(1, len(last_highs)))

            last_lows = [v for _, v in confirmed_swing_lows[-trend_swings:]]
            lows_ascending = all(last_lows[j] > last_lows[j - 1] for j in range(1, len(last_lows)))

            sawtooth_uptrend[i] = highs_ascending and lows_ascending

        # Distance from most recent confirmed swing low + price proximity
        if confirmed_swing_lows:
            last_sw_low_idx = confirmed_swing_lows[-1][0]
            last_sw_low_val = confirmed_swing_lows[-1][1]
            bars_since_swing_low[i] = i - last_sw_low_idx

            near_in_price = abs(close[i] - last_sw_low_val) / last_sw_low_val < 0.0015
            at_local_low[i] = near_in_price

        # Bounce after dip: in a sawtooth uptrend, a swing low was just confirmed
        # (within a few bars of confirmation), and price hasn't run away yet.
        if sawtooth_uptrend[i] and confirmed_swing_lows and i >= 1:
            last_sw_low_idx = confirmed_swing_lows[-1][0]
            last_sw_low_val = confirmed_swing_lows[-1][1]
            bars_since_confirm = i - (last_sw_low_idx + swing_window)

            just_confirmed = 0 <= bars_since_confirm <= 3
            pct_above_low = (close[i] - last_sw_low_val) / last_sw_low_val
            price_near_low = 0 <= pct_above_low < 0.003

            bounce_after_dip[i] = just_confirmed and price_near_low

    enriched["sawtooth_uptrend"] = sawtooth_uptrend.astype(float)
    enriched["at_local_low"] = at_local_low.astype(float)
    enriched["bounce_after_dip"] = bounce_after_dip.astype(float)
    enriched["bars_since_swing_low"] = bars_since_swing_low

    n_uptrend = sawtooth_uptrend.sum()
    n_bounce = bounce_after_dip.sum()
    logger.info("Sawtooth: %d bars in uptrend, %d bounce-after-dip signals (no look-ahead)",
                n_uptrend, n_bounce)

    return enriched
