"""Log stream endpoint.

GET /api/logs — returns recent log entries from the structured JSON log file.
Supports ?limit=N (default 100) and ?after=<iso_timestamp> for incremental polling.
"""
import json
import os

from fastapi import APIRouter, Query


router = APIRouter(prefix="/api/logs", tags=["logs"])

_LOG_FILE = os.path.join(os.getcwd(), "logs", "ib_trader.log")


def _parse_log_line(line: str) -> dict | None:
    """Parse a single JSON log line. Returns None if unparseable."""
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
        return {
            "timestamp": entry.get("timestamp", ""),
            "level": entry.get("level", "INFO"),
            "event": entry.get("event", entry.get("message", "")),
            "message": entry.get("message", entry.get("event", "")),
        }
    except json.JSONDecodeError:
        # Non-JSON log line — wrap it
        return {
            "timestamp": "",
            "level": "INFO",
            "event": "log",
            "message": line,
        }


@router.get("")
def get_logs(
    limit: int = Query(100, ge=1, le=1000),
    after: str | None = Query(None, description="ISO timestamp — return only entries after this time"),
):
    """Return recent log entries from the structured JSON log file.

    Reads the last N lines from the log file. If `after` is provided,
    filters to only entries with timestamp > after.
    """
    if not os.path.exists(_LOG_FILE):
        return []

    # Read last `limit * 2` lines (oversample to account for filtering)
    try:
        with open(_LOG_FILE, "r") as f:
            # Efficient tail: seek to end, read backwards
            lines = f.readlines()
            tail = lines[-(limit * 2):]
    except Exception:
        return []

    entries = []
    for line in tail:
        parsed = _parse_log_line(line)
        if parsed is None:
            continue
        if after and parsed["timestamp"] and parsed["timestamp"] <= after:
            continue
        entries.append(parsed)

    # Return the last `limit` entries, oldest first
    return entries[-limit:]
