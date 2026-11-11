"""Tests for the chart timeframe helpers in the engine history path (#105)."""
from __future__ import annotations

import pytest

from ib_trader.engine.internal_api import BAR_SECONDS_BY_SIZE, history_duration_str


@pytest.mark.parametrize("bar_size,seconds", [
    ("1 min", 60), ("3 mins", 180), ("1 hour", 3600),
    ("4 hours", 14400), ("1 day", 86400),
])
def test_chart_timeframes_have_bar_seconds(bar_size, seconds):
    assert BAR_SECONDS_BY_SIZE[bar_size] == seconds


@pytest.mark.parametrize("hours,expected", [
    (0, "3600 S"),        # clamps to one hour
    (1, "3600 S"),
    (24, "86400 S"),      # "S" tops out at a day
    (25, "3 D"),          # rounded up plus one session of buffer
    (72, "4 D"),
    (24 * 60, "61 D"),
    (24 * 364, "365 D"),  # largest "N D" IB accepts
    (24 * 365, "1 Y"),    # past 365 D it must be years
    (24 * 400, "2 Y"),
])
def test_history_duration_str(hours, expected):
    assert history_duration_str(hours) == expected
