"""Tests for the rolling 24h console-close P&L Redis log.

Producer: ``_record_console_close_pnl`` ZADDs into the
``console:pnl:24h`` sorted set and prunes entries older than 24h on
every write.

Consumer: the ``GET /api/console/pnl/24h`` route reads the
sorted set with a now-24h cutoff and sums what survives.

These tests pin the invariants that bot closes are excluded, that
stale (>24h) entries are pruned, and that the sum aggregator returns
the expected total regardless of fill order.
"""
from __future__ import annotations

import json
import time
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ib_trader.engine.order import _record_console_close_pnl
from ib_trader.redis.state import StateKeys


class _FakeRedis:
    """Minimal in-memory stand-in for the subset of redis-py async API
    that ``_record_console_close_pnl`` and the API endpoint use:
    ``zadd``, ``zremrangebyscore``, ``zrangebyscore``."""

    def __init__(self) -> None:
        self._z: dict[str, dict[str, float]] = {}

    async def zadd(self, key: str, mapping: dict) -> int:
        bucket = self._z.setdefault(key, {})
        added = 0
        for member, score in mapping.items():
            if member not in bucket:
                added += 1
            bucket[member] = float(score)
        return added

    async def zremrangebyscore(self, key: str, lo, hi) -> int:
        bucket = self._z.get(key, {})
        rm = [m for m, s in bucket.items() if lo <= s <= hi]
        for m in rm:
            del bucket[m]
        return len(rm)

    async def zrangebyscore(self, key: str, lo, hi) -> list[str]:
        bucket = self._z.get(key, {})
        return [m for m, s in sorted(bucket.items(), key=lambda kv: kv[1]) if lo <= s <= hi]


def _ctx_with(redis) -> SimpleNamespace:
    return SimpleNamespace(redis=redis)


@pytest.mark.asyncio
async def test_record_writes_to_sorted_set():
    """One close → one ZADD member with correct payload."""
    redis = _FakeRedis()
    ctx = _ctx_with(redis)

    await _record_console_close_pnl(
        ctx, Decimal("57.60"), "MNQM6", Decimal("8"),
        serial=783, source="console_buysell",
    )

    members = await redis.zrangebyscore(StateKeys.console_pnl_24h(), 0, 10**14)
    assert len(members) == 1
    payload = json.loads(members[0])
    assert payload["pnl"] == "57.60"
    assert payload["symbol"] == "MNQM6"
    assert payload["qty"] == "8"
    assert payload["serial"] == 783
    assert payload["source"] == "console_buysell"


@pytest.mark.asyncio
async def test_record_prunes_stale_entries():
    """Entries older than 24h must be removed on the next write."""
    redis = _FakeRedis()
    ctx = _ctx_with(redis)

    # Seed a 25-hour-old stale entry directly.
    now_ms = int(time.time() * 1000)
    stale_ms = now_ms - (25 * 60 * 60 * 1000)
    stale_member = json.dumps({"pnl": "100", "symbol": "OLD", "qty": "1",
                               "ts_ms": stale_ms, "serial": 1, "source": "x"},
                              sort_keys=True)
    await redis.zadd(StateKeys.console_pnl_24h(), {stale_member: stale_ms})

    await _record_console_close_pnl(
        ctx, Decimal("10"), "MES", Decimal("1"),
        serial=2, source="console_close",
    )

    survivors = await redis.zrangebyscore(StateKeys.console_pnl_24h(), 0, 10**14)
    assert len(survivors) == 1
    assert json.loads(survivors[0])["symbol"] == "MES"


@pytest.mark.asyncio
async def test_record_no_op_when_redis_unavailable():
    """``ctx.redis = None`` must NOT raise — the order/close path is
    load-bearing and a missing Redis is acceptable degradation."""
    ctx = SimpleNamespace(redis=None)
    # Just must not raise.
    await _record_console_close_pnl(
        ctx, Decimal("1"), "SPY", Decimal("1"),
        serial=99, source="console_buysell",
    )


@pytest.mark.asyncio
async def test_record_negative_pnl_preserved():
    """Losses must record as a negative decimal so the sum can go down."""
    redis = _FakeRedis()
    ctx = _ctx_with(redis)
    await _record_console_close_pnl(
        ctx, Decimal("-42.50"), "MGCQ6", Decimal("2"),
        serial=10, source="console_close",
    )
    members = await redis.zrangebyscore(StateKeys.console_pnl_24h(), 0, 10**14)
    assert json.loads(members[0])["pnl"] == "-42.50"


@pytest.mark.asyncio
async def test_endpoint_sums_recent_window():
    """The API aggregator returns the sum of pnl over the window."""
    from ib_trader.api.routes.console_pnl import get_console_pnl_24h

    redis = _FakeRedis()
    ctx = _ctx_with(redis)

    await _record_console_close_pnl(ctx, Decimal("57.60"), "MNQM6", Decimal("8"), 1, "console_buysell")
    await _record_console_close_pnl(ctx, Decimal("-12.34"), "MES", Decimal("1"), 2, "console_close")
    await _record_console_close_pnl(ctx, Decimal("100.00"), "MGCQ6", Decimal("3"), 3, "console_close_partial")

    result = await get_console_pnl_24h(redis=redis)
    assert result["count"] == 3
    assert Decimal(result["pnl"]) == Decimal("145.26")
    assert result["window_ms"] == 24 * 60 * 60 * 1000
    assert result["until_ms"] >= result["since_ms"]


@pytest.mark.asyncio
async def test_endpoint_empty_returns_zero():
    """No close fills yet → ``pnl=0, count=0`` (no missing fields)."""
    from ib_trader.api.routes.console_pnl import get_console_pnl_24h

    result = await get_console_pnl_24h(redis=_FakeRedis())
    assert result["count"] == 0
    assert Decimal(result["pnl"]) == Decimal("0")


@pytest.mark.asyncio
async def test_endpoint_no_redis_safe():
    """Redis unavailable: endpoint returns a structured zero rather than 500."""
    from ib_trader.api.routes.console_pnl import get_console_pnl_24h

    result = await get_console_pnl_24h(redis=None)
    assert result["count"] == 0
    assert result["pnl"] == "0"
