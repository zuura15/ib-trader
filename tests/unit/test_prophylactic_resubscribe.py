"""Tests for the hourly prophylactic resubscribe path.

Addresses the IB-side "parked subscription" failure mode: socket stays
open, no error fires, but IB stops pushing ticks for some symbols.
A fresh ``reqMktData`` unsticks them. These tests pin that
``InsyncClient.prophylactic_resubscribe_all`` actually issues the
cancel + reissue pair for every live subscription, preserves ref
counts, re-attaches RT-bar callbacks, and tolerates per-symbol
failure without aborting the rest.

The companion loop (``engine/ib_resilience.prophylactic_resubscribe_loop``)
is a thin async timer; its behavior is covered by the integration
smoke (engine startup log line) and the loop-specific resolve-alert
test below.
"""
from __future__ import annotations

from unittest.mock import MagicMock, AsyncMock

import pytest

from ib_trader.ib.insync_client import InsyncClient


def _make_client() -> InsyncClient:
    client = InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
    )
    fake_ib = MagicMock()
    fake_ib.reqMktData = MagicMock(return_value=MagicMock())
    fake_ib.cancelMktData = MagicMock()
    fake_ib.reqRealTimeBars = MagicMock(return_value=MagicMock(updateEvent=MagicMock()))
    fake_ib.cancelRealTimeBars = MagicMock()
    client._InsyncClient__ib = fake_ib  # name-mangled __ib
    return client


@pytest.fixture
def client():
    return _make_client()


async def _seed_contract(client: InsyncClient, con_id: int, symbol: str) -> None:
    contract = MagicMock(conId=con_id, symbol=symbol, localSymbol=symbol)
    client._contract_cache[con_id] = contract


async def test_resubscribe_all_cancels_and_reissues_every_streaming_sub(client):
    """Each live mkt-data sub gets exactly one cancelMktData + at least
    one reqMktData on the cycle. Refs are preserved."""
    for con_id, sym, refs in [(100, "AAPL", 1), (200, "MNQM6", 2), (300, "MGCQ6", 3)]:
        await _seed_contract(client, con_id, sym)
        for _ in range(refs):
            await client.subscribe_market_data(con_id, sym)

    pre_req_calls = client._InsyncClient__ib.reqMktData.call_count
    assert pre_req_calls == 3  # initial subs each issued reqMktData once

    result = await client.prophylactic_resubscribe_all(stagger_s=0)

    # 3 distinct cancels, one per symbol.
    assert client._InsyncClient__ib.cancelMktData.call_count == 3
    # 1 reissue + (refs-1) bumps per symbol — but bumps short-circuit
    # without calling reqMktData (existing entry path). So reqMktData
    # is called exactly once per symbol during the resub: total +3.
    assert client._InsyncClient__ib.reqMktData.call_count == pre_req_calls + 3

    # Refs preserved exactly.
    assert client._streaming[100]["refs"] == 1
    assert client._streaming[200]["refs"] == 2
    assert client._streaming[300]["refs"] == 3

    assert result == {
        "mkt_ok": 3, "mkt_fail": 0, "mkt_total": 3,
        "bar_ok": 0, "bar_fail": 0, "bar_total": 0,
    }


async def test_resubscribe_all_handles_realtime_bars_with_callbacks(client):
    """RT-bar subs: cancelRealTimeBars + reqRealTimeBars per symbol;
    all callbacks re-attached to the new bars object."""
    await _seed_contract(client, 400, "MGCQ6")
    cb1 = MagicMock()
    cb2 = MagicMock()
    await client.subscribe_realtime_bars(400, "MGCQ6", what_to_show="TRADES", callback=cb1)
    await client.subscribe_realtime_bars(400, "MGCQ6", what_to_show="TRADES", callback=cb2)
    assert client._realtime_bars[400]["refs"] == 2

    result = await client.prophylactic_resubscribe_all(stagger_s=0)

    assert client._InsyncClient__ib.cancelRealTimeBars.call_count == 1
    # 1 reissue per symbol; additional refs short-circuit on the
    # existing-entry path inside subscribe_realtime_bars.
    assert client._InsyncClient__ib.reqRealTimeBars.call_count == 2  # initial + reissue

    entry = client._realtime_bars[400]
    assert entry["refs"] == 2
    assert cb1 in entry["callbacks"]
    assert cb2 in entry["callbacks"]
    assert result["bar_ok"] == 1
    assert result["bar_fail"] == 0


async def test_resubscribe_all_failure_on_one_symbol_skips_rest(client, monkeypatch):
    """A reqMktData failure on one symbol must NOT abort the others —
    best-effort semantics. The error counter records the failure."""
    for con_id, sym in [(500, "GOOD"), (501, "BAD"), (502, "ALSO_GOOD")]:
        await _seed_contract(client, con_id, sym)
        await client.subscribe_market_data(con_id, sym)

    # Stub subscribe_market_data on the recovery path to fail for BAD.
    real_subscribe = client.subscribe_market_data

    async def flaky_subscribe(con_id, symbol, *, count_ref=True):
        if symbol == "BAD":
            raise RuntimeError("simulated reqMktData failure on resub")
        return await real_subscribe(con_id, symbol, count_ref=count_ref)

    monkeypatch.setattr(client, "subscribe_market_data", flaky_subscribe)

    result = await client.prophylactic_resubscribe_all(stagger_s=0)

    assert result["mkt_ok"] == 2
    assert result["mkt_fail"] == 1
    assert result["mkt_total"] == 3
    # GOOD and ALSO_GOOD made it back into _streaming.
    assert 500 in client._streaming
    assert 502 in client._streaming
    # BAD was popped before the failed re-issue, and stays out.
    assert 501 not in client._streaming


async def test_resubscribe_all_no_op_when_dicts_empty(client):
    """Nothing subscribed → no IB calls, returns zeros, no raise."""
    result = await client.prophylactic_resubscribe_all(stagger_s=0)
    assert result == {
        "mkt_ok": 0, "mkt_fail": 0, "mkt_total": 0,
        "bar_ok": 0, "bar_fail": 0, "bar_total": 0,
    }
    assert client._InsyncClient__ib.reqMktData.call_count == 0
    assert client._InsyncClient__ib.cancelMktData.call_count == 0


async def test_resubscribe_all_concurrent_mutation_safe(client):
    """Snapshot keys before iterating: if another path adds a symbol
    mid-cycle (e.g. a bot subscribes), iteration must not raise
    RuntimeError: dict changed size."""
    for con_id, sym in [(600, "ES"), (601, "NQ"), (602, "CL")]:
        await _seed_contract(client, con_id, sym)
        await client.subscribe_market_data(con_id, sym)

    # Inject a concurrent insertion inside subscribe_market_data so we
    # exercise iteration over a moving target.
    real_subscribe = client.subscribe_market_data
    inserted = {"done": False}

    async def race_subscribe(con_id, symbol, *, count_ref=True):
        if not inserted["done"]:
            # Add a brand-new entry mid-iteration. Simulates a bot
            # starting during the resub window.
            inserted["done"] = True
            client._streaming[999] = {
                "ticker": MagicMock(),
                "refs": 1,
                "contract": MagicMock(localSymbol="LATE", symbol="LATE"),
                "enriched": True,
            }
        return await real_subscribe(con_id, symbol, count_ref=count_ref)

    monkeypatch_obj = pytest.MonkeyPatch()
    monkeypatch_obj.setattr(client, "subscribe_market_data", race_subscribe)
    try:
        result = await client.prophylactic_resubscribe_all(stagger_s=0)
    finally:
        monkeypatch_obj.undo()

    # The 3 originals all cycled, no crash; the LATE inserted entry
    # does NOT participate (was not in the snapshot).
    assert result["mkt_total"] == 3
    assert result["mkt_ok"] == 3
    assert 999 in client._streaming


@pytest.mark.asyncio
async def test_loop_resolve_alert_no_redis_is_safe():
    """``_resolve_prophylactic_alert`` with redis=None must be a no-op
    rather than raising — the loop's finally-block calls it on every
    cycle and must not crash a Redis-less test/dev setup."""
    from ib_trader.engine.ib_resilience import _resolve_prophylactic_alert
    # Just must not raise.
    await _resolve_prophylactic_alert(None)


@pytest.mark.asyncio
async def test_loop_resolve_alert_hdels_stable_id():
    """The dedup_key ``prophylactic_resub`` gives a stable uuid5 id
    (NAMESPACE_DNS + "IB_PROPHYLACTIC_RESUB_INFLIGHT|prophylactic_resub").
    The resolver must HDEL that exact id from alerts:active so the UI
    clears the in-flight banner promptly."""
    import uuid
    from ib_trader.engine.ib_resilience import _resolve_prophylactic_alert
    from ib_trader.redis.state import StateKeys

    expected_id = str(uuid.uuid5(
        uuid.NAMESPACE_DNS,
        "IB_PROPHYLACTIC_RESUB_INFLIGHT|prophylactic_resub",
    ))

    redis = MagicMock()
    redis.hdel = AsyncMock(return_value=1)
    # publish_activity is awaited too; stub it via the import path.
    import ib_trader.redis.streams as _streams
    _orig = getattr(_streams, "publish_activity", None)
    _streams.publish_activity = AsyncMock()
    try:
        await _resolve_prophylactic_alert(redis)
    finally:
        if _orig is not None:
            _streams.publish_activity = _orig

    redis.hdel.assert_awaited_once_with(StateKeys.alerts_active(), expected_id)
