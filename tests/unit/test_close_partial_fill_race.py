"""Regression: close SMART_MARKET path must promote to FILLED when the
cancel-vs-fill race lands as fill — i.e. when the engine asks IB to
cancel the residual but IB has already terminalised the order as Filled
(because the second ExecDetails callback queued behind the terminal
status dispatch and the local _fill_qty accumulator under-counted).

Operator incident 2026-06-03 (order #108025, MNQM6 SELL 10):
4 contracts filled in the first ExecDetails. The second on_fill for the
remaining 6 was scheduled via _spawn_background but the order's
callbacks were auto-unregistered the moment status went Filled, so the
relay limped in 800 ms late — well after the close-handler had snapped
qty_filled=4 and dispatched into _handle_close_partial. Pre-fix that
function fired cancel_order blindly and wrote PARTIAL_FILL filled=4
for a fully-filled order. IB had 10, ledger had 4.

Fix: _handle_close_partial now uses _cancel_and_await_resolution and,
when resolution == "filled", calls _handle_close_fill with the
IB-authoritative qty. PARTIAL_FILL is only written when the cancel
actually succeeded.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from ib_trader.data.models import TransactionAction
from ib_trader.engine import order as order_module


def _make_ctx():
    """Minimal ctx for _handle_close_partial — only the attributes the
    function actually touches."""
    txns: list = []

    class _Transactions:
        def insert(self, event):
            txns.append(event)

        def get_entry_fill(self, _trade_id):
            return SimpleNamespace(
                ib_avg_fill_price=Decimal("30604.0"), side="BUY",
            )

    ctx = SimpleNamespace(
        account_id="DU0",
        settings={"cancel_settle_timeout_seconds": 5},
        tracker=SimpleNamespace(
            get=lambda _: SimpleNamespace(
                is_filled=False, is_canceled=False,
            ),
        ),
        transactions=_Transactions(),
        trades=SimpleNamespace(update_pnl=MagicMock()),
        router=SimpleNamespace(emit=MagicMock(), update_order_row=MagicMock()),
        redis=None,
        ib=SimpleNamespace(),
    )
    return ctx, txns


def _close_ctx_and_trade_group():
    close_ctx = SimpleNamespace(
        symbol="MNQM6", side="SELL", order_type="LIMIT",
        trade_id="tg-1", leg_type=None, correlation_id="cor-1",
        security_type="FUT", multiplier=Decimal("2"),
    )
    trade_group = SimpleNamespace(
        id="tg-1", serial_number=900,
        realized_pnl=Decimal("0"), total_commission=Decimal("0"),
    )
    return close_ctx, trade_group


@pytest.mark.asyncio
async def test_close_partial_promotes_to_fill_when_cancel_loses_race(monkeypatch):
    """resolution == 'filled' → _handle_close_fill called with IB qty,
    PARTIAL_FILL NOT written. Caller's stale qty_filled is overridden."""
    ctx, txns = _make_ctx()

    async def _stub_cancel_resolve(*_args, **_kwargs):
        # IB reports the order fully filled (10) — cancel was skipped
        # because already terminal Filled.
        return ("filled", Decimal("10"), Decimal("30604.5"),
                Decimal("6.20"), "Filled")

    monkeypatch.setattr(
        order_module, "_cancel_and_await_resolution", _stub_cancel_resolve,
    )

    fill_calls: list = []

    async def _stub_handle_close_fill(close_ctx, trade_group, qty, avg, comm, _ctx):
        fill_calls.append((qty, avg, comm))

    monkeypatch.setattr(
        order_module, "_handle_close_fill", _stub_handle_close_fill,
    )

    close_ctx, trade_group = _close_ctx_and_trade_group()

    await order_module._handle_close_partial(
        close_ctx, trade_group,
        qty_requested=Decimal("10"),
        qty_filled=Decimal("4"),            # caller's stale view
        avg_price=Decimal("30604.5"),
        commission=Decimal("2.48"),
        ib_order_id="108025",
        ctx=ctx,
    )

    assert len(fill_calls) == 1, "Should have promoted to _handle_close_fill"
    qty, avg, comm = fill_calls[0]
    assert qty == Decimal("10"), (
        f"Should call _handle_close_fill with IB's authoritative qty=10, "
        f"got {qty} — order #108025 bug recurring"
    )
    assert avg == Decimal("30604.5")
    assert comm == Decimal("6.20")

    partial_writes = [
        e for e in txns
        if getattr(e, "action", None) == TransactionAction.PARTIAL_FILL
    ]
    assert partial_writes == [], (
        f"PARTIAL_FILL must NOT be written when cancel lost the race. "
        f"Pre-fix this was the bug: ledger said 4/10 while IB had 10/10. "
        f"Writes seen: {partial_writes}"
    )


@pytest.mark.asyncio
async def test_close_partial_writes_partial_when_cancel_succeeds(monkeypatch):
    """resolution == 'cancelled' → genuine partial. PARTIAL_FILL written
    with the final_qty (max of caller's qty and IB-reported qty)."""
    ctx, txns = _make_ctx()

    async def _stub_cancel_resolve(*_args, **_kwargs):
        return ("cancelled", Decimal("4"), Decimal("30604.5"),
                Decimal("2.48"), "Cancelled")

    monkeypatch.setattr(
        order_module, "_cancel_and_await_resolution", _stub_cancel_resolve,
    )

    fill_calls: list = []

    async def _stub_handle_close_fill(*args, **_kwargs):
        fill_calls.append(args)

    monkeypatch.setattr(
        order_module, "_handle_close_fill", _stub_handle_close_fill,
    )

    async def _record_pnl_noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        order_module, "_record_console_close_pnl", _record_pnl_noop,
    )

    close_ctx, trade_group = _close_ctx_and_trade_group()

    await order_module._handle_close_partial(
        close_ctx, trade_group,
        qty_requested=Decimal("10"),
        qty_filled=Decimal("4"),
        avg_price=Decimal("30604.5"),
        commission=Decimal("2.48"),
        ib_order_id="108025",
        ctx=ctx,
    )

    assert fill_calls == [], "Should NOT call _handle_close_fill on a real partial"
    partial_writes = [
        e for e in txns
        if getattr(e, "action", None) == TransactionAction.PARTIAL_FILL
    ]
    assert len(partial_writes) == 1
    assert partial_writes[0].ib_filled_qty == Decimal("4")


@pytest.mark.asyncio
async def test_close_partial_uses_ib_reported_qty_when_higher(monkeypatch):
    """IB may have delivered more fills during the cancel-settle window
    than the caller saw. final_qty = max(caller's qty, IB-reported qty)."""
    ctx, txns = _make_ctx()

    async def _stub_cancel_resolve(*_args, **_kwargs):
        # Caller passed qty_filled=4; IB reports 7 in the cancel-settle
        # window (one more execution landed before the cancel went
        # through, and then the cancel was confirmed for the remaining 3).
        return ("cancelled", Decimal("7"), Decimal("30604.5"),
                Decimal("4.34"), "Cancelled")

    monkeypatch.setattr(
        order_module, "_cancel_and_await_resolution", _stub_cancel_resolve,
    )

    async def _record_pnl_noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(
        order_module, "_record_console_close_pnl", _record_pnl_noop,
    )

    close_ctx, trade_group = _close_ctx_and_trade_group()

    await order_module._handle_close_partial(
        close_ctx, trade_group,
        qty_requested=Decimal("10"),
        qty_filled=Decimal("4"),
        avg_price=Decimal("30604.5"),
        commission=Decimal("2.48"),
        ib_order_id="108025",
        ctx=ctx,
    )

    partial_writes = [
        e for e in txns
        if getattr(e, "action", None) == TransactionAction.PARTIAL_FILL
    ]
    assert len(partial_writes) == 1
    assert partial_writes[0].ib_filled_qty == Decimal("7"), (
        "PARTIAL_FILL should record IB's higher count, not the stale "
        "caller-supplied 4"
    )
