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

Futures use LIFO realized P&L when a ``lifo`` result is supplied for the
contract (GH #101); IB's average-cost figures are always carried along as
``ib_pnl_24h`` / ``ib_pnl_session`` for the UI tooltip. ``method`` says
which basis ``pnl_24h`` / ``pnl_session`` use ("lifo" or "avg").
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ib_trader.engine.lifo_pnl import LifoResult


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
    lifo: "dict[str, LifoResult] | None" = None,
) -> dict[str, dict]:
    """Roll executions up to ``{ local_symbol: {pnl_24h, pnl_session,
    ib_pnl_24h, ib_pnl_session, method, sec_type} }``.

    ``now`` must be timezone-aware (server-local). Each execution dict
    needs ``local_symbol``, ``sec_type``, ``realized_pnl`` (Decimal|None —
    None on opening fills), ``exec_id`` and ``exec_time`` (tz-aware
    datetime). Rows without a usable timestamp/symbol are ignored.
    """
    cutoff_24h = now - timedelta(hours=24)
    sess_start = session_start(now, session_start_hour)
    lifo = lifo or {}

    rollup: dict[str, dict] = {}
    for ex in executions:
        ts = ex.get("exec_time")
        if not isinstance(ts, datetime):
            continue
        sym = (ex.get("local_symbol") or "").strip()
        if not sym:
            continue
        sec = (ex.get("sec_type") or "").upper()
        ib_pnl = ex.get("realized_pnl")
        res = lifo.get(sym) if sec == "FUT" else None
        use_lifo = res is not None and res.method == "lifo"
        pnl = res.realized_by_exec.get(ex.get("exec_id") or "") if use_lifo else ib_pnl
        if pnl is None and ib_pnl is None:
            continue  # opening fill / no realized P&L on this execution

        slot = rollup.get(sym)
        if slot is None:
            slot = {
                "pnl_24h": 0.0, "pnl_session": None,
                "ib_pnl_24h": 0.0, "ib_pnl_session": None,
                "method": "lifo" if use_lifo else "avg",
                "sec_type": sec,
            }
            rollup[sym] = slot

        # Session number is futures-only and measured from the session
        # boundary. STK/OPT keep ``pnl_session = None`` (the chart hides
        # the "today" figure for them).
        in_session = sec == "FUT" and ts >= sess_start
        for key, val in (("pnl", pnl), ("ib_pnl", ib_pnl)):
            if val is None:
                continue
            v = float(val)
            if ts >= cutoff_24h:
                slot[f"{key}_24h"] += v
            if in_session:
                slot[f"{key}_session"] = (slot[f"{key}_session"] or 0.0) + v
        if in_session and slot["pnl_session"] is None:
            # A session close realized under one basis but not yet the
            # other (e.g. IB's report unpaired) still marks the session.
            slot["pnl_session"] = 0.0

    # Drop contracts whose only activity fell outside the 24 h window and
    # never set a session value (all-zero, no signal) — keeps the map tight.
    return {
        s: v for s, v in rollup.items()
        if v["pnl_24h"] != 0.0 or v["pnl_session"] is not None
        or v["ib_pnl_24h"] != 0.0
    }
