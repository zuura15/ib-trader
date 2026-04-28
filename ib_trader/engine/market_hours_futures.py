"""CME-family daily-settle session predicate (Epic 1 D7).

CME equity futures (ES/NQ/YM/RTY + micros) halt Mon-Thu 4:00–5:00 PM CT
for the daily maintenance / settlement window. Trading resumes at 5:00
PM CT and runs through 4:00 PM CT the next day.

This predicate is **advisory**. It is used to log a WARNING when a FUT
order is entered during the window; it does NOT block order placement.
IB's own rejection remains the ultimate authority. The UI copy always
reads "expected CME daily-settle window — IB may reject" so operators
do not treat the local check as a hard gate.

MVP does not model per-product calendars (holiday / half-day / early
close). ICE and EUREX are out of scope for the Epic 1 MVP.
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

CT = ZoneInfo("America/Chicago")

_BREAK_START_HOUR = 16  # 4 PM CT
_BREAK_END_HOUR = 17    # 5 PM CT


def _now_ct() -> datetime:
    """Isolated for test patching."""
    return datetime.now(CT)


def _ct(now: datetime | None) -> datetime:
    return (now or _now_ct()).astimezone(CT)


def is_cme_equity_break(now: datetime | None = None) -> bool:
    """True during 4:00–5:00 PM CT Mon-Thu (daily maintenance window).

    Fri close through Sun 5pm CT is the weekend closure and is handled
    separately; this predicate returns False outside Mon-Thu.
    """
    n = _ct(now)
    wd = n.weekday()  # 0=Mon … 4=Fri … 6=Sun
    if wd >= 4:
        return False
    return _BREAK_START_HOUR <= n.hour < _BREAK_END_HOUR


def cme_break_reason(now: datetime | None = None) -> str:
    """Human-readable explanation when inside the window; empty otherwise."""
    if is_cme_equity_break(now):
        return "expected CME daily-settle window (4–5 PM CT Mon-Thu) — IB may reject"
    return ""
