"""Startup policy tests — bots always force-OFF on app startup.

Apr 19 incident: the supervisor auto-restarted bots whose FSM said they
were holding a position, and that stale state fed into the runaway.
New rule:

  - Every bot starts OFF. User re-enables manually.
  - AWAITING_ENTRY_TRIGGER → silent force OFF, no alert.
  - ENTRY_ORDER_PLACED / AWAITING_EXIT_TRIGGER / EXIT_ORDER_PLACED /
    ERRORED → force OFF + CATASTROPHIC alert surfaced to the UI.
  - OFF → no-op.

These tests exercise the two supervisor helpers added in
`ib_trader/bots/runner.py` (`_panic_alert_on_startup` + the startup
force-OFF loop logic) without starting the full runner task.
"""
from __future__ import annotations

import json

import pytest

from ib_trader.bots.lifecycle import BotState, force_off_state
from ib_trader.bots.runner import _panic_alert_on_startup


class _FakeRedis:
    """In-memory Redis stand-in. Captures hset writes to alerts:active
    and xadd calls to the activity stream so the test can verify the
    alert was published."""

    def __init__(self):
        self._kv: dict[str, str] = {}
        self._hashes: dict[str, dict] = {}
        self.xadd_calls: list[tuple] = []

    async def set(self, key, value):
        self._kv[key] = value

    async def setex(self, key, ttl, value):
        self._kv[key] = value

    async def get(self, key):
        return self._kv.get(key)

    async def delete(self, key):
        self._kv.pop(key, None)

    async def hset(self, key, field, value):
        self._hashes.setdefault(key, {})[field] = value

    async def hgetall(self, key):
        return self._hashes.get(key, {})

    async def xadd(self, *args, **kwargs):
        self.xadd_calls.append((args, kwargs))
        return "0-0"


class _FakeDefn:
    """BotDefinition-shaped stub."""

    def __init__(self, bot_id, name="Test Bot", symbol="QQQ"):
        self.id = bot_id
        self.name = name
        self.symbol = symbol
        self.config = {"symbol": symbol}


async def _prime(redis, bot_id: str, state: BotState, extra: dict | None = None):
    doc = {"state": state.value}
    if extra:
        doc.update(extra)
    await redis.set(f"bot:{bot_id}", json.dumps(doc))


@pytest.fixture
def redis():
    return _FakeRedis()


@pytest.mark.parametrize("state", [
    BotState.AWAITING_EXIT_TRIGGER,
    BotState.EXIT_ORDER_PLACED,
    BotState.ENTRY_ORDER_PLACED,
    BotState.ERRORED,
])
@pytest.mark.asyncio
async def test_panic_alert_published_for_active_states(redis, state):
    """Each of the four panic-worthy states must produce a CATASTROPHIC
    alert written to alerts:active + an activity-stream nudge."""
    defn = _FakeDefn("bot-abc")
    await _prime(redis, defn.id, state, extra={
        "qty": "3",
        "entry_price": "100.00",
        "ib_order_id": "ib-1234",
        "symbol": "QQQ",
    })

    await _panic_alert_on_startup(redis, defn, state)

    alerts = redis._hashes.get("alerts:active", {})
    assert len(alerts) == 1
    payload = json.loads(next(iter(alerts.values())))
    assert payload["severity"] == "CATASTROPHIC"
    assert payload["trigger"] == "BOT_ACTIVE_STATE_AT_STARTUP"
    assert payload["pager"] is True
    assert payload["bot_id"] == "bot-abc"
    assert payload["symbol"] == "QQQ"
    assert payload["prior_state"] == state.value
    assert payload["qty"] == "3"
    assert payload["entry_price"] == "100.00"
    assert payload["ib_order_id"] == "ib-1234"

    # publish_activity("alerts") fires an xadd on the activity stream.
    assert len(redis.xadd_calls) >= 1


@pytest.mark.asyncio
async def test_panic_alert_no_redis_logs_but_doesnt_crash():
    """Missing Redis must not crash the supervisor startup — the bot
    still force-stops locally, and the missing alert is itself logged."""
    defn = _FakeDefn("bot-abc")
    await _panic_alert_on_startup(None, defn, BotState.AWAITING_EXIT_TRIGGER)
    # No assertion needed — completion without exception is the pass.


@pytest.mark.asyncio
async def test_force_off_state_resets_all_active_states(redis):
    """``force_off_state`` must write OFF + clear position fields
    regardless of the prior state. This is the helper the startup
    panic path and the /reset endpoint both call."""
    from ib_trader.redis.state import StateStore
    store = StateStore(redis)
    for state in (BotState.AWAITING_ENTRY_TRIGGER,
                  BotState.ENTRY_ORDER_PLACED,
                  BotState.AWAITING_EXIT_TRIGGER,
                  BotState.EXIT_ORDER_PLACED,
                  BotState.ERRORED):
        key = f"bot:bot-{state.value}"
        await store.set(key, {
            "state": state.value,
            "qty": "5",
            "entry_price": "99.99",
        })
        await force_off_state(f"bot-{state.value}", redis, reason="test")
        doc = await store.get(key)
        assert doc["state"] == BotState.OFF.value
        # Position-anchor fields are cleared.
        assert doc.get("qty") in (None, "0")
        assert doc.get("entry_price") is None
