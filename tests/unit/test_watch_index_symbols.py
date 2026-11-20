"""Watch-only cash indices (TNX…) on the watchlist: symbol routing,
contract exchange, and the generic-tick list sent with reqMktData."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ib_trader.ib.insync_client import InsyncClient, _generic_ticks_for
from ib_trader.repl.commands import watch_contract_spec


class TestWatchContractSpec:
    def test_yield_index_routes_to_cboe_ind(self):
        assert watch_contract_spec("TNX") == ("IND", "CBOE", "USD")
        assert watch_contract_spec("tnx") == ("IND", "CBOE", "USD")

    def test_ndx_on_nasdaq(self):
        assert watch_contract_spec("NDX") == ("IND", "NASDAQ", "USD")

    def test_phlx_semiconductor_index(self):
        assert watch_contract_spec("SOX") == ("IND", "PHLX", "USD")

    def test_kospi200_on_kse_in_krw(self):
        assert watch_contract_spec("K200") == ("IND", "KSE", "KRW")

    def test_futures_local_symbol_stays_fut(self):
        assert watch_contract_spec("GCZ6") == ("FUT", "SMART", "USD")

    def test_stock_stays_stk(self):
        assert watch_contract_spec("QQQ") == ("STK", "SMART", "USD")
        assert watch_contract_spec("") == ("STK", "SMART", "USD")


class TestGenericTicks:
    def test_index_gets_no_generic_ticks(self):
        assert _generic_ticks_for(SimpleNamespace(secType="IND")) == ""

    def test_stock_keeps_misc_stats(self):
        assert _generic_ticks_for(SimpleNamespace(secType="STK")) == "165"


@pytest.mark.asyncio
async def test_index_contract_keeps_its_exchange():
    """SMART can't route an index, so the cached contract used for
    reqMktData must carry IB's exchange (CBOE), unlike stocks."""
    client = InsyncClient(host="127.0.0.1", port=4002, client_id=9999, account_id="DU0")
    fake_ib = MagicMock()
    fake_ib.qualifyContractsAsync = AsyncMock(return_value=[SimpleNamespace(
        conId=13455763, symbol="TNX", secType="IND",
        exchange="CBOE", currency="USD", multiplier="",
    )])
    client._InsyncClient__ib = fake_ib

    info = await client.qualify_contract("TNX", sec_type="IND", exchange="CBOE")

    assert info["con_id"] == 13455763
    sent = fake_ib.qualifyContractsAsync.call_args.args[0]
    assert (sent.secType, sent.exchange) == ("IND", "CBOE")
    assert client._contract_cache[13455763].exchange == "CBOE"
