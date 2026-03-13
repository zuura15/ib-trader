"""Unit tests for engine/market_hours.py.

All times are constructed in US/Eastern to match IB session windows.
Session schedule per IB official docs:
  https://www.interactivebrokers.com/en/trading/us-overnight-trading.php
"""
import pytest
from datetime import datetime
from zoneinfo import ZoneInfo

from ib_trader.engine.market_hours import (
    is_weekend_closure,
    is_session_break,
    is_ib_session_active,
    is_overnight_session,
    presubmitted_reason,
    session_label,
)

ET = ZoneInfo("America/New_York")


def _et(year, month, day, hour, minute=0):
    """Helper: build an ET-aware datetime."""
    return datetime(year, month, day, hour, minute, tzinfo=ET)


# ── Weekend closure ────────────────────────────────────────────────────────────

class TestWeekendClosure:
    def test_friday_before_8pm_not_weekend(self):
        assert not is_weekend_closure(_et(2026, 3, 6, 19, 59))  # Fri 7:59 PM ET

    def test_friday_exactly_8pm_is_weekend(self):
        assert is_weekend_closure(_et(2026, 3, 6, 20, 0))  # Fri 8:00 PM ET

    def test_friday_after_8pm_is_weekend(self):
        assert is_weekend_closure(_et(2026, 3, 6, 22, 0))  # Fri 10 PM ET

    def test_saturday_midnight_is_weekend(self):
        assert is_weekend_closure(_et(2026, 3, 7, 0, 0))  # Sat midnight

    def test_saturday_noon_is_weekend(self):
        assert is_weekend_closure(_et(2026, 3, 7, 12, 0))  # Sat noon

    def test_sunday_before_8pm_is_weekend(self):
        assert is_weekend_closure(_et(2026, 3, 8, 19, 59))  # Sun 7:59 PM ET

    def test_sunday_exactly_8pm_not_weekend(self):
        assert not is_weekend_closure(_et(2026, 3, 8, 20, 0))  # Sun 8:00 PM ET

    def test_sunday_after_8pm_not_weekend(self):
        assert not is_weekend_closure(_et(2026, 3, 8, 21, 0))  # Sun 9 PM ET

    def test_monday_is_not_weekend(self):
        assert not is_weekend_closure(_et(2026, 3, 9, 2, 0))  # Mon 2 AM ET

    def test_thursday_is_not_weekend(self):
        assert not is_weekend_closure(_et(2026, 3, 12, 15, 0))  # Thu 3 PM ET


# ── Session break ──────────────────────────────────────────────────────────────

class TestSessionBreak:
    def test_before_break_not_break(self):
        assert not is_session_break(_et(2026, 3, 9, 3, 49))  # Mon 3:49 AM

    def test_exactly_at_break_start(self):
        assert is_session_break(_et(2026, 3, 9, 3, 50))  # Mon 3:50 AM

    def test_mid_break(self):
        assert is_session_break(_et(2026, 3, 9, 3, 55))  # Mon 3:55 AM

    def test_exactly_at_break_end_not_break(self):
        assert not is_session_break(_et(2026, 3, 9, 4, 0))  # Mon 4:00 AM

    def test_weekend_not_classified_as_break(self):
        # Saturday 3:55 AM is weekend closure, not session break
        assert not is_session_break(_et(2026, 3, 7, 3, 55))


# ── Session active ─────────────────────────────────────────────────────────────

class TestSessionActive:
    # Overnight session (Mon-Thu 8 PM → next day 3:50 AM)
    def test_monday_midnight_overnight_active(self):
        assert is_ib_session_active(_et(2026, 3, 9, 0, 0))   # Mon 12 AM

    def test_monday_3am_overnight_active(self):
        assert is_ib_session_active(_et(2026, 3, 9, 3, 0))   # Mon 3 AM

    # Session break
    def test_session_break_not_active(self):
        assert not is_ib_session_active(_et(2026, 3, 9, 3, 55))  # Mon 3:55 AM

    # Pre-market
    def test_premarket_active(self):
        assert is_ib_session_active(_et(2026, 3, 9, 5, 0))   # Mon 5 AM

    # RTH
    def test_rth_active(self):
        assert is_ib_session_active(_et(2026, 3, 9, 14, 0))  # Mon 2 PM

    # After-hours
    def test_afterhours_active(self):
        assert is_ib_session_active(_et(2026, 3, 9, 17, 0))  # Mon 5 PM

    # Overnight start (Mon 8 PM)
    def test_monday_8pm_overnight_active(self):
        assert is_ib_session_active(_et(2026, 3, 9, 20, 0))  # Mon 8 PM

    # Friday
    def test_friday_premarket_active(self):
        assert is_ib_session_active(_et(2026, 3, 6, 6, 0))   # Fri 6 AM

    def test_friday_rth_active(self):
        assert is_ib_session_active(_et(2026, 3, 6, 12, 0))  # Fri noon

    def test_friday_afterhours_active(self):
        assert is_ib_session_active(_et(2026, 3, 6, 18, 0))  # Fri 6 PM

    def test_friday_8pm_not_active(self):
        assert not is_ib_session_active(_et(2026, 3, 6, 20, 0))  # Fri 8 PM = weekend

    # Full weekend
    def test_saturday_not_active(self):
        assert not is_ib_session_active(_et(2026, 3, 7, 10, 0))

    def test_sunday_noon_not_active(self):
        assert not is_ib_session_active(_et(2026, 3, 8, 12, 0))

    def test_sunday_8pm_active(self):
        assert is_ib_session_active(_et(2026, 3, 8, 20, 0))  # overnight opens


# ── is_overnight_session ───────────────────────────────────────────────────────

class TestOvernightSession:
    # Should be True during overnight hours
    def test_midnight_is_overnight(self):
        assert is_overnight_session(_et(2026, 3, 9, 0, 0))   # Mon midnight

    def test_3am_is_overnight(self):
        assert is_overnight_session(_et(2026, 3, 9, 3, 0))   # Mon 3:00 AM

    def test_just_before_break_is_overnight(self):
        assert is_overnight_session(_et(2026, 3, 9, 3, 49))  # Mon 3:49 AM

    def test_monday_8pm_is_overnight(self):
        assert is_overnight_session(_et(2026, 3, 9, 20, 0))  # Mon 8:00 PM

    def test_monday_11pm_is_overnight(self):
        assert is_overnight_session(_et(2026, 3, 9, 23, 0))  # Mon 11:00 PM

    def test_sunday_8pm_is_overnight(self):
        assert is_overnight_session(_et(2026, 3, 8, 20, 0))  # Sun 8:00 PM reopening

    # Should be False outside overnight hours
    def test_session_break_not_overnight(self):
        assert not is_overnight_session(_et(2026, 3, 9, 3, 55))  # 3:55 AM break

    def test_premarket_not_overnight(self):
        assert not is_overnight_session(_et(2026, 3, 9, 6, 0))   # 6:00 AM pre-market

    def test_rth_not_overnight(self):
        assert not is_overnight_session(_et(2026, 3, 9, 11, 0))  # 11:00 AM RTH

    def test_afterhours_not_overnight(self):
        assert not is_overnight_session(_et(2026, 3, 9, 17, 0))  # 5:00 PM after-hours

    def test_just_before_overnight_start_not_overnight(self):
        assert not is_overnight_session(_et(2026, 3, 9, 19, 59))  # 7:59 PM

    def test_weekend_not_overnight(self):
        assert not is_overnight_session(_et(2026, 3, 7, 23, 0))  # Saturday 11 PM

    def test_sunday_noon_not_overnight(self):
        assert not is_overnight_session(_et(2026, 3, 8, 12, 0))  # Sunday noon


# ── presubmitted_reason ────────────────────────────────────────────────────────

class TestPresubmittedReason:
    def test_weekend_returns_reason(self):
        reason = presubmitted_reason(_et(2026, 3, 7, 12, 0))  # Saturday
        assert "weekend" in reason.lower()
        assert "Sunday 8:00 PM ET" in reason

    def test_break_returns_reason(self):
        reason = presubmitted_reason(_et(2026, 3, 9, 3, 55))  # Mon 3:55 AM break
        assert "4:00 AM" in reason

    def test_active_session_returns_empty(self):
        reason = presubmitted_reason(_et(2026, 3, 9, 10, 0))  # Mon 10 AM RTH
        assert reason == ""


# ── session_label ──────────────────────────────────────────────────────────────

class TestSessionLabel:
    def test_weekend_label(self):
        label = session_label(_et(2026, 3, 7, 10, 0))
        assert "weekend" in label.lower()

    def test_break_label(self):
        label = session_label(_et(2026, 3, 9, 3, 52))
        assert "break" in label.lower()

    def test_overnight_label(self):
        label = session_label(_et(2026, 3, 9, 1, 0))
        assert "overnight" in label.lower()

    def test_premarket_label(self):
        label = session_label(_et(2026, 3, 9, 7, 0))
        assert "pre-market" in label.lower()

    def test_rth_label(self):
        label = session_label(_et(2026, 3, 9, 11, 0))
        assert "regular" in label.lower()

    def test_afterhours_label(self):
        label = session_label(_et(2026, 3, 9, 17, 0))
        assert "after" in label.lower()

    def test_overnight_start_label(self):
        label = session_label(_et(2026, 3, 9, 21, 0))
        assert "overnight" in label.lower()
