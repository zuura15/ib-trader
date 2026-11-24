"""IB crypto (BTC on ZEROHASH) as a watch-only chart: contract routing,
bar data type, quote ticks, chart-bot anchors and the order refusal."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ib_trader.config import watchlist_runtime as wr
from ib_trader.engine.order import _get_contract, execute_order
from ib_trader.ib.insync_client import (
    CRYPTO_EXCHANGE, InsyncClient, _bar_data_type_for, _generic_ticks_for,
)
from ib_trader.repl.commands import BuyCommand, watch_contract_spec


def _client_with(qualified) -> tuple[InsyncClient, MagicMock]:
    client = InsyncClient(host="127.0.0.1", port=4002, client_id=9999, account_id="DU0")
    fake_ib = MagicMock()
    fake_ib.qualifyContractsAsync = AsyncMock(return_value=[qualified])
    client._InsyncClient__ib = fake_ib
    return client, fake_ib


_BTC = SimpleNamespace(
    conId=479624278, symbol="BTC", secType="CRYPTO",
    exchange="ZEROHASH", currency="USD", multiplier="",
)


@pytest.mark.asyncio
async def test_crypto_qualifies_on_crypto_venue_not_smart():
    client, fake_ib = _client_with(_BTC)
    info = await client.qualify_contract("BTC", sec_type="CRYPTO")
    assert info["con_id"] == 479624278
    sent = fake_ib.qualifyContractsAsync.call_args.args[0]
    assert (sent.secType, sent.exchange) == ("CRYPTO", CRYPTO_EXCHANGE)
    # Market data + bars reuse the cached contract — it must keep ZEROHASH.
    assert client._contract_cache[479624278].exchange == "ZEROHASH"


def test_crypto_bars_use_aggtrades():
    btc = SimpleNamespace(secType="CRYPTO")
    assert _bar_data_type_for(btc, "TRADES") == "AGGTRADES"
    assert _bar_data_type_for(btc, "MIDPOINT") == "MIDPOINT"
    assert _bar_data_type_for(SimpleNamespace(secType="FUT"), "TRADES") == "TRADES"


def test_crypto_quotes_skip_generic_ticks():
    assert _generic_ticks_for(SimpleNamespace(secType="CRYPTO")) == ""


def test_crypto_hint_beats_stock_ticker_collision():
    # "BTC" alone is the Grayscale mini ETF.
    assert watch_contract_spec("BTC") == ("STK", "SMART", "USD")
    assert watch_contract_spec("BTC", "CRYPTO") == ("CRYPTO", "SMART", "USD")
    # Non-crypto hints don't change the shape-based routing.
    assert watch_contract_spec("GCZ6", "FUT") == ("FUT", "SMART", "USD")


def test_bitcoin_chart_bot_is_anchored_as_crypto():
    types = wr.chart_anchor_sec_types()
    assert types.get("BTC") == "CRYPTO"
    assert types.get("GCG7") == "FUT"
    assert wr.chart_anchor_symbols() == list(types)


@pytest.mark.asyncio
async def test_get_contract_never_falls_back_to_stock_for_crypto():
    ib = MagicMock()
    ib.qualify_contract = AsyncMock(return_value={"con_id": 479624278})
    ctx = SimpleNamespace(ib=ib)
    info = await _get_contract("BTC", ctx, sec_type="CRYPTO")
    assert info["sec_type"] == "CRYPTO"
    assert ib.qualify_contract.call_args.kwargs["sec_type"] == "CRYPTO"


@pytest.mark.asyncio
async def test_crypto_orders_are_refused_before_touching_ib():
    emitted: list[str] = []
    router = SimpleNamespace(emit=lambda msg, **_: emitted.append(msg))
    ib = MagicMock()
    ib.qualify_contract = AsyncMock()
    ctx = SimpleNamespace(settings={}, router=router, ib=ib)
    cmd = BuyCommand(
        symbol="BTC", qty=None, dollars=None, strategy="mid",
        profit_amount=None, take_profit_price=None, stop_loss=None,
        security_type="CRYPTO",
    )
    await execute_order(cmd, ctx)
    assert emitted and "watch-only" in emitted[0]
    ib.qualify_contract.assert_not_called()
