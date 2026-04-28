"""Unit tests for ib_trader/engine/market_hours_futures.py."""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from ib_trader.engine.market_hours_futures import (
    cme_break_reason,
    is_cme_equity_break,
)

CT = ZoneInfo("America/Chicago")


class TestBreakWindow:
    def test_monday_inside_window(self) -> None:
        now = datetime(2026, 4, 20, 16, 30, tzinfo=CT)  # Mon 4:30 PM CT
        assert is_cme_equity_break(now) is True

    def test_thursday_inside_window(self) -> None:
        now = datetime(2026, 4, 23, 16, 59, tzinfo=CT)  # Thu 4:59 PM CT
        assert is_cme_equity_break(now) is True

    def test_monday_3_59_pm_not_in_window(self) -> None:
        now = datetime(2026, 4, 20, 15, 59, tzinfo=CT)  # Mon 3:59 PM CT
        assert is_cme_equity_break(now) is False

    def test_monday_5_00_pm_not_in_window(self) -> None:
        now = datetime(2026, 4, 20, 17, 0, tzinfo=CT)  # Mon 5:00 PM CT
        assert is_cme_equity_break(now) is False

    def test_friday_430pm_not_in_window(self) -> None:
        now = datetime(2026, 4, 24, 16, 30, tzinfo=CT)
        assert is_cme_equity_break(now) is False

    def test_sunday_not_in_window(self) -> None:
        now = datetime(2026, 4, 19, 16, 30, tzinfo=CT)
        assert is_cme_equity_break(now) is False

    def test_saturday_not_in_window(self) -> None:
        now = datetime(2026, 4, 18, 16, 30, tzinfo=CT)
        assert is_cme_equity_break(now) is False


class TestReasonString:
    def test_inside_returns_reason(self) -> None:
        now = datetime(2026, 4, 20, 16, 30, tzinfo=CT)
        assert "CME daily-settle" in cme_break_reason(now)
        assert "IB may reject" in cme_break_reason(now)

    def test_outside_returns_empty(self) -> None:
        now = datetime(2026, 4, 20, 10, 0, tzinfo=CT)
        assert cme_break_reason(now) == ""

    @pytest.mark.parametrize("hour", [15, 17, 9, 22])
    def test_various_non_break_hours(self, hour: int) -> None:
        now = datetime(2026, 4, 20, hour, 0, tzinfo=CT)
        assert is_cme_equity_break(now) is False
