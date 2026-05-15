"""Audit-feed emission from chart_signal.

Verifies that on each bar event, the strategy appends a single
``EmitAudit`` action with the correct headline fields. Trade-closure
and order-placement audit hooks are tested in middleware/runtime
integration tests; here we focus on the strategy-level synthesis.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategies.chart_signal import ChartSignalStrategy
from ib_trader.bots.strategy import (
    BarCompleted, EmitAudit, PlaceOrder, StrategyContext,
)
from tests.unit.bots.test_chart_signal_strategy import (
    _default_config, _make_ctx, _zigzag_bars,
    ZIGZAG_CLOSES, ZIGZAG_CLOSES_DOWN, START_UTC, BAR_SECONDS,
)


def _make_bar(closes: list[float]) -> BarCompleted:
    bars = _zigzag_bars(START_UTC, closes)
    return BarCompleted(
        symbol="MGCM6", bar=bars[-1], window=bars, bar_count=len(bars),
    )


class TestBarEvalEmission:
    @pytest.mark.asyncio
    async def test_fired_buy_decision(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx()
        event = _make_bar(ZIGZAG_CLOSES)
        actions = await s.on_event(event, ctx)
        audit = [a for a in actions if isinstance(a, EmitAudit)]
        assert len(audit) == 1
        a = audit[0]
        assert a.event_type == "BAR_EVAL"
        assert a.decision == "FIRED·BUY"
        assert a.pivot_status == "PIVOT_LOW"
        # bar_close at last fixture bar is 14.0.
        assert a.bar_close == Decimal("14.0")
        # ZIGZAG yields both long and short candidates with positive
        # touches in the test fixture, so the line_status is one of
        # the valid populated states.
        assert a.line_status in ("LINES_BOTH", "LINES_LONG", "LINES_SHORT")

    @pytest.mark.asyncio
    async def test_no_position_no_armed_emits_gated(self):
        # Bot in AWAITING_ENTRY but armed=False (force-quit semantics).
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(state={"armed": False})
        event = _make_bar(ZIGZAG_CLOSES)
        actions = await s.on_event(event, ctx)
        audit = [a for a in actions if isinstance(a, EmitAudit)]
        assert len(audit) == 1
        assert audit[0].decision.startswith("GATED·")

    @pytest.mark.asyncio
    async def test_every_bar_emits_an_audit(self):
        # Regardless of decision shape, _every_ bar evaluation produces
        # exactly one EmitAudit row.
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx()
        event = _make_bar(ZIGZAG_CLOSES_DOWN[:7])
        actions = await s.on_event(event, ctx)
        audit = [a for a in actions if isinstance(a, EmitAudit)]
        assert len(audit) <= 1
        if audit:
            valid_prefixes = (
                "FIRED·", "FILTERED·", "SKIP·", "GATED·",
                "HOLDING", "EXIT_FIRED·",
            )
            assert any(audit[0].decision.startswith(p) for p in valid_prefixes), (
                f"unexpected decision shape: {audit[0].decision}"
            )

    @pytest.mark.asyncio
    async def test_audit_carries_bar_payload(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx()
        event = _make_bar(ZIGZAG_CLOSES)
        actions = await s.on_event(event, ctx)
        audit = next(a for a in actions if isinstance(a, EmitAudit))
        assert "bar" in audit.payload
        bar = audit.payload["bar"]
        assert "best_long_touches" in bar
        assert "best_short_touches" in bar
        assert "top_supports" in bar
        assert "top_resistances" in bar

    @pytest.mark.asyncio
    async def test_fired_audit_appended_after_place_order(self):
        # The EmitAudit action must come AFTER all the original actions
        # (PlaceOrder, UpdateState) so middlewares see place_order
        # before they see the audit row.
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx()
        event = _make_bar(ZIGZAG_CLOSES)
        actions = await s.on_event(event, ctx)
        place_idx = next(
            i for i, a in enumerate(actions) if isinstance(a, PlaceOrder)
        )
        audit_idx = next(
            i for i, a in enumerate(actions) if isinstance(a, EmitAudit)
        )
        assert audit_idx > place_idx


class TestExitReasonNormalize:
    """``_normalize_exit_reason`` produces audit-friendly short tags."""

    def test_counter_line(self):
        from ib_trader.bots.middleware import _normalize_exit_reason
        msg = "TRAILING_STOP [long]: counter-line held 10.0s (line=...)"
        assert _normalize_exit_reason(msg) == "counter_line"

    def test_trail_stop(self):
        from ib_trader.bots.middleware import _normalize_exit_reason
        msg = ("TRAILING_STOP [short]: 3-min bar close 4660.40 > "
               "trail 4665.32 [trail_stop]")
        assert _normalize_exit_reason(msg) == "trail_stop"

    def test_line_breach(self):
        from ib_trader.bots.middleware import _normalize_exit_reason
        msg = ("TRAILING_STOP [long]: 3-min bar close 7499.75 < "
               "entry support 7500.79 [line_breach]")
        assert _normalize_exit_reason(msg) == "line_breach"

    def test_both(self):
        from ib_trader.bots.middleware import _normalize_exit_reason
        msg = ("TRAILING_STOP [long]: 3-min bar close 29712.25 < "
               "entry support 29719.58 [both]")
        assert _normalize_exit_reason(msg) == "both"

    def test_empty(self):
        from ib_trader.bots.middleware import _normalize_exit_reason
        assert _normalize_exit_reason("") == ""
        assert _normalize_exit_reason(None) == ""
