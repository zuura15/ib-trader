"""Debug-log endpoint for the frontend.

Frontend posts arbitrary JSON snapshots (currently SR detection state)
to ``POST /api/debug/log/{tag}``. The server appends one JSON line per
call to ``logs/<tag>.log``. The file is auto-rotated by size cap so
"the logs need to live just for a couple of hours" — no permanent
retention.

Intended use: short-lived debug/troubleshoot only. Not a logging
pipeline. The endpoint is unauth'd and trivial; turn it off (remove
the include_router line) once the bug is found.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/debug", tags=["debug"])

# Files live under logs/ alongside the app log. 2 MB rotation cap →
# keeps a few hours of rapid-fire snapshots without growing
# unbounded.
_LOG_DIR = Path("logs")
_MAX_BYTES = 2 * 1024 * 1024


def _safe_tag(tag: str) -> str:
    # Whitelist alphanumerics + dash/underscore so we don't get path
    # traversal via tag.
    out = "".join(c for c in tag if c.isalnum() or c in ("-", "_"))
    return out[:32] or "untagged"


@router.post("/log/{tag}")
async def append_debug_log(tag: str, body: dict[str, Any]) -> dict[str, str]:
    """Append a single JSON line to logs/<tag>.log."""
    safe = _safe_tag(tag)
    if not safe:
        raise HTTPException(status_code=422, detail="empty tag")

    _LOG_DIR.mkdir(exist_ok=True)
    path = _LOG_DIR / f"{safe}.log"

    # Crude rotation: if the file exceeds the cap, truncate to keep
    # only the last N bytes' worth of complete lines. Keeps disk use
    # bounded without depending on logrotate.
    try:
        if path.exists() and path.stat().st_size > _MAX_BYTES:
            with path.open("rb") as f:
                f.seek(-_MAX_BYTES // 2, os.SEEK_END)
                # Skip partial line
                f.readline()
                tail = f.read()
            path.write_bytes(tail)
    except Exception:  # noqa: BLE001 - rotation is best-effort
        logger.debug("debug-log rotation failed", exc_info=True)

    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        **body,
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with path.open("a", encoding="utf-8") as f:
        f.write(line)
    return {"status": "ok", "path": str(path)}
