"""Per-contract execution markers for the chart panes.

Pure transform: a flat list of account-wide executions (from
``ib.req_recent_executions``) → ``{ local_symbol: [marker, ...] }`` where
each marker is one *order's* fill — partial fills sharing a ``perm_id``
are summed into a single marker (lot size = total filled), with a
qty-weighted average price. Account-wide, so trades placed directly in
TWS appear too. No open/close pairing — every order's fill is its own
marker.

Deliberately off the order/fill money path (same as the P&L rollup): a
failure here degrades a chart overlay, never trading.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

# Cap markers per contract so a heavy session can't bloat the payload or
# clutter the chart. Newest kept.
_MAX_PER_SYMBOL = 300

# Grace window: only "correct" an execution time that's MORE than this far
# in the future. Guards against treating a just-now fill as bogus on minor
# clock skew between the engine host and IB.
_FUTURE_GRACE = timedelta(minutes=2)


def _to_utc(t: Any) -> datetime | None:
    """Coerce an execution time to a tz-aware UTC datetime, or None."""
    if isinstance(t, datetime):
        if t.tzinfo is None:
            return t.replace(tzinfo=timezone.utc)
        return t.astimezone(timezone.utc)
    if isinstance(t, str):
        try:
            d = datetime.fromisoformat(t)
        except ValueError:
            return None
        return d.replace(tzinfo=timezone.utc) if d.tzinfo is None else d.astimezone(timezone.utc)
    return None


def compute_exec_markers(
    executions: list[dict[str, Any]],
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Group fills into one marker per order, keyed by contract.

    Each marker: ``{time (ISO UTC), side ('B'|'S'), qty (float), price
    (float)}``. ``executions`` rows missing time/price/shares are skipped.

    ``now`` is a tz-aware datetime (defaults to host-local now). ib_async
    hands back some backfilled (manual-TWS) execution times shifted by the
    host's UTC offset, landing them in the future; a fill can never be in
    the future, so any time past ``now`` is rolled back by that offset.
    """
    if now is None:
        now = datetime.now().astimezone()
    host_offset = now.utcoffset() or timedelta(0)
    now_utc = now.astimezone(timezone.utc)
    future_cutoff = now_utc + _FUTURE_GRACE

    # key = (local_symbol, "p<permId>") when permId is truthy, else
    # (local_symbol, "e<execId>") so a foreign/old order with permId 0
    # still renders one marker per fill rather than collapsing together.
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for ex in executions:
        sym = ex.get("local_symbol") or ""
        t = _to_utc(ex.get("exec_time"))
        shares = ex.get("shares")
        price = ex.get("price")
        if not sym or t is None or shares is None or price is None:
            continue
        # Correct the ib_async backfill tz bug: a future fill time is
        # off by the host's UTC offset.
        if t > future_cutoff:
            t = t + host_offset
        try:
            shares_d = Decimal(str(shares))
            price_d = Decimal(str(price))
        except (InvalidOperation, ValueError, TypeError):
            continue
        if shares_d <= 0:
            continue
        perm = ex.get("perm_id") or 0
        gkey = (sym, f"p{perm}") if perm else (sym, f"e{ex.get('exec_id', '')}")
        raw_side = (ex.get("side") or "").upper()
        side = "B" if raw_side in ("BOT", "BUY", "B") else "S"
        g = groups.get(gkey)
        if g is None:
            groups[gkey] = {
                "sym": sym,
                "side": side,
                "qty": shares_d,
                "notional": price_d * shares_d,  # for qty-weighted avg
                "time": t,
            }
        else:
            g["qty"] += shares_d
            g["notional"] += price_d * shares_d
            if t > g["time"]:
                g["time"] = t

    by_sym: dict[str, list[dict[str, Any]]] = {}
    for g in groups.values():
        qty = g["qty"]
        avg_price = (g["notional"] / qty) if qty else Decimal("0")
        by_sym.setdefault(g["sym"], []).append({
            "time": g["time"].isoformat(),
            "side": g["side"],
            "qty": float(qty),
            "price": float(avg_price),
        })

    # Sort each contract's markers chronologically (ISO strings in a fixed
    # UTC offset sort lexically = chronologically); keep the newest N.
    for sym, lst in by_sym.items():
        lst.sort(key=lambda m: m["time"])
        if len(lst) > _MAX_PER_SYMBOL:
            by_sym[sym] = lst[-_MAX_PER_SYMBOL:]
    return by_sym
