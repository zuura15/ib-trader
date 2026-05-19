"""Live SQLite sink for trade-relevant structured log events.

Mirrors selected JSON-structured logger events into the
``log_events`` SQLite table so post-trade investigations can query
them with SQL instead of grep'ing ``ib_trader.log``. The audit_log
table already covers BAR_EVAL / ORDER_PLACED / TRADE_CLOSED;
log_events covers the process-level events (FSM transitions, fills,
SL fires, commissions, cancel/fill races).

Design:
- Trading path never blocks on the DB write. ``logging.Handler.emit``
  parses the JSON message, checks the allowlist, and pushes a row
  onto a bounded queue. Queue overflow is silently dropped (logging
  is fire-and-forget) — overflow means the writer thread is stuck,
  not that a trade is at risk.
- A daemon thread drains the queue, batches inserts, and runs a
  retention prune every 5 min (delete rows older than 48 h).
- Multiple processes (ib-bots / ib-engine / ib-api) can each install
  the sink. SQLite WAL mode supports concurrent writers; the same
  trader.db already takes simultaneous writes from audit_log inserts.

Allowlist (prefix-based, on the ``event`` field of the JSON message):
  BAR_*, ORDER_*, FILL_*, FSM_*, SL_*, EXIT_*, ENTRY_*, CLOSE_*,
  BOT_TRADE_*, BOT_POSITION_*, BOT_ORDER_*, IB_COMMISSION_*,
  IB_CANCEL_*, CANCEL_FILL_*, plus BOT_RECONCILED exactly.

Explicit denylist trims known-noisy prefixed events that aren't
actually trade-relevant.

To install in a daemon entry point:
    from ib_trader.logging_.db_sink import install_db_sink
    install_db_sink("trader.db")
"""
from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)


# Allowlist by prefix on the JSON ``event`` field.
_TRADE_PREFIXES: tuple[str, ...] = (
    "BAR_", "ORDER_", "FILL_", "FSM_", "SL_",
    "EXIT_", "ENTRY_", "CLOSE_",
    "BOT_TRADE_", "BOT_POSITION_", "BOT_ORDER_",
    "IB_COMMISSION_", "IB_CANCEL_", "CANCEL_FILL_",
)
_TRADE_EXACT: frozenset[str] = frozenset({"BOT_RECONCILED"})

# Known noisy events that share a prefix with the allowlist but
# aren't price-action / bot-decision relevant.
_DENYLIST: frozenset[str] = frozenset({
    "BAR_PUBLISH_ERROR",  # engine plumbing noise; lives in ib_trader.log
})


_RETENTION = timedelta(hours=48)
_PRUNE_INTERVAL_S = 300.0  # 5 min
_BATCH_SIZE = 100
_QUEUE_MAX = 10_000
_DRAIN_TIMEOUT_S = 1.0


def _is_trade_relevant(event: str) -> bool:
    """Return True when this event name belongs in log_events."""
    if event in _DENYLIST:
        return False
    if event in _TRADE_EXACT:
        return True
    return any(event.startswith(p) for p in _TRADE_PREFIXES)


_CREATE_SQL = (
    "CREATE TABLE IF NOT EXISTS log_events ("
    "  id      INTEGER PRIMARY KEY,"
    "  ts      TEXT NOT NULL,"
    "  event   TEXT NOT NULL,"
    "  symbol  TEXT,"
    "  bot_id  TEXT,"
    "  payload TEXT,"
    "  raw     TEXT"
    ");"
)
_INDEX_SQL = (
    "CREATE INDEX IF NOT EXISTS ix_log_events_ts        ON log_events(ts);",
    "CREATE INDEX IF NOT EXISTS ix_log_events_event_ts  ON log_events(event, ts);",
    "CREATE INDEX IF NOT EXISTS ix_log_events_symbol_ts ON log_events(symbol, ts);",
    "CREATE INDEX IF NOT EXISTS ix_log_events_bot_ts    ON log_events(bot_id, ts);",
)
_INSERT_SQL = (
    "INSERT INTO log_events (ts, event, symbol, bot_id, payload, raw) "
    "VALUES (?, ?, ?, ?, ?, ?)"
)
_PRUNE_SQL = "DELETE FROM log_events WHERE ts < ?"


class DBSinkHandler(logging.Handler):
    """``logging.Handler`` that mirrors trade-relevant JSON events
    into the ``log_events`` SQLite table on a background thread."""

    def __init__(self, db_path: Union[str, Path]):
        super().__init__()
        self._db_path = str(db_path)
        self._queue: "queue.Queue[tuple]" = queue.Queue(maxsize=_QUEUE_MAX)
        self._stop = threading.Event()
        self._writer = threading.Thread(
            target=self._run, name="log-db-sink", daemon=True,
        )
        self._writer.start()

    def emit(self, record: logging.LogRecord) -> None:
        """Synchronous, non-blocking. Parse → filter → enqueue."""
        try:
            msg = record.getMessage()
            if not msg or msg[:1] != "{":
                return  # plain-text log line; skip
            parsed = json.loads(msg)
            if not isinstance(parsed, dict):
                return
            event = parsed.get("event")
            if not isinstance(event, str) or not event:
                return
            if not _is_trade_relevant(event):
                return
            ts = datetime.now().astimezone().isoformat()
            symbol = parsed.get("symbol")
            bot_id = parsed.get("bot_id")
            row = (
                ts,
                event,
                symbol if isinstance(symbol, str) else None,
                bot_id if isinstance(bot_id, str) else None,
                msg,    # payload = the full JSON object
                None,   # raw — unused; payload IS the raw JSON here
            )
            self._queue.put_nowait(row)
        except queue.Full:
            # Logging must not block trading. Drop on overflow.
            return
        except Exception:
            # Logging must never raise. Swallow EVERY error here.
            return

    def close(self) -> None:
        """Stop the background writer. Called by logging.shutdown()."""
        self._stop.set()
        super().close()

    def _run(self) -> None:
        """Background thread main loop.

        Opens its own sqlite3 connection (per-thread). Loops:
        - block for up to 1 s on the first queue item
        - drain up to BATCH_SIZE more items non-blocking
        - executemany the batch
        - prune retention every PRUNE_INTERVAL_S
        Each step is wrapped in a broad exception swallow so a bad
        row or a transient DB lock never kills the writer.
        """
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(self._db_path, isolation_level=None)
            # WAL so we don't contend with the runtime's audit_log
            # writes. Idempotent — already on if another process set it.
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(_CREATE_SQL)
            for sql in _INDEX_SQL:
                conn.execute(sql)
        except Exception:
            # DB unreachable — sink becomes a no-op. Don't crash the
            # process.
            return

        last_prune = time.time()

        while not self._stop.is_set():
            try:
                first = self._queue.get(timeout=_DRAIN_TIMEOUT_S)
            except queue.Empty:
                first = None

            batch: list[tuple] = []
            if first is not None:
                batch.append(first)
                while len(batch) < _BATCH_SIZE:
                    try:
                        batch.append(self._queue.get_nowait())
                    except queue.Empty:
                        break

            if batch:
                try:
                    conn.executemany(_INSERT_SQL, batch)
                except Exception:
                    # Don't propagate. A locked DB or a malformed row
                    # shouldn't take down the writer.
                    pass

            now = time.time()
            if now - last_prune >= _PRUNE_INTERVAL_S:
                cutoff = (
                    datetime.now(timezone.utc) - _RETENTION
                ).isoformat()
                try:
                    conn.execute(_PRUNE_SQL, (cutoff,))
                except Exception:
                    pass
                last_prune = now

        try:
            conn.close()
        except Exception:
            pass


def install_db_sink(
    db_path: Union[str, Path] = "trader.db",
) -> DBSinkHandler | None:
    """Install the DB sink on the root logger.

    Idempotent — if a DBSinkHandler is already attached, returns it
    without installing a second one. Level is set to INFO since
    trade-relevant events are all INFO+ and DEBUG records would
    waste cycles in emit() before being filtered out anyway.

    Returns the handler, or ``None`` if installation failed.
    """
    root = logging.getLogger()
    for h in root.handlers:
        if isinstance(h, DBSinkHandler):
            return h
    try:
        handler = DBSinkHandler(db_path)
        handler.setLevel(logging.INFO)
        root.addHandler(handler)
        logger.info(
            '{"event": "DB_SINK_INSTALLED", "db_path": "%s"}', db_path,
        )
        return handler
    except Exception:
        logger.exception(
            '{"event": "DB_SINK_INSTALL_FAILED", "db_path": "%s"}',
            db_path,
        )
        return None
