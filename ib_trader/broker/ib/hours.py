"""IB-specific market hours provider.

Wraps the existing engine/market_hours.py functions into the
MarketHoursProvider interface. No logic duplication — delegates
to the proven implementation.
"""
from datetime import datetime

from ib_trader.broker.market_hours import MarketHoursProvider
from ib_trader.engine.market_hours import (
    is_ib_session_active,
    is_overnight_session,
    is_session_break,
    is_weekend_closure,
    presubmitted_reason,
    session_label as ib_session_label,
)


class IBMarketHours(MarketHoursProvider):
    """IB US Equity market hours.

    Sessions (all times ET, DST-aware):
      Overnight:   8:00 PM – 3:50 AM (Sun 8 PM through Fri 3:50 AM)
      Break:       3:50 AM – 4:00 AM (10-minute gap)
      Pre-market:  4:00 AM – 9:30 AM (Mon–Fri)
      RTH:         9:30 AM – 4:00 PM (Mon–Fri)
      After-hours: 4:00 PM – 8:00 PM (Mon–Fri)
      Weekend:     Fri 8:00 PM – Sun 8:00 PM
    """

    def is_session_active(self, now: datetime | None = None) -> bool:
        return is_ib_session_active(now)

    def is_extended_hours(self, now: datetime | None = None) -> bool:
        """True during overnight, pre-market, or after-hours."""
        if is_weekend_closure(now) or is_session_break(now):
            return False
        return is_overnight_session(now) or not self._is_rth(now)

    def _is_rth(self, now: datetime | None = None) -> bool:
        """True during regular trading hours (9:30 AM - 4:00 PM ET)."""
        from zoneinfo import ZoneInfo
        ET = ZoneInfo("America/New_York")
        n = (now or datetime.now(ET)).astimezone(ET)
        mins = n.hour * 60 + n.minute
        return 570 <= mins < 960  # 9:30 (570) to 16:00 (960)

    def session_label(self, now: datetime | None = None) -> str:
        return ib_session_label(now)

    def order_params(self, now: datetime | None = None) -> dict:
        """IB order params: outsideRth always True, GTC normally, DAY during overnight.

        The insync_client handles overnight routing (includeOvernight=True)
        internally, so the engine just needs tif and extended_hours.
        """
        if is_overnight_session(now):
            return {"tif": "DAY", "extended_hours": True}
        return {"tif": "GTC", "extended_hours": True}

    def supports_market_orders(self, now: datetime | None = None) -> bool:
        """IB supports market orders in all sessions."""
        return True
