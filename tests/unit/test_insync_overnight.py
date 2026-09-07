"""Overnight-venue (IBEOS) twin subscriptions.

SMART streaming market data carries nothing during IB's overnight
session (Sun 8:00 PM – Fri 3:30 AM ET), which blanked the watchlist
every Sunday evening. The client keeps a SECOND subscription per STK
watchlist symbol with exchange="OVERNIGHT"; it lives in its own dict
(the overnight contract shares the SMART contract's conId) and
``get_ticker`` merges its bid/ask/last as a fallback.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ib_trader.ib.insync_client import InsyncClient


def _make_client() -> InsyncClient:
    client = InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
    )
    fake_ib = MagicMock()
    fake_ib.reqMktData = MagicMock(return_value=MagicMock())
    client._InsyncClient__ib = fake_ib  # name-mangled __ib
    return client


@pytest.fixture
def client():
    return _make_client()


def _ticker(**vals):
    """Ticker stand-in with explicit numeric/None fields (MagicMock
    breaks _val's NaN/<=0 checks)."""
    base = dict(bid=None, ask=None, last=None, close=None, prevClose=None,
                open=None, high=None, low=None, volume=None, avVolume=None,
                high52week=None, low52week=None, time=None)
    base.update(vals)
    return SimpleNamespace(**base)


class TestSubscribe:
    async def test_separate_dict_and_overnight_exchange(self, client):
        await client.subscribe_overnight_market_data(100, "QQQ")
        assert 100 in client._overnight_streaming
        assert 100 not in client._streaming          # never collides
        req = client._InsyncClient__ib.reqMktData
        contract = req.call_args.args[0]
        assert contract.exchange == "OVERNIGHT"
        assert contract.secType == "STK"

    async def test_idempotent_per_con_id(self, client):
        await client.subscribe_overnight_market_data(100, "QQQ")
        await client.subscribe_overnight_market_data(100, "QQQ")
        assert client._InsyncClient__ib.reqMktData.call_count == 1


class TestGetTickerFallback:
    async def test_overnight_fills_silent_smart(self, client):
        client._streaming[100] = {"ticker": _ticker()}          # SMART silent
        client._overnight_streaming[100] = {
            "ticker": _ticker(bid=432.5, ask=432.7, last=432.6),
            "contract": SimpleNamespace(symbol="QQQ"),
        }
        data = client.get_ticker(100)
        assert data["bid"] == 432.5
        assert data["ask"] == 432.7
        assert data["last"] == 432.6

    async def test_smart_wins_when_present(self, client):
        client._streaming[100] = {"ticker": _ticker(bid=431.0, last=431.2)}
        client._overnight_streaming[100] = {
            "ticker": _ticker(bid=432.5, ask=432.7, last=432.6),
            "contract": SimpleNamespace(symbol="QQQ"),
        }
        data = client.get_ticker(100)
        assert data["bid"] == 431.0      # SMART preferred
        assert data["ask"] == 432.7      # overnight fills the gap
        assert data["last"] == 431.2

    async def test_no_overnight_entry_unchanged(self, client):
        client._streaming[100] = {"ticker": _ticker(bid=431.0)}
        data = client.get_ticker(100)
        assert data["bid"] == 431.0
        assert data["ask"] is None


class TestProphylactic:
    async def test_cycles_overnight_twins(self, client):
        await client.subscribe_overnight_market_data(100, "QQQ")
        result = await client.prophylactic_resubscribe_all(stagger_s=0)
        assert result["overnight_ok"] == 1
        assert result["overnight_fail"] == 0
        assert 100 in client._overnight_streaming   # re-issued
        # cancel + initial + re-issue = 2 reqMktData calls total
        assert client._InsyncClient__ib.reqMktData.call_count == 2
        assert client._InsyncClient__ib.cancelMktData.call_count == 1
