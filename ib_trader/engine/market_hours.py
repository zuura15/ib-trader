"""US equity market session detection based on IB trading hour windows.

IB US equity sessions (all times US Eastern, DST-aware):
  Overnight:   8:00 PM – 3:50 AM ET  (Sun 8 PM through Fri 3:50 AM, nightly)
  Break:       3:50 AM – 4:00 AM ET  (daily 10-minute gap between overnight and pre-market)
  Pre-market:  4:00 AM – 9:30 AM ET  (Mon–Fri)
  RTH:         9:30 AM – 4:00 PM ET  (Mon–Fri)
  After-hours: 4:00 PM – 8:00 PM ET  (Mon–Fri)
  Weekend:     Fri 8:00 PM – Sun 8:00 PM ET  (no trading at all)

Source: https://www.interactivebrokers.com/en/trading/us-overnight-trading.php
        https://interactivebrokers.github.io/tws-api/order_submission.html

A GTC limit order with outsideRth=True placed during any active session should
transition from PreSubmitted → Submitted at the exchange within seconds.  If it
stays PreSubmitted during an active session, the order is NOT working and the
cause must be investigated via the IB `whyHeld` field and error callbacks.

PreSubmitted is only expected during:
  - The weekend closure (Fri 8 PM – Sun 8 PM ET)
  - The nightly 10-minute session break (3:50–4:00 AM ET)
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# Minute-of-day constants
_BREAK_START_MINS = 3 * 60 + 50   # 3:50 AM
_BREAK_END_MINS = 4 * 60           # 4:00 AM
_OVERNIGHT_START_MINS = 20 * 60    # 8:00 PM


def _now_et() -> datetime:
    """Return the current time in US/Eastern. Isolated for test patching."""
    return datetime.now(ET)


def _et(now: datetime | None) -> datetime:
    return (now or _now_et()).astimezone(ET)


def is_weekend_closure(now: datetime | None = None) -> bool:
    """True during Fri 8:00 PM ET through Sun 8:00 PM ET.

    No IB US equity trading session exists during this window.  A GTC order
    placed with outsideRth=True will sit as PreSubmitted until the overnight
    session opens Sunday 8:00 PM ET — this is expected and documented IB
    behaviour, not an error.

    Source: https://www.interactivebrokers.com/en/trading/us-overnight-trading.php
    """
    n = _et(now)
    wd = n.weekday()   # 0=Mon … 4=Fri, 5=Sat, 6=Sun
    mins = n.hour * 60 + n.minute
    if wd == 4:   # Friday: closed at/after 8 PM ET
        return mins >= _OVERNIGHT_START_MINS
    if wd == 5:   # Saturday: fully closed
        return True
    if wd == 6:   # Sunday: closed until 8 PM ET
        return mins < _OVERNIGHT_START_MINS
    return False


def is_session_break(now: datetime | None = None) -> bool:
    """True during the 3:50–4:00 AM ET nightly session break.

    IB closes the overnight session at 3:50 AM ET and opens pre-market at
    4:00 AM ET.  Orders may briefly show PreSubmitted during this 10-minute
    gap — they will become Submitted when pre-market opens.
    """
    n = _et(now)
    if is_weekend_closure(n):
        return False
    mins = n.hour * 60 + n.minute
    return _BREAK_START_MINS <= mins < _BREAK_END_MINS


def is_overnight_session(now: datetime | None = None) -> bool:
    """True during the IB overnight trading session (8:00 PM – 3:50 AM ET, Mon–Fri).

    During this window orders should route through exchange='OVERNIGHT'
    with tif='GTC'.  The OVERNIGHT exchange routing is applied automatically
    in insync_client.place_limit_order when this function returns True.

    Returns False during weekend closure and the 3:50–4:00 AM session break.
    """
    n = _et(now)
    if is_weekend_closure(n):
        return False
    if is_session_break(n):
        return False
    mins = n.hour * 60 + n.minute
    return mins < _BREAK_START_MINS or mins >= _OVERNIGHT_START_MINS


def is_outside_rth(now: datetime | None = None) -> bool:
    """True when outside Regular Trading Hours (9:30 AM – 4:00 PM ET).

    Covers pre-market, after-hours, overnight, weekend, session break —
    any time a MARKET order won't fill on NASDAQ/NYSE. Used by order
    placement to convert market orders to aggressive limit orders.
    """
    n = _et(now)
    mins = n.hour * 60 + n.minute
    return mins < 9 * 60 + 30 or mins >= 16 * 60 or is_weekend_closure(n) or is_session_break(n)


def is_ib_session_active(now: datetime | None = None) -> bool:
    """True when at least one IB US equity trading session is open.

    A GTC limit order with outsideRth=True should transition from
    PreSubmitted → Submitted (live at exchange) within seconds when this
    returns True.  If it remains PreSubmitted, the order is not working and
    the IB whyHeld field + error callbacks should be checked.
    """
    n = _et(now)
    if is_weekend_closure(n):
        return False
    if is_session_break(n):
        return False
    return True


def presubmitted_reason(now: datetime | None = None) -> str:
    """Human-readable explanation of why PreSubmitted may be acceptable right now.

    Returns an empty string when PreSubmitted is NOT expected (active session).
    """
    n = _et(now)
    if is_weekend_closure(n):
        return "weekend closure — market reopens Sunday 8:00 PM ET"
    if is_session_break(n):
        return "session break (3:50–4:00 AM ET) — pre-market opens at 4:00 AM ET"
    return ""


def session_label(now: datetime | None = None) -> str:
    """Human-readable name of the current session window."""
    n = _et(now)
    if is_weekend_closure(n):
        return "weekend closure (Fri 8 PM – Sun 8 PM ET)"
    if is_session_break(n):
        return "session break (3:50–4:00 AM ET)"
    mins = n.hour * 60 + n.minute
    if mins < _BREAK_START_MINS:
        return "overnight session"
    if mins < 9 * 60 + 30:
        return "pre-market session (4:00–9:30 AM ET)"
    if mins < 16 * 60:
        return "regular trading hours (9:30 AM–4:00 PM ET)"
    if mins < _OVERNIGHT_START_MINS:
        return "after-hours session (4:00–8:00 PM ET)"
    return "overnight session"
