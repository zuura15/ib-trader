"""Per-symbol IB commission estimates for display-time fallback.

IB's ``commissionReport`` arrives asynchronously after every fill and
is normally folded into ``TransactionEvent.commission`` and then
aggregated up to ``TradeGroup.total_commission``. For some contracts
(notably the full-size CME/COMEX futures ``NQM6`` and ``GCQ6`` per
the operator's 2026-06-08 report) the report never seems to land in
practice, leaving the stored commission at 0 and making the console's
realized-P&L line read as gross rather than net.

These helpers provide a **display-time fallback only**: when the
stored value is 0, we substitute a per-symbol-root round-trip
estimate so the operator sees a realistic commission and a net P&L
that's consistent with it. The database stays factual — nothing is
written. If IB's report later arrives and bumps the stored value
above 0, the fallback stops firing automatically and the real number
takes over.

Rates here are IB Fixed-tier all-in (broker + exchange + regulatory
+ clearing) **per side** in USD, current as of 2026-06-08. Source:
ibkr.com/commissions (futures). Check on rate changes — they drift
1-2x per year on exchange fee schedules.
"""
from __future__ import annotations

import re
from decimal import Decimal

# Per-side commission, per contract, in USD. Round-trip = 2x.
# Keyed by the root symbol (no month/year suffix). Add new contracts
# as the operator picks them up.
_PER_SIDE: dict[str, Decimal] = {
    # Full-size CME equity index
    "ES":  Decimal("2.05"),
    "NQ":  Decimal("2.05"),
    "RTY": Decimal("2.05"),
    "YM":  Decimal("2.05"),
    # Full-size COMEX/NYMEX metals
    "GC":  Decimal("2.21"),
    "SI":  Decimal("2.21"),
    "HG":  Decimal("2.21"),
    # Full-size NYMEX energy
    "CL":  Decimal("1.74"),
    "NG":  Decimal("1.74"),
    # Micros (lower exchange + reg fees → ~25% of the full-size rate)
    "MES": Decimal("0.47"),
    "MNQ": Decimal("0.52"),
    "M2K": Decimal("0.47"),
    "MYM": Decimal("0.47"),
    "MGC": Decimal("0.50"),
    "MCL": Decimal("0.42"),
}

# Futures localSymbol shape: root + month letter (F G H J K M N Q U V X Z)
# + 1- or 2-digit year. Match the LONGEST valid root by trying greedy
# alpha-only prefix, then peeling back if no rate is registered. Examples:
#   GCQ6   → root GC, expiry Q6
#   MGCQ6  → root MGC, expiry Q6
#   MNQM26 → root MNQ, expiry M26
#   ES H 6 has no spaces in IB local syms; "ESM6" is what we see
_LOCAL_SYM_RE = re.compile(r"^([A-Z][A-Z0-9]{0,4}?)([FGHJKMNQUVXZ]\d{1,2})$")


def root_of(symbol: str) -> str:
    """Extract the root from a futures localSymbol.

    ``MGCQ6 → MGC``, ``ESM6 → ES``, ``GCQ6 → GC``, ``NQM26 → NQ``.
    Returns the symbol unchanged if it doesn't look like a futures
    localSymbol (lookup will fail and the caller falls back to zero).
    """
    s = symbol.upper().strip()
    m = _LOCAL_SYM_RE.match(s)
    if not m:
        return s
    return m.group(1)


def _to_decimal(qty: Decimal | int | float | str) -> Decimal:
    if isinstance(qty, Decimal):
        return qty
    return Decimal(str(qty))


def estimate_one_side(symbol: str, qty: Decimal | int | float | str) -> Decimal:
    """One-side commission estimate in USD for ``qty`` contracts.

    Returns ``Decimal("0")`` when the root isn't in the rate table
    (caller treats that as "no estimate available" — typically STK /
    OPT, which have different fee structures we don't bother modeling
    here since the operator's stale-commission issue is futures-only).
    """
    rate = _PER_SIDE.get(root_of(symbol))
    if rate is None:
        return Decimal("0")
    return rate * _to_decimal(qty)


def estimate_round_trip(symbol: str, qty: Decimal | int | float | str) -> Decimal:
    """Round-trip (entry + exit) commission estimate in USD."""
    return estimate_one_side(symbol, qty) * Decimal("2")


def effective_commission(
    reported: Decimal | None,
    symbol: str,
    qty: Decimal | int | float | str,
    *,
    round_trip: bool = True,
) -> Decimal:
    """Display helper: ``reported`` if non-zero, else an estimate.

    ``round_trip=True`` for closed trades (entry + exit estimate).
    ``round_trip=False`` for per-side contexts (a single fill, or an
    open trade where only the entry leg's commission is in play).
    """
    if reported is not None and Decimal(str(reported)) != 0:
        return Decimal(str(reported))
    if round_trip:
        return estimate_round_trip(symbol, qty)
    return estimate_one_side(symbol, qty)
