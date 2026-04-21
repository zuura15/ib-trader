"""Per-order fill ledger — accumulates partials, emits terminal summaries.

The engine maintains one ``OrderLedger`` entry per in-flight ``ib_order_id``.
Each partial fill (IB exec-details callback) updates the ledger. When IB
signals a terminal status (Filled with remaining=0, Cancelled, Inactive,
ApiCancelled), the ledger computes cumulative aggregates and emits a
``terminal=True`` event on the ``order:updates`` Redis stream.

Progress events (``terminal=False``) are emitted on every meaningful
change — partials, status flips — so the UI can render live progression.

The ledger is in-memory only (dict keyed by ib_order_id). On engine
restart, any in-flight orders are rediscovered via ``reqAllOpenOrders``
and fresh ledger entries are created. Partial fills from the previous
engine session that weren't summarized are lost (IB is source of truth
for final state anyway).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"Filled", "Cancelled", "Inactive", "ApiCancelled"})

# IB fires a spurious "Cancelled" (perm_id=0) when re-routing an order
# between venues. The sequence is: Submitted → Cancelled → Submitted →
# Filled. The ledger must NOT emit a terminal event for these.
_PREROUTE_PREV_STATUSES = frozenset({"PreSubmitted", "Submitted", "PendingSubmit"})


@dataclass
class _Fill:
    qty: Decimal
    price: Decimal
    commission: Decimal
    exec_id: str


@dataclass
class _LedgerEntry:
    ib_order_id: str
    order_ref: str
    symbol: str
    sec_type: str
    con_id: int
    side: str              # "BUY" | "SELL"
    target_qty: Decimal
    fills: list[_Fill] = field(default_factory=list)
    last_status: str = "PendingSubmit"
    created_at: float = field(default_factory=lambda: __import__('time').monotonic())

    @property
    def filled_qty(self) -> Decimal:
        return sum((f.qty for f in self.fills), Decimal("0"))

    @property
    def avg_price(self) -> Optional[Decimal]:
        total_qty = self.filled_qty
        if total_qty == 0:
            return None
        return sum(f.qty * f.price for f in self.fills) / total_qty

    @property
    def total_commission(self) -> Decimal:
        return sum((f.commission for f in self.fills), Decimal("0"))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_terminal_status(entry: _LedgerEntry) -> str:
    """Determine the human-readable terminal status."""
    if entry.filled_qty > 0 and (
        entry.filled_qty >= entry.target_qty or entry.target_qty == 0
    ):
        # target_qty=0 means we auto-created the entry without knowing
        # the original order size. If anything filled, call it Filled.
        return "Filled"
    if entry.filled_qty == 0:
        if entry.last_status in ("Inactive",):
            return "Rejected"
        return "Cancelled"
    return "PartialFillCancelled"


class OrderLedger:
    """In-memory per-order ledger. One instance per engine process.

    Public API:
      - ``register(ib_order_id, ...)`` — create an entry when we place an order
      - ``record_fill(ib_order_id, ...)`` — called from on_fill callback
      - ``record_status(ib_order_id, ...)`` — called from on_status callback

    Each call returns a list of ``dict`` payloads to publish to the
    ``order:updates`` stream. The caller (engine event relay) does the
    XADD.
    """

    def __init__(self) -> None:
        self._entries: dict[str, _LedgerEntry] = {}
        # Recently evicted order IDs — suppress duplicate terminals from
        # IB firing both exec-details and on_status("Filled") for the same fill.
        self._recently_evicted: set[str] = set()

    def register(
        self,
        ib_order_id: str,
        order_ref: str,
        symbol: str,
        sec_type: str,
        con_id: int,
        side: str,
        target_qty: Decimal,
    ) -> list[dict]:
        """Create a ledger entry when an order is placed.

        Returns a progress event with status=Submitted.
        """
        entry = _LedgerEntry(
            ib_order_id=ib_order_id,
            order_ref=order_ref,
            symbol=symbol,
            sec_type=sec_type,
            con_id=con_id,
            side=side,
            target_qty=target_qty,
        )
        self._entries[ib_order_id] = entry
        return [self._make_event(entry, terminal=False, status="Submitted")]

    def record_fill(
        self,
        ib_order_id: str,
        qty: Decimal,
        price: Decimal,
        commission: Decimal,
        exec_id: str = "",
        *,
        order_ref: str = "",
        symbol: str = "",
        sec_type: str = "STK",
        con_id: int = 0,
        side: str = "",
        remaining: Decimal = Decimal("-1"),
        total_qty: Decimal = Decimal("-1"),
    ) -> list[dict]:
        """Record a partial or complete fill.

        If the entry doesn't exist yet (e.g., order placed by a prior
        engine session or by another IB client), a best-effort entry is
        created from the fill metadata.

        ``total_qty`` is the original order size (``trade.order.totalQuantity``).
        Preferred over ``qty + remaining`` for computing the entry's
        target_qty because IB updates ``orderStatus.remaining`` across
        all fills of a SMART-split order before any on_fill task runs —
        so by the time the first fill's callback reads remaining, it can
        already be 0 even though more fills are coming. Falls back to
        ``qty + remaining`` for paths that don't plumb totalQuantity
        (reconciler reconstructions, older callers).

        Returns one or two events:
        - Always: a progress event with last_fill_* populated
        - If this fill completes the order (filled_qty >= target_qty or
          remaining==0 with filled matching target): also a terminal event
        """
        # Short-circuit: the order was already terminalized earlier
        # (this is a LATE fill from IBEOS / split-venue delivery, or a
        # duplicate from IB firing execDetails + orderStatus("Filled")
        # for the same fill). Emit nothing — the terminal was already
        # delivered downstream; a second terminal with per-fill qty
        # would corrupt the bot's cumulative position tracking.
        if ib_order_id in self._recently_evicted:
            logger.warning(
                '{"event": "ORDER_LEDGER_LATE_FILL_AFTER_TERMINAL", '
                '"ib_order_id": "%s", "symbol": "%s", "qty": "%s", '
                '"price": "%s", "reason": "already_terminal"}',
                ib_order_id, symbol, qty, price,
            )
            return []

        entry = self._entries.get(ib_order_id)
        if entry is None:
            # Prefer the authoritative total_qty (static once placed).
            # Fall back to qty+remaining for callers that don't plumb it.
            if total_qty > 0:
                target = total_qty
            elif remaining == Decimal("-1"):
                target = qty
            else:
                target = qty + remaining
            entry = _LedgerEntry(
                ib_order_id=ib_order_id,
                order_ref=order_ref,
                symbol=symbol,
                sec_type=sec_type,
                con_id=con_id,
                side=side,
                target_qty=target,
            )
            self._entries[ib_order_id] = entry

        entry.fills.append(_Fill(qty=qty, price=price, commission=commission, exec_id=exec_id))

        events: list[dict] = []

        # Always emit a progress event for the partial
        events.append(self._make_event(
            entry,
            terminal=False,
            status="PartiallyFilled" if entry.filled_qty < entry.target_qty else "Filled",
            last_fill_qty=qty,
            last_fill_price=price,
        ))

        # Check for terminal: remaining==0 from IB OR we've accumulated target
        is_terminal = (
            (remaining == Decimal("0"))
            or (entry.filled_qty >= entry.target_qty)
            or (entry.last_status in _TERMINAL_STATUSES)
        )
        if is_terminal:
            events.append(self._make_terminal(entry))

        return events

    def record_status(
        self,
        ib_order_id: str,
        status: str,
        *,
        order_ref: str = "",
        symbol: str = "",
        sec_type: str = "STK",
        con_id: int = 0,
        side: str = "",
    ) -> list[dict]:
        """Record an IB order-status change.

        Returns events. Terminal statuses (Cancelled, Inactive, etc.)
        produce a terminal event and evict the ledger entry.
        """
        # Suppress duplicates for recently-completed orders
        if ib_order_id in self._recently_evicted:
            logger.debug(
                '{"event": "ORDER_LEDGER_SUPPRESSED", "ib_order_id": "%s", '
                '"status": "%s", "reason": "recently_evicted"}',
                ib_order_id, status,
            )
            return []

        entry = self._entries.get(ib_order_id)
        if entry is None:
            # For Cancelled/ApiCancelled on an untracked order: this is
            # likely IB's preroute cancel (the order was placed by us but
            # register() was never called). Create an entry so the
            # preroute-hold logic can evaluate it — don't immediately
            # emit a terminal event.
            if status in ("Inactive",) and not order_ref:
                # Truly untracked + terminal (manual TWS order we never placed)
                return [self._make_untracked_terminal(
                    ib_order_id, status, order_ref, symbol, sec_type, con_id, side,
                )]
            entry = _LedgerEntry(
                ib_order_id=ib_order_id,
                order_ref=order_ref,
                symbol=symbol,
                sec_type=sec_type,
                con_id=con_id,
                side=side,
                target_qty=Decimal("0"),  # unknown
            )
            self._entries[ib_order_id] = entry

        prev_status = entry.last_status
        entry.last_status = status

        events: list[dict] = []

        if status in _TERMINAL_STATUSES:
            # Guard against IB's prerouting cancel: Submitted → Cancelled
            # with zero fills is likely a venue re-route, not a real cancel.
            # Hold the entry (don't evict); if a fill arrives later the
            # cancel was spurious. If the entry ages out on the next
            # record_status call with a REAL terminal, it'll be evicted then.
            if (
                status in ("Cancelled", "ApiCancelled")
                and entry.filled_qty == 0
                and prev_status in _PREROUTE_PREV_STATUSES
            ):
                logger.info(
                    '{"event": "ORDER_LEDGER_CANCEL_HELD", "ib_order_id": "%s", '
                    '"prev_status": "%s", "reason": "possible_preroute"}',
                    entry.ib_order_id, prev_status,
                )
                events.append(self._make_event(entry, terminal=False, status="CancelHeld"))
            else:
                events.append(self._make_terminal(entry))
        else:
            events.append(self._make_event(entry, terminal=False, status=status))

        return events

    def get(self, ib_order_id: str) -> Optional[_LedgerEntry]:
        return self._entries.get(ib_order_id)

    def sweep_stale(self, max_age_seconds: float = 300) -> int:
        """Evict held entries older than max_age_seconds. Returns count evicted."""
        import time
        cutoff = time.monotonic() - max_age_seconds
        stale = [oid for oid, e in self._entries.items() if e.created_at < cutoff]
        for oid in stale:
            logger.info('{"event": "ORDER_LEDGER_STALE_EVICTED", "ib_order_id": "%s"}', oid)
            del self._entries[oid]
        # Also clear the recently_evicted set (no longer needed after sweep interval)
        self._recently_evicted.clear()
        return len(stale)

    def _make_event(
        self,
        entry: _LedgerEntry,
        *,
        terminal: bool,
        status: str,
        last_fill_qty: Optional[Decimal] = None,
        last_fill_price: Optional[Decimal] = None,
    ) -> dict:
        return {
            "ib_order_id": entry.ib_order_id,
            "orderRef": entry.order_ref,
            "symbol": entry.symbol,
            "sec_type": entry.sec_type,
            "con_id": entry.con_id,
            "side": entry.side,
            "terminal": terminal,
            "status": status,
            "target_qty": str(entry.target_qty),
            "filled_qty": str(entry.filled_qty),
            "avg_price": str(entry.avg_price) if entry.avg_price is not None else None,
            "total_commission": str(entry.total_commission),
            "last_fill_qty": str(last_fill_qty) if last_fill_qty is not None else None,
            "last_fill_price": str(last_fill_price) if last_fill_price is not None else None,
            "ts": _now_iso(),
        }

    def _make_terminal(self, entry: _LedgerEntry) -> dict:
        status = _resolve_terminal_status(entry)
        event = self._make_event(
            entry,
            terminal=True,
            status=status,
        )
        # Evict and remember — IB fires both exec-details and on_status("Filled")
        # for the same fill. Without this, the second callback creates a fresh
        # entry and emits a spurious terminal.
        self._entries.pop(entry.ib_order_id, None)
        self._recently_evicted.add(entry.ib_order_id)
        logger.info(
            '{"event": "ORDER_LEDGER_TERMINAL", "ib_order_id": "%s", '
            '"status": "%s", "filled_qty": "%s", "avg_price": "%s"}',
            entry.ib_order_id, status, entry.filled_qty, entry.avg_price,
        )
        return event

    def _make_untracked_terminal(
        self, ib_order_id: str, status: str, order_ref: str,
        symbol: str, sec_type: str, con_id: int, side: str,
    ) -> dict:
        """Terminal event for an order we never tracked (manual / prior session)."""
        resolved = "Cancelled" if status in ("Cancelled", "ApiCancelled") else "Rejected"
        return {
            "ib_order_id": ib_order_id,
            "orderRef": order_ref,
            "symbol": symbol,
            "sec_type": sec_type,
            "con_id": con_id,
            "side": side,
            "terminal": True,
            "status": resolved,
            "target_qty": "0",
            "filled_qty": "0",
            "avg_price": None,
            "total_commission": "0",
            "last_fill_qty": None,
            "last_fill_price": None,
            "ts": _now_iso(),
        }
