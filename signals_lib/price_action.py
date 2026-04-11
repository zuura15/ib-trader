"""Price action and candle structure features."""

from __future__ import annotations

import pandas as pd


def add_price_action_features(frame: pd.DataFrame, price_column: str = "close") -> pd.DataFrame:
    """Append price action features to a DataFrame."""
    enriched = frame.copy()
    close = enriched[price_column].astype(float)

    # Price change over 1 and 5 bars
    enriched["price_change_1"] = close.pct_change(1)
    enriched["price_change_5"] = close.pct_change(5)

    # Intrabar range as fraction of close
    enriched["intrabar_range"] = (enriched["high"] - enriched["low"]) / close

    # Body fraction: abs(open - close) / (high - low), how much of the bar is body
    bar_range = enriched["high"] - enriched["low"]
    body = (enriched["close"] - enriched["open"]).abs()
    enriched["body_fraction"] = body / bar_range.replace(0, float("nan"))

    # Higher highs / higher lows flags (rolling 5 bar window)
    enriched["higher_highs"] = (
        enriched["high"].rolling(5, min_periods=5).apply(
            lambda w: float(all(w.iloc[i] >= w.iloc[i - 1] for i in range(1, len(w)))), raw=False
        )
    ).fillna(0.0)

    enriched["higher_lows"] = (
        enriched["low"].rolling(5, min_periods=5).apply(
            lambda w: float(all(w.iloc[i] >= w.iloc[i - 1] for i in range(1, len(w)))), raw=False
        )
    ).fillna(0.0)

    return enriched
