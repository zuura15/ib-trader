"""Regression coverage for the in-flight-order state gate.

Historical context: the April 19 "runaway" happened when a pipeline's
HTTP round-trip gave queued quote ticks a chance to re-evaluate the
strategy and each fire its own SELL. The original fix was a
``_order_submit_in_flight`` stoic-mode flag that blocked quote /
bar / position handlers while an order was in flight.

Post-ADR 016 (FSM collapse), stoic mode is gone. The bot's lifecycle
state itself is the gate: ``on_place_entry_order`` /
``on_place_exit_order`` flip the state synchronously to
``ENTRY_ORDER_PLACED`` / ``EXIT_ORDER_PLACED`` **before** the pipeline
runs. While the bot is in one of those states every stream handler
returns early (see ``run_event_loop`` guards), so no duplicate order
can be emitted.

These tests pin the state-gate's lifecycle so future refactors can't
silently regress.
"""
from decimal import Decimal

import pytest

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.runtime import StrategyBotRunner
from ib_trader.bots.strategy import PlaceOrder


class _InMemoryRedis:
    """Stub just big enough for ``_load_doc`` / ``_save_doc`` to
    round-trip a single bot's doc through Redis. StateStore reads
    ``.get`` / ``.set`` on this object via its keys."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    async def get(self, key: str):
        return self._store.get(key)

    async def set(self, key: str, value: str, **kwargs):
        self._store[key] = value

    async def setex(self, key: str, ttl: int, value: str):
        self._store[key] = value

    async def delete(self, *keys: str):
        for k in keys:
            self._store.pop(k, None)

    async def xadd(self, *args, **kwargs):
        # Activity nudges are best-effort; ignore in tests.
        return None


class _FakePipeline:
    """Records the runner's state when ``process()`` is invoked."""

    def __init__(self, observed_state: list):
        self._observed = observed_state
        self.last_cmd_id = None

    async def process(self, actions, ctx):
        self._observed.append(await self._runner.current_state())
        return actions


class _FailingPipeline(_FakePipeline):
    async def process(self, actions, ctx):
        self._observed.append(await self._runner.current_state())
        raise RuntimeError("simulated pipeline failure")


class _CmdIdPipeline:
    """Mimics ExecutionMiddleware producing a ``last_cmd_id`` after a
    successful submission."""

    def __init__(self, cmd_id: str, observed: list):
        self._cmd_id = cmd_id
        self._observed = observed
        self.last_cmd_id = None

    async def process(self, actions, ctx):
        self._observed.append(True)
        self.last_cmd_id = self._cmd_id
        return actions


def _make_runner(initial_state: BotState = BotState.AWAITING_EXIT_TRIGGER) -> StrategyBotRunner:
    """Build a minimal runner shell with an in-memory Redis and the
    starting lifecycle state pre-seeded."""
    import asyncio

    runner = StrategyBotRunner.__new__(StrategyBotRunner)
    runner.bot_id = "test-bot"
    runner.ctx = None
    runner._state_lock = asyncio.Lock()
    runner._PENDING_ORDER_ID = "__pending__"
    runner._recent_submit_times = []
    runner._submitted_logged = set()
    # Stoic-mode fields stay on the instance for now (still consumed
    # by old code paths we haven't ripped out yet) but are never
    # flipped by the new state-gate flow.
    runner._pending_cmd_id = None
    redis = _InMemoryRedis()
    runner.config = {"_redis": redis}
    runner.strategy_config = {"symbol": "QQQ"}

    # Seed the doc so ``on_place_*_order`` can transition from a valid
    # starting state.
    import json
    key = f"bot:{runner.bot_id}"
    redis._store[key] = json.dumps({"state": initial_state.value,
                                     "symbol": "QQQ", "qty": "1"})
    return runner


def _sell_order() -> PlaceOrder:
    return PlaceOrder(
        symbol="QQQ", side="SELL", qty=Decimal("1"),
        order_type="market", origin="exit",
    )


def _buy_order() -> PlaceOrder:
    return PlaceOrder(
        symbol="QQQ", side="BUY", qty=Decimal("1"),
        order_type="market", origin="strategy",
    )


@pytest.mark.asyncio
async def test_state_is_order_placed_during_pipeline_process():
    """While pipeline.process runs, the bot's state must be in
    EXIT_ORDER_PLACED / ENTRY_ORDER_PLACED. That's the gate that
    prevents concurrent quote ticks from emitting duplicate orders.
    """
    runner = _make_runner(initial_state=BotState.AWAITING_EXIT_TRIGGER)
    observed: list[BotState] = []
    pipeline = _FakePipeline(observed)
    pipeline._runner = runner
    runner.pipeline = pipeline

    await runner._run_pipeline([_sell_order()], ctx=object())
    assert observed == [BotState.EXIT_ORDER_PLACED]


@pytest.mark.asyncio
async def test_state_unchanged_when_no_place_orders():
    """Non-order actions (log events, state updates) must not flip
    lifecycle state. The strategy can re-run on the next tick."""
    runner = _make_runner(initial_state=BotState.AWAITING_EXIT_TRIGGER)
    observed: list[BotState] = []
    pipeline = _FakePipeline(observed)
    pipeline._runner = runner
    runner.pipeline = pipeline

    await runner._run_pipeline([], ctx=object())
    assert observed == [BotState.AWAITING_EXIT_TRIGGER]
    assert await runner.current_state() == BotState.AWAITING_EXIT_TRIGGER


@pytest.mark.asyncio
async def test_state_reverts_on_pipeline_exception():
    """If the pipeline raises, the state-gate revert must bring the
    bot back to the pre-order state — otherwise the bot is stuck in
    EXIT_ORDER_PLACED forever and won't accept new ticks."""
    runner = _make_runner(initial_state=BotState.AWAITING_EXIT_TRIGGER)
    observed: list[BotState] = []
    pipeline = _FailingPipeline(observed)
    pipeline._runner = runner
    runner.pipeline = pipeline

    with pytest.raises(RuntimeError):
        await runner._run_pipeline([_sell_order()], ctx=object())
    assert observed == [BotState.EXIT_ORDER_PLACED]
    assert await runner.current_state() == BotState.AWAITING_EXIT_TRIGGER


@pytest.mark.asyncio
async def test_state_reverts_when_pipeline_yields_no_cmd_id():
    """Pipeline succeeded but dropped the order (RiskMiddleware reject,
    etc.). State must revert so the bot isn't stuck in ORDER_PLACED."""
    runner = _make_runner(initial_state=BotState.AWAITING_EXIT_TRIGGER)
    pipeline = _FakePipeline(observed_state=[])
    pipeline._runner = runner
    runner.pipeline = pipeline

    await runner._run_pipeline([_sell_order()], ctx=object())
    # Pipeline never set last_cmd_id — revert happened.
    assert await runner.current_state() == BotState.AWAITING_EXIT_TRIGGER


@pytest.mark.asyncio
async def test_ib_order_id_captured_on_successful_submission():
    """When the pipeline returns with a cmd_id, the bot records it on
    the doc so the stream handler can match terminal events."""
    runner = _make_runner(initial_state=BotState.AWAITING_EXIT_TRIGGER)
    pipeline = _CmdIdPipeline("ib-9999", observed=[])
    pipeline._runner = runner
    runner.pipeline = pipeline

    await runner._run_pipeline([_sell_order()], ctx=object())
    assert await runner.current_state() == BotState.EXIT_ORDER_PLACED
    doc = await runner._load_doc()
    assert doc["ib_order_id"] == "ib-9999"
    assert doc["awaiting_ib_order_id"] == "ib-9999"


@pytest.mark.asyncio
async def test_buy_order_from_awaiting_entry_transitions_correctly():
    """BUY entry flips the state to ENTRY_ORDER_PLACED (not
    EXIT_ORDER_PLACED). The state machine dispatches on state, not
    side, but the on_place_entry_order method is the right dispatch
    for AWAITING_ENTRY_TRIGGER + BUY."""
    runner = _make_runner(initial_state=BotState.AWAITING_ENTRY_TRIGGER)
    observed: list[BotState] = []
    pipeline = _FakePipeline(observed)
    pipeline._runner = runner
    runner.pipeline = pipeline

    await runner._run_pipeline([_buy_order()], ctx=object())
    assert observed == [BotState.ENTRY_ORDER_PLACED]
