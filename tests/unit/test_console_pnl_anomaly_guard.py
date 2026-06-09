"""Regression test for the 2026-06-09 trade #1121 console-P&L bug.

The synchronous close-fill P&L emit (``_emit_console_close_pnl``) reads
``pre_avg`` from a snapshot of the positions cache. On 2026-06-09 the
cache held a stale NQM6 entry with the wrong multiplier (50 instead of
20), so ``pre_avg`` came out 2.5× too low. The formula then produced
~$707,891 for a real close P&L of ~$715. That bogus value:
  - landed in the operator's console scrollback
  - poisoned the rolling 24h ``console:pnl:24h`` sorted set

The fix adds a deviation guard: when ``pre_avg`` is more than 50% away
from ``avg_price`` (the fill price of the same instrument seconds
later), skip the emit + Redis record entirely and log a
``PNL_ANOMALY_REJECTED`` warning. The DB stays factual — the close
fill is still recorded; only the operator-facing instant-P&L line is
suppressed when the input is obviously garbage.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from ib_trader.data.models import LegType, TradeGroup, TradeStatus
from ib_trader.engine.order import _OrderContext, _emit_console_close_pnl


def _now():
    return datetime.now(timezone.utc)


def _make_router_emit_capture():
    captured = []

    class _Router:
        def emit(self, msg, *, pane=None, severity=None, event=None):
            captured.append({"msg": msg, "event": event})

    return _Router(), captured


def _make_ctx_with_router():
    router, captured = _make_router_emit_capture()
    # Minimal AppContext-shaped namespace. ``redis=None`` → the
    # _record_console_close_pnl path no-ops gracefully.
    return SimpleNamespace(router=router, redis=None), captured


def _make_order_ctx(pre_qty: Decimal, pre_avg: Decimal, multiplier: Decimal):
    return _OrderContext(
        trade_id="00000000-0000-0000-0000-000000000001",
        trade_serial=9999,
        symbol="NQM6",
        side="SELL",
        order_type="MID",
        qty_requested=Decimal("2"),
        leg_type=LegType.ENTRY,
        correlation_id="cid-test",
        security_type="FUT",
        ib_order_id="IB1234",
        multiplier=multiplier,
        pre_position_qty=pre_qty,
        pre_position_avg_cost=pre_avg,
    )


class TestConsoleClosePnlAnomalyGuard:
    @pytest.mark.asyncio
    async def test_emit_skipped_when_pre_avg_is_stale_multiplier_bug(self, caplog):
        """The exact 2026-06-09 trade #1121 fingerprint: pre_avg ~= exit/2.5
        because positions cache had a stale multiplier. Guard rejects the
        emit and logs PNL_ANOMALY_REJECTED."""
        ctx, captured = _make_ctx_with_router()
        # Stale pre_avg: real entry was ~29465, but cache showed 11786.345.
        order_ctx = _make_order_ctx(
            pre_qty=Decimal("2"),         # LONG 2
            pre_avg=Decimal("11786.345"), # stale (real ~29465)
            multiplier=Decimal("20"),
        )
        with caplog.at_level("WARNING"):
            await _emit_console_close_pnl(
                ctx, order_ctx,
                qty_filled=Decimal("2"),
                avg_price=Decimal("29483.625"),  # actual fill price
                commission=Decimal("4.5"),
            )

        # No console emit fired.
        assert not [c for c in captured
                    if c["event"] == "CONSOLE_CLOSE_PNL_DISPLAY"]
        # Anomaly warning was logged.
        assert any("PNL_ANOMALY_REJECTED" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_emit_fires_when_pre_avg_is_close_to_exit(self):
        """Normal case: pre_avg ~ exit price (within 50%). Guard passes,
        the emit + log fire as before."""
        ctx, captured = _make_ctx_with_router()
        order_ctx = _make_order_ctx(
            pre_qty=Decimal("2"),
            pre_avg=Decimal("29465.75"),   # correct entry
            multiplier=Decimal("20"),
        )
        await _emit_console_close_pnl(
            ctx, order_ctx,
            qty_filled=Decimal("2"),
            avg_price=Decimal("29483.625"),
            commission=Decimal("4.5"),
        )

        emits = [c for c in captured
                 if c["event"] == "CONSOLE_CLOSE_PNL_DISPLAY"]
        assert len(emits) == 1
        # Sanity-check the displayed P&L is in the sensible range
        # ((29483.625 - 29465.75) × 2 × 20 - 4.5 = $710.50).
        assert "$710.50" in emits[0]["msg"]

    @pytest.mark.asyncio
    async def test_emit_skipped_for_50pct_gap_move_just_over_threshold(self, caplog):
        """Deviation exactly at the 50% threshold is rejected (>0.5 ratio).
        Anything beyond a 50% intra-day swing in a futures contract is
        almost certainly a stale-snapshot bug — refuse to print it."""
        ctx, captured = _make_ctx_with_router()
        order_ctx = _make_order_ctx(
            pre_qty=Decimal("1"),
            pre_avg=Decimal("10000"),    # 66% below exit
            multiplier=Decimal("20"),
        )
        with caplog.at_level("WARNING"):
            await _emit_console_close_pnl(
                ctx, order_ctx,
                qty_filled=Decimal("1"),
                avg_price=Decimal("30000"),
                commission=Decimal("2.25"),
            )
        assert not [c for c in captured
                    if c["event"] == "CONSOLE_CLOSE_PNL_DISPLAY"]
        assert any("PNL_ANOMALY_REJECTED" in r.getMessage() for r in caplog.records)
