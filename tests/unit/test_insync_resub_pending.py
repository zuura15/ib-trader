"""Tests for ``resubscribe_pending`` — replaying market-data + RT-bar
subscriptions across an IB Gateway reconnect.

The bug (2026-06-01): the 02:30 PT scheduled reconnect cleared
``_streaming``/``_realtime_bars`` (correct — IB-side subs are dropped on
disconnect), but the engine's post-reconnect routine only re-subscribed
watchlist symbols and held positions. Bot-owned feeds (chart-bot-4's
MNQM6 quote + 5s bars — not on the watchlist, no open position) got
orphaned, so the bot logged ``STALE_QUOTES`` every 10 s with
``redis_quote_key_present=False`` for ~5.6 hours.

These tests pin the new invariant: ``_on_disconnected`` snapshots both
dicts BEFORE clearing, and ``resubscribe_pending`` replays every entry
preserving its ref count and bar callbacks.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from ib_trader.ib.insync_client import InsyncClient


def _make_client() -> InsyncClient:
    client = InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
    )
    fake_ib = MagicMock()
    fake_ib.reqMktData = MagicMock(return_value=MagicMock())
    fake_ib.reqRealTimeBars = MagicMock(return_value=MagicMock(updateEvent=MagicMock()))
    client._InsyncClient__ib = fake_ib  # name-mangled __ib
    return client


@pytest.fixture
def client():
    return _make_client()


async def _seed_contract(client: InsyncClient, con_id: int, symbol: str) -> None:
    contract = MagicMock(conId=con_id, symbol=symbol, localSymbol=symbol)
    client._contract_cache[con_id] = contract


async def test_disconnect_snapshots_streaming_then_clears(client):
    """``_on_disconnected`` must populate the pending list BEFORE
    clearing ``_streaming`` — otherwise we'd snapshot an empty dict."""
    await _seed_contract(client, 100, "AAPL")
    await client.subscribe_market_data(100, "AAPL")
    await client.subscribe_market_data(100, "AAPL")  # refs=2

    client._expected_disconnect = True  # silence the reconnect alert path
    client._on_disconnected()

    assert client._streaming == {}
    assert client._pending_resub_streaming == [(100, "AAPL", 2)]


async def test_resubscribe_pending_restores_refs(client):
    """A symbol with refs=3 pre-disconnect must end up with refs=3
    after replay — bot's eventual unsubscribe sequence relies on the
    count surviving."""
    await _seed_contract(client, 200, "MNQM6")
    await client.subscribe_market_data(200, "MNQM6")
    await client.subscribe_market_data(200, "MNQM6")
    await client.subscribe_market_data(200, "MNQM6")
    assert client._streaming[200]["refs"] == 3

    client._expected_disconnect = True
    client._on_disconnected()
    assert 200 not in client._streaming

    (mkt_ok, mkt_fail, _, _) = await client.resubscribe_pending()
    assert (mkt_ok, mkt_fail) == (1, 0)
    assert client._streaming[200]["refs"] == 3
    # pending list is drained — a second call is a no-op.
    assert client._pending_resub_streaming == []


async def test_resubscribe_pending_replays_realtime_bars(client):
    """RT-bar subscriptions must also be replayed, with their
    callbacks re-attached — chart-bot's signal path consumes 5s bars
    via the Redis stream that's fed by these callbacks."""
    await _seed_contract(client, 300, "MNQM6")
    cb1 = MagicMock()
    cb2 = MagicMock()
    await client.subscribe_realtime_bars(300, "MNQM6", what_to_show="TRADES", callback=cb1)
    # A second subscriber bumps refs and adds a callback.
    await client.subscribe_realtime_bars(300, "MNQM6", what_to_show="TRADES", callback=cb2)
    assert client._realtime_bars[300]["refs"] == 2
    assert cb1 in client._realtime_bars[300]["callbacks"]
    assert cb2 in client._realtime_bars[300]["callbacks"]

    client._expected_disconnect = True
    client._on_disconnected()
    assert 300 not in client._realtime_bars

    (_, _, bar_ok, bar_fail) = await client.resubscribe_pending()
    assert (bar_ok, bar_fail) == (1, 0)
    entry = client._realtime_bars[300]
    assert entry["refs"] == 2
    assert cb1 in entry["callbacks"]
    assert cb2 in entry["callbacks"]


async def test_resubscribe_pending_no_op_when_empty(client):
    """No pre-disconnect snapshot → call is a clean no-op, doesn't
    raise, doesn't issue any IB requests."""
    result = await client.resubscribe_pending()
    assert result == (0, 0, 0, 0)
    assert client._InsyncClient__ib.reqMktData.call_count == 0
    assert client._InsyncClient__ib.reqRealTimeBars.call_count == 0


async def test_resubscribe_pending_failures_dont_abort_rest(client, monkeypatch):
    """One bad symbol must NOT prevent replay of the rest — best-effort
    semantics let a single dead contract fail without orphaning the
    other bot-owned feeds."""
    await _seed_contract(client, 400, "GOOD")
    await _seed_contract(client, 401, "BAD")
    await _seed_contract(client, 402, "ALSO_GOOD")
    await client.subscribe_market_data(400, "GOOD")
    await client.subscribe_market_data(401, "BAD")
    await client.subscribe_market_data(402, "ALSO_GOOD")

    client._expected_disconnect = True
    client._on_disconnected()

    real_sub = client.subscribe_market_data

    async def flaky_subscribe(con_id, symbol, *, count_ref=True):
        if symbol == "BAD":
            raise RuntimeError("simulated reqMktData failure")
        return await real_sub(con_id, symbol, count_ref=count_ref)

    monkeypatch.setattr(client, "subscribe_market_data", flaky_subscribe)
    (mkt_ok, mkt_fail, _, _) = await client.resubscribe_pending()
    assert mkt_ok == 2
    assert mkt_fail == 1
    assert 400 in client._streaming
    assert 402 in client._streaming
    assert 401 not in client._streaming
