"""Verify-by-pull defends against the IB positionEvent / order-fill race.

GH #85 root cause: IB's two push streams (order-fill + positionEvent)
are eventually consistent with each other but not synchronised. After
a multi-venue fill completes, a positionEvent carrying the mid-fill
snapshot can arrive ms after the order stream's terminal event. The
old ``_apply_position_event`` blindly trusted that stale push and
silently rewrote ``state["qty"]`` downward, logging a phantom
``MANUAL_CLOSE`` and corrupting the bot's view of its own position.

The fix: when a positionEvent suggests a reduction below tracked qty,
issue a fresh ``reqPositionsAsync()`` (the *pull*) and use the
response as the tiebreaker. The pull goes against IB's authoritative
position book, so it cannot be in the racy mid-fill window.

These tests pin that contract: stale push → state stays correct;
real manual close → state reconciled; pull failure → state preserved
(fail-closed); pull says position is *higher* than tracked → bot
parks ERRORED rather than guessing.
"""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from ib_trader.bots.runtime import StrategyBotRunner
from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategy import StrategyContext


class _FakeStore:
    """In-memory bot:<id> doc with a write log for assertions."""

    def __init__(self, seed: dict | None = None):
        self._doc = dict(seed) if seed else {}
        self.writes: list[dict] = []

    async def get(self, key):
        return dict(self._doc) if self._doc else None

    async def set(self, key, value):
        self._doc = dict(value)
        self.writes.append(dict(value))


def _make_runner(store: _FakeStore, monkeypatch, *,
                 seed_state: BotState = BotState.AWAITING_EXIT_TRIGGER
                 ) -> StrategyBotRunner:
    """Build a minimally-wired StrategyBotRunner backed by ``store``.

    Patches ``ib_trader.redis.state.StateStore`` via the supplied
    ``monkeypatch`` fixture so the patch is unwound after the test —
    avoids polluting other tests that depend on the real StateStore.
    """
    runner = StrategyBotRunner.__new__(StrategyBotRunner)
    runner.bot_id = "test-bot"
    runner.strategy_config = {"symbol": "META"}
    runner.ctx = StrategyContext(
        state={"qty": "140", "symbol": "META"},
        fsm_state=seed_state,
        bot_id="test-bot",
        config={"symbol": "META"},
    )
    runner.config = {"_redis": None, "_engine_url": "http://test"}
    runner._position_event_lock = asyncio.Lock()
    # Seed the doc with state/qty so _apply_position_event sees a
    # tracked position.
    store._doc.setdefault("state", seed_state.value)
    store._doc.setdefault("qty", "140")
    store._doc.setdefault("symbol", "META")

    class _StubStateStore:
        def __init__(self, _redis):
            pass

        async def get(self, key):
            return await store.get(key)

        async def set(self, key, value):
            await store.set(key, value)

    # Use monkeypatch so the stub is removed after the test.
    monkeypatch.setattr(
        "ib_trader.redis.state.StateStore", _StubStateStore,
    )

    # Stub out the audit log + child handlers; tests inspect runner.* attrs.
    runner.log_event = MagicMock()  # type: ignore[method-assign]  # sync method
    runner.on_manual_close = AsyncMock()  # type: ignore[method-assign]
    runner.on_ib_position_mismatch = AsyncMock()  # type: ignore[method-assign]
    return runner


@pytest.mark.asyncio
async def test_terminal_fill_then_stale_position_event_does_not_corrupt_qty(caplog, monkeypatch):
    """Order stream wrote qty=140 (terminal fill). 5 ms later a stale
    positionEvent claims qty=100. Pull confirms qty=140. State must
    stay 140; phantom MANUAL_CLOSE must NOT fire.
    """
    store = _FakeStore()
    runner = _make_runner(store, monkeypatch)
    runner._verify_position_via_pull = AsyncMock(return_value=Decimal("140"))  # type: ignore[method-assign]

    with caplog.at_level(logging.INFO):
        await runner._apply_position_event(
            bot_ref="test-bot",
            symbol="META",
            ib_qty=Decimal("100"),  # the racing stale push
            ib_avg_price=Decimal("679.71"),
        )

    assert store._doc.get("qty") == "140", "state qty must not be reduced"
    assert any("BOT_POSITION_PUSH_STALE_DETECTED" in r.getMessage() for r in caplog.records), \
        "must log the stale-push detection event"
    runner.log_event.assert_not_called()  # no MANUAL_CLOSE audit row
    runner.on_manual_close.assert_not_called()
    runner._verify_position_via_pull.assert_awaited_once_with("META")


@pytest.mark.asyncio
async def test_real_manual_close_after_fill_reconciles(monkeypatch):
    """Push qty=100, pull qty=100 — pull confirms a real manual
    reduction. State reconciles to 100; MANUAL_CLOSE audit row written.
    """
    store = _FakeStore()
    runner = _make_runner(store, monkeypatch)
    runner._verify_position_via_pull = AsyncMock(return_value=Decimal("100"))  # type: ignore[method-assign]

    await runner._apply_position_event(
        bot_ref="test-bot",
        symbol="META",
        ib_qty=Decimal("100"),
        ib_avg_price=Decimal("679.71"),
    )

    assert store._doc.get("qty") == "100", "state must reconcile to verified qty"
    runner.log_event.assert_called_once()
    args, kwargs = runner.log_event.call_args
    assert args[0] == "MANUAL_CLOSE"
    payload = kwargs["payload"]
    assert payload["expected_qty"] == "140"
    assert payload["actual_qty"] == "100"
    assert payload["reduction"] == "40"
    assert payload["full_close"] is False


@pytest.mark.asyncio
async def test_pull_unavailable_fails_closed(caplog, monkeypatch):
    """If the pull fails (engine unreachable), do NOT reconcile. State
    stays put; the next positionEvent will retry."""
    store = _FakeStore()
    runner = _make_runner(store, monkeypatch)
    runner._verify_position_via_pull = AsyncMock(return_value=None)  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING):
        await runner._apply_position_event(
            bot_ref="test-bot",
            symbol="META",
            ib_qty=Decimal("100"),
            ib_avg_price=Decimal("679.71"),
        )

    assert store._doc.get("qty") == "140", "fail-closed: state preserved"
    assert any("BOT_POSITION_VERIFY_UNAVAILABLE" in r.getMessage() for r in caplog.records)
    runner.log_event.assert_not_called()
    runner.on_manual_close.assert_not_called()


@pytest.mark.asyncio
async def test_pull_says_position_higher_than_tracked_escalates(monkeypatch):
    """Push qty=100, pull qty=200 — IB shows MORE than the bot
    tracked. Direction can't be a manual close. Park ERRORED via
    on_ib_position_mismatch and emit a CATASTROPHIC alert.
    """
    store = _FakeStore()
    runner = _make_runner(store, monkeypatch)
    runner._verify_position_via_pull = AsyncMock(return_value=Decimal("200"))  # type: ignore[method-assign]

    await runner._apply_position_event(
        bot_ref="test-bot",
        symbol="META",
        ib_qty=Decimal("100"),
        ib_avg_price=Decimal("679.71"),
    )

    runner.on_ib_position_mismatch.assert_awaited_once()
    msg = runner.on_ib_position_mismatch.call_args.kwargs["message"]
    assert "200" in msg and "140" in msg
    runner.log_event.assert_not_called()  # no MANUAL_CLOSE audit


@pytest.mark.asyncio
async def test_actual_geq_expected_short_circuits_no_pull(monkeypatch):
    """When push qty >= tracked qty, no reconciliation is needed and
    no pull is fired (cost: zero IB calls in the happy path)."""
    store = _FakeStore()
    runner = _make_runner(store, monkeypatch)
    runner._verify_position_via_pull = AsyncMock(return_value=Decimal("0"))  # type: ignore[method-assign]

    await runner._apply_position_event(
        bot_ref="test-bot",
        symbol="META",
        ib_qty=Decimal("140"),  # equal to tracked
        ib_avg_price=Decimal("679.71"),
    )

    runner._verify_position_via_pull.assert_not_called()
    assert store._doc.get("qty") == "140"


@pytest.mark.asyncio
async def test_full_close_pull_zero_invokes_on_manual_close(monkeypatch):
    """Pull confirms 0 — operator closed the full position at TWS.
    Reconcile to 0, fire on_manual_close so the FSM transitions back
    to AWAITING_ENTRY_TRIGGER (existing behaviour preserved).
    """
    store = _FakeStore()
    runner = _make_runner(store, monkeypatch)
    runner._verify_position_via_pull = AsyncMock(return_value=Decimal("0"))  # type: ignore[method-assign]

    await runner._apply_position_event(
        bot_ref="test-bot",
        symbol="META",
        ib_qty=Decimal("0"),
        ib_avg_price=Decimal("0"),
    )

    assert store._doc.get("qty") == "0"
    runner.on_manual_close.assert_awaited_once()
    runner.log_event.assert_called_once()
    args, _ = runner.log_event.call_args
    assert args[0] == "MANUAL_CLOSE"
