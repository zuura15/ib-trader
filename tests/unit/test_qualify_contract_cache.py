"""Regression: qualify_contract memoizes by symbol so chart/SR polling
loops don't burn the global IB throttle on redundant lookups.

2026-06-03 incident: each visible chart-bot pane runs a 30 s history
loop AND a 15 s SR refresh, both via the symbol-only HTTP endpoints
(``GET /engine/history?symbol=...``, ``GET /engine/sr?symbol=...``).
The backend handler unconditionally called ``qualify_contract`` for
every symbol-only request — BEFORE consulting its bars cache — so
two visible chart-bots produced ~12 wasted IB lookups/minute. Each
goes through ``_throttle()`` (100 ms min interval) and serialises
on the global IB throttle lock, blocking unrelated order calls
behind hundreds of redundant lookups. Smart_market orders that
"used to fly so fast" were getting stuck behind the queue.

Fix: ``qualify_contract`` memoizes its full return dict by the
full arg-tuple. Repeat calls with the same args skip throttle +
IB entirely. A regression detector logs WARNING ``CONTRACT_FETCH_BURST``
when the same con_id is re-qualified within 5 min after process
uptime > 2 min — that means some caller bypassed the cache contract
(different kwargs each time, or hit reqContractDetailsAsync directly).
"""
from __future__ import annotations

import asyncio
import time
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ib_trader.ib.insync_client import InsyncClient


def _make_client() -> InsyncClient:
    return InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
    )


def _stub_stk_qualified(con_id: int = 12345) -> object:
    """Build a stub matching ib_async's qualifyContractsAsync result shape."""
    return SimpleNamespace(
        conId=con_id, symbol="MSFT", secType="STK",
        exchange="NASDAQ", currency="USD", multiplier="",
    )


@pytest.mark.asyncio
async def test_qualify_caches_repeat_calls_for_same_symbol():
    """Two qualify_contract calls with identical args → exactly one
    IB roundtrip and one cached result returned twice. This is the
    direct fix for the 2026-06-03 chart-bot storm."""
    client = _make_client()
    fake_ib = MagicMock()
    fake_ib.qualifyContractsAsync = AsyncMock(
        return_value=[_stub_stk_qualified()],
    )
    client._InsyncClient__ib = fake_ib

    first = await client.qualify_contract("MSFT", sec_type="STK")
    second = await client.qualify_contract("MSFT", sec_type="STK")

    assert first == second
    # Exactly one IB call regardless of two qualify calls.
    assert fake_ib.qualifyContractsAsync.call_count == 1


@pytest.mark.asyncio
async def test_qualify_cache_distinguishes_by_arg_tuple():
    """Different sec_type / exchange / currency / expiry → different
    cache keys → separate IB calls. The cache is keyed on the FULL
    arg-tuple, not just the symbol, so two genuinely-different
    lookups for the same symbol still round-trip."""
    client = _make_client()
    fake_ib = MagicMock()
    fake_ib.qualifyContractsAsync = AsyncMock(
        return_value=[_stub_stk_qualified()],
    )
    client._InsyncClient__ib = fake_ib

    await client.qualify_contract("MSFT", sec_type="STK", exchange="SMART")
    await client.qualify_contract("MSFT", sec_type="STK", exchange="NASDAQ")
    await client.qualify_contract("MSFT", sec_type="STK", currency="EUR")

    assert fake_ib.qualifyContractsAsync.call_count == 3


@pytest.mark.asyncio
async def test_qualify_burst_detector_fires_on_cache_bypass(caplog):
    """When the SAME con_id is resolved via two different cache keys
    inside the 5 min window AND after process uptime > 2 min, log a
    WARNING. Signals that some caller is bypassing the symbol cache —
    the regression class that drove the 2026-06-03 incident.

    We simulate "process uptime > 2 min" by back-dating
    ``_process_start_ts``. The cache-bypass is genuine: same con_id
    returned for two different arg-tuples."""
    client = _make_client()
    client._process_start_ts = time.monotonic() - 200  # uptime ~200s

    fake_ib = MagicMock()
    fake_ib.qualifyContractsAsync = AsyncMock(
        return_value=[_stub_stk_qualified(con_id=99999)],
    )
    client._InsyncClient__ib = fake_ib

    caplog.set_level("WARNING", logger="ib_trader.ib.insync_client")
    # First call seeds the cache + records the timestamp.
    await client.qualify_contract("MSFT", sec_type="STK", exchange="SMART")
    # Same con_id, different cache key → ought to fire the detector.
    await client.qualify_contract("MSFT", sec_type="STK", exchange="NASDAQ")

    burst_logs = [
        r for r in caplog.records
        if "CONTRACT_FETCH_BURST" in r.getMessage()
    ]
    assert len(burst_logs) == 1, (
        f"Expected exactly one CONTRACT_FETCH_BURST warning, got "
        f"{len(burst_logs)}. All warnings: "
        f"{[r.getMessage() for r in caplog.records]}"
    )


@pytest.mark.asyncio
async def test_qualify_burst_detector_silent_during_startup(caplog):
    """Multiple resolves at startup (process uptime < 2 min) are
    expected — watchlist warmup, reconnect, etc. — and must NOT fire
    the regression detector. Otherwise every fresh start would flood
    WARNING channels."""
    client = _make_client()
    # Fresh start: _process_start_ts ≈ now → uptime < 1s
    fake_ib = MagicMock()
    fake_ib.qualifyContractsAsync = AsyncMock(
        return_value=[_stub_stk_qualified(con_id=99999)],
    )
    client._InsyncClient__ib = fake_ib

    caplog.set_level("WARNING", logger="ib_trader.ib.insync_client")
    await client.qualify_contract("MSFT", sec_type="STK", exchange="SMART")
    await client.qualify_contract("MSFT", sec_type="STK", exchange="NASDAQ")

    burst_logs = [
        r for r in caplog.records
        if "CONTRACT_FETCH_BURST" in r.getMessage()
    ]
    assert burst_logs == [], (
        "Burst detector must NOT fire during startup warm — "
        "fresh-process resolves are expected behaviour."
    )


@pytest.mark.asyncio
async def test_qualify_skips_throttle_on_cache_hit(monkeypatch):
    """A cache hit must short-circuit BEFORE ``_throttle()`` — the
    whole point is to bypass the 100ms IB rate limiter. If we still
    hit throttle on cached results, the storm doesn't actually go
    away."""
    client = _make_client()
    fake_ib = MagicMock()
    fake_ib.qualifyContractsAsync = AsyncMock(
        return_value=[_stub_stk_qualified()],
    )
    client._InsyncClient__ib = fake_ib

    throttle_calls = {"n": 0}
    original_throttle = client._throttle

    async def counting_throttle():
        throttle_calls["n"] += 1
        await original_throttle()

    monkeypatch.setattr(client, "_throttle", counting_throttle)

    await client.qualify_contract("MSFT", sec_type="STK")
    after_first = throttle_calls["n"]
    await client.qualify_contract("MSFT", sec_type="STK")
    after_second = throttle_calls["n"]

    assert after_first >= 1, "First call must throttle (cache miss)"
    assert after_second == after_first, (
        f"Cached call must skip throttle. Throttle calls: "
        f"first={after_first}, second={after_second}"
    )
