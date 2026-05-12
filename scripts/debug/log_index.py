"""Fast debug index over ``logs/ib_trader.log``.

Reads new lines since the last invocation (file offset persisted at
``/tmp/ib_trader_log_indexer.pos``) and inserts a slim row per
interesting event into a ``log_events`` SQLite table. Subsequent
``explain.py`` lookups query this indexed table instead of grepping
a multi-MB log file, taking lookup latency from seconds to ms.

Design notes:
- Schema is created lazily on first run; no alembic migration. This
  is a transactional debug cache — losing it costs nothing.
- Old rows (> 48h) are pruned at the end of each refresh so the table
  stays bounded.
- Idempotent: calling refresh twice in a row is a no-op the second
  time (pointer file tracks position).
- A live in-engine sweeper is the obvious next step. Left out of
  this first cut because lazy-on-demand is enough for the operator's
  current workflow ("I'll ask about an event minutes-to-hours later").
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[2]
DB_PATH = ROOT / "trader.db"
LOG_PATH = ROOT / "logs" / "ib_trader.log"
POS_FILE = Path("/tmp/ib_trader_log_indexer.pos")

# Events worth indexing for debug investigations. Keep this list
# small — every extra event_type adds rows to the index but doesn't
# help the typical "why didn't the bot fire" lookup. Add new entries
# only when an investigation actually requires them.
INTERESTING_EVENTS = {
    "ORDER_CREATED",
    "ORDER_PLACED",
    "ORDER_FILLED",
    "ORDER_REJECTED",
    "ORDER_CANCELLED",
    "FILL_RELAYED",
    "FSM_TRANSITION",
    "IB_ERROR",
    "SUBSCRIBE_BARS_FAILED",
    "UNSUBSCRIBE_BARS_FAILED",
    "BOT_STARTUP_FORCED_OFF",
    "BOT_AUTOSTARTED",
    "BOT_TASK_STARTED",
    "STRATEGY_BOT_STARTED",
    "WARMUP_BARS_PUBLISHED",
    "BOT_ORDER_HTTP",
    "CHART_SIGNAL_HISTORY_FETCH_FAILED",
}

# Drop log_events older than this. 48h is plenty for active-session
# debugging; longer windows force a full re-index from the log file.
RETENTION = timedelta(hours=48)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS log_events (
    id      INTEGER PRIMARY KEY,
    ts      TEXT NOT NULL,
    event   TEXT NOT NULL,
    symbol  TEXT,
    bot_id  TEXT,
    payload TEXT,
    raw     TEXT
);
CREATE INDEX IF NOT EXISTS ix_log_events_ts        ON log_events(ts);
CREATE INDEX IF NOT EXISTS ix_log_events_event_ts  ON log_events(event, ts);
CREATE INDEX IF NOT EXISTS ix_log_events_symbol_ts ON log_events(symbol, ts);
CREATE INDEX IF NOT EXISTS ix_log_events_bot_ts    ON log_events(bot_id, ts);
"""


def ensure_schema(db: sqlite3.Connection) -> None:
    for stmt in _CREATE_SQL.strip().split(";"):
        s = stmt.strip()
        if s:
            db.execute(s)
    db.commit()


def _read_pos() -> int:
    try:
        return int(POS_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def _write_pos(pos: int) -> None:
    POS_FILE.write_text(str(pos))


def _parse_line(line: str) -> tuple | None:
    """Return (ts, event, symbol, bot_id, payload_json, raw) or None.

    Lines without a parsable JSON body or an event of interest are
    dropped. Symbol/bot_id are best-effort — some events lack them
    and that's fine (the index still answers ts/event queries)."""
    line = line.rstrip("\n")
    if not line:
        return None
    try:
        doc = json.loads(line)
    except json.JSONDecodeError:
        return None
    ev = doc.get("event")
    if ev not in INTERESTING_EVENTS:
        return None
    ts = doc.get("timestamp")
    if not ts:
        return None
    symbol = doc.get("symbol") or doc.get("local_symbol")
    if not symbol:
        contract = doc.get("contract")
        if isinstance(contract, dict):
            symbol = contract.get("symbol")
    # Also pick up the local symbol embedded inside orderRef:
    # ``IBT:<host>:<bot>:<localSym>:<side>:<serial>``. ORDER_FILLED /
    # FILL_RELAYED log only the root (``symbol=MES``), but operators
    # almost always investigate by the local symbol (``MESM6``).
    if not symbol:
        order_ref = doc.get("orderRef") or doc.get("order_ref")
        if isinstance(order_ref, str) and order_ref.startswith("IBT:"):
            parts = order_ref.split(":")
            if len(parts) >= 4:
                symbol = parts[3]
    bot_id = doc.get("bot_id")
    # Some events log only orderRef; recover bot_id from there too
    # since explain.py queries by bot_id for the per-bot stream.
    if not bot_id:
        order_ref = doc.get("orderRef") or doc.get("order_ref")
        if isinstance(order_ref, str) and order_ref.startswith("IBT:"):
            parts = order_ref.split(":")
            if len(parts) >= 3:
                # bot_ref looks like ``chart-bot-3-mes``; the bot_id
                # is the leading ``chart-bot-N`` token.
                bot_ref = parts[2]
                seg = bot_ref.split("-")
                if len(seg) >= 3 and seg[0] == "chart" and seg[1] == "bot":
                    bot_id = f"chart-bot-{seg[2]}"
    return (ts, ev, symbol, bot_id, json.dumps(doc), line)


def _stream_new_lines(path: Path, pos: int) -> tuple[Iterable[str], int]:
    """Yield new lines past ``pos``. Returns (iter, new_pos). On log
    rotation (size < pos) restarts from the beginning."""
    if not path.exists():
        return [], pos
    size = path.stat().st_size
    if size < pos:
        # Rotation — restart.
        pos = 0
    f = path.open("r", errors="replace")
    f.seek(pos)
    lines = f.readlines()
    new_pos = f.tell()
    f.close()
    return lines, new_pos


def refresh(verbose: bool = False) -> dict:
    """Read new log lines, insert into log_events, prune old rows.

    Idempotent — safe to call repeatedly. Returns a small stats
    dict (lines read, rows inserted, ms elapsed)."""
    import time
    t0 = time.time()
    pos = _read_pos()
    lines, new_pos = _stream_new_lines(LOG_PATH, pos)
    rows: list[tuple] = []
    n_read = 0
    for line in lines:
        n_read += 1
        parsed = _parse_line(line)
        if parsed is not None:
            rows.append(parsed)
    with sqlite3.connect(DB_PATH) as db:
        ensure_schema(db)
        if rows:
            db.executemany(
                "INSERT INTO log_events (ts, event, symbol, bot_id, "
                "payload, raw) VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
        # Prune anything older than the retention window. ``ts`` is
        # ISO-8601 with timezone offset — lexicographic compare is
        # safe for same-offset entries (which is the case here since
        # the daemon writes server-local timestamps consistently).
        cutoff = (datetime.now(timezone.utc) - RETENTION).isoformat()
        db.execute("DELETE FROM log_events WHERE ts < ?", (cutoff,))
        db.commit()
    _write_pos(new_pos)
    stats = {
        "lines_read": n_read,
        "rows_inserted": len(rows),
        "new_pos": new_pos,
        "ms": int((time.time() - t0) * 1000),
    }
    if verbose:
        print(stats)
    return stats


def query(lo_iso: str, hi_iso: str, *, symbol: str | None = None,
          bot_id: str | None = None, events: set[str] | None = None,
          limit: int = 500) -> list[tuple]:
    """Fetch indexed events. Returns list of (ts, event, symbol,
    bot_id, payload_json)."""
    sql = "SELECT ts, event, symbol, bot_id, payload FROM log_events WHERE ts >= ? AND ts <= ?"
    args: list = [lo_iso, hi_iso]
    if symbol:
        # Match either the full local_symbol (MESM6) or the underlying
        # root (MES). Both shapes occur in the log depending on the
        # event source.
        sql += " AND (symbol = ? OR symbol = ?)"
        root = "".join(c for c in symbol if not c.isdigit())[:3] or symbol
        args += [symbol, root]
    if bot_id:
        sql += " AND bot_id = ?"
        args.append(bot_id)
    if events:
        placeholders = ",".join("?" * len(events))
        sql += f" AND event IN ({placeholders})"
        args += list(events)
    sql += " ORDER BY ts ASC LIMIT ?"
    args.append(limit)
    with sqlite3.connect(DB_PATH) as db:
        return db.execute(sql, args).fetchall()


if __name__ == "__main__":
    import sys
    refresh(verbose="-v" in sys.argv)
