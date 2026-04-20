"""Repository for the append-only TransactionEvent audit log.

Rows are never updated or deleted. Each row records a single interaction
our system has with IB around an order.

After the orders table removal, this is the sole persistent record of
order state. IB is the source of truth for live state; transactions are
the source of truth for historical state and trade-group linkage.
"""
from sqlalchemy.orm import scoped_session, Session
from sqlalchemy import func

from ib_trader.data.models import (
    TransactionEvent, TransactionAction, LegType,
)

# Actions that mark an order as terminally complete. Used by insert() to
# derive is_terminal automatically, preventing code paths from accidentally
# leaving orders "open" forever.
_TERMINAL_ACTIONS: frozenset[TransactionAction] = frozenset({
    TransactionAction.FILLED,
    TransactionAction.CANCELLED,
    TransactionAction.PLACE_REJECTED,
    TransactionAction.ERROR_TERMINAL,
    TransactionAction.RECONCILED,
})


class TransactionRepository:
    """SQLAlchemy repository for TransactionEvent persistence.

    All methods are insert or read — no updates or deletes.
    """

    def __init__(self, session_factory: scoped_session) -> None:
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def insert(self, event: TransactionEvent) -> None:
        """Persist a new transaction event.

        Automatically derives is_terminal from the action if not explicitly set,
        ensuring terminal actions are never accidentally left as non-terminal.
        """
        if event.action in _TERMINAL_ACTIONS:
            event.is_terminal = True
        s = self._session()
        s.add(event)
        s.commit()

    # -----------------------------------------------------------------------
    # Queries by IB order ID
    # -----------------------------------------------------------------------

    def get_open_orders(self) -> list[TransactionEvent]:
        """Return the most recent row per ib_order_id where is_terminal is False.

        Only includes rows with a non-null ib_order_id. For each distinct
        ib_order_id, returns the row with the highest id (most recent insert).
        Excludes any ib_order_id whose most recent row has is_terminal=True.
        """
        s = self._session()
        latest = (
            s.query(func.max(TransactionEvent.id).label("max_id"))
            .filter(TransactionEvent.ib_order_id.isnot(None))
            .group_by(TransactionEvent.ib_order_id)
            .subquery()
        )
        return (
            s.query(TransactionEvent)
            .join(latest, TransactionEvent.id == latest.c.max_id)
            .filter(TransactionEvent.is_terminal == False)
            .all()
        )

    def get_by_ib_order_id(self, ib_order_id: int) -> list[TransactionEvent]:
        """Return all rows for a given IB order ID, chronological."""
        return (
            self._session()
            .query(TransactionEvent)
            .filter(TransactionEvent.ib_order_id == ib_order_id)
            .order_by(TransactionEvent.requested_at.asc())
            .all()
        )

    def get_latest_by_ib_order_id(self, ib_order_id: int) -> TransactionEvent | None:
        """Return the most recent row for a given IB order ID."""
        return (
            self._session()
            .query(TransactionEvent)
            .filter(TransactionEvent.ib_order_id == ib_order_id)
            .order_by(TransactionEvent.id.desc())
            .first()
        )

    # -----------------------------------------------------------------------
    # Queries by trade
    # -----------------------------------------------------------------------

    def get_for_trade(self, trade_id: str) -> list[TransactionEvent]:
        """Return all transactions for a trade group, chronological."""
        return (
            self._session()
            .query(TransactionEvent)
            .filter(TransactionEvent.trade_id == trade_id)
            .order_by(TransactionEvent.id.asc())
            .all()
        )

    def get_for_trade_serial(self, trade_serial: int) -> list[TransactionEvent]:
        """Return all transactions for a trade serial number, chronological."""
        return (
            self._session()
            .query(TransactionEvent)
            .filter(TransactionEvent.trade_serial == trade_serial)
            .order_by(TransactionEvent.id.asc())
            .all()
        )

    def get_entry_fill(self, trade_id: str) -> TransactionEvent | None:
        """Return the FILLED or PARTIAL_FILL transaction for the ENTRY leg of a trade.

        Used for P&L calculation: provides avg_fill_price, filled_qty,
        commission for the entry. Checks FILLED first, falls back to
        PARTIAL_FILL so partially-filled entries are still closeable.
        """
        s = self._session()
        # Prefer full fill
        full = (
            s.query(TransactionEvent)
            .filter(
                TransactionEvent.trade_id == trade_id,
                TransactionEvent.leg_type == LegType.ENTRY,
                TransactionEvent.action == TransactionAction.FILLED,
            )
            .first()
        )
        if full:
            return full
        # Fall back to partial fill
        return (
            s.query(TransactionEvent)
            .filter(
                TransactionEvent.trade_id == trade_id,
                TransactionEvent.leg_type == LegType.ENTRY,
                TransactionEvent.action == TransactionAction.PARTIAL_FILL,
            )
            .order_by(TransactionEvent.id.desc())
            .first()
        )

    def get_filled_legs(self, trade_id: str) -> list[TransactionEvent]:
        """Return all FILLED and PARTIAL_FILL transactions for a trade.

        Used for P&L computation across entry + profit taker + close legs.
        Includes partial fills so that partially-filled positions are
        correctly reflected in P&L and remaining-quantity calculations.
        """
        return (
            self._session()
            .query(TransactionEvent)
            .filter(
                TransactionEvent.trade_id == trade_id,
                TransactionEvent.action.in_([
                    TransactionAction.FILLED,
                    TransactionAction.PARTIAL_FILL,
                ]),
            )
            .order_by(TransactionEvent.id.asc())
            .all()
        )

    def get_open_for_trade(self, trade_id: str) -> list[TransactionEvent]:
        """Return non-terminal legs for a trade (latest state per ib_order_id).

        Used by execute_close to find open profit-taker/stop-loss legs to cancel.
        """
        s = self._session()
        latest = (
            s.query(func.max(TransactionEvent.id).label("max_id"))
            .filter(
                TransactionEvent.trade_id == trade_id,
                TransactionEvent.ib_order_id.isnot(None),
            )
            .group_by(TransactionEvent.ib_order_id)
            .subquery()
        )
        return (
            s.query(TransactionEvent)
            .join(latest, TransactionEvent.id == latest.c.max_id)
            .filter(TransactionEvent.is_terminal == False)
            .all()
        )

    def get_trade_leg_summary(self, trade_id: str) -> list[TransactionEvent]:
        """Return the latest transaction per ib_order_id for a trade.

        Provides the current state of each leg (open, filled, cancelled, etc.)
        for trade group closing logic and display.
        """
        s = self._session()
        latest = (
            s.query(func.max(TransactionEvent.id).label("max_id"))
            .filter(
                TransactionEvent.trade_id == trade_id,
                TransactionEvent.ib_order_id.isnot(None),
            )
            .group_by(TransactionEvent.ib_order_id)
            .subquery()
        )
        return (
            s.query(TransactionEvent)
            .join(latest, TransactionEvent.id == latest.c.max_id)
            .all()
        )

    def has_unconfirmed_placements(self, trade_id: str) -> bool:
        """Check if a trade has PLACE_ATTEMPT rows without a corresponding
        PLACE_ACCEPTED or terminal event.

        Used for crash recovery: these are orders that may or may not have
        reached IB before the crash.
        """
        s = self._session()
        txns = (
            s.query(TransactionEvent)
            .filter(TransactionEvent.trade_id == trade_id)
            .all()
        )

        # Find correlation_ids that have PLACE_ATTEMPT but no PLACE_ACCEPTED
        # and no terminal event
        attempts: dict[str | None, bool] = {}
        for t in txns:
            cid = t.correlation_id
            if t.action == TransactionAction.PLACE_ATTEMPT:
                if cid not in attempts:
                    attempts[cid] = False  # not yet confirmed
            elif t.action == TransactionAction.PLACE_ACCEPTED:
                attempts[cid] = True
            elif t.is_terminal:
                attempts[cid] = True

        return any(not confirmed for confirmed in attempts.values())

    def get_by_correlation_id(self, correlation_id: str) -> list[TransactionEvent]:
        """Return all transactions with a given correlation ID.

        Links reprice events to their transaction sequence.
        """
        return (
            self._session()
            .query(TransactionEvent)
            .filter(TransactionEvent.correlation_id == correlation_id)
            .order_by(TransactionEvent.id.asc())
            .all()
        )
