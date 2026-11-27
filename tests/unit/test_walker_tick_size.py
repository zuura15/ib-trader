"""Regression: the SMART_MARKET walker steps in the contract's tick.

Incident 2026-11-25: a GCG7 sell (tick $0.10) walked in hardcoded
$0.01 steps (3608.60 → 3608.59 → …). IB rejected the off-tick amends
("Invalid Price", code 201) and the order took ~50 s to fill.
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from ib_trader.engine.order import _slippage_floor, _walk_limit_aggressive
from ib_trader.engine.ticks import is_on_tick

GC_TICK = Decimal("0.10")


def _make_ctx(bid: Decimal, ask: Decimal, fill_after: int):
    """Stub ctx: fixed quote; full fill reported after ``fill_after`` polls."""
    calls = {"n": 0}

    async def _get_order_status(_oid):
        calls["n"] += 1
        filled = Decimal("1") if calls["n"] > fill_after else Decimal("0")
        return {"qty_filled": filled, "status": "Submitted"}

    async def _get_market_snapshot(_cid):
        return {"bid": bid, "ask": ask, "last": bid}

    amend = AsyncMock(return_value=None)
    ctx = SimpleNamespace(
        ib=SimpleNamespace(
            get_order_status=_get_order_status,
            get_market_snapshot=_get_market_snapshot,
            amend_order=amend,
        ),
        tracker=SimpleNamespace(get=lambda _: None),
        redis=None,
    )
    return ctx, amend


def _amended_prices(amend: AsyncMock) -> list[Decimal]:
    prices = []
    for call in amend.call_args_list:
        args = list(call.args) + list(call.kwargs.values())
        prices.extend(a for a in args if isinstance(a, Decimal))
    return prices


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["SELL", "BUY"])
async def test_walker_steps_one_contract_tick(side):
    # Far side sits at the trigger, so the walker steps a tick at a time
    # instead of chasing the quote in one jump.
    bid, ask = (Decimal("3610.0"), Decimal("3610.5")) if side == "SELL" else (
        Decimal("3609.5"), Decimal("3610.0"))
    ctx, amend = _make_ctx(bid=bid, ask=ask, fill_after=12)
    await _walk_limit_aggressive(
        ctx, con_id=1, ib_order_id="1", symbol="GCG7", side=side,
        trigger_price=Decimal("3610.0"), interval_seconds=0.001,
        total_duration_seconds=None, floor_price=None,
        target_qty=Decimal("1"), tick_size=GC_TICK,
    )
    prices = _amended_prices(amend)
    assert prices, "walker should have amended"
    assert all(is_on_tick(p, GC_TICK) for p in prices), prices
    step = Decimal("-0.10") if side == "SELL" else Decimal("0.10")
    assert prices[:3] == [Decimal("3610.0") + step * k for k in (1, 2, 3)]


@pytest.mark.asyncio
async def test_walker_snaps_off_tick_quote_toward_passive_side():
    # An off-tick bid (bad data) must not reach IB unsnapped; SELL snaps up.
    ctx, amend = _make_ctx(bid=Decimal("3605.27"), ask=Decimal("3620.0"), fill_after=3)
    await _walk_limit_aggressive(
        ctx, con_id=1, ib_order_id="1", symbol="GCG7", side="SELL",
        trigger_price=Decimal("3610.0"), interval_seconds=0.001,
        total_duration_seconds=None, floor_price=None,
        target_qty=Decimal("1"), tick_size=GC_TICK,
    )
    prices = _amended_prices(amend)
    assert prices[0] == Decimal("3605.30")


@pytest.mark.asyncio
async def test_walker_reaches_on_tick_floor_and_stops():
    floor = _slippage_floor(Decimal("3610.0"), "SELL", Decimal("0.0001"), GC_TICK)
    assert is_on_tick(floor, GC_TICK)
    ctx, _amend = _make_ctx(bid=Decimal("3500.0"), ask=Decimal("3620.0"), fill_after=10_000)
    result = await _walk_limit_aggressive(
        ctx, con_id=1, ib_order_id="1", symbol="GCG7", side="SELL",
        trigger_price=Decimal("3610.0"), interval_seconds=0.001,
        total_duration_seconds=None, floor_price=floor,
        target_qty=Decimal("1"), tick_size=GC_TICK,
    )
    assert result["hit_cap"] is True
    assert result["last_sent_price"] == floor


@pytest.mark.parametrize("side,expected", [
    ("SELL", Decimal("3592.0")),   # 3610 × 0.995 = 3591.95 → snapped up
    ("BUY", Decimal("3628.0")),    # 3610 × 1.005 = 3628.05 → snapped down
])
def test_slippage_floor_snaps_conservatively(side, expected):
    assert _slippage_floor(Decimal("3610.0"), side, Decimal("0.005"), GC_TICK) == expected


def test_slippage_floor_default_is_stock_tick():
    assert _slippage_floor(Decimal("100"), "SELL", Decimal("0.005")) == Decimal("99.50")
