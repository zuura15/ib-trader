"""Tests for engine ``get_history``'s stale-cache fallback (IB pacing recovery).

When IB returns error 162 (historical-data pacing limit) the
underlying ``req_historical_data_async`` raises. Before this fallback
the engine surfaced 502s to the chart on every poll until pacing
cleared, which broke the chart for minutes at a time on both dev and
prod (observed 2026-05-19 / 2026-05-20). The fallback serves stale
cached bars (up to 30 min old) so the chart paints slightly older
data instead of breaking.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from ib_trader.engine import internal_api
from ib_trader.engine.internal_api import (
    _HISTORY_CACHE, _HISTORY_TTL_SECONDS, _HISTORY_STALE_FALLBACK_SECONDS,
    get_history, set_context,
)


@pytest.fixture
def stub_ctx(monkeypatch):
    """Inject a fake engine context with a controllable IB client.

    The test sets ``ib.req_historical_data_async.side_effect`` to
    control whether the IB call succeeds or raises.
    """
    fake_ib = MagicMock()
    fake_ib.req_historical_data_async = AsyncMock()
    fake_ib._contract_cache = {12345: MagicMock()}  # contract for con_id=12345
    fake_ctx = MagicMock()
    fake_ctx.ib = fake_ib
    monkeypatch.setattr(internal_api, "_ctx", fake_ctx)
    yield fake_ib
    # Clean the shared module-level cache + inflight registry so tests
    # don't bleed into each other.
    _HISTORY_CACHE.clear()
    for t in list(internal_api._HISTORY_INFLIGHT.values()):
        t.cancel()
    internal_api._HISTORY_INFLIGHT.clear()


async def _settle_inflight():
    """Await any background refresh tasks (swallowing their errors)."""
    for t in list(internal_api._HISTORY_INFLIGHT.values()):
        try:
            await t
        except Exception:
            pass


def _fake_bar(close: float = 100.0):
    bar = MagicMock()
    bar.date = MagicMock()
    bar.date.timestamp = lambda: 1716200000.0
    bar.date.isoformat = lambda: "2026-05-20T07:00:00+00:00"
    bar.open = close - 0.1
    bar.high = close + 0.5
    bar.low = close - 0.5
    bar.close = close
    bar.volume = 100
    return bar


class TestTTL:
    def test_ttl_is_300_seconds(self):
        # If someone lowers this carelessly the chart pane burns
        # through IB pacing again. Pin it.
        assert _HISTORY_TTL_SECONDS == 300.0

    def test_stale_fallback_horizon_is_30_min(self):
        assert _HISTORY_STALE_FALLBACK_SECONDS == 1800.0


class TestStaleFallback:
    @pytest.mark.asyncio
    async def test_returns_stale_cache_when_ib_raises(self, stub_ctx):
        # Seed the cache with a stale entry (older than the fresh TTL
        # but within the stale-fallback horizon).
        stale_bars = [{"ts": "2026-05-20T06:55:00+00:00", "close": 100.0}]
        cache_key = (12345, 8, "3 mins", True)
        stale_age_seconds = _HISTORY_TTL_SECONDS + 60  # 6 min old
        _HISTORY_CACHE[cache_key] = (
            time.monotonic() - stale_age_seconds,
            stale_bars,
        )
        # IB now raises (simulate error 162).
        stub_ctx.req_historical_data_async.side_effect = Exception("pacing")

        result = await get_history(
            con_id=12345, hours=8, bar_size="3 mins", include_partial=True,
        )

        assert result == stale_bars
        # The failed background refresh must not clobber the stale
        # entry — the next poll serves it again and retries.
        await _settle_inflight()
        assert _HISTORY_CACHE[cache_key][1] == stale_bars

    @pytest.mark.asyncio
    async def test_502_when_ib_raises_and_no_cache(self, stub_ctx):
        stub_ctx.req_historical_data_async.side_effect = Exception("pacing")

        with pytest.raises(HTTPException) as exc:
            await get_history(
                con_id=12345, hours=8, bar_size="3 mins", include_partial=True,
            )
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_502_when_cache_is_too_stale(self, stub_ctx):
        # Cache entry older than the stale-fallback horizon: don't
        # return it (data is misleadingly old).
        cache_key = (12345, 8, "3 mins", True)
        very_stale_age = _HISTORY_STALE_FALLBACK_SECONDS + 60  # > 30 min
        _HISTORY_CACHE[cache_key] = (
            time.monotonic() - very_stale_age,
            [{"ts": "old", "close": 100.0}],
        )
        stub_ctx.req_historical_data_async.side_effect = Exception("pacing")

        with pytest.raises(HTTPException) as exc:
            await get_history(
                con_id=12345, hours=8, bar_size="3 mins", include_partial=True,
            )
        assert exc.value.status_code == 502

    @pytest.mark.asyncio
    async def test_fresh_cache_short_circuits_ib_call(self, stub_ctx):
        # Cache within the fresh TTL — should never reach IB.
        cache_key = (12345, 8, "3 mins", True)
        fresh_bars = [{"ts": "fresh", "close": 200.0}]
        _HISTORY_CACHE[cache_key] = (time.monotonic() - 1.0, fresh_bars)

        result = await get_history(
            con_id=12345, hours=8, bar_size="3 mins", include_partial=True,
        )

        assert result == fresh_bars
        stub_ctx.req_historical_data_async.assert_not_called()


class TestStaleWhileRevalidate:
    """A stale-but-servable cache entry answers immediately; the IB
    refetch happens in the background. Added 2026-10-01 after HMDS
    slow spells (45+ s answers) turned every synchronized chart-pane
    TTL expiry into a visible 504 burst across all charts."""

    @pytest.mark.asyncio
    async def test_stale_served_immediately_while_ib_is_slow(self, stub_ctx):
        stale_bars = [{"ts": "2026-10-01T06:55:00+00:00", "close": 100.0}]
        cache_key = (12345, 8, "3 mins", True)
        _HISTORY_CACHE[cache_key] = (
            time.monotonic() - (_HISTORY_TTL_SECONDS + 60), stale_bars,
        )

        async def slow_fetch(*a, **kw):
            await asyncio.sleep(0.3)
            return [_fake_bar(close=101.0)]

        stub_ctx.req_historical_data_async.side_effect = slow_fetch

        loop = asyncio.get_event_loop()
        t0 = loop.time()
        result = await get_history(
            con_id=12345, hours=8, bar_size="3 mins", include_partial=True,
        )
        elapsed = loop.time() - t0

        assert result == stale_bars
        assert elapsed < 0.15, "stale-cache response blocked on the IB fetch"

        # The background refresh lands in the cache for the next poll.
        await _settle_inflight()
        refreshed = _HISTORY_CACHE[cache_key][1]
        assert refreshed and refreshed[0]["close"] == 101.0

    @pytest.mark.asyncio
    async def test_single_flight_dedups_concurrent_cold_calls(self, stub_ctx):
        calls = 0

        async def counted_fetch(*a, **kw):
            nonlocal calls
            calls += 1
            await asyncio.sleep(0.05)
            return [_fake_bar(close=102.0)]

        stub_ctx.req_historical_data_async.side_effect = counted_fetch

        results = await asyncio.gather(
            get_history(con_id=12345, hours=8, bar_size="3 mins",
                        include_partial=True),
            get_history(con_id=12345, hours=8, bar_size="3 mins",
                        include_partial=True),
            get_history(con_id=12345, hours=8, bar_size="3 mins",
                        include_partial=True),
        )

        assert calls == 1, "concurrent identical requests must share one fetch"
        assert all(r == results[0] for r in results)
        assert results[0][0]["close"] == 102.0
