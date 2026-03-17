"""Alpaca-specific market hours provider.

Alpaca US Equity Hours (all times ET, DST-aware):
  Pre-market:   4:00 AM – 9:30 AM
  RTH:          9:30 AM – 4:00 PM
  After-hours:  4:00 PM – 8:00 PM
  No overnight session (8 PM – 4 AM: no trading)
  No weekend trading

Key constraints:
  - GTC orders do NOT fill in extended hours. Must use tif=day + extended_hours=True.
  - Market orders are REJECTED during extended hours (limit only).
  - Fractional shares only during RTH.
  - extended_hours=True requires tif=day (not gtc).
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from ib_trader.broker.market_hours import MarketHoursProvider

ET = ZoneInfo("America/New_York")

# Minute-of-day constants
_PRE_MARKET_START = 4 * 60         # 4:00 AM
_RTH_START = 9 * 60 + 30           # 9:30 AM
_RTH_END = 16 * 60                 # 4:00 PM
_AFTER_HOURS_END = 20 * 60         # 8:00 PM


def _now_et() -> datetime:
    return datetime.now(ET)


def _et(now: datetime | None) -> datetime:
    return (now or _now_et()).astimezone(ET)


class AlpacaMarketHours(MarketHoursProvider):
    """Alpaca US Equity market hours."""

    def is_session_active(self, now: datetime | None = None) -> bool:
        """True when any Alpaca trading session is open (4 AM - 8 PM ET, Mon-Fri)."""
        n = _et(now)
        wd = n.weekday()
        if wd >= 5:  # Saturday=5, Sunday=6
            return False
        mins = n.hour * 60 + n.minute
        return _PRE_MARKET_START <= mins < _AFTER_HOURS_END

    def is_extended_hours(self, now: datetime | None = None) -> bool:
        """True during pre-market (4-9:30 AM) or after-hours (4-8 PM), Mon-Fri."""
        n = _et(now)
        wd = n.weekday()
        if wd >= 5:
            return False
        mins = n.hour * 60 + n.minute
        if not (_PRE_MARKET_START <= mins < _AFTER_HOURS_END):
            return False
        return mins < _RTH_START or mins >= _RTH_END

    def session_label(self, now: datetime | None = None) -> str:
        n = _et(now)
        wd = n.weekday()
        if wd >= 5:
            return "weekend (no trading)"
        mins = n.hour * 60 + n.minute
        if mins < _PRE_MARKET_START:
            return "closed (pre 4:00 AM ET)"
        if mins < _RTH_START:
            return "pre-market (4:00–9:30 AM ET)"
        if mins < _RTH_END:
            return "regular trading hours (9:30 AM–4:00 PM ET)"
        if mins < _AFTER_HOURS_END:
            return "after-hours (4:00–8:00 PM ET)"
        return "closed (post 8:00 PM ET)"

    def order_params(self, now: datetime | None = None) -> dict:
        """Session-aware order parameters for Alpaca.

        During RTH:
            {"tif": "gtc", "extended_hours": False}
            GTC orders persist and fill during RTH only.

        During extended hours (pre-market/after-hours):
            {"tif": "day", "extended_hours": True}
            Order active in current extended session, expires at 8 PM.

        This mirrors IB's pattern where overnight forces tif=DAY + includeOvernight.
        """
        if self.is_extended_hours(now):
            return {"tif": "day", "extended_hours": True}
        return {"tif": "gtc", "extended_hours": False}

    def supports_market_orders(self, now: datetime | None = None) -> bool:
        """Alpaca rejects market orders during extended hours."""
        return not self.is_extended_hours(now)
