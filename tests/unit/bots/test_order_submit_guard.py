"""Regression coverage for the in-flight-order guard.

Apr 19 runaway: SMART_MARKET's ~600 ms HTTP round-trip let 17 queued
quote ticks each fire their own SELL before the FSM transitioned out
of AWAITING_EXIT_TRIGGER. The fix is a runtime flag set synchronously
at the top of ``_run_pipeline`` and released in ``finally``; quote /
bar dispatch handlers bail if the flag is held.

These tests pin the flag's lifecycle so the fix can't silently
regress.
"""
import pytest

from ib_trader.bots.runtime import StrategyBotRunner
from ib_trader.bots.strategy import PlaceOrder
from decimal import Decimal


class _FakePipeline:
    """Records whether the flag was set when process() was invoked."""

    def __init__(self, observed_flag: list):
        self._observed = observed_flag
        self.last_cmd_id = None  # real MiddlewarePipeline slot

    async def process(self, actions, ctx):
        # Capture runner._order_submit_in_flight *during* the call.
        self._observed.append(self._runner._order_submit_in_flight)
        return actions


class _FailingPipeline(_FakePipeline):
    async def process(self, actions, ctx):
        self._observed.append(self._runner._order_submit_in_flight)
        raise RuntimeError("simulated pipeline failure")


def _make_runner() -> StrategyBotRunner:
    """Build a minimal StrategyBotRunner shell — enough state for
    ``_run_pipeline`` to execute without touching Redis / SQLite."""
    runner = StrategyBotRunner.__new__(StrategyBotRunner)
    runner._order_submit_in_flight = False
    runner._pending_cmd_id = None
    runner._awaiting_terminal_ib_order_id = None
    runner._stoic_mode_set_at = 0.0
    runner._recent_submit_times = []
    runner.bot_id = "test-bot"
    runner.ctx = None
    runner.config = {"_redis": None}
    runner.strategy_config = {"symbol": "TEST"}
    return runner


def _make_place_order() -> PlaceOrder:
    return PlaceOrder(
        symbol="QQQ", side="SELL", qty=Decimal("1"),
        order_type="market", origin="exit",
    )


@pytest.mark.asyncio
async def test_flag_set_during_pipeline_process():
    """When PlaceOrder actions flow through, the guard is True during
    pipeline.process and False again after _run_pipeline returns."""
    runner = _make_runner()
    observed: list[bool] = []
    pipeline = _FakePipeline(observed)
    pipeline._runner = runner
    runner.pipeline = pipeline

    assert runner._order_submit_in_flight is False
    await runner._run_pipeline([_make_place_order()], ctx=object())
    assert observed == [True]
    assert runner._order_submit_in_flight is False


@pytest.mark.asyncio
async def test_flag_not_set_when_no_place_orders():
    """Non-order actions (log events, state updates) must not hold the
    guard — that would unnecessarily block quote-driven exits."""
    runner = _make_runner()
    observed: list[bool] = []
    pipeline = _FakePipeline(observed)
    pipeline._runner = runner
    runner.pipeline = pipeline

    # No PlaceOrder in the list
    await runner._run_pipeline([], ctx=object())
    assert observed == [False]
    assert runner._order_submit_in_flight is False


@pytest.mark.asyncio
async def test_flag_cleared_on_pipeline_exception():
    """If the pipeline raises, the guard must still release — otherwise
    the bot is stuck ignoring quotes forever."""
    runner = _make_runner()
    observed: list[bool] = []
    pipeline = _FailingPipeline(observed)
    pipeline._runner = runner
    runner.pipeline = pipeline

    with pytest.raises(RuntimeError):
        await runner._run_pipeline([_make_place_order()], ctx=object())
    assert observed == [True]
    assert runner._order_submit_in_flight is False
    assert runner._awaiting_terminal_ib_order_id is None


class _CmdIdPipeline:
    """Records an ib_order_id on last_cmd_id after processing — mimics
    the real ExecutionMiddleware after a successful /engine/orders
    submission."""

    def __init__(self, cmd_id: str, observed: list):
        self._cmd_id = cmd_id
        self._observed = observed
        self.last_cmd_id = None

    async def process(self, actions, ctx):
        self._observed.append(True)
        self.last_cmd_id = self._cmd_id
        return actions


@pytest.mark.asyncio
async def test_flag_remains_set_after_pipeline_when_order_submitted():
    """With an ib_order_id captured, the guard stays set until the
    order-stream handler consumes the terminal event."""
    runner = _make_runner()
    runner._dispatch_place_order_fsm = _async_noop  # type: ignore[method-assign]
    pipeline = _CmdIdPipeline("ib-9999", observed=[])
    pipeline._runner = runner
    runner.pipeline = pipeline

    await runner._run_pipeline([_make_place_order()], ctx=object())
    assert runner._order_submit_in_flight is True
    assert runner._awaiting_terminal_ib_order_id == "ib-9999"


async def _async_noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_stoic_mode_timeout_releases_and_alerts(monkeypatch):
    """If the terminal event never arrives, _check_stoic_mode_timeout
    must eventually release the flag and surface a WARNING alert."""
    runner = _make_runner()
    # Pretend the flag was set well beyond the configured timeout.
    import time as _time
    runner._order_submit_in_flight = True
    runner._awaiting_terminal_ib_order_id = "ib-stuck"
    runner._stoic_mode_set_at = _time.monotonic() - 999.0

    # Minimum config so the helper can read stoic_mode_max_seconds.
    runner.config = {"stoic_mode_max_seconds": 1}
    runner.strategy_config = {"symbol": "QQQ"}

    alerted: list[dict] = []

    async def fake_pager(args):
        alerted.append(args)

    runner._handle_pager_alert = fake_pager  # type: ignore[method-assign]
    await runner._check_stoic_mode_timeout()

    assert runner._order_submit_in_flight is False
    assert runner._awaiting_terminal_ib_order_id is None
    assert len(alerted) == 1
    assert alerted[0]["trigger"] == "BOT_STOIC_MODE_TIMEOUT"
    assert alerted[0]["severity"] == "WARNING"
