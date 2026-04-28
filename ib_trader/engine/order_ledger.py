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
from typing import Callable, Optional

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"Filled", "Cancelled", "Inactive", "ApiCancelled"})

# ib_async uses "0" (or empty) for the client-side orderId on status
# events that have no matching live trade — e.g. foreign orders IB
# re-delivers on reconnect for orders placed by a prior engine
# session or a different IB client. These are phantoms from our
# perspective; creating a ledger entry for them wires them into
# check_stuck and produces useless WARNING alerts with empty
# metadata. Filter at the auto-create boundary.
_PHANTOM_IB_ORDER_IDS: frozenset[str] = frozenset({"", "0"})

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
    # True once the engine's timeout watchdog has raised a panic alert
    # for this entry. Prevents the same stuck entry from re-alerting on
    # every watchdog tick.
    stuck_alerted: bool = False
    # IB-reported net position for this symbol immediately *before* the
    # order was placed. ``None`` if not snapshotted (e.g. auto-created
    # entries for orders we didn't place). Used by ``_apply_position_diff``
    # at terminal time to detect when IB processed shares that we never
    # received fill events for (re-route quirks, callback drops, late
    # fills) — the broker-side position is the ground truth.
    pre_position: Optional[Decimal] = None

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

    def __init__(
        self,
        position_getter: Optional[Callable[[str, str], Decimal]] = None,
    ) -> None:
        """``position_getter(symbol, sec_type) -> qty`` returns the broker's
        current net position for the symbol. Used at terminal-emit time
        to reconcile against pre-place snapshots when our tracked fills
        fall short of the order's target."""
        self._entries: dict[str, _LedgerEntry] = {}
        # Recently evicted order IDs — suppress duplicate terminals from
        # IB firing both exec-details and on_status("Filled") for the same fill.
        self._recently_evicted: set[str] = set()
        self._position_getter = position_getter

    def register(
        self,
        ib_order_id: str,
        order_ref: str,
        symbol: str,
        sec_type: str,
        con_id: int,
        side: str,
        target_qty: Decimal,
        *,
        pre_position: Optional[Decimal] = None,
    ) -> list[dict]:
        """Create a ledger entry when an order is placed.

        ``pre_position`` is the broker's net position for ``symbol``
        immediately *before* this order was placed. Stashed for the
        position-diff reconcile at terminal-emit time.

        If an entry already exists for this ``ib_order_id`` (a fill or
        status auto-created it during a race), populate the missing
        target_qty / pre_position fields rather than overwriting fills.

        Returns a progress event with status=Submitted.
        """
        existing = self._entries.get(ib_order_id)
        if existing is not None:
            # Auto-create races: backfill the fields the auto-create
            # didn't know about. Don't clobber accumulated fills.
            if existing.target_qty == 0 and target_qty > 0:
                existing.target_qty = target_qty
            if existing.pre_position is None and pre_position is not None:
                existing.pre_position = pre_position
            return [self._make_event(existing, terminal=False, status="Submitted")]

        entry = _LedgerEntry(
            ib_order_id=ib_order_id,
            order_ref=order_ref,
            symbol=symbol,
            sec_type=sec_type,
            con_id=con_id,
            side=side,
            target_qty=target_qty,
            pre_position=pre_position,
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
        """Record a partial or complete fill — ALWAYS non-terminal.

        The ledger never derives a terminal event from fill data. Terminal
        events are only emitted by ``record_status`` when IB delivers an
        explicit terminal status (Filled, Cancelled, Inactive, ApiCancelled).
        If IB never sends one, the engine's watchdog (``check_stuck``)
        surfaces the stuck order as a panic alert instead. This keeps
        the ledger from second-guessing IB — fill timing, split routing,
        and event-dispatch races can all mis-trigger a self-derived
        terminal, which in practice corrupted bot position tracking.

        Returns a single progress event with ``last_fill_*`` populated.

        If the entry doesn't exist yet (e.g., order placed by a prior
        engine session or by another IB client), a best-effort entry is
        created from the fill metadata. ``total_qty`` is preferred for
        target_qty; ``qty + remaining`` is the fallback.
        """
        # A fill arriving after the entry was evicted (IB already sent a
        # terminal status) indicates late delivery — IBEOS overnight
        # exec-details, duplicate callbacks, or split-venue quirks.
        # Log loudly and drop: the terminal has already been relayed
        # downstream; re-emitting anything would double-count.
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
            if ib_order_id in _PHANTOM_IB_ORDER_IDS:
                logger.debug(
                    '{"event": "ORDER_LEDGER_PHANTOM_FILL_IGNORED", '
                    '"ib_order_id": "%s", "symbol": "%s", "qty": "%s"}',
                    ib_order_id, symbol, qty,
                )
                return []
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

        status = "PartiallyFilled"
        if entry.target_qty > 0 and entry.filled_qty >= entry.target_qty:
            status = "Filled"
        return [self._make_event(
            entry,
            terminal=False,
            status=status,
            last_fill_qty=qty,
            last_fill_price=price,
        )]

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
            # Phantom status events: IB re-delivers live open orders on
            # reconnect, and ib_async reports them with ib_order_id="0"
            # when no client-side trade record exists (foreign orders
            # from a prior session or different client). Creating an
            # entry for these wires them into check_stuck and fires a
            # useless WARNING 5 min later. Drop them at the boundary.
            if ib_order_id in _PHANTOM_IB_ORDER_IDS:
                logger.debug(
                    '{"event": "ORDER_LEDGER_PHANTOM_STATUS_IGNORED", '
                    '"ib_order_id": "%s", "status": "%s", "symbol": "%s"}',
                    ib_order_id, status, symbol,
                )
                return []
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
            # Guard against IB's prerouting cancel: while re-routing an
            # order between venues, IB sends a spurious "Cancelled" with
            # the previous-venue stats. The sequence is
            # Submitted/PreSubmitted → Cancelled → Submitted → Filled.
            # Two shapes seen in production:
            #   1. zero fills (re-route before any execution)
            #   2. partial fill + remaining > 0 (the first venue executed
            #      N shares before the cancel, the rest re-routed). The
            #      GLD bug: 34/114 filled, then "Cancelled" with
            #      remaining=80, then 80 more fills + Filled status.
            # We hold the entry on either shape; if a fill or non-terminal
            # status arrives later the cancel was spurious. If no follow-up
            # arrives the watchdog (`check_stuck`) raises a WARNING per the
            # "ledger never self-derives terminal" rule.
            cancel_might_be_preroute = (
                status in ("Cancelled", "ApiCancelled")
                and prev_status in _PREROUTE_PREV_STATUSES
                and (
                    entry.filled_qty == 0
                    or (entry.target_qty > 0
                        and entry.filled_qty < entry.target_qty)
                )
            )
            if cancel_might_be_preroute:
                logger.info(
                    '{"event": "ORDER_LEDGER_CANCEL_HELD", "ib_order_id": "%s", '
                    '"prev_status": "%s", "filled_qty": "%s", "target_qty": "%s", '
                    '"reason": "possible_preroute"}',
                    entry.ib_order_id, prev_status,
                    entry.filled_qty, entry.target_qty,
                )
                events.append(self._make_event(entry, terminal=False, status="CancelHeld"))
            else:
                events.append(self._make_terminal(entry))
        else:
            events.append(self._make_event(entry, terminal=False, status=status))

        return events

    def get(self, ib_order_id: str) -> Optional[_LedgerEntry]:
        return self._entries.get(ib_order_id)

    def check_stuck(self, timeout_seconds: float) -> list[_LedgerEntry]:
        """Return entries older than ``timeout_seconds`` that IB has not
        yet terminalized.

        The caller (engine watchdog) raises a user-facing panic alert for
        each returned entry and marks it as alerted so the same entry does
        not re-alert on every tick. Entries are NOT evicted — they stay in
        the ledger so the user can inspect/acknowledge. The engine relies
        on IB as the eventual source of truth; a stuck entry is an
        escalation path for the human, not a cleanup hook.

        Also drops any ``_recently_evicted`` IDs older than the timeout
        window (the set is only useful for the brief period after a
        terminal to deduplicate late fills).
        """
        import time
        cutoff = time.monotonic() - timeout_seconds
        stuck: list[_LedgerEntry] = []
        for entry in self._entries.values():
            if entry.stuck_alerted:
                continue
            if entry.created_at >= cutoff:
                continue
            # Fully-filled entries whose status flip we're still waiting
            # on count as stuck — IB should have sent Filled by now.
            entry.stuck_alerted = True
            stuck.append(entry)
        # The duplicate-late-fill guard is only load-bearing for the few
        # seconds after a terminal; beyond the timeout window, any
        # genuinely late fill belongs in a reconciler DISCREPANCY alert,
        # not silent suppression.
        self._recently_evicted.clear()
        return stuck

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

    def _apply_position_diff(self, entry: _LedgerEntry) -> Optional[Decimal]:
        """Reconcile entry.filled_qty against the broker-side position diff.

        Honors the no-self-derive rule: the ledger never invents a
        terminal — IB has already given us one (or a Cancelled that the
        caller of ``_make_terminal`` deemed terminal). What we *do* here
        is decide *what qty to attribute* on a terminal that arrived with
        ``filled_qty < target_qty``. IB occasionally drops fill events
        during venue re-routes; the broker's net position is the
        ground truth fallback.

        Returns the effective filled qty (≥ ``entry.filled_qty``) when
        an upgrade applies, or ``None`` to leave ``entry.filled_qty``
        alone.

        Guards:
        - No upgrade unless ``pre_position`` was snapshotted at place time
          AND a ``position_getter`` is wired AND ``target_qty > 0``.
        - No upgrade if ``filled_qty`` already meets ``target_qty``.
        - No upgrade if we tracked zero fills — too dangerous to invent
          a fill price; let the operator reconcile.
        - The diff is capped at ``target_qty`` so we never over-attribute
          on concurrent same-symbol activity.
        """
        if self._position_getter is None or entry.pre_position is None:
            return None
        if entry.target_qty <= 0 or entry.filled_qty >= entry.target_qty:
            return None
        if entry.filled_qty == 0:
            return None
        try:
            current_qty = self._position_getter(entry.symbol, entry.sec_type)
        except Exception as e:
            logger.warning(
                '{"event": "ORDER_LEDGER_POSITION_LOOKUP_FAILED", '
                '"ib_order_id": "%s", "symbol": "%s", "error": "%s"}',
                entry.ib_order_id, entry.symbol, str(e),
            )
            return None

        if entry.side == "BUY":
            delta = current_qty - entry.pre_position
        elif entry.side == "SELL":
            delta = entry.pre_position - current_qty
        else:
            return None

        # Floor at tracked fills, cap at the order target — never lose
        # what we already know, never claim more than we asked for.
        effective = min(max(delta, entry.filled_qty), entry.target_qty)
        if effective <= entry.filled_qty:
            return None

        logger.info(
            '{"event": "ORDER_LEDGER_POSITION_DIFF_RECONCILE", '
            '"ib_order_id": "%s", "symbol": "%s", "side": "%s", '
            '"pre_position": "%s", "current_position": "%s", '
            '"raw_delta": "%s", "tracked_filled": "%s", '
            '"target": "%s", "effective": "%s"}',
            entry.ib_order_id, entry.symbol, entry.side,
            entry.pre_position, current_qty, delta,
            entry.filled_qty, entry.target_qty, effective,
        )
        return effective

    def _make_terminal(self, entry: _LedgerEntry) -> dict:
        # Position-diff reconcile: if IB's net position confirms shares
        # moved beyond what our tracked fills reflect (callback drop /
        # re-route quirk), synthesize a "ghost" fill for the gap so the
        # emitted terminal carries the broker-truth qty. The ghost uses
        # the most recent tracked fill price as its best estimate.
        # Strict guards live in ``_apply_position_diff``.
        effective_qty = self._apply_position_diff(entry)
        if effective_qty is not None and effective_qty > entry.filled_qty:
            gap_qty = effective_qty - entry.filled_qty
            ghost_price = entry.fills[-1].price if entry.fills else Decimal("0")
            entry.fills.append(_Fill(
                qty=gap_qty,
                price=ghost_price,
                commission=Decimal("0"),
                exec_id="POSITION_DIFF_RECONCILE",
            ))

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
