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
    client._InsyncClient__ib = AsyncMock()
    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(return_value=[trade])

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="1029")

    client._on_order_status(trade)
    await _settle()

    assert callback_invocations == []
    # Callbacks must remain registered for the eventual real terminal.
    assert client._status_callbacks.get("1029")
    client._InsyncClient__ib.reqOpenOrdersAsync.assert_awaited_once()


async def test_cancel_with_462_and_not_open_at_ib_is_dispatched():
    """If IB confirms the order is no longer open, the synthetic Cancelled
    is real after all and must propagate as a normal terminal."""
    client = _make_client()
    trade = _make_trade("2001", "Cancelled", [
        _log("Submitted"),
        _log("Submitted", "Modify"),
        _log("Cancelled", "Order modify failed", 462),
    ])

    client._InsyncClient__ib = AsyncMock()
    # Different order id in the open list — ours is gone.
    other = _make_trade("9999", "Submitted", [_log("Submitted")])
    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(return_value=[other])

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

    client._InsyncClient__ib = AsyncMock()
    client._InsyncClient__ib.reqOpenOrdersAsync = fake_req_open_orders

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

    client._InsyncClient__ib = AsyncMock()
    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(side_effect=ConnectionError("transient"))

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="4020")

    client._on_order_status(trade)
    await _settle()

    assert callback_invocations == []
    assert client._status_callbacks.get("4020")


async def test_real_cancel_without_errorcode_now_verifies_too():
    """Updated for the universal verify gate: every Cancelled the wrapper
    sees triggers a reqOpenOrdersAsync verification, even ones with no
    errorCode in trade.log (the original dataclass-default-zero shape).
    Real cancels (verified not-open at IB) still dispatch as terminal —
    just with one extra round-trip's latency."""
    client = _make_client()
    trade = _make_trade("5030", "Cancelled", [
        _log("Submitted"),
        _log("Cancelled"),  # errorCode defaults to 0
    ])

    # Verify confirms the order is gone — dispatch should fire as terminal.
    client._InsyncClient__ib = AsyncMock()
    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(return_value=[])

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="5030")

    client._on_order_status(trade)
    await _settle()

    assert callback_invocations == [("5030", "Cancelled")]
    assert "5030" not in client._status_callbacks
    client._InsyncClient__ib.reqOpenOrdersAsync.assert_awaited_once()
    # Restored to "Cancelled" after verify confirmed.
    assert trade.orderStatus.status == "Cancelled"


async def test_cancel_with_other_errorcode_now_verifies():
    """Updated for the universal verify gate: any Cancelled (regardless of
    error code in trade.log) goes through the verify round-trip. Was
    previously gated on a {462} allowlist — now generalised so unknown
    synthetic-cancel patterns can't bypass the gate."""
    client = _make_client()
    trade = _make_trade("6040", "Cancelled", [
        _log("PendingSubmit"),
        _log("Cancelled", "Price does not conform to the minimum price variation", 110),
    ])

    # Verify confirms still open — suppression path. trade.orderStatus.status
    # gets patched back to the previous clean value ("Submitted" — the
    # last non-Cancelled status before the synthetic cancel).
    client._InsyncClient__ib = AsyncMock()
    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(return_value=[trade])

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="6040")
    # Seed previous clean status to "Submitted" — under the universal gate
    # we patch the field back to this value while verify is in flight.
    client._previous_clean_status_map["6040"] = "Submitted"

    client._on_order_status(trade)
    await _settle()

    # Verify ran, found order open at IB → no callback dispatch.
    assert callback_invocations == []
    # Status patched back to the previous clean value.
    assert trade.orderStatus.status == "Submitted"
    # Callbacks still registered for the next event.
    assert client._status_callbacks.get("6040")
    client._InsyncClient__ib.reqOpenOrdersAsync.assert_awaited_once()


# ─────────────────────────────────────────────────────────────────────────────
# Universal-verify-gate tests (added when the verify scope was generalised
# beyond the {462} allowlist). Each scenario pins one corner of the new
# behaviour: tracking the previous clean status, patching the Trade field
# during verify-pending, restoring on verify-failure, and surviving common
# edge cases (PreSubmitted, default fallback, unregister cleanup).
# ─────────────────────────────────────────────────────────────────────────────


async def test_unknown_error_code_now_verified():
    """Synthetic Cancelled with an unknown error code (was outside the old
    {462} allowlist) now triggers verify and gets suppressed when IB
    confirms still-open. Trade field is patched back to the previous
    clean status."""
    client = _make_client()
    trade = _make_trade("7100", "Cancelled", [
        _log("Submitted"),
        _log("Cancelled", "made-up rejection", 99999),
    ])
    client._InsyncClient__ib = AsyncMock()
    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(return_value=[trade])

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="7100")
    client._previous_clean_status_map["7100"] = "Submitted"

    client._on_order_status(trade)
    await _settle()

    assert callback_invocations == []
    assert trade.orderStatus.status == "Submitted"


async def test_cancel_with_no_error_log_also_verified():
    """Synthetic Cancelled with empty trade.log (the case the old
    discriminator skipped entirely) now verifies."""
    client = _make_client()
    trade = _make_trade("7200", "Cancelled", [])  # empty log
    client._InsyncClient__ib = AsyncMock()
    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(return_value=[trade])

    client.register_status_callback(
        lambda *a: None, ib_order_id="7200"
    )

    client._on_order_status(trade)
    await _settle()

    client._InsyncClient__ib.reqOpenOrdersAsync.assert_awaited_once()


async def test_get_order_status_returns_patched_value():
    """During the verify window, get_order_status reads
    trade.orderStatus.status which has been patched back to the previous
    clean value. Walker polls see a non-misleading state."""
    from ib_async import Contract

    client = _make_client()
    trade = _make_trade("7300", "Cancelled", [
        _log("Submitted"),
        _log("Cancelled", "modify failed", 462),
    ])
    # Make trade visible via __active_trades so get_order_status finds it.
    client._InsyncClient__active_trades["7300"] = trade
    client._InsyncClient__ib = AsyncMock()
    # Block the verify so we read the patched state mid-flight.
    verify_started = asyncio.Event()
    verify_unblock = asyncio.Event()

    async def slow_verify():
        verify_started.set()
        await verify_unblock.wait()
        return [trade]

    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(side_effect=slow_verify)

    client.register_status_callback(lambda *a: None, ib_order_id="7300")
    client._previous_clean_status_map["7300"] = "Submitted"

    client._on_order_status(trade)
    # Wait for verify to be in flight.
    await asyncio.wait_for(verify_started.wait(), timeout=1.0)

    # Mid-flight: get_order_status reads the patched value, not "Cancelled".
    status = await client.get_order_status("7300")
    assert status["status"] == "Submitted"

    # Let verify finish.
    verify_unblock.set()
    await _settle()


async def test_verify_failure_restores_cancelled_and_dispatches():
    """When verify confirms the cancel was real (order not in open list),
    the wrapper restores trade.orderStatus.status to 'Cancelled' and
    dispatches callbacks as terminal."""
    client = _make_client()
    trade = _make_trade("7400", "Cancelled", [
        _log("Submitted"),
        _log("Cancelled", "modify failed", 462),
    ])
    # Verify says order is GONE (not in open list).
    client._InsyncClient__ib = AsyncMock()
    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(return_value=[])

    callback_invocations: list[tuple[str, str]] = []

    async def status_cb(ib_order_id: str, status: str) -> None:
        callback_invocations.append((ib_order_id, status))

    client.register_status_callback(status_cb, ib_order_id="7400")
    client._previous_clean_status_map["7400"] = "Submitted"

    client._on_order_status(trade)
    await _settle()

    # Trade field restored to "Cancelled".
    assert trade.orderStatus.status == "Cancelled"
    # Callback fired with the terminal status.
    assert callback_invocations == [("7400", "Cancelled")]
    # Callbacks unregistered after terminal dispatch.
    assert "7400" not in client._status_callbacks


async def test_previous_clean_status_tracks_presubmitted():
    """If the order's last clean status was PreSubmitted (not yet at a
    venue), the patch should preserve PreSubmitted, not regress to
    'Submitted'."""
    client = _make_client()
    # First: PreSubmitted lands, recorded.
    pre_trade = _make_trade("7500", "PreSubmitted", [_log("PreSubmitted")])
    client._InsyncClient__ib = AsyncMock()
    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(return_value=[pre_trade])
    client.register_status_callback(lambda *a: None, ib_order_id="7500")
    client._on_order_status(pre_trade)
    await _settle()
    assert client._previous_clean_status_map.get("7500") == "PreSubmitted"

    # Now: synthetic Cancelled on the SAME order. Patch should be
    # "PreSubmitted", not "Submitted".
    cancel_trade = _make_trade("7500", "Cancelled", [
        _log("PreSubmitted"),
        _log("Cancelled", "modify failed", 462),
    ])
    client._on_order_status(cancel_trade)
    await _settle()
    assert cancel_trade.orderStatus.status == "PreSubmitted"


async def test_previous_clean_status_default_is_submitted():
    """If no clean status has ever been seen for an order, the patch
    falls back to 'Submitted' (safe lower bound for an order ib_async
    tracks as alive)."""
    client = _make_client()
    trade = _make_trade("7600", "Cancelled", [
        _log("Cancelled", "modify failed", 462),
    ])
    client._InsyncClient__ib = AsyncMock()
    client._InsyncClient__ib.reqOpenOrdersAsync = AsyncMock(return_value=[trade])
    client.register_status_callback(lambda *a: None, ib_order_id="7600")
    # Note: NO seed of _previous_clean_status_map.

    client._on_order_status(trade)
    await _settle()

    assert trade.orderStatus.status == "Submitted"


async def test_unregister_clears_previous_clean_status_entry():
    """Map doesn't leak entries across orders — cleared on
    unregister_callbacks (which fires after terminal dispatch)."""
    client = _make_client()
    client._previous_clean_status_map["7700"] = "Submitted"
    client._fill_callbacks["7700"] = [lambda *a: None]

    client.unregister_callbacks("7700")

    assert "7700" not in client._previous_clean_status_map


# Use asyncio mode for all tests in this module.
pytestmark = pytest.mark.asyncio
