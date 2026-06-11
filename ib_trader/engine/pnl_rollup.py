"""Per-contract realized-P&L rollup for the chart panes.

Pure computation split out from the engine loop so it can be unit-tested
without IB or Redis. The engine's ``_pnl_rollup_loop`` sweeps account
executions via ``req_recent_executions`` (which includes trades the
operator placed directly in TWS, not just orders our system originated)
and hands the normalized list here.

Two windows per contract:
  - ``pnl_24h``    : realized P&L over the rolling 24 h (all sec types).
  - ``pnl_session``: realized P&L since the most recent futures-session
                     boundary (``session_start_hour`` server-local).
                     Futures only — ``None`` for STK/OPT, where a daily
                     session boundary isn't meaningful for the operator.
"""
from __future__ import annotations

from datetime import datetime, timedelta


def session_start(now: datetime, session_start_hour: int) -> datetime:
    """Most recent occurrence of ``session_start_hour``:00 in ``now``'s tz.

    For a Pacific desk the futures day reopens at 15:00 PT (= 17:00 CT
    Globex reopen), so realized P&L "today" is measured from there. If the
    current wall-clock is past today's boundary we anchor to today;
    otherwise to yesterday's.
    """
    anchor = now.replace(
        hour=session_start_hour, minute=0, second=0, microsecond=0,
    )
    if now < anchor:
        anchor -= timedelta(days=1)
    return anchor


def compute_pnl_rollup(
    executions: list[dict],
    now: datetime,
    session_start_hour: int,
) -> dict[str, dict]:
    """Roll executions up to ``{ local_symbol: {pnl_24h, pnl_session,
    sec_type} }``.

    ``now`` must be timezone-aware (server-local). Each execution dict
    needs ``local_symbol``, ``sec_type``, ``realized_pnl`` (Decimal|None —
    None on opening fills, skipped), and ``exec_time`` (tz-aware datetime).
    Rows missing realized P&L or a usable timestamp are ignored.
    """
    cutoff_24h = now - timedelta(hours=24)
    sess_start = session_start(now, session_start_hour)

    rollup: dict[str, dict] = {}
    for ex in executions:
        pnl = ex.get("realized_pnl")
        if pnl is None:
            continue  # opening fill / no realized P&L on this execution
        ts = ex.get("exec_time")
        if not isinstance(ts, datetime):
            continue
        sym = (ex.get("local_symbol") or "").strip()
        if not sym:
            continue
        sec = (ex.get("sec_type") or "").upper()

        slot = rollup.get(sym)
        if slot is None:
            slot = {"pnl_24h": 0.0, "pnl_session": None, "sec_type": sec}
            rollup[sym] = slot

        pnl_f = float(pnl)
        if ts >= cutoff_24h:
            slot["pnl_24h"] += pnl_f
        # Session number is futures-only and measured from the session
        # boundary. STK/OPT keep ``pnl_session = None`` (the chart hides
        # the "today" figure for them).
        if sec == "FUT" and ts >= sess_start:
            slot["pnl_session"] = (slot["pnl_session"] or 0.0) + pnl_f

    # Drop contracts whose only activity fell outside the 24 h window and
    # never set a session value (all-zero, no signal) — keeps the map tight.
    return {
        s: v for s, v in rollup.items()
        if v["pnl_24h"] != 0.0 or v["pnl_session"] is not None
    }
