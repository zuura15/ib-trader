"""Force-quit safety: bar-close race + EXIT_ORDER_PLACED branch.

P0-8: a stale ``cur = await self.current_state()`` outside the lock
plus a stale ``pre_state`` in ``_run_pipeline`` plus the
``new_state == pre_state and pre_state != AWAITING_EXIT_TRIGGER`` guard
meant a force-quit racing a bar-close exit could fire a SECOND exit
order. The fix: ``on_place_exit_order`` returns ``None`` on FSM
rejection so the pipeline aborts cleanly regardless of pre-lock state.

P0-9: force-quit was a no-op in EXIT_ORDER_PLACED. The fix:
cancel-and-replace with a fresh mid exit.
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
    runner.strategy_config = {"symbol": "MGCM6"}
    runner.ctx = StrategyContext(
        state=dict(store._doc),
        fsm_state=fsm_state,
        bot_id="test-bot",
        config={"symbol": "MGCM6"},
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
    return runner


@pytest.mark.asyncio
async def test_on_place_exit_order_returns_none_on_fsm_rejection():
    """If state is anything other than AWAITING_EXIT_TRIGGER inside the
    lock, on_place_exit_order returns None (not the unchanged state).
    Caller uses None to detect rejection without depending on a stale
    pre_state comparison."""
    store = _FakeStore({
        "state": BotState.EXIT_ORDER_PLACED.value,
        "symbol": "MGCM6",
    })
    runner = _make_runner(store, BotState.EXIT_ORDER_PLACED)

    result = await runner.on_place_exit_order(
        symbol="MGCM6", qty=1, order_type="mid",
    )
    assert result is None
    # State must not have changed.
    assert store._doc["state"] == BotState.EXIT_ORDER_PLACED.value


@pytest.mark.asyncio
async def test_on_place_entry_order_returns_none_on_fsm_rejection():
    """Same contract for the entry side."""
    store = _FakeStore({
        "state": BotState.ENTRY_ORDER_PLACED.value,
        "symbol": "MGCM6",
    })
    runner = _make_runner(store, BotState.ENTRY_ORDER_PLACED)

    result = await runner.on_place_entry_order(
        symbol="MGCM6", qty=1, order_type="mid", ib_order_id="x",
    )
    assert result is None


@pytest.mark.asyncio
async def test_force_quit_in_exit_order_placed_cancels_and_replaces():
    """force_quit in EXIT_ORDER_PLACED must cancel the in-flight exit
    AND submit a fresh mid — not silently no-op."""
    store = _FakeStore({
        "state": BotState.EXIT_ORDER_PLACED.value,
        "symbol": "MGCM6",
        "qty": "1",
        "entry_price": "2400.00",
        "position_direction": "LONG",
        "order_qty": "1",
        "filled_qty": "0",
    })
    runner = _make_runner(store, BotState.EXIT_ORDER_PLACED)

    cancels: list[dict] = []
    force_sells: list[str] = []

    async def _refresh_state():
        runner.ctx.state = await store.get("bot:test-bot") or {}

    async def _write_state(fields):
        doc = await store.get("bot:test-bot") or {}
        merged = {**doc, **fields}
        await store.set("bot:test-bot", merged)

    async def _handle_cancel_order(args):
        cancels.append(args)

    async def _execute_force_sell(symbol):
        force_sells.append(symbol)

    runner._refresh_state = _refresh_state  # type: ignore[method-assign]
    runner._write_state = _write_state  # type: ignore[method-assign]
    runner._handle_cancel_order = _handle_cancel_order  # type: ignore[method-assign]
    runner._execute_force_sell = _execute_force_sell  # type: ignore[method-assign]

    # Need a minimal "strategy" object so force_quit's guard passes;
    # the actual build_exit_actions path is stubbed via _execute_force_sell.
    runner.strategy = object()

    result = await runner.force_quit()

    assert result["exited"] is True
    assert result["cancelled"] is True
    assert result["fsm_state"] == BotState.AWAITING_EXIT_TRIGGER.value
    assert cancels == [{"symbol": "MGCM6"}]
    assert force_sells == ["MGCM6"]
    # The synchronous revert must have happened so the follow-up exit
    # could proceed.
    assert store._doc["state"] == BotState.AWAITING_EXIT_TRIGGER.value
