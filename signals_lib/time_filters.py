"""Time-of-day features and session filters."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pandas as pd


PT = ZoneInfo("America/Los_Angeles")


def add_time_of_day_features(
    frame: pd.DataFrame,
    timestamp_col: str = "timestamp_utc",
) -> pd.DataFrame:
    """Append time-of-day feature as fractional hours since midnight PT.

    Produces `time_of_day` as a float (e.g. 9.5 = 9:30 AM, 13.25 = 1:15 PM).
    """
    enriched = frame.copy()
    ts = pd.to_datetime(enriched[timestamp_col], utc=True)
    pt_times = ts.dt.tz_convert(PT)
    enriched["time_of_day"] = pt_times.dt.hour + pt_times.dt.minute / 60.0
    return enriched


def passes_session_filter(
    timestamp_utc: datetime,
    skip_close_transition: bool = True,
    skip_turn_minutes: int = 5,
) -> tuple[bool, str]:
    """Check if a timestamp passes session filters for trading.

    Args:
        timestamp_utc: UTC-aware datetime to check.
        skip_close_transition: If True, reject 1:00-2:00 PM PT.
        skip_turn_minutes: Minutes before/after hour and half-hour to skip.

    Returns:
        (passes, reason) — True if OK to trade, False with reason if not.
    """
    pt = timestamp_utc.astimezone(PT)
    hour = pt.hour
    minute = pt.minute

    # Close transition filter: 1:00 PM - 2:00 PM PT
    if skip_close_transition and 13 <= hour < 14:
        return False, "close_transition (1:00-2:00 PM PT)"

    # Turn-of-hour filter: within N minutes of :00 or :30
    minutes_past_half = minute % 30
    if minutes_past_half < skip_turn_minutes:
        return False, f"near_turn ({minute:02d} is within {skip_turn_minutes}min of :{minute - minutes_past_half:02d})"
    if minutes_past_half > (30 - skip_turn_minutes):
        next_turn = (minute // 30 + 1) * 30
        return False, f"near_turn ({minute:02d} is within {skip_turn_minutes}min of :{next_turn % 60:02d})"

    return True, ""
