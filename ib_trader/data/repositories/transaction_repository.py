"""Repository for the append-only TransactionEvent audit log.

Rows are never updated or deleted. Each row records a single interaction
our system has with IB around an order.
"""
from sqlalchemy.orm import scoped_session, Session
from sqlalchemy import func

from ib_trader.data.models import TransactionEvent


class TransactionRepository:
    """SQLAlchemy repository for TransactionEvent persistence.

    All methods are insert or read — no updates or deletes.
    """

    def __init__(self, session_factory: scoped_session) -> None:
        """Initialize with a scoped session factory.

        Args:
            session_factory: Thread-safe scoped session factory.
        """
        self._session_factory = session_factory

    def _session(self) -> Session:
        """Return the current scoped session."""
        return self._session_factory()

    def insert(self, event: TransactionEvent) -> None:
        """Persist a new transaction event.

        Args:
            event: TransactionEvent to insert.
        """
        s = self._session()
        s.add(event)
        s.commit()

    def get_open_orders(self) -> list[TransactionEvent]:
        """Return the most recent row per ib_order_id where is_terminal is False.

        Only includes rows that have a non-null ib_order_id. For each distinct
        ib_order_id, returns the row with the highest id (most recent insert).
        Excludes any ib_order_id whose most recent row has is_terminal=True.

        Returns:
            List of TransactionEvent rows, one per open order.
        """
        s = self._session()

        # Subquery: max id per ib_order_id
        latest = (
            s.query(func.max(TransactionEvent.id).label("max_id"))
            .filter(TransactionEvent.ib_order_id.isnot(None))
            .group_by(TransactionEvent.ib_order_id)
            .subquery()
        )

        # Join back to get full rows, filter non-terminal
        rows = (
            s.query(TransactionEvent)
            .join(latest, TransactionEvent.id == latest.c.max_id)
            .filter(TransactionEvent.is_terminal == False)  # noqa: E712
            .all()
        )
        return rows

    def get_by_ib_order_id(self, ib_order_id: int) -> list[TransactionEvent]:
        """Return all rows for a given IB order ID, sorted by requested_at ascending.

        Args:
            ib_order_id: The IB-assigned order ID.

        Returns:
            List of TransactionEvent rows in chronological order.
        """
        return (
            self._session()
            .query(TransactionEvent)
            .filter(TransactionEvent.ib_order_id == ib_order_id)
            .order_by(TransactionEvent.requested_at.asc())
            .all()
        )
