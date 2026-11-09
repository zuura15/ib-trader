"""LIFO lot matching for futures P&L (GH #101).

IB reports realized P&L and position cost on an average-cost basis. For
futures the operator wants LIFO: a partial sale is matched against the
most recent add, and the open remainder keeps the cost of the older lots.
Round-trip totals are identical under either method; only the split
between realized and open P&L differs.

This is a display-only view derived from IB executions (IB stays the
source of truth). Pure / IB-free so it can be unit-tested directly.

History limit: IB only hands us executions from the trailing ~24 h
store. Whatever position existed before the oldest execution (the
"seed") gets its average cost back-solved from IB's own numbers:
  * position never went flat/flipped since → solve from IB's current
    average cost (IB's avg is affine in the seed cost);
  * the seed was fully closed out → solve from IB's realized P&L on the
    fills that closed it.
The seed's individual lots are then rebuilt from the archive of every IB
execution on the account (``history``): replay from the latest point the
position was flat, accepted only when it reproduces IB exactly — same
quantity, and an average-cost replay landing on the back-solved seed
cost. When no such point exists (e.g. the position predates the
archive), the seed stays ONE lot at IB's average cost. Any contract that can't be solved cleanly is reported with
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
# History replay must land on IB's average cost within this fraction of
# price (~0.04 on gold at 4100) — tight enough that a wrong flat point
# can't match by coincidence; IB's average includes commissions exactly
# as the replay folds them.
_HISTORY_MATCH_RATIO = Decimal("0.00001")
# Only search this many most-recent archived fills for the flat point.
_HISTORY_MAX_FILLS = 3000
# Memo of history rebuilds — compute_lifo runs every sweep, the archive
# only changes when a fill lands.
_history_memo: dict[tuple, tuple[list[Lot] | None, str | None]] = {}
_HISTORY_MEMO_MAX = 64


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
    # Where the pre-window lots came from: "history" (archived fills
    # replayed and verified against IB), "ib_avg" (one lot at IB's
    # average cost), or None when there was no pre-window position.
    seed_source: str | None = None
    # Why archived history wasn't used, when it was offered but failed.
    history_note: str | None = None
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
    history: dict[str, list[dict]] | None = None,
) -> dict[str, LifoResult]:
    """LIFO-match every FUT contract in ``executions``.

    ``executions`` are dicts shaped like ``req_recent_executions`` output
    (``local_symbol``, ``sec_type``, ``side``, ``shares``, ``price``,
    ``commission``, ``realized_pnl``, ``exec_id``, ``exec_time``,
    ``multiplier``). ``positions`` maps local_symbol → ``{quantity,
    avg_cost (per unit), multiplier}`` for currently-held FUT contracts.
    ``history`` maps local_symbol → the app's archived fills for that
    contract from BEFORE the executions window (same dict shape), used
    to rebuild the pre-window lots. Contracts held with no executions in
    the window are returned only when history rebuilds their lots —
    otherwise IB's average cost is already their basis.
    """
    by_sym: dict[str, list[dict]] = {}
    for ex in executions:
        if (ex.get("sec_type") or "").upper() != "FUT":
            continue
        sym = (ex.get("local_symbol") or "").strip()
        if sym:
            by_sym.setdefault(sym, []).append(ex)

    history = history or {}
    out: dict[str, LifoResult] = {}
    for sym, rows in by_sym.items():
        out[sym] = _compute_one(rows, positions.get(sym), history.get(sym))
    # Held with no fills in the window: only history can say anything.
    for sym, pos in positions.items():
        if sym in out or not history.get(sym):
            continue
        res = _compute_one([], pos, history[sym])
        if res.seed_source == "history":
            out[sym] = res
    return out


def _fills_from_rows(rows: list[dict]) -> tuple[list[tuple], dict] | None:
    """Sorted ``(exec_id, signed_qty, price, commission)`` tuples plus
    IB realized by exec_id; None when any row is unusable."""
    rows = sorted(rows, key=lambda r: (r["exec_time"], r.get("exec_id") or ""))
    fills: list[tuple] = []
    ib_realized: dict[str, Decimal | None] = {}
    for r in rows:
        sq = _signed_qty(r)
        price = r.get("price")
        if sq is None or price is None:
            return None
        eid = r.get("exec_id") or ""
        fills.append((eid, sq, price, r.get("commission") or _ZERO))
        ib_realized[eid] = r.get("realized_pnl")
    return fills, ib_realized


def _lots_from_history(
    hist_rows: list[dict], seed_qty: Decimal, seed_cost: Decimal,
    multiplier: Decimal,
) -> tuple[list[Lot] | None, str | None]:
    """Rebuild the seed's LIFO lots from archived fills.

    Scans back for the latest fill k such that replaying fills[k:] from
    flat reproduces IB's seed: same net quantity AND an average-cost
    replay matching the seed cost. That k is where the position was last
    flat with every later fill on record. Returns (lots, None) or
    (None, reason).
    """
    if not hist_rows:
        return None, "history_empty"
    key = (
        hist_rows[-1].get("exec_id"), len(hist_rows), seed_qty,
        seed_cost.quantize(Decimal("0.000001")), multiplier,
    )
    hit = _history_memo.get(key)
    if hit is not None:
        return ([Lot(lot.qty, lot.cost) for lot in hit[0]] if hit[0] else None), hit[1]
    parsed = _fills_from_rows(hist_rows[-_HISTORY_MAX_FILLS:])
    if parsed is None:
        result: tuple[list[Lot] | None, str | None] = (None, "history_incomplete_fill")
    else:
        fills, _ = parsed
        result = (None, "history_no_flat_point")
        net = _ZERO
        tol = abs(seed_cost) * _HISTORY_MATCH_RATIO
        for k in range(len(fills) - 1, -1, -1):
            net += fills[k][1]
            if net != seed_qty:
                continue
            suffix = fills[k:]
            avg, _, _ = _simulate_avg(_ZERO, _ZERO, suffix, multiplier)
            if avg is None or abs(avg - seed_cost) > tol:
                continue
            lots: list[Lot] = []
            for _eid, sq, price, comm in suffix:
                realize_lifo(lots, sq, price, comm, multiplier)
            result = (lots, None)
            break
    if len(_history_memo) >= _HISTORY_MEMO_MAX:
        _history_memo.clear()
    _history_memo[key] = result
    lots_out = [Lot(lot.qty, lot.cost) for lot in result[0]] if result[0] else None
    return lots_out, result[1]


def _compute_one(
    rows: list[dict], position: dict | None,
    hist_rows: list[dict] | None = None,
) -> LifoResult:
    pos = position or {}
    mult = next(
        (m for m in (_dec(r.get("multiplier")) for r in rows) if m), None,
    ) or _dec(pos.get("multiplier"))
    if not mult or mult <= 0:
        return LifoResult(method="avg", reason="unknown_multiplier")

    parsed = _fills_from_rows(rows)
    if parsed is None:
        return LifoResult(method="avg", reason="incomplete_fill")
    fills, ib_realized = parsed

    current_qty = _dec(pos.get("quantity")) or _ZERO
    seed_qty = current_qty - sum((f[1] for f in fills), _ZERO)

    lots: list[Lot] = []
    seed_source: str | None = None
    history_note: str | None = None
    if seed_qty != 0:
        if fills:
            seed_cost, reason = _solve_seed_cost(
                seed_qty, fills, mult, _dec(pos.get("avg_cost")), ib_realized,
            )
            if seed_cost is None:
                return LifoResult(method="avg", reason=reason)
            first_price = fills[0][2]
            if first_price and abs(seed_cost - first_price) / first_price > _SEED_SANITY_RATIO:
                return LifoResult(method="avg", reason="seed_cost_implausible")
        else:
            # No fills in the window: the whole position is the seed and
            # IB's current average cost is its basis.
            seed_cost = _dec(pos.get("avg_cost"))
            if seed_cost is None:
                return LifoResult(method="avg", reason="no_ib_avg_cost")
        if hist_rows:
            hist_lots, history_note = _lots_from_history(
                hist_rows, seed_qty, seed_cost, mult,
            )
        else:
            hist_lots = None
        if hist_lots is not None:
            lots.extend(hist_lots)
            seed_source = "history"
        else:
            lots.append(Lot(qty=seed_qty, cost=seed_cost))
            seed_source = "ib_avg"

    result = LifoResult(
        method="lifo", lots=lots, seed_source=seed_source,
        history_note=history_note,
    )
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
