"""Audit feed endpoints.

GET  /api/audit         — paged list of audit rows, newest-first
GET  /api/audit/stream  — Server-Sent Events stream of new rows
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import AsyncIterator

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse

from ib_trader.api.deps import get_audit_log
from ib_trader.data.repositories.audit_log_repository import AuditLogRepository

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/audit", tags=["audit"])


from datetime import timezone as _tz


def _isoformat_utc(dt) -> str | None:
    """Return ISO-8601 with explicit ``Z`` suffix.

    SQLite stores ``audit_log.event_ts_utc`` as naive UTC (tz stripped on
    insert). ``.isoformat()`` on a naive datetime produces a string with
    no tz marker, which the browser's ``new Date()`` then interprets as
    LOCAL time — shifting the displayed clock by the local offset and
    confusing the operator. Forcing the ``Z`` makes the browser parse
    as UTC and ``toLocaleString``/``toLocaleTimeString`` then render
    correctly in the user's actual timezone.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_tz.utc)
    return dt.isoformat()


def _serialize(row) -> dict:
    payload = None
    if row.payload_json:
        try:
            payload = json.loads(row.payload_json)
        except Exception:  # noqa: BLE001
            payload = {"_raw": row.payload_json}
    return {
        "id": row.id,
        "bot_id": row.bot_id,
        "symbol": row.symbol,
        "event_ts_utc": _isoformat_utc(row.event_ts_utc),
        "event_type": row.event_type,
        "pivot_status": row.pivot_status,
        "line_status": row.line_status,
        "decision": row.decision,
        "bar_close": str(row.bar_close) if row.bar_close is not None else None,
        "pnl_net": str(row.pnl_net) if row.pnl_net is not None else None,
        "payload": payload,
    }


@router.get("")
def list_audit(
    bot_id: str | None = Query(None, description="Filter to one bot's events"),
    since: datetime | None = Query(
        None, description="ISO timestamp lower bound on event_ts_utc",
    ),
    before: datetime | None = Query(
        None, description="ISO timestamp upper bound (exclusive) — use the "
                         "oldest row's event_ts_utc to page backwards",
    ),
    limit: int = Query(100, ge=1, le=500),
    repo: AuditLogRepository = Depends(get_audit_log),
):
    """List audit rows, newest-first. Both ``bot_id`` and time bounds
    are optional."""
    rows = repo.list_recent(
        bot_id=bot_id, since=since, before=before, limit=limit,
    )
    return [_serialize(r) for r in rows]


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------

async def _audit_event_generator(
    request: Request,
    repo: AuditLogRepository,
    bot_id: str | None,
    after_id: int,
    poll_seconds: float,
) -> AsyncIterator[bytes]:
    """Yield SSE-formatted bytes for each new audit row.

    Polls the repository every ``poll_seconds`` for rows with id >
    ``after_id``. Sends a comment line every 30s as a keepalive so
    proxies don't drop the connection during idle stretches.
    """
    cursor = after_id
    last_keepalive = asyncio.get_event_loop().time()
    while True:
        if await request.is_disconnected():
            break
        try:
            rows = repo.list_after_id(cursor, limit=200)
        except Exception:  # noqa: BLE001
            logger.exception(
                '{"event":"AUDIT_SSE_QUERY_FAILED"}',
            )
            rows = []
        for r in rows:
            if bot_id is not None and r.bot_id != bot_id:
                cursor = r.id
                continue
            data = json.dumps(_serialize(r), default=str)
            yield f"data: {data}\n\n".encode("utf-8")
            cursor = r.id
        now = asyncio.get_event_loop().time()
        if now - last_keepalive > 30:
            yield b": keepalive\n\n"
            last_keepalive = now
        await asyncio.sleep(poll_seconds)


@router.get("/stream")
async def stream_audit(
    request: Request,
    bot_id: str | None = Query(None),
    after_id: int | None = Query(
        None, description="Resume from a specific row id; omit to start from "
                         "the latest at connection time",
    ),
    poll_seconds: float = Query(1.5, ge=0.25, le=10.0),
    repo: AuditLogRepository = Depends(get_audit_log),
):
    """Server-Sent Events stream of new audit rows.

    Defaults to "tail" semantics: connects at the current latest id
    and emits only rows that arrive AFTER the connection.
    """
    start = after_id
    if start is None:
        start = repo.latest_id() or 0
    generator = _audit_event_generator(
        request, repo, bot_id=bot_id, after_id=start,
        poll_seconds=poll_seconds,
    )
    return StreamingResponse(
        generator,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",  # disable nginx buffering if proxied
        },
    )
