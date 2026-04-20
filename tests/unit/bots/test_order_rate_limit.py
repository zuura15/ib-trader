"""Tests for the bot order-rate circuit breaker.

Final safety net regardless of upstream cause: if a bot emits more
than ``bot_order_rate_limit_count`` orders inside
``bot_order_rate_limit_window_seconds``, force-STOP to OFF + raise
CATASTROPHIC. These tests pin that invariant.
"""
from __future__ import annotations

import time

import pytest

from ib_trader.bots.runtime import StrategyBotRunner
from ib_trader.bots.strategy import PlaceOrder
from decimal import Decimal


def _make_runner(**config) -> StrategyBotRunner:
    """Minimal runner shell — enough for _check_order_rate_limit."""
    runner = StrategyBotRunner.__new__(StrategyBotRunner)
    runner._order_submit_in_flight = False
    runner._pending_cmd_id = None
    runner._awaiting_terminal_ib_order_id = None
    runner._stoic_mode_set_at = 0.0
    runner._recent_submit_times = []
    runner.bot_id = "test-bot"
    runner.strategy_config = {"symbol": "QQQ"}
    runner.ctx = None
    runner.config = {"_redis": None}
    runner.config.update(config)
    return runner


@pytest.mark.asyncio
async def test_within_limit_is_silent():
    """Fewer than limit submissions must pass without tripping."""
    runner = _make_runner(
        bot_order_rate_limit_count=5,
        bot_order_rate_limit_window_seconds=2.0,
    )
    # Four submissions in 100 ms — under the limit of 5.
    for _ in range(4):
        runner._recent_submit_times.append(time.monotonic())
    # The next check is for the 5th — still within limit (5 <= 5 means
    # the 5th would trip; but the check computes len+1 >= limit, so
    # 4+1=5 IS the trip threshold). Use 3 submissions to stay safe.
    runner._recent_submit_times = runner._recent_submit_times[:3]
    await runner._check_order_rate_limit()
    # Should return without raising.


@pytest.mark.asyncio
async def test_trips_and_raises_on_runaway():
    """Crossing the limit must raise RuntimeError so the caller aborts
    the in-progress submission."""
    runner = _make_runner(
        bot_order_rate_limit_count=3,
        bot_order_rate_limit_window_seconds=2.0,
    )
    # Pre-load 2 recent submissions inside the window — the next
    # submission (whose check this is) would make 3 total, hitting
    # the limit.
    now = time.monotonic()
    runner._recent_submit_times = [now - 0.1, now - 0.05]

    # Stub fsm dispatch + alert publish so the test runs without
    # Redis. We only care that the rate-limit logic raises and clears
    # the ring.
    with pytest.raises(RuntimeError, match="rate-limit exceeded"):
        await runner._check_order_rate_limit()
    assert runner._recent_submit_times == []


@pytest.mark.asyncio
async def test_old_timestamps_age_out():
    """Submissions older than the window must not count toward the limit."""
    runner = _make_runner(
        bot_order_rate_limit_count=3,
        bot_order_rate_limit_window_seconds=1.0,
    )
    now = time.monotonic()
    # Two "old" submissions outside the window — should be discarded.
    runner._recent_submit_times = [now - 5.0, now - 2.0]
    await runner._check_order_rate_limit()
    # Ring now holds zero entries (both aged out); no raise.
    assert runner._recent_submit_times == []
