"""Regression tests for the SMART_MARKET walker's pre-amend status check.

The walker in ``_walk_limit_aggressive`` must re-check the order's current
state right before every ``amend_order`` call — not just at the top of the
loop. Without that, a fill arriving on the IB callback during the walker's
sleep + quote-fetch window produces an amend on a terminal order, which
IB rejects with code 104/201 and ib_async flags with an internal
AssertionError. The user-visible outcome was correct (order filled), but
the log noise made every fast-fill look like a bug.
"""
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ib_trader.engine.order import _walk_limit_aggressive


class _MockIB:
    def __init__(self, status_seq: list[dict], snap: dict) -> None:
        self._status_seq = list(status_seq)
        self._snap = snap
        self.amend_calls: list[tuple[str, Decimal]] = []

    async def get_order_status(self, ib_order_id: str) -> dict:
        if len(self._status_seq) > 1:
            return self._status_seq.pop(0)
        return self._status_seq[0]

    async def get_market_snapshot(self, con_id: int) -> dict:
        return self._snap

    async def amend_order(self, ib_order_id: str, price: Decimal) -> None:
        self.amend_calls.append((ib_order_id, price))


def _ctx(ib: _MockIB) -> SimpleNamespace:
    tracker = SimpleNamespace(get=lambda _oid: None)
    return SimpleNamespace(ib=ib, tracker=tracker, redis=None)


class TestPreAmendStatusCheck:
    @pytest.mark.asyncio
    async def test_skips_amend_when_filled_mid_sleep(self):
        # First get_order_status (top-of-loop) sees Submitted with a
        # partial fill; second call (pre-amend re-check) sees Filled.
        # The amend call MUST be skipped.
        ib = _MockIB(
            status_seq=[
                {"status": "Submitted", "qty_filled": Decimal("0")},
                {"status": "Filled", "qty_filled": Decimal("10")},
            ],
            snap={"bid": Decimal("100.00"), "ask": Decimal("100.10"), "last": Decimal("100.05")},
        )
        result = await _walk_limit_aggressive(
            _ctx(ib), con_id=1, ib_order_id="500", symbol="USO", side="SELL",
            trigger_price=Decimal("100.05"), interval_seconds=0.001,
            total_duration_seconds=0.5, floor_price=None,
            target_qty=Decimal("10"),
        )
        assert ib.amend_calls == [], "amend must not be called on a filled order"
        assert result["status"] == "filled"

    @pytest.mark.asyncio
    async def test_skips_amend_when_cancelled_mid_sleep(self):
        ib = _MockIB(
            status_seq=[
                {"status": "Submitted", "qty_filled": Decimal("0")},
                {"status": "Cancelled", "qty_filled": Decimal("0")},
            ],
            snap={"bid": Decimal("100.00"), "ask": Decimal("100.10"), "last": Decimal("100.05")},
        )
        result = await _walk_limit_aggressive(
            _ctx(ib), con_id=1, ib_order_id="501", symbol="USO", side="SELL",
            trigger_price=Decimal("100.05"), interval_seconds=0.001,
            total_duration_seconds=0.5, floor_price=None,
            target_qty=Decimal("10"),
        )
        assert ib.amend_calls == []
        assert result["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_amends_when_still_submitted(self):
        # Both checks see Submitted — walker should send at least one amend.
        ib = _MockIB(
            status_seq=[{"status": "Submitted", "qty_filled": Decimal("0")}],
            snap={"bid": Decimal("99.98"), "ask": Decimal("100.10"), "last": Decimal("100.00")},
        )
        result = await _walk_limit_aggressive(
            _ctx(ib), con_id=1, ib_order_id="502", symbol="USO", side="SELL",
            trigger_price=Decimal("100.05"), interval_seconds=0.001,
            total_duration_seconds=0.05, floor_price=None,
            target_qty=Decimal("10"),
        )
        assert len(ib.amend_calls) >= 1
        assert result["status"] == "duration_expired"
