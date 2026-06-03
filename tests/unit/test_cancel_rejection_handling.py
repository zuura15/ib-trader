"""Tests for the cancel-rejection failure mode and its recovery.

The bug (2026-06-02, order #98852): operator placed a partial sell, the
engine attempted to cancel the residual, IB rejected the cancel with
error 10340 ("ManualOrderIndicator not supported"), the engine's
_verify_cancel correctly detected the order was still open at IB and
suppressed the false Cancelled event — but then sat there until the
120s _cancel_and_await_resolution timeout, after which _handle_partial
unconditionally wrote TransactionAction.CANCELLED. The audit ledger
recorded the order as cancelled while it was still working at IB.

Two layered fixes:
  A. _on_error special-cases 10340 during an in-flight cancel and
     schedules a single retry with manualOrderIndicator stripped.
  B. _finalize_partial_cancel verifies via reqOpenOrders before
     writing CANCELLED, falls back to CATASTROPHIC + ledger left open
     when IB still has it working.

These tests pin both. We do NOT test asyncio orchestration of
_spawn_background here — that's covered implicitly by the engine
integration; we test that the retry SCHEDULER fires the coroutine
and that the helper does the right work when awaited directly.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock, AsyncMock

import pytest

from ib_trader.ib.insync_client import InsyncClient


# ──────────────────────────────────────────────────────────────────────
# Part A: InsyncClient cancel-retry on error 10340
# ──────────────────────────────────────────────────────────────────────

def _make_client_with_active_trade(ib_order_id: str) -> InsyncClient:
    client = InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
    )
    fake_ib = MagicMock()
    fake_ib.cancelOrder = MagicMock()
    client._InsyncClient__ib = fake_ib
    # Seed an active trade with a non-terminal status and a fake Order
    fake_order = MagicMock()
    fake_order.manualOrderIndicator = "True"  # the attribute IB rejected
    fake_trade = MagicMock()
    fake_trade.order = fake_order
    fake_trade.orderStatus = MagicMock(status="Submitted")
    client._InsyncClient__active_trades[ib_order_id] = fake_trade
    return client


@pytest.mark.asyncio
async def test_retry_cancel_clean_strips_manual_indicator_and_resends():
    """The retry path clears manualOrderIndicator AND calls cancelOrder
    a second time on the same Order object."""
    client = _make_client_with_active_trade("98852")
    client._pending_cancel_ids.add("98852")

    await client._retry_cancel_clean(
        "98852", original_code=10340, original_msg="not supported",
    )

    trade = client._InsyncClient__active_trades["98852"]
    assert trade.order.manualOrderIndicator is None
    client._InsyncClient__ib.cancelOrder.assert_called_once_with(trade.order)
    assert "98852" in client._cancel_retry_attempted


@pytest.mark.asyncio
async def test_retry_cancel_skipped_when_already_retried():
    """One retry per cancel attempt — second call is a no-op."""
    client = _make_client_with_active_trade("98852")
    client._pending_cancel_ids.add("98852")
    client._cancel_retry_attempted.add("98852")

    await client._retry_cancel_clean(
        "98852", original_code=10340, original_msg="not supported",
    )

    client._InsyncClient__ib.cancelOrder.assert_not_called()


@pytest.mark.asyncio
async def test_retry_cancel_skipped_when_terminal():
    """If the order terminalized between the original cancel and the
    retry trigger, don't re-cancel a dead order."""
    client = _make_client_with_active_trade("98852")
    client._InsyncClient__active_trades["98852"].orderStatus.status = "Filled"

    await client._retry_cancel_clean(
        "98852", original_code=10340, original_msg="not supported",
    )

    client._InsyncClient__ib.cancelOrder.assert_not_called()


def test_on_error_10340_during_pending_cancel_schedules_retry(monkeypatch):
    """_on_error(10340) on an in-flight cancel must spawn the retry.
    For non-cancel contexts the same code stays a no-op notice."""
    client = _make_client_with_active_trade("98852")
    client._pending_cancel_ids.add("98852")

    # Capture _spawn_background invocations so we can assert the retry
    # was scheduled without actually running it.
    spawned: list = []
    monkeypatch.setattr(
        "ib_trader.ib.insync_client._spawn_background",
        lambda coro: spawned.append(coro),
    )

    client._on_error(98852, 10340, "ManualOrderIndicator not supported")

    assert len(spawned) == 1, "exactly one retry coroutine should be scheduled"
    # The scheduled object is a coroutine; close it so pytest doesn't warn.
    spawned[0].close()


def test_on_error_10340_without_pending_cancel_is_noop(monkeypatch):
    """Same code with no in-flight cancel: it's the benign place-order
    notice, must not schedule a retry."""
    client = _make_client_with_active_trade("98852")
    # NOT adding "98852" to _pending_cancel_ids

    spawned: list = []
    monkeypatch.setattr(
        "ib_trader.ib.insync_client._spawn_background",
        lambda coro: spawned.append(coro),
    )

    client._on_error(98852, 10340, "ManualOrderIndicator not supported")

    assert spawned == [], "no retry should be scheduled outside a cancel context"


@pytest.mark.asyncio
async def test_cancel_order_records_pending_id():
    """cancel_order must add to _pending_cancel_ids so a subsequent
    10340 error is recognised as a cancel rejection."""
    client = _make_client_with_active_trade("98852")
    await client.cancel_order("98852")
    assert "98852" in client._pending_cancel_ids


def test_unregister_clears_cancel_retry_state():
    """Terminal-status cleanup drops both pending-cancel sets so a
    later 10340 for a recycled order_id doesn't replay stale state."""
    client = _make_client_with_active_trade("98852")
    client._pending_cancel_ids.add("98852")
    client._cancel_retry_attempted.add("98852")
    client.unregister_callbacks("98852")
    assert "98852" not in client._pending_cancel_ids
    assert "98852" not in client._cancel_retry_attempted


def test_ib_async_cancel_order_patched_full_fields_by_serverversion():
    """Importing insync_client monkey-patches ``ib_async.Client.cancelOrder``
    to send the FULL cancelOrder field set the Gateway's serverVersion
    expects. ib_async 2.1.0 only sends through v169 natively, but the
    prod Gateway is on v178 and rejects under-sized cancel messages
    with error 10340 "{expected field name} not supported".

    Pins the per-serverVersion field count so we'll fail loudly if the
    patch ever gets accidentally reverted or the version thresholds
    drift out of sync with IB's API changelog. See insync_client.py
    module docstring for the incident #894 (and prior #867/#883)
    context."""
    import ib_trader.ib.insync_client  # noqa: F401
    from ib_async.client import Client

    sent: list = []

    class _StubClient:
        def __init__(self, sv):
            self._sv = sv

        def send(self, *fields):
            sent.append(fields)

        def serverVersion(self):
            return self._sv

    # serverVersion 178 (prod) → expect all v169-v176 optional fields
    # present with empty-string defaults for the operator-identity ones.
    sent.clear()
    Client.cancelOrder(_StubClient(178), 12345, "")
    assert sent == [(4, 1, 12345, "", "", "", "", "")], (
        f"v178: expected base+manualCancelOrderTime+extOperator+"
        f"manualOrderIndicator+externalUserId+externalUserIdType; got {sent}"
    )

    # Old Gateway < v169 — no optional fields appended.
    sent.clear()
    Client.cancelOrder(_StubClient(168), 12345, "")
    assert sent == [(4, 1, 12345)], (
        f"v168: expected [4, 1, orderId] only; got {sent}"
    )

    # v169 floor — just manualCancelOrderTime appended.
    sent.clear()
    Client.cancelOrder(_StubClient(169), 12345, "20260602 18:48:06")
    assert sent == [(4, 1, 12345, "20260602 18:48:06")], (
        f"v169: expected manualCancelOrderTime only; got {sent}"
    )

    # v175 — through manualOrderIndicator; externalUserId NOT yet appended.
    sent.clear()
    Client.cancelOrder(_StubClient(175), 12345, "")
    assert sent == [(4, 1, 12345, "", "", "")], (
        f"v175: expected through manualOrderIndicator; got {sent}"
    )


# ──────────────────────────────────────────────────────────────────────
# Part B: _finalize_partial_cancel — verify before writing CANCELLED
# ──────────────────────────────────────────────────────────────────────

class _FakeRouter:
    def emit(self, *args, **kwargs):
        pass


class _FakeIB:
    """Stub IB client exposing only what _finalize_partial_cancel
    consumes: ``is_order_open_at_ib``."""
    def __init__(self, *, still_open: bool | None):
        self._still_open = still_open

    async def is_order_open_at_ib(self, ib_order_id: str) -> bool | None:
        return self._still_open


def _make_ctx(*, still_open: bool | None, redis=None) -> SimpleNamespace:
    return SimpleNamespace(
        ib=_FakeIB(still_open=still_open),
        router=_FakeRouter(),
        redis=redis,
        # _write_txn references ctx.transactions.insert; provide a no-op stub
        transactions=SimpleNamespace(
            insert=MagicMock(),
            get_entry_fill=MagicMock(return_value=None),
            get_filled_legs=MagicMock(return_value=[]),
        ),
        trades=SimpleNamespace(
            update_pnl=MagicMock(), update_status=MagicMock(),
        ),
    )


def _make_order_ctx() -> SimpleNamespace:
    return SimpleNamespace(
        symbol="MGCQ6", side="SELL", order_type="MID",
        trade_id="t-1", leg_type=None,
        correlation_id="c-1", security_type="FUT",
        ib_order_id="98852",
    )


def _make_trade_group():
    return SimpleNamespace(id="tg-1", serial_number=836)


@pytest.mark.asyncio
async def test_finalize_resolution_cancelled_writes_cancelled_skips_probe(monkeypatch):
    """resolution="cancelled" → write CANCELLED, do NOT call reqOpenOrders.
    Fast path: IB already returned Cancelled and _verify_cancel passed."""
    from ib_trader.engine import order as order_mod

    ctx = _make_ctx(still_open=True)  # would say still_open=True if probed
    # But it should NOT be probed.
    writes: list = []
    monkeypatch.setattr(
        order_mod, "_write_txn",
        lambda ctx, action, *a, **kw: writes.append((action, kw)),
    )

    written, note = await order_mod._finalize_partial_cancel(
        ctx, _make_order_ctx(), _make_trade_group(),
        ib_order_id="98852", qty_requested=Decimal("20"),
        final_qty=Decimal("14"), effective_avg=Decimal("4519.0"),
        commission=Decimal("3.0"), resolution="cancelled",
    )
    assert written is True
    assert note == "cancel confirmed"
    actions = [w[0].name if hasattr(w[0], "name") else str(w[0]) for w in writes]
    assert any("CANCELLED" in a for a in actions)


@pytest.mark.asyncio
async def test_finalize_timeout_with_still_open_does_NOT_write_cancelled(monkeypatch):
    """resolution="timeout" + reqOpenOrders says still_open=True →
    NO CANCELLED row + CATASTROPHIC alert fires. This is THE bug fix."""
    from ib_trader.engine import order as order_mod

    ctx = _make_ctx(still_open=True)
    writes: list = []
    monkeypatch.setattr(
        order_mod, "_write_txn",
        lambda ctx, action, *a, **kw: writes.append((action, kw)),
    )

    alerts: list = []

    async def fake_log_and_alert(**kw):
        alerts.append(kw)

    monkeypatch.setattr(
        "ib_trader.logging_.alerts.log_and_alert", fake_log_and_alert,
    )

    written, note = await order_mod._finalize_partial_cancel(
        ctx, _make_order_ctx(), _make_trade_group(),
        ib_order_id="98852", qty_requested=Decimal("20"),
        final_qty=Decimal("14"), effective_avg=Decimal("4519.0"),
        commission=Decimal("3.0"), resolution="timeout",
    )
    assert written is False, "CANCELLED must NOT be written when IB still has the order"
    assert "FAILED" in note or "failed" in note.lower()

    action_names = [
        w[0].name if hasattr(w[0], "name") else str(w[0]) for w in writes
    ]
    assert not any("CANCELLED" in a for a in action_names), (
        f"no CANCELLED write expected; got writes: {action_names}"
    )

    assert len(alerts) == 1
    a = alerts[0]
    assert a["severity"] == "CATASTROPHIC"
    assert a["trigger"] == "IB_CANCEL_FAILED_ORDER_STILL_LIVE"
    assert a["dedup_key"] == "cancel_failed:98852"
    assert "98852" in a["message"]
    assert "MGCQ6" in a["message"]


@pytest.mark.asyncio
async def test_finalize_timeout_with_ib_confirms_gone_writes_cancelled(monkeypatch):
    """resolution="timeout" + reqOpenOrders says still_open=False →
    safe to write CANCELLED. IB just didn't push us the terminal event."""
    from ib_trader.engine import order as order_mod

    ctx = _make_ctx(still_open=False)
    writes: list = []
    monkeypatch.setattr(
        order_mod, "_write_txn",
        lambda ctx, action, *a, **kw: writes.append((action, kw)),
    )

    written, note = await order_mod._finalize_partial_cancel(
        ctx, _make_order_ctx(), _make_trade_group(),
        ib_order_id="98852", qty_requested=Decimal("20"),
        final_qty=Decimal("14"), effective_avg=Decimal("4519.0"),
        commission=Decimal("3.0"), resolution="timeout",
    )
    assert written is True
    assert "verified terminal" in note
    action_names = [
        w[0].name if hasattr(w[0], "name") else str(w[0]) for w in writes
    ]
    assert any("CANCELLED" in a for a in action_names)


@pytest.mark.asyncio
async def test_finalize_timeout_with_probe_failure_does_NOT_write_cancelled(monkeypatch):
    """Probe returns None (network blip) → fail-safe: treat as
    'might still be open', do NOT write CANCELLED, escalate."""
    from ib_trader.engine import order as order_mod

    ctx = _make_ctx(still_open=None)
    writes: list = []
    monkeypatch.setattr(
        order_mod, "_write_txn",
        lambda ctx, action, *a, **kw: writes.append((action, kw)),
    )

    alerts: list = []

    async def fake_log_and_alert(**kw):
        alerts.append(kw)

    monkeypatch.setattr(
        "ib_trader.logging_.alerts.log_and_alert", fake_log_and_alert,
    )

    written, note = await order_mod._finalize_partial_cancel(
        ctx, _make_order_ctx(), _make_trade_group(),
        ib_order_id="98852", qty_requested=Decimal("20"),
        final_qty=Decimal("14"), effective_avg=Decimal("4519.0"),
        commission=Decimal("3.0"), resolution="timeout",
    )
    assert written is False, (
        "fail-safe: when probe can't confirm, do not record CANCELLED"
    )
    assert "UNVERIFIED" in note or "probe" in note.lower()
    action_names = [
        w[0].name if hasattr(w[0], "name") else str(w[0]) for w in writes
    ]
    assert not any("CANCELLED" in a for a in action_names)
    assert len(alerts) == 1
    assert alerts[0]["severity"] == "CATASTROPHIC"


# ──────────────────────────────────────────────────────────────────────
# Part D: Cancel-verification reconciler
# ──────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cancel_verification_flags_ledger_cancelled_but_ib_open(monkeypatch):
    """A CANCELLED row whose ib_order_id appears in IB's open-orders
    list must produce a DISCREPANCY row + CATASTROPHIC alert."""
    from ib_trader.daemon import reconciler as recon

    cancelled_txn = SimpleNamespace(
        id=2820, ib_order_id=98852, ib_perm_id=None, symbol="MGCQ6",
        side="SELL", order_type="MID", quantity=Decimal("6"),
        limit_price=None, account_id="DU0", trade_serial=836,
    )

    inserted: list = []
    alert_created: list = []

    ctx = SimpleNamespace(
        transactions=SimpleNamespace(
            get_recent_cancelled=MagicMock(return_value=[cancelled_txn]),
            insert=lambda e: inserted.append(e),
        ),
        ib=SimpleNamespace(
            get_open_orders=AsyncMock(return_value=[
                {"ib_order_id": 98852, "symbol": "MGCQ6"},
                {"ib_order_id": 12345, "symbol": "FOO"},
            ]),
        ),
        alerts=SimpleNamespace(create=lambda a: alert_created.append(a)),
    )

    result = await recon.run_cancel_verification(ctx, since_minutes=60)

    assert result["checked"] == 1
    assert result["still_open"] == 1
    assert 98852 in result["details"]
    # Discrepancy row written
    assert len(inserted) == 1
    assert inserted[0].action.name == "DISCREPANCY"
    assert inserted[0].ib_order_id == 98852
    # CATASTROPHIC alert created
    assert len(alert_created) == 1
    assert alert_created[0].severity.name == "CATASTROPHIC"
    assert "98852" in alert_created[0].message


@pytest.mark.asyncio
async def test_cancel_verification_no_op_when_ledger_matches_ib():
    """A CANCELLED row whose ib_order_id is NOT in IB's open-orders
    list = ledger and IB agree → no discrepancy, no alert."""
    from ib_trader.daemon import reconciler as recon

    cancelled_txn = SimpleNamespace(
        id=2820, ib_order_id=98852, ib_perm_id=None, symbol="MGCQ6",
        side="SELL", order_type="MID", quantity=Decimal("6"),
        limit_price=None, account_id="DU0", trade_serial=836,
    )

    inserted: list = []
    alert_created: list = []

    ctx = SimpleNamespace(
        transactions=SimpleNamespace(
            get_recent_cancelled=MagicMock(return_value=[cancelled_txn]),
            insert=lambda e: inserted.append(e),
        ),
        ib=SimpleNamespace(
            get_open_orders=AsyncMock(return_value=[
                {"ib_order_id": 99999, "symbol": "FOO"},
            ]),
        ),
        alerts=SimpleNamespace(create=lambda a: alert_created.append(a)),
    )

    result = await recon.run_cancel_verification(ctx, since_minutes=60)

    assert result["checked"] == 1
    assert result["still_open"] == 0
    assert inserted == []
    assert alert_created == []


@pytest.mark.asyncio
async def test_cancel_verification_handles_empty_recent_cancelled():
    """No recently-cancelled rows → no IB call, no work, no error."""
    from ib_trader.daemon import reconciler as recon

    ctx = SimpleNamespace(
        transactions=SimpleNamespace(
            get_recent_cancelled=MagicMock(return_value=[]),
        ),
        ib=SimpleNamespace(get_open_orders=AsyncMock(return_value=[])),
        alerts=SimpleNamespace(create=MagicMock()),
    )

    result = await recon.run_cancel_verification(ctx, since_minutes=60)
    assert result == {"checked": 0, "still_open": 0, "details": []}
    # Should not have hit IB at all (early return).
    ctx.ib.get_open_orders.assert_not_called()
