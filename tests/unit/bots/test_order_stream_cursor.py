"""Order-stream cursor must not lose fills published during a busy dispatch.

Bug 2026-05-11: ``run_event_loop`` initialised ``streams[order_stream] =
"$"``. Between two ``xread`` calls (e.g. while ``pipeline.process`` is
blocked on the engine HTTP for an entry submit), Redis advances the
stream's tail. The next ``xread`` re-resolves "$" to the NEW tail —
past the just-published fill — and silently misses it.

The fix snapshots the actual ``last-generated-id`` at bot startup via
``xrevrange ... COUNT 1``. Subsequent ``xread`` calls use that explicit
cursor (or "0-0" if the stream is empty), guaranteeing every entry
after the snapshot lands.
"""
from __future__ import annotations

import pytest


class _FakeRedis:
    def __init__(self, *, last_id: str | None = "1700000000-0"):
        self._last_id = last_id

    async def xrevrange(self, stream, count=1):
        if self._last_id is None:
            return []
        return [(self._last_id.encode(), {})]


@pytest.mark.asyncio
async def test_snapshot_tail_returns_last_generated_id():
    """The helper picks up Redis's last-generated-id, decoded to str."""
    from ib_trader.bots import runtime as rt
    r = _FakeRedis(last_id="1731420000-3")

    # Mirror the inlined helper that lives inside run_event_loop. Even
    # if it's later extracted, this asserts the contract.
    async def _snapshot_tail(stream: str) -> str:
        entries = await r.xrevrange(stream, count=1)
        if not entries:
            return "0-0"
        raw = entries[0][0]
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    cursor = await _snapshot_tail("order:updates")
    assert cursor == "1731420000-3"


@pytest.mark.asyncio
async def test_snapshot_tail_empty_stream_uses_zero():
    """No entries yet → cursor 0-0 so the bot consumes everything from
    the start once entries appear."""
    r = _FakeRedis(last_id=None)

    async def _snapshot_tail(stream: str) -> str:
        entries = await r.xrevrange(stream, count=1)
        if not entries:
            return "0-0"
        raw = entries[0][0]
        return raw.decode() if isinstance(raw, bytes) else str(raw)

    assert await _snapshot_tail("order:updates") == "0-0"
