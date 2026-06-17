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

from decimal import Decimal, InvalidOperation
from typing import Any

# Cap markers per contract so a heavy session can't bloat the payload or
# clutter the chart. Newest kept.
_MAX_PER_SYMBOL = 300


def compute_exec_markers(
    executions: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Group fills into one marker per order, keyed by contract.

    Each marker: ``{time (ISO), side ('B'|'S'), qty (float), price
    (float)}``. ``executions`` rows missing time/price/shares are skipped.
    """
    # key = (local_symbol, "p<permId>") when permId is truthy, else
    # (local_symbol, "e<execId>") so a foreign/old order with permId 0
    # still renders one marker per fill rather than collapsing together.
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    for ex in executions:
        sym = ex.get("local_symbol") or ""
        t = ex.get("exec_time")
        shares = ex.get("shares")
        price = ex.get("price")
        if not sym or t is None or shares is None or price is None:
            continue
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
        ts = g["time"]
        by_sym.setdefault(g["sym"], []).append({
            "time": ts.isoformat() if hasattr(ts, "isoformat") else str(ts),
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
