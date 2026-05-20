"""Concrete SQLAlchemy repository implementations.

All database access goes through these classes.
No raw SQL strings — use SQLAlchemy ORM only.
Sessions are scoped and never exposed outside this module.
Contract caching logic lives here, not in a separate module.
"""
import logging
import os
import traceback
from datetime import datetime, timezone
from decimal import Decimal

import time
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import scoped_session, sessionmaker, Session

from ib_trader.data.base import (
    TradeRepositoryBase, RepriceEventRepositoryBase,
    ContractRepositoryBase, HeartbeatRepositoryBase, AlertRepositoryBase,
)
from ib_trader.data.models import (
    Base, TradeGroup, RepriceEvent, Contract,
    SystemHeartbeat, SystemAlert, TradeStatus,
)
from sqlalchemy import event as sa_event

logger = logging.getLogger(__name__)


def safe_commit(session: Session, *, retries: int = 3,
                backoff_seconds: float = 0.1) -> None:
    """Commit the session, rolling back on failure to keep it usable.

    Two failure modes this guards against:

    1. **Transient SQLite write-lock contention.** With WAL +
       ``PRAGMA busy_timeout=5000`` (set in ``create_db_engine``),
       SQLite already retries internally for 5 s before raising
       ``OperationalError("database is locked")``. We add an
       application-level fallback that retries the commit a few more
       times with a small exponential backoff for the rare cases that
       still slip through.

    2. **Session poisoning.** When ``commit()`` raises, the SQLAlchemy
       session transitions to a "pending rollback" state. Every
       subsequent operation on that session raises
       ``PendingRollbackError`` until something calls ``rollback()``.
       Because our repositories hold a ``scoped_session`` that is
       reused across calls, a single failed commit can silently break
       every subsequent write — observed 2026-05-19 in the
       ``BOT_RUNNER`` heartbeat loop where one lock event poisoned
       the session and every later heartbeat write raised
       ``PendingRollbackError``.

    This helper always calls ``rollback()`` on failure so the session
    is left clean for the next caller. If retries are exhausted, the
    original exception is re-raised so the caller sees the failure.
    """
    for attempt in range(1, retries + 1):
        try:
            session.commit()
            return
        except OperationalError as e:
            # Recover the session so it stays usable for the retry /
            # the caller's next operation.
            try:
                session.rollback()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            msg = str(e).lower()
            if "database is locked" in msg and attempt < retries:
                # Exponential backoff: 0.1 s, 0.2 s, 0.4 s …
                wait = backoff_seconds * (2 ** (attempt - 1))
                logger.warning(
                    '{"event": "SAFE_COMMIT_LOCK_RETRY", '
                    '"attempt": %d, "wait_seconds": %.3f}',
                    attempt, wait,
                )
                time.sleep(wait)
                continue
            # Non-lock OperationalError, or final retry on a lock —
            # re-raise so the caller sees it. Session is already
            # rolled back, so it's clean for the next caller.
            raise
        except Exception:
            # Any other exception during commit: roll back and re-raise.
            try:
                session.rollback()
            except Exception:  # noqa: BLE001 — best-effort cleanup
                pass
            raise


def create_db_engine(db_url: str):
    """Create a SQLAlchemy engine with WAL mode and foreign keys enabled.

    Args:
        db_url: SQLAlchemy database URL (e.g. 'sqlite:///trader.db')

    Returns:
        Configured SQLAlchemy engine.
    """
    engine = create_engine(
        db_url,
        echo=False,
        # SQLite is a local file — connections are cheap. Use a larger pool
        # to avoid QueuePool exhaustion from concurrent WebSocket polling,
        # REST endpoints, and the engine service all sharing the same DB.
        pool_size=20,
        max_overflow=30,
        pool_timeout=10,
        pool_recycle=300,
    )

    if db_url.startswith("sqlite"):
        @sa_event.listens_for(engine, "connect")
        def set_pragmas(dbapi_conn, _):
            """Enable WAL mode + foreign keys + a 5-second busy timeout on
            every connection.

            ``busy_timeout=5000`` is the critical defense against transient
            "database is locked" errors: SQLite will retry the operation
            internally for up to 5 s before raising ``OperationalError``,
            instead of giving up immediately (the default ``busy_timeout=0``).
            Catches the vast majority of writer-vs-writer contention in our
            multi-process setup (engine + bots + api + db_sink all writing)
            without surfacing user-visible errors. The session-poisoning
            guard in ``safe_commit`` below handles whatever still slips
            through that 5 s window.
            """
            dbapi_conn.execute("PRAGMA journal_mode=WAL")
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
            dbapi_conn.execute("PRAGMA busy_timeout=5000")

    # Audit hook — every SQL statement gets logged with its caller so we can
    # hunt down code paths that still reach into SQLite when they shouldn't.
    # Off by default; flip IB_TRADER_SQLITE_AUDIT=1 to enable.
    if os.environ.get("IB_TRADER_SQLITE_AUDIT") == "1":
        _skip_prefixes = ("PRAGMA", "CREATE", "BEGIN", "COMMIT", "ROLLBACK", "SAVEPOINT", "RELEASE")
        _process = os.path.basename(
            os.environ.get("IB_TRADER_PROCESS", "")
        ) or f"pid{os.getpid()}"

        @sa_event.listens_for(engine, "before_cursor_execute")
        def _audit(conn, cursor, statement, params, context, executemany):
            stmt_stripped = statement.lstrip()
            if stmt_stripped[:16].upper().startswith(_skip_prefixes):
                return
            # Walk the stack to find the closest frame outside sqlalchemy
            # and this file. That's the real caller.
            caller = "?"
            for frame in reversed(traceback.extract_stack()[:-1]):
                fn = frame.filename
                if "sqlalchemy" in fn:
                    continue
                if fn.endswith("data/repository.py"):
                    continue
                if "data/repositories/" in fn:
                    # The repo method itself — keep walking for the real caller
                    # if possible, but record as a fallback.
                    if caller == "?":
                        caller = f"{os.path.basename(fn)}:{frame.lineno} ({frame.name})"
                    continue
                caller = f"{os.path.basename(fn)}:{frame.lineno} ({frame.name})"
                break
            # Keep statement short — just the verb + table is usually enough.
            short = " ".join(statement.split())[:200]
            logger.info(
                '{"event":"SQLITE_QUERY","proc":"%s","stmt":%r,"caller":%r}',
                _process, short, caller,
            )

    return engine


def create_session_factory(engine) -> scoped_session:
    """Create a thread-safe scoped session factory.

    Args:
        engine: SQLAlchemy engine.

    Returns:
        scoped_session factory bound to the engine.
    """
    factory = sessionmaker(bind=engine)
    return scoped_session(factory)


def init_db(engine) -> None:
    """Create all tables if they do not exist.

    In production this is superseded by Alembic migrations.
    Used for in-memory SQLite in tests.
    """
    Base.metadata.create_all(engine, checkfirst=True)


def _now_utc() -> datetime:
    """Return the current UTC datetime (timezone-aware)."""
    return datetime.now(timezone.utc)


class TradeRepository(TradeRepositoryBase):
    """SQLAlchemy implementation of trade group persistence."""

    def __init__(self, session_factory: scoped_session) -> None:
        """Initialize with a scoped session factory."""
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def create(self, trade: TradeGroup) -> TradeGroup:
        """Persist a new trade group and return it."""
        s = self._session()
        s.add(trade)
        safe_commit(s)
        s.refresh(trade)
        return trade

    def get_by_serial(self, serial: int) -> TradeGroup | None:
        """Return the trade group with the given serial number, or None."""
        return (
            self._session()
            .query(TradeGroup)
            .filter(TradeGroup.serial_number == serial)
            .one_or_none()
        )

    def get_open(self) -> list[TradeGroup]:
        """Return all trade groups with status OPEN."""
        return (
            self._session()
            .query(TradeGroup)
            .filter(TradeGroup.status == TradeStatus.OPEN)
            .all()
        )

    def get_all(self) -> list[TradeGroup]:
        """Return all trade groups ordered by serial number descending."""
        return (
            self._session()
            .query(TradeGroup)
            .order_by(TradeGroup.serial_number.desc())
            .all()
        )

    def aggregate_by_source(self, source_prefix: str) -> dict[str, dict]:
        """Return ``{source: {total, today, pnl_today}}`` per source.

        TODO: ``TradeGroup`` has no ``source`` column yet. Proper
        implementation needs either a ``source`` column on trade_groups
        or a join through PendingCommand. For now returns empty —
        callers fall back to 0 for all counters (same as pre-refactor).
        """
        return {}

    def update_status(self, trade_id: str, status: TradeStatus) -> None:
        """Update the status of a trade group."""
        s = self._session()
        trade = s.query(TradeGroup).filter(TradeGroup.id == trade_id).one()
        trade.status = status
        if status == TradeStatus.CLOSED:
            trade.closed_at = _now_utc()
        safe_commit(s)

    def update_pnl(self, trade_id: str, pnl: Decimal | None, commission: Decimal) -> None:
        """Update realized P&L and total commission for a trade group.

        ``pnl=None`` skips the P&L write — for entry fills we want the
        commission recorded without overwriting the OPEN trade's
        realized_pnl with a meaningless zero. The close-path callers
        still pass the computed P&L explicitly.
        """
        s = self._session()
        trade = s.query(TradeGroup).filter(TradeGroup.id == trade_id).one()
        if pnl is not None:
            trade.realized_pnl = pnl
        trade.total_commission = commission
        safe_commit(s)

    def add_ib_realized_pnl(self, trade_id: str, pnl_delta: Decimal) -> None:
        """Add an IB ``CommissionReport.realizedPNL`` contribution to a trade.

        Sums into ``ib_realized_pnl`` so multi-execution closes accumulate
        correctly. Lives on a separate column from ``realized_pnl`` so the
        IB-authoritative value cannot collide with bot-computed P&L. The
        API serializer prefers ``ib_realized_pnl`` when set.
        """
        s = self._session()
        trade = s.query(TradeGroup).filter(TradeGroup.id == trade_id).one()
        existing = trade.ib_realized_pnl or Decimal("0")
        trade.ib_realized_pnl = existing + pnl_delta
        safe_commit(s)

    def next_serial_number(self) -> int:
        """Return the lowest unused integer serial number in range 0–999.

        Reuses the lowest available number. Wraps within 0–999.
        """
        s = self._session()
        used = {
            row.serial_number
            for row in s.query(TradeGroup.serial_number).all()
            if row.serial_number is not None
        }
        for n in range(1000):
            if n not in used:
                return n
        raise RuntimeError("All serial numbers 0–999 are in use.")


class RepriceEventRepository(RepriceEventRepositoryBase):
    """SQLAlchemy implementation of reprice event persistence."""

    def __init__(self, session_factory: scoped_session) -> None:
        """Initialize with a scoped session factory."""
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def create(self, evt: RepriceEvent) -> RepriceEvent:
        """Persist a new reprice event and return it."""
        s = self._session()
        try:
            s.add(evt)
            safe_commit(s)
            s.refresh(evt)
            return evt
        except Exception:
            s.rollback()
            raise

    def get_for_correlation_id(self, correlation_id: str) -> list[RepriceEvent]:
        """Return all reprice events for a correlation ID, ordered by step number."""
        return (
            self._session()
            .query(RepriceEvent)
            .filter(RepriceEvent.correlation_id == correlation_id)
            .order_by(RepriceEvent.step_number)
            .all()
        )

    def confirm_amendment(self, event_id: str) -> None:
        """Mark a reprice event as amendment confirmed by IB."""
        s = self._session()
        evt = s.query(RepriceEvent).filter(RepriceEvent.id == event_id).one()
        evt.amendment_confirmed = True
        safe_commit(s)


class ContractRepository(ContractRepositoryBase):
    """SQLAlchemy implementation of contract cache persistence.

    Cache logic: contracts are fresh if fetched_at is within ttl_seconds.
    Invalidation deletes the row, forcing re-fetch on next use.
    """

    def __init__(self, session_factory: scoped_session) -> None:
        """Initialize with a scoped session factory."""
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def get(self, symbol: str) -> Contract | None:
        """Return the cached contract for the symbol, or None."""
        return (
            self._session()
            .query(Contract)
            .filter(Contract.symbol == symbol)
            .one_or_none()
        )

    def upsert(self, contract: Contract) -> None:
        """Insert or update the cached contract for the symbol."""
        s = self._session()
        existing = s.query(Contract).filter(Contract.symbol == contract.symbol).one_or_none()
        if existing:
            existing.con_id = contract.con_id
            existing.exchange = contract.exchange
            existing.currency = contract.currency
            existing.multiplier = contract.multiplier
            existing.raw_response = contract.raw_response
            existing.fetched_at = contract.fetched_at
        else:
            s.add(contract)
        safe_commit(s)

    def invalidate(self, symbol: str) -> None:
        """Delete the cached contract for the symbol, forcing re-fetch."""
        s = self._session()
        s.query(Contract).filter(Contract.symbol == symbol).delete()
        safe_commit(s)

    def is_fresh(self, symbol: str, ttl_seconds: int) -> bool:
        """Return True if the cached contract is within the TTL window."""
        contract = self.get(symbol)
        if not contract:
            return False
        fetched = contract.fetched_at
        # Ensure timezone-aware comparison
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age = (_now_utc() - fetched).total_seconds()
        return age < ttl_seconds


class HeartbeatRepository(HeartbeatRepositoryBase):
    """SQLAlchemy implementation of process heartbeat persistence."""

    def __init__(self, session_factory: scoped_session) -> None:
        """Initialize with a scoped session factory."""
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def upsert(self, process: str, pid: int) -> None:
        """Insert or update the heartbeat record for a process."""
        s = self._session()
        existing = s.query(SystemHeartbeat).filter(SystemHeartbeat.process == process).one_or_none()
        if existing:
            existing.last_seen_at = _now_utc()
            existing.pid = pid
        else:
            s.add(SystemHeartbeat(process=process, last_seen_at=_now_utc(), pid=pid))
        safe_commit(s)

    def get(self, process: str) -> SystemHeartbeat | None:
        """Return the heartbeat record for a process, or None."""
        return (
            self._session()
            .query(SystemHeartbeat)
            .filter(SystemHeartbeat.process == process)
            .one_or_none()
        )

    def delete(self, process: str) -> None:
        """Delete the heartbeat record for a process (on clean exit)."""
        s = self._session()
        s.query(SystemHeartbeat).filter(SystemHeartbeat.process == process).delete()
        safe_commit(s)


class AlertRepository(AlertRepositoryBase):
    """SQLAlchemy implementation of system alert persistence."""

    def __init__(self, session_factory: scoped_session) -> None:
        """Initialize with a scoped session factory."""
        self._session_factory = session_factory

    def _session(self) -> Session:
        return self._session_factory()

    def create(self, alert: SystemAlert) -> SystemAlert:
        """Persist a new system alert and return it."""
        s = self._session()
        s.add(alert)
        safe_commit(s)
        s.refresh(alert)
        return alert

    def get_open(self) -> list[SystemAlert]:
        """Return all unresolved system alerts."""
        return (
            self._session()
            .query(SystemAlert)
            .filter(SystemAlert.resolved_at.is_(None))
            .order_by(SystemAlert.created_at)
            .all()
        )

    def resolve(self, alert_id: str) -> None:
        """Mark an alert as resolved with the current UTC timestamp."""
        s = self._session()
        alert = s.query(SystemAlert).filter(SystemAlert.id == alert_id).one()
        alert.resolved_at = _now_utc()
        safe_commit(s)
