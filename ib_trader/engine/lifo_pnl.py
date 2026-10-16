"""LIFO lot matching for futures P&L (GH #101).

IB reports realized P&L and position cost on an average-cost basis. For
futures the operator wants LIFO: a partial sale is matched against the
most recent add, and the open remainder keeps the cost of the older lots.
Round-trip totals are identical under either method; only the split
between realized and open P&L differs.

This is a display-only view derived from IB executions (IB stays the
source of truth). Pure / IB-free so it can be unit-tested directly.

History limit: we only see executions from the trailing ~24 h store.
Whatever position existed before the oldest execution is treated as ONE
seed lot at IB's average cost, back-solved from IB's own numbers:
  * position never went flat/flipped since → solve from IB's current
    average cost (IB's avg is affine in the seed cost);
  * the seed was fully closed out → solve from IB's realized P&L on the
    fills that closed it.
Any contract that can't be solved cleanly is reported with
``method="avg"`` and callers fall back to IB's numbers for it.

Conventions (match IB): opening commission is folded into the lot cost,
closing commission is subtracted from that fill's realized P&L, so a
full round trip sums to IB's realized total.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

_ZERO = Decimal("0")
_ONE = Decimal("1")
# Seed cost further than this from the first fill price means our inputs
# disagree (stale position snapshot, missing fills) — fall back to IB.
_SEED_SANITY_RATIO = Decimal("0.5")


@dataclass
class Lot:
    """An open lot: signed quantity (+long / -short) and per-unit cost."""
    qty: Decimal
    cost: Decimal


@dataclass
class LifoResult:
    """Per-contract LIFO outcome."""
    method: str                      # "lifo" or "avg" (fallback)
    reason: str | None = None        # why we fell back, when method == "avg"
    realized_by_exec: dict[str, Decimal] = field(default_factory=dict)
    lots: list[Lot] = field(default_factory=list)

    @property
    def open_qty(self) -> Decimal:
        return sum((lot.qty for lot in self.lots), _ZERO)

    @property
    def avg_cost(self) -> Decimal | None:
        """Per-unit LIFO cost of the open remainder (None when flat)."""
        q = self.open_qty
        if q == 0:
            return None
        return sum((lot.qty * lot.cost for lot in self.lots), _ZERO) / q


def _sign(x: Decimal) -> int:
    return (x > 0) - (x < 0)


def _signed_qty(ex: dict[str, Any]) -> Decimal | None:
    shares = ex.get("shares")
    if shares is None or shares <= 0:
        return None
    side = (ex.get("side") or "").upper()
    if side in ("BOT", "BUY", "B"):
        return shares
    if side in ("SLD", "SELL", "S"):
        return -shares
    return None


def _open_cost(price: Decimal, comm: Decimal, qty: Decimal,
               mult: Decimal, direction: int) -> Decimal:
    """Per-unit lot cost with the opening commission folded in."""
    return price + direction * comm / (abs(qty) * mult)


def realize_lifo(
    lots: list[Lot],
    signed_qty: Decimal,
    price: Decimal,
    commission: Decimal,
    multiplier: Decimal,
) -> Decimal | None:
    """Apply one fill to ``lots`` (mutated in place), newest lot first.

    Returns the fill's realized P&L (net of its closing commission share),
    or None when the fill only opened/added.
    """
    total = abs(signed_qty)
    remaining = signed_qty
    realized: Decimal | None = None
    while remaining != 0 and lots and _sign(lots[-1].qty) == -_sign(remaining):
        top = lots[-1]
        matched = min(abs(remaining), abs(top.qty))
        direction = _sign(top.qty)
        comm_share = commission * matched / total
        pnl = (price - top.cost) * matched * multiplier * direction - comm_share
        realized = (realized or _ZERO) + pnl
        top.qty -= direction * matched
        remaining += direction * matched
        if top.qty == 0:
            lots.pop()
    if remaining != 0:
        comm_share = commission * abs(remaining) / total
        lots.append(Lot(
            qty=remaining,
            cost=_open_cost(price, comm_share, remaining, multiplier, _sign(remaining)),
        ))
    return realized


def _simulate_avg(
    seed_qty: Decimal, seed_cost: Decimal, fills: list[tuple],
    multiplier: Decimal,
) -> tuple[Decimal | None, Decimal, bool]:
    """IB-style average-cost walk from a seed position.

    Returns ``(final_avg, realized_until_seed_gone, seed_gone)``.
    ``realized_until_seed_gone`` sums realized P&L up to and including the
    fill that first takes the position to flat or through zero.
    """
    qty, avg = seed_qty, seed_cost
    realized = _ZERO
    seed_gone = False
    for _eid, sq, price, comm in fills:
        if qty == 0 or _sign(sq) == _sign(qty):
            new_qty = qty + sq
            cost = _open_cost(price, comm, sq, multiplier, _sign(sq))
            avg = cost if qty == 0 else (abs(qty) * avg + abs(sq) * cost) / abs(new_qty)
            qty = new_qty
            continue
        closed = min(abs(sq), abs(qty))
        direction = _sign(qty)
        if not seed_gone:
            realized += (
                (price - avg) * closed * multiplier * direction
                - comm * closed / abs(sq)
            )
        new_qty = qty + sq
        if _sign(new_qty) != direction:
            seed_gone = True
            if new_qty != 0:
                opened = abs(new_qty)
                avg = _open_cost(price, comm * opened / abs(sq), new_qty,
                                 multiplier, _sign(new_qty))
        qty = new_qty
    return (avg if qty != 0 else None), realized, seed_gone


def _solve_seed_cost(
    seed_qty: Decimal,
    fills: list[tuple],
    multiplier: Decimal,
    ib_avg_now: Decimal | None,
    ib_realized: dict[str, Decimal | None],
) -> tuple[Decimal | None, str | None]:
    """Back-solve the seed lot's average cost from IB's own numbers."""
    avg0, r0, gone = _simulate_avg(seed_qty, _ZERO, fills, multiplier)
    avg1, r1, _ = _simulate_avg(seed_qty, _ONE, fills, multiplier)
    if not gone:
        if ib_avg_now is None or avg0 is None or avg1 is None or avg1 == avg0:
            return None, "no_ib_avg_cost"
        return (ib_avg_now - avg0) / (avg1 - avg0), None
    # Seed fully closed out — use IB's realized on the fills that closed it.
    target = _ZERO
    qty = seed_qty
    for eid, sq, _p, _c in fills:
        if qty != 0 and _sign(sq) != _sign(qty):
            pnl = ib_realized.get(eid)
            if pnl is None:
                return None, "ib_realized_missing"
            target += pnl
            if _sign(qty + sq) != _sign(qty):
                break
        qty += sq
    if r1 == r0:
        return None, "degenerate_seed"
    return (target - r0) / (r1 - r0), None


def compute_lifo(
    executions: list[dict],
    positions: dict[str, dict],
) -> dict[str, LifoResult]:
    """LIFO-match every FUT contract in ``executions``.

    ``executions`` are dicts shaped like ``req_recent_executions`` output
    (``local_symbol``, ``sec_type``, ``side``, ``shares``, ``price``,
    ``commission``, ``realized_pnl``, ``exec_id``, ``exec_time``,
    ``multiplier``). ``positions`` maps local_symbol → ``{quantity,
    avg_cost (per unit), multiplier}`` for currently-held FUT contracts.
    Contracts held with no executions in the window are not returned —
    there's nothing to re-match, IB's average cost is already their basis.
    """
    by_sym: dict[str, list[dict]] = {}
    for ex in executions:
        if (ex.get("sec_type") or "").upper() != "FUT":
            continue
        sym = (ex.get("local_symbol") or "").strip()
        if sym:
            by_sym.setdefault(sym, []).append(ex)

    out: dict[str, LifoResult] = {}
    for sym, rows in by_sym.items():
        out[sym] = _compute_one(rows, positions.get(sym))
    return out


def _compute_one(rows: list[dict], position: dict | None) -> LifoResult:
    pos = position or {}
    mult = next(
        (m for m in (_dec(r.get("multiplier")) for r in rows) if m), None,
    ) or _dec(pos.get("multiplier"))
    if not mult or mult <= 0:
        return LifoResult(method="avg", reason="unknown_multiplier")

    rows = sorted(rows, key=lambda r: (r["exec_time"], r.get("exec_id") or ""))
    fills: list[tuple] = []
    ib_realized: dict[str, Decimal | None] = {}
    for r in rows:
        sq = _signed_qty(r)
        price = r.get("price")
        if sq is None or price is None:
            return LifoResult(method="avg", reason="incomplete_fill")
        eid = r.get("exec_id") or ""
        fills.append((eid, sq, price, r.get("commission") or _ZERO))
        ib_realized[eid] = r.get("realized_pnl")

    current_qty = _dec(pos.get("quantity")) or _ZERO
    seed_qty = current_qty - sum((f[1] for f in fills), _ZERO)

    lots: list[Lot] = []
    if seed_qty != 0:
        seed_cost, reason = _solve_seed_cost(
            seed_qty, fills, mult, _dec(pos.get("avg_cost")), ib_realized,
        )
        if seed_cost is None:
            return LifoResult(method="avg", reason=reason)
        first_price = fills[0][2]
        if first_price and abs(seed_cost - first_price) / first_price > _SEED_SANITY_RATIO:
            return LifoResult(method="avg", reason="seed_cost_implausible")
        lots.append(Lot(qty=seed_qty, cost=seed_cost))

    result = LifoResult(method="lifo", lots=lots)
    for eid, sq, price, comm in fills:
        pnl = realize_lifo(lots, sq, price, comm, mult)
        if pnl is not None:
            result.realized_by_exec[eid] = pnl
    return result


def _dec(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except (ValueError, TypeError, ArithmeticError):
        return None
