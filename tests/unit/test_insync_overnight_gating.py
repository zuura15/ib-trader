"""Regression: includeOvernight is STK-only.

IB error 10362 ("OVERNIGHT order is not supported") fires when the
overnight-venue tags (``includeOvernight=True`` + ``tif="DAY"``) are
sent on FUT/OPT/FOP contracts — the Blue Ocean ATS routing is a US
equity feature only.

Bug: ``ib/insync_client.py`` previously applied the tags whenever
``is_overnight_session()`` returned True, regardless of secType.
A live ``sell mgcm6 1 mid`` during overnight hours was rejected.

Fix: gate the tagging on ``contract.secType == "STK"``.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from ib_async import Contract

from ib_trader.ib.insync_client import InsyncClient


def _make_client() -> InsyncClient:
    return InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
    )


def _install_place_capture(client: InsyncClient) -> list:
    """Replace ``__ib.placeOrder`` with a capture, return the captured list."""
    captured: list = []

    def fake_place(contract, order):
        captured.append((contract, order))
        return SimpleNamespace(
            order=SimpleNamespace(orderId=42, orderType=order.orderType),
            orderStatus=SimpleNamespace(status=""),
            contract=contract,
        )

    # Bypass the rate-limit sleep.
    async def _no_throttle():
        return None

    client._InsyncClient__ib = SimpleNamespace(placeOrder=fake_place)
    client._throttle = _no_throttle  # type: ignore[method-assign]
    return captured


@pytest.mark.parametrize("strategy_method,extra_kwargs", [
    ("place_limit_order", {"price": Decimal("3300.0")}),
    ("place_market_order", {}),
])
async def test_overnight_skipped_for_futures(strategy_method, extra_kwargs):
    """FUT contract during overnight session must NOT receive includeOvernight."""
    client = _make_client()
    captured = _install_place_capture(client)

    # MGC future, cached so place_*_order picks up secType=FUT.
    fut = Contract(conId=1001, symbol="MGC", secType="FUT",
                   exchange="COMEX", currency="USD", localSymbol="MGCM6")
    client._contract_cache[1001] = fut

    with patch("ib_trader.ib.insync_client.is_overnight_session", return_value=True):
        method = getattr(client, strategy_method)
        await method(
            con_id=1001, symbol="MGCM6", side="SELL", qty=Decimal("1"),
            **extra_kwargs,
        )

    assert len(captured) == 1
    _, order = captured[0]
    assert getattr(order, "includeOvernight", False) is False, \
        "FUT order must not set includeOvernight (IB error 10362)"
    assert order.tif != "DAY", "FUT must not be flipped to DAY tif"


@pytest.mark.parametrize("strategy_method,extra_kwargs", [
    ("place_limit_order", {"price": Decimal("400.0")}),
    ("place_market_order", {}),
])
async def test_overnight_applied_for_stocks(strategy_method, extra_kwargs):
    """STK contract during overnight session DOES get includeOvernight + DAY tif."""
    client = _make_client()
    captured = _install_place_capture(client)

    stk = Contract(conId=2002, symbol="MSFT", secType="STK",
                   exchange="SMART", currency="USD")
    client._contract_cache[2002] = stk

    with patch("ib_trader.ib.insync_client.is_overnight_session", return_value=True):
        method = getattr(client, strategy_method)
        await method(
            con_id=2002, symbol="MSFT", side="BUY", qty=Decimal("1"),
            **extra_kwargs,
        )

    assert len(captured) == 1
    _, order = captured[0]
    assert order.includeOvernight is True
    assert order.tif == "DAY"


async def test_amend_skips_overnight_for_futures():
    """amend_order must not re-apply includeOvernight on FUT amendments."""
    client = _make_client()
    captured = _install_place_capture(client)

    fut = Contract(conId=1001, symbol="MGC", secType="FUT",
                   exchange="COMEX", currency="USD", localSymbol="MGCM6")

    # Stage an active trade to amend. amend_limit_order awaits a non-empty
    # status, so seed it as Submitted.
    order = SimpleNamespace(
        orderId=99, orderType="LMT", lmtPrice=3300.0,
        outsideRth=True, tif="GTC", includeOvernight=False,
    )
    trade = SimpleNamespace(
        order=order,
        orderStatus=SimpleNamespace(status="Submitted"),
        contract=fut,
    )
    client._InsyncClient__active_trades = {"99": trade}

    with patch("ib_trader.ib.insync_client.is_overnight_session", return_value=True):
        await client.amend_order("99", Decimal("3305.0"))

    assert len(captured) == 1
    _, amended = captured[0]
    assert getattr(amended, "includeOvernight", False) is False
    assert amended.tif == "GTC"
