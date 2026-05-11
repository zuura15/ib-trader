"""Runtime dispatch + leg-aware bookkeeping for bidirectional strategies.

chart_signal opens shorts with SELL and closes them with BUY. The
historical ``BUY → entry / SELL → exit`` routing in ``_dispatch_event``
and the hardcoded ``side="SELL"`` exit-strategy notify in
``on_exit_filled`` silently broke every short. These tests pin the
post-2026-05-11 invariants: route by FSM state, derive direction from
the actual fill side, write ``position_direction`` so exit-leg P&L can
sign correctly, propagate the closing side to ``_handle_retry_exit_order``.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock

import pytest

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.runtime import StrategyBotRunner
from ib_trader.bots.strategy import StrategyContext


class _FakeStore:
    def __init__(self, seed: dict | None = None):
        self._doc = dict(seed) if seed else {}

    async def get(self, key):
        return dict(self._doc) if self._doc else None

    async def set(self, key, value):
        self._doc = dict(value)


def _make_runner(store: _FakeStore, fsm_state: BotState) -> StrategyBotRunner:
    runner = StrategyBotRunner.__new__(StrategyBotRunner)
    runner.bot_id = "test-bot"
    runner.strategy_config = {
        "symbol": "MNQM6",
        "exit": {
            "hard_stop_loss_pct": 0.003,
            "trail_activation_pct": 0.00005,
            "trail_width_pct": 0.0005,
        },
    }
    runner.ctx = StrategyContext(
        state=dict(store._doc),
        fsm_state=fsm_state,
        bot_id="test-bot",
        config={"symbol": "MNQM6"},
    )
    runner.config = {"_redis": None, "_engine_url": "http://test"}
    runner._state_lock = asyncio.Lock()
    runner._stop_requested = False
    runner.strategy = None
    runner.pipeline = None

    async def _load_doc():
        return await store.get("bot:test-bot") or {}

    async def _save_doc(doc):
        await store.set("bot:test-bot", doc)

    runner._load_doc = _load_doc  # type: ignore[method-assign]
    runner._save_doc = _save_doc  # type: ignore[method-assign]
    runner.record_close = AsyncMock()  # type: ignore[method-assign]
    runner._handle_pager_alert = AsyncMock()  # type: ignore[method-assign]
    return runner


@pytest.mark.asyncio
async def test_short_entry_fill_transitions_and_records_direction():
    """Short entry = SELL. on_entry_filled with side='SELL' must:
    (1) transition ENTRY_ORDER_PLACED → AWAITING_EXIT_TRIGGER (not
    rejected like the old long-only path did), (2) write
    position_direction='SHORT' so a later exit knows the side."""
    store = _FakeStore({
        "state": BotState.ENTRY_ORDER_PLACED.value,
        "qty": "0",
        "order_qty": "1",
        "filled_qty": "0",
        "symbol": "MNQM6",
        "serial": 100,
        "ib_order_id": "1500",
    })
    runner = _make_runner(store, BotState.ENTRY_ORDER_PLACED)

    new_state = await runner.on_entry_filled(
        qty=Decimal("1"),
        price=Decimal("17050.00"),
        terminal=True,
        side="SELL",
        commission=Decimal("0.40"),
        serial=100,
    )

    assert new_state == BotState.AWAITING_EXIT_TRIGGER
    assert store._doc["state"] == BotState.AWAITING_EXIT_TRIGGER.value
    assert store._doc["position_direction"] == "SHORT"
    assert store._doc["entry_price"] == "17050.00"
    assert store._doc["qty"] == "1"


@pytest.mark.asyncio
async def test_short_exit_fill_records_short_pnl_and_direction():
    """Short close = BUY (buy-to-cover). on_exit_filled with side='BUY'
    must compute P&L as (entry - exit) * qty (positive when exit price
    is below entry — winning short) and record direction='SHORT' on the
    closed trade."""
    captured: dict[str, dict] = {}

    store = _FakeStore({
        "state": BotState.EXIT_ORDER_PLACED.value,
        "qty": "1",
        "order_qty": "1",
        "entry_price": "17050.00",
        "entry_time": "2026-05-11T18:00:00+00:00",
        "position_direction": "SHORT",
        "symbol": "MNQM6",
        "serial": 101,
        "ib_order_id": "1501",
        "entry_serial": 100,
        "entry_ib_order_id": "1500",
        "exit_retries": 0,
        "trail_reset_count": 0,
    })
    runner = _make_runner(store, BotState.EXIT_ORDER_PLACED)

    async def _handle_record_trade_closed(args):
        captured["close"] = args

    runner._handle_record_trade_closed = _handle_record_trade_closed  # type: ignore[method-assign]

    new_state = await runner.on_exit_filled(
        qty=Decimal("1"),
        price=Decimal("17000.00"),  # exited below entry → +50 per contract
        terminal=True,
        side="BUY",
        commission=Decimal("0.40"),
        serial=101,
    )

    assert new_state == BotState.AWAITING_ENTRY_TRIGGER
    assert store._doc["state"] == BotState.OFF.value
    args = captured["close"]
    assert args["direction"] == "SHORT"
    # (17050 - 17000) * 1 = 50
    assert Decimal(args["realized_pnl"]) == Decimal("50.00")


@pytest.mark.asyncio
async def test_short_partial_exit_retry_side_is_buy():
    """Partial buy-to-cover should retry with side='BUY' so we keep
    covering, not extending the short with another SELL."""
    captured: dict[str, dict] = {}

    store = _FakeStore({
        "state": BotState.EXIT_ORDER_PLACED.value,
        "qty": "2",
        "order_qty": "2",
        "entry_price": "17050.00",
        "position_direction": "SHORT",
        "symbol": "MNQM6",
        "serial": 102,
        "ib_order_id": "1502",
        "exit_retries": 0,
    })
    runner = _make_runner(store, BotState.EXIT_ORDER_PLACED)

    async def _handle_retry_exit_order(args):
        captured["retry"] = args

    runner._handle_retry_exit_order = _handle_retry_exit_order  # type: ignore[method-assign]

    await runner.on_exit_filled(
        qty=Decimal("1"),  # filled 1 of 2
        price=Decimal("17000.00"),
        terminal=True,  # IB closed this order with residual
        side="BUY",
        commission=Decimal("0.40"),
        serial=102,
    )

    assert captured["retry"]["side"] == "BUY"
    assert captured["retry"]["qty"] == "1"


@pytest.mark.asyncio
async def test_long_entry_fill_records_long_direction_default():
    """Legacy long-only callers that don't pass ``side`` should still
    get position_direction='LONG'."""
    store = _FakeStore({
        "state": BotState.ENTRY_ORDER_PLACED.value,
        "qty": "0",
        "order_qty": "1",
        "filled_qty": "0",
        "symbol": "META",
        "serial": 200,
        "ib_order_id": "2500",
    })
    runner = _make_runner(store, BotState.ENTRY_ORDER_PLACED)

    new_state = await runner.on_entry_filled(
        qty=Decimal("1"),
        price=Decimal("679.71"),
        terminal=True,
        # side omitted — defaults to "BUY"
        commission=Decimal("0"),
        serial=200,
    )

    assert new_state == BotState.AWAITING_EXIT_TRIGGER
    assert store._doc["position_direction"] == "LONG"


@pytest.mark.asyncio
async def test_apply_fill_entry_short_flips_stop_direction():
    """Short entry stops belong ABOVE the fill, not below."""
    store = _FakeStore({
        "state": BotState.AWAITING_EXIT_TRIGGER.value,
        "qty": "1",
        "entry_price": "17050.00",
        "symbol": "MNQM6",
        "position_direction": "SHORT",
    })
    runner = StrategyBotRunner.__new__(StrategyBotRunner)
    runner.bot_id = "test-bot"
    runner.strategy_config = {
        "symbol": "MNQM6",
        "exit": {
            "hard_stop_loss_pct": Decimal("0.003"),
            "trail_activation_pct": Decimal("0.001"),
            "trail_width_pct": Decimal("0.0005"),
        },
    }
    runner.ctx = StrategyContext(
        state=dict(store._doc),
        fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        bot_id="test-bot",
        config={"symbol": "MNQM6"},
    )
    runner.config = {"_redis": None}
    runner.strategy = None

    async def _write_state(fields):
        doc = await store.get("bot:test-bot") or {}
        merged = {**doc, **fields}
        await store.set("bot:test-bot", merged)
        runner.ctx.state = merged

    async def _read_state_doc():
        return await store.get("bot:test-bot")

    async def _refresh_state():
        runner.ctx.state = await store.get("bot:test-bot") or {}

    runner._write_state = _write_state  # type: ignore[method-assign]
    runner._read_state_doc = _read_state_doc  # type: ignore[method-assign]
    runner._refresh_state = _refresh_state  # type: ignore[method-assign]

    await runner._apply_fill(
        bot_ref="test-bot",
        symbol="MNQM6",
        leg="entry",
        side="SELL",  # short entry
        qty=Decimal("1"),
        price=Decimal("17050.00"),
        commission=Decimal("0.40"),
        ib_order_id="1500",
    )

    # Short stop ABOVE the entry: 17050 * (1 + 0.003) = 17101.15
    assert Decimal(store._doc["hard_stop"]) > Decimal("17050.00")
    # Trail activation BELOW entry on a short: 17050 * (1 - 0.001) = 17032.95
    assert Decimal(store._doc["trail_activation_price"]) < Decimal("17050.00")
    assert store._doc["position_direction"] == "SHORT"
