"""Tests for InsyncClient cancel-verification path (GH #48, ADR-018).

ib_async (wrapper.py:1657-1668) synthesizes a Cancelled status from any
non-warning order error on a live trade, including modify-rejection errors
where IB itself leaves the original order live. Our _on_order_status guard
defers dispatch on those Cancelled events and asks IB whether the order is
actually still open before propagating.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from ib_async import Contract, Order, OrderStatus, Trade
from ib_async.objects import TradeLogEntry

from ib_trader.ib.insync_client import InsyncClient


def _make_client() -> InsyncClient:
    return InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
        min_call_interval_ms=0,
    )


def _make_trade(
    ib_order_id: str, status: str, log_entries: list[TradeLogEntry],
) -> Trade:
    contract = Contract(symbol="PSQ", secType="STK", exchange="SMART", currency="USD")
    order = Order(orderId=int(ib_order_id), permId=1770665954)
    order_status = OrderStatus(orderId=int(ib_order_id), status=status, permId=1770665954)
    return Trade(
        contract=contract, order=order, orderStatus=order_status,
        fills=[], log=list(log_entries),
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _log(status: str, message: str = "", errorCode: int = 0) -> TradeLogEntry:
    return TradeLogEntry(time=_now(), status=status, message=message, errorCode=errorCode)


async def _settle() -> None:
    """Yield long enough for any spawned background task to run to completion."""
    for _ in range(20):
        await asyncio.sleep(0)


async def test_cancel_with_462_and_open_at_ib_is_suppressed():
    """The PSQ-incident pattern: synthetic Cancelled with errorCode=462 lands
    on a trade IB still has open. Engine callbacks must NOT be invoked and
    must remain registered so the eventual real terminal can resolve them."""
    client = _make_client()
    trade = _make_trade("1029", "Cancelled", [
        _log("Submitted"),
        _log("Submitted", "Modify"),
        _log("Cancelled", "Order modify failed. Cannot change to the new Time in Force.DAY", 462),
    ])

    # IB authoritatively reports the order as still open.
    client._ib = AsyncMock()
    client._ib.reqOpenOrdersAsync = AsyncMock(return_value=[trade])

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="1029")

    client._on_order_status(trade)
    await _settle()

    assert callback_invocations == []
    # Callbacks must remain registered for the eventual real terminal.
    assert client._status_callbacks.get("1029")
    client._ib.reqOpenOrdersAsync.assert_awaited_once()


async def test_cancel_with_462_and_not_open_at_ib_is_dispatched():
    """If IB confirms the order is no longer open, the synthetic Cancelled
    is real after all and must propagate as a normal terminal."""
    client = _make_client()
    trade = _make_trade("2001", "Cancelled", [
        _log("Submitted"),
        _log("Submitted", "Modify"),
        _log("Cancelled", "Order modify failed", 462),
    ])

    client._ib = AsyncMock()
    # Different order id in the open list — ours is gone.
    other = _make_trade("9999", "Submitted", [_log("Submitted")])
    client._ib.reqOpenOrdersAsync = AsyncMock(return_value=[other])

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="2001")

    client._on_order_status(trade)
    await _settle()

    assert callback_invocations == [("2001", "Cancelled")]
    # Auto-cleanup after terminal dispatch.
    assert "2001" not in client._status_callbacks


async def test_cancel_with_462_superseded_by_fill_during_query():
    """Real Filled status arrives via the normal path while the verification
    round-trip is in flight. The post-query Filled re-check must suppress the
    stale Cancelled so callbacks don't re-fire on an already-terminal order."""
    client = _make_client()
    trade = _make_trade("3010", "Cancelled", [
        _log("Submitted"),
        _log("Submitted", "Modify"),
        _log("Cancelled", "Order modify failed", 462),
    ])

    async def fake_req_open_orders():
        # While we're "waiting" for IB, the real Filled lands and updates the
        # trade.orderStatus.status via ib_async's normal path.
        trade.orderStatus.status = "Filled"
        return []  # IB no longer has it open

    client._ib = AsyncMock()
    client._ib.reqOpenOrdersAsync = fake_req_open_orders

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="3010")

    client._on_order_status(trade)
    await _settle()

    # The verifier must NOT dispatch a stale Cancelled when Filled has arrived.
    assert callback_invocations == []
    # Callbacks remain registered for the real Filled path to handle.
    assert client._status_callbacks.get("3010")


async def test_cancel_with_462_default_suppress_on_query_failure():
    """If reqOpenOrdersAsync raises, the asymmetry favors suppress: a missed
    cancel is recoverable via the engine's 120s timeout, a missed fill is
    not. Callbacks must remain registered."""
    client = _make_client()
    trade = _make_trade("4020", "Cancelled", [
        _log("Submitted"),
        _log("Submitted", "Modify"),
        _log("Cancelled", "Order modify failed", 462),
    ])

    client._ib = AsyncMock()
    client._ib.reqOpenOrdersAsync = AsyncMock(side_effect=ConnectionError("transient"))

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="4020")

    client._on_order_status(trade)
    await _settle()

    assert callback_invocations == []
    assert client._status_callbacks.get("4020")


async def test_real_cancel_without_errorcode_dispatches_synchronously():
    """A genuine cancel via ib_async's _orderStatus() path appends a log entry
    with errorCode=0 (the dataclass default — _orderStatus() doesn't set
    errorCode). It must NOT trigger the verification round-trip."""
    client = _make_client()
    trade = _make_trade("5030", "Cancelled", [
        _log("Submitted"),
        _log("Cancelled"),  # errorCode defaults to 0
    ])

    client._ib = AsyncMock()
    # Wire reqOpenOrdersAsync to fail loudly — it must NOT be called.
    client._ib.reqOpenOrdersAsync = AsyncMock(
        side_effect=AssertionError("verification path must not run for real cancels"),
    )

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="5030")

    client._on_order_status(trade)
    await _settle()

    assert callback_invocations == [("5030", "Cancelled")]
    assert "5030" not in client._status_callbacks
    client._ib.reqOpenOrdersAsync.assert_not_called()


async def test_cancel_with_other_errorcode_dispatches_synchronously():
    """Errors outside the verify whitelist (e.g. 110 minimum-tick on a brand-new
    order rejection) follow today's path: dispatch immediately. Verification
    is currently scoped to 462; broadening is a deliberate future change."""
    client = _make_client()
    trade = _make_trade("6040", "Cancelled", [
        _log("PendingSubmit"),
        _log("Cancelled", "Price does not conform to the minimum price variation", 110),
    ])

    client._ib = AsyncMock()
    client._ib.reqOpenOrdersAsync = AsyncMock(
        side_effect=AssertionError("verification must not run for non-whitelisted codes"),
    )

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="6040")

    client._on_order_status(trade)
    await _settle()

    assert callback_invocations == [("6040", "Cancelled")]
    assert "6040" not in client._status_callbacks
    client._ib.reqOpenOrdersAsync.assert_not_called()


# Use asyncio mode for all tests in this module.
pytestmark = pytest.mark.asyncio
