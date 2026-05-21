"""Tests for the ``count_ref`` parameter on subscribe_market_data.

The fix (2026-05-21): high-frequency callers like ``positionEvent``,
watchlist seed, and ``_refresh_positions_cache`` were leaking
``STREAMING_REF_INC`` counts. Each IB position push bumped the
ref counter via the ``subscribe_market_data`` wrapper even though
those subscriptions have no paired unsubscribe. Over hours the
ref count climbed unboundedly (2700+ leaked refs observed in a
single session), and the engine's per-tick subscriber-callback
fanout grew, slowing the chart's tick stream.

These tests pin the invariant: ``count_ref=False`` makes the
wrapper truly idempotent — the IB subscription is created once
on first call, every subsequent call for the same con_id is a
no-op (no ref bump, no IB re-subscribe).
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ib_trader.ib.insync_client import InsyncClient


def _make_client() -> InsyncClient:
    client = InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
    )
    # Stub the underlying ib_async client — we only care about whether
    # reqMktData fires and what state ``_streaming`` ends up in.
    fake_ib = MagicMock()
    fake_ib.reqMktData = MagicMock(return_value=MagicMock())
    client._InsyncClient__ib = fake_ib  # name-mangled __ib
    return client


@pytest.fixture
def client():
    return _make_client()


async def _seed_contract(client: InsyncClient, con_id: int) -> None:
    client._contract_cache[con_id] = MagicMock(conId=con_id)


async def test_count_ref_true_increments_on_repeat(client):
    """Default behaviour — successive subscribes bump refs."""
    await _seed_contract(client, 100)
    await client.subscribe_market_data(100, "AAPL")
    await client.subscribe_market_data(100, "AAPL")
    await client.subscribe_market_data(100, "AAPL")
    assert client._streaming[100]["refs"] == 3


async def test_count_ref_false_is_idempotent(client):
    """count_ref=False — refs stays at 1 across many calls."""
    await _seed_contract(client, 200)
    await client.subscribe_market_data(200, "MES", count_ref=False)
    await client.subscribe_market_data(200, "MES", count_ref=False)
    await client.subscribe_market_data(200, "MES", count_ref=False)
    await client.subscribe_market_data(200, "MES", count_ref=False)
    assert client._streaming[200]["refs"] == 1


async def test_count_ref_false_no_duplicate_ib_subscribe(client):
    """First call subscribes to IB; subsequent calls don't re-subscribe."""
    await _seed_contract(client, 300)
    await client.subscribe_market_data(300, "QQQ", count_ref=False)
    await client.subscribe_market_data(300, "QQQ", count_ref=False)
    await client.subscribe_market_data(300, "QQQ", count_ref=False)
    # reqMktData fires once (initial subscription); idempotent calls
    # don't re-issue it.
    assert client._InsyncClient__ib.reqMktData.call_count == 1


async def test_mixed_callers_dont_leak(client):
    """Realistic pattern: bot subscribes once (count_ref=True), and
    positionEvent fires many times (count_ref=False). Bot's
    subsequent unsubscribe must reduce refs to 0 and cancel — the
    ambient positionEvent calls must NOT have leaked refs."""
    await _seed_contract(client, 400)
    # Bot start — refcounted subscribe.
    await client.subscribe_market_data(400, "MNQM6")
    # positionEvent fires 50 times during the bot's lifetime.
    for _ in range(50):
        await client.subscribe_market_data(400, "MNQM6", count_ref=False)
    # refs is still 1 — only the bot's subscribe contributed.
    assert client._streaming[400]["refs"] == 1
    # Bot stops — unsubscribe drives refs to 0 and cancels IB.
    await client.unsubscribe_market_data(400)
    assert 400 not in client._streaming


async def test_count_ref_false_after_existing_refcounted_sub(client):
    """If a refcounted subscriber already holds the slot, an ambient
    ``count_ref=False`` caller must NOT change the count — leaving the
    refcounted owner's pair intact."""
    await _seed_contract(client, 500)
    await client.subscribe_market_data(500, "ES")  # bot, refs=1
    await client.subscribe_market_data(500, "ES", count_ref=False)
    await client.subscribe_market_data(500, "ES", count_ref=False)
    assert client._streaming[500]["refs"] == 1


async def test_count_ref_true_after_ambient(client):
    """Symmetric direction: ambient established first (refs=1), then a
    refcounted caller arrives — it MUST bump to 2 so its eventual
    unsubscribe doesn't yank the ambient slot."""
    await _seed_contract(client, 600)
    await client.subscribe_market_data(600, "SPY", count_ref=False)
    await client.subscribe_market_data(600, "SPY")  # refcounted caller
    assert client._streaming[600]["refs"] == 2
