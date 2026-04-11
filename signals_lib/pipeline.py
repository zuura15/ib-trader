"""Master feature pipeline: computes all features in correct order."""

from __future__ import annotations

import logging
import time

import pandas as pd

from signals_lib.indicators import add_rsi, add_bollinger_bands
from signals_lib.price_action import add_price_action_features
from signals_lib.volume import add_volume_features
from signals_lib.divergence import add_rsi_divergence
from signals_lib.channels import add_channel_features
from signals_lib.time_filters import add_time_of_day_features
from signals_lib.sawtooth import add_sawtooth_features
from signals_lib.ibs import add_ibs_features


logger = logging.getLogger(__name__)

FEATURE_SET_VERSION = "2026.04.09.v1"

ALL_FEATURE_COLUMNS = [
    "rsi",
    "bb_middle", "bb_upper", "bb_lower", "bb_bandwidth", "bb_percent_b",
    "price_change_1", "price_change_5",
    "intrabar_range", "body_fraction",
    "higher_highs", "higher_lows",
    "rel_volume", "volume_zscore",
    "bullish_divergence", "bearish_divergence",
    "channel_position", "channel_slope",
    "time_of_day",
    "sawtooth_uptrend", "at_local_low", "bounce_after_dip", "bars_since_swing_low",
    "ibs", "rsi2", "consec_down_bars",
]


def _run_stage(name: str, func, enriched: pd.DataFrame, step: int, total: int, **kwargs) -> pd.DataFrame:
    """Run a single feature stage with timing and progress logging."""
    logger.debug("[%d/%d] Computing %s...", step, total, name)
    t0 = time.time()
    result = func(enriched, **kwargs)
    elapsed = time.time() - t0
    logger.debug("[%d/%d] %s done (%.1fs)", step, total, name, elapsed)
    return result


def build_features(frame: pd.DataFrame, price_column: str = "close") -> pd.DataFrame:
    """Apply the full feature pipeline to a bar DataFrame.

    Args:
        frame: DataFrame with OHLCV columns + timestamp_utc.
        price_column: Column name for close price.

    Returns:
        DataFrame with all feature columns appended.
    """
    total = 9
    logger.info("Building features on %d rows (version %s)", len(frame), FEATURE_SET_VERSION)
    t0 = time.time()

    enriched = frame.copy()

    # Order matters: RSI must come before divergence
    enriched = _run_stage("RSI (14)", add_rsi, enriched, 1, total, window=14, price_column=price_column)
    enriched = _run_stage("Bollinger Bands (20, 2σ)", add_bollinger_bands, enriched, 2, total, window=20, std_dev=2.0, price_column=price_column)
    enriched = _run_stage("Price Action", add_price_action_features, enriched, 3, total, price_column=price_column)
    enriched = _run_stage("Volume", add_volume_features, enriched, 4, total, window=20)
    enriched = _run_stage("RSI Divergence", add_rsi_divergence, enriched, 5, total, price_column=price_column, rsi_column="rsi")
    enriched = _run_stage("Channel", add_channel_features, enriched, 6, total, window=20, price_column=price_column)
    enriched = _run_stage("Time of Day", add_time_of_day_features, enriched, 7, total)
    enriched = _run_stage("Sawtooth", add_sawtooth_features, enriched, 8, total)
    enriched = _run_stage("IBS + RSI(2)", add_ibs_features, enriched, 9, total)

    elapsed = time.time() - t0
    logger.info("Feature pipeline complete: %d columns, %d rows (%.1fs total)", len(enriched.columns), len(enriched), elapsed)
    return enriched
