"""_apply_fill must not resurrect cleared position fields on full exit.

Apr-19 root cause: on a SELL that fully exits the position, the FSM's
_h_exit_filled cleared entry_price / entry_time via _clear_position_fields.
Then _apply_fill merged engine_fields built from self.ctx.state (a
pre-FSM snapshot) back into the doc, restoring entry_price to its
pre-fill value. Result: state=AWAITING_ENTRY_TRIGGER + entry_price=644.26,
a mismatch that (combined with other events) led to the zero-qty
runaway. Plaster fix: on full exit, only write qty + avg_price.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from ib_trader.bots.runtime import StrategyBotRunner
from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategy import StrategyContext


class _FakeStore:
    """Captures the last set() so the test can inspect what _apply_fill
    actually wrote to Redis."""

    def __init__(self, seed: dict | None = None):
        self._doc = dict(seed) if seed else {}
        self.writes: list[dict] = []

    async def get(self, key):
        return dict(self._doc) if self._doc else None

    async def set(self, key, value):
        self._doc = dict(value)
        self.writes.append(dict(value))


class _FakeStrategy:
    def __init__(self):
        self.events = []

    async def on_event(self, event, ctx):
        self.events.append(event)
        return []


def _make_runner(store: _FakeStore) -> StrategyBotRunner:
    runner = StrategyBotRunner.__new__(StrategyBotRunner)
    runner.bot_id = "test-bot"
    runner.strategy_config = {
        "symbol": "QQQ",
        "exit": {
            "hard_stop_loss_pct": 0.003,
            "trail_activation_pct": 0.00005,
            "trail_width_pct": 0.0005,
        },
    }
    # Pre-FSM snapshot: stale values that must NOT be resurrected.
    runner.ctx = StrategyContext(
        state={
            "qty": "15",
            "entry_price": "644.26",
            "entry_time": "2026-04-19T22:44:00+00:00",
            "symbol": "QQQ",
        },
        fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        bot_id="test-bot",
        config={"symbol": "QQQ"},
    )
    runner.strategy = _FakeStrategy()
    runner.config = {"_redis": None}
    # Monkey-patch Redis accessors to point at our fake store.

    async def _write_state(fields: dict) -> None:
        doc = await store.get("bot:test-bot") or {}
        safe = {k: v for k, v in fields.items()
                if k not in ("state", "position_state")}
        merged = {**doc, **safe}
        await store.set("bot:test-bot", merged)
        runner.ctx.state = merged

    async def _read_state_doc():
        return await store.get("bot:test-bot")

    async def _refresh_state():
        runner.ctx.state = await store.get("bot:test-bot") or {}

    async def _run_pipeline(actions, ctx=None):
        pass  # strategy.on_event is what we care about

    runner._write_state = _write_state  # type: ignore[method-assign]
    runner._read_state_doc = _read_state_doc  # type: ignore[method-assign]
    runner._refresh_state = _refresh_state  # type: ignore[method-assign]
    runner._run_pipeline = _run_pipeline  # type: ignore[method-assign]
    return runner


@pytest.mark.asyncio
async def test_full_exit_does_not_resurrect_entry_price():
    """After FSM clears entry_price/entry_time, _apply_fill on a full
    SELL must NOT write those fields back."""
    # Start with the doc as FSM would have left it after _h_exit_filled
    # on a full exit: state=AWAITING_ENTRY_TRIGGER, qty=0, entry_price=None.
    store = _FakeStore({
        "state": BotState.AWAITING_ENTRY_TRIGGER.value,
        "qty": "0",
        "entry_price": None,
        "entry_time": None,
        "high_water_mark": None,
        "symbol": "QQQ",
    })
    runner = _make_runner(store)

    # Simulate the order-stream terminal fill handler calling _apply_fill
    # for a SELL that fully closed the 15-share position.
    await runner._apply_fill(
        bot_ref="test-bot",
        symbol="QQQ",
        side="S",
        qty=Decimal("15"),
        price=Decimal("644.30"),
        commission=Decimal("1.00"),
        ib_order_id="ib-42",
    )

    final = store._doc
    # qty must be 0 — new_qty = 15 - 15 = 0.
    assert final.get("qty") == "0"
    # entry_price and entry_time must REMAIN cleared (not resurrected).
    assert final.get("entry_price") is None
    assert final.get("entry_time") is None


@pytest.mark.asyncio
async def test_partial_exit_preserves_entry_from_fresh_doc():
    """On a partial SELL (residual > 0), entry fields still matter —
    preserve them by reading the post-FSM doc, not the stale ctx.state."""
    # Doc as FSM's retry path would leave it: state=EXIT_ORDER_PLACED
    # (retry dispatched), qty=5 (15 - 10 filled), entry_price preserved.
    store = _FakeStore({
        "state": BotState.EXIT_ORDER_PLACED.value,
        "qty": "5",
        "entry_price": "644.26",
        "entry_time": "2026-04-19T22:44:00+00:00",
        "symbol": "QQQ",
    })
    runner = _make_runner(store)
    # Mutate ctx.state so the test catches any reliance on it.
    runner.ctx.state = dict(runner.ctx.state)
    runner.ctx.state["entry_price"] = "999.99"  # stale

    await runner._apply_fill(
        bot_ref="test-bot", symbol="QQQ", side="S",
        qty=Decimal("10"),  # this fill — cumulative 10 sold
        price=Decimal("644.30"),
        commission=Decimal("0.50"),
        ib_order_id="ib-43",
    )

    final = store._doc
    # new_qty = existing 15 - 10 = 5 (existing_qty from ctx.state.qty).
    assert final.get("qty") == "5"
    # entry_price came from the fresh doc, NOT the polluted ctx.state.
    assert final.get("entry_price") == "644.26"
    assert final.get("entry_time") == "2026-04-19T22:44:00+00:00"
