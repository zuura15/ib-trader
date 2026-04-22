"""Tests for BotStateStore — Redis-backed bot runtime state.

Covers the invariants the runtime relies on:
  - status round-trip (RUNNING → STOPPED → ERROR).
  - heartbeat TTL'd; last_action TTL'd.
  - error_message cleared when status returns to RUNNING/STOPPED.
  - kill_switch engage / release / is_engaged.
  - fail-closed: is_kill_switch_engaged returns True on unreachable
    Redis and on read exceptions.
  - snapshot_runtime_state returns a filled dict with defaults.
"""
from __future__ import annotations

import pytest

from ib_trader.bots.state import (
    STATUS_ERROR, STATUS_RUNNING, STATUS_STOPPED, BotStateStore,
)


@pytest.fixture
async def fake_redis():
    fakeredis = pytest.importorskip("fakeredis")
    r = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield r
    await r.aclose()


@pytest.fixture
async def store(fake_redis):
    return BotStateStore(fake_redis)


class TestStatus:
    @pytest.mark.asyncio
    async def test_default_is_stopped(self, store):
        assert await store.get_status("bot1") == STATUS_STOPPED

    @pytest.mark.asyncio
    async def test_set_and_get(self, store):
        await store.set_status("bot1", STATUS_RUNNING)
        assert await store.get_status("bot1") == STATUS_RUNNING

    @pytest.mark.asyncio
    async def test_invalid_status_raises(self, store):
        with pytest.raises(ValueError, match="invalid bot status"):
            await store.set_status("bot1", "BOGUS")

    @pytest.mark.asyncio
    async def test_error_message_persisted_on_error(self, store):
        await store.set_status("bot1", STATUS_ERROR, error_message="boom")
        assert await store.get_error_message("bot1") == "boom"

    @pytest.mark.asyncio
    async def test_error_message_cleared_on_return_to_running(self, store):
        await store.set_status("bot1", STATUS_ERROR, error_message="boom")
        await store.set_status("bot1", STATUS_RUNNING)
        assert await store.get_error_message("bot1") is None

    @pytest.mark.asyncio
    async def test_error_message_cleared_on_stop(self, store):
        await store.set_status("bot1", STATUS_ERROR, error_message="boom")
        await store.set_status("bot1", STATUS_STOPPED)
        assert await store.get_error_message("bot1") is None


class TestHeartbeat:
    @pytest.mark.asyncio
    async def test_default_missing(self, store):
        assert await store.get_heartbeat("bot1") is None

    @pytest.mark.asyncio
    async def test_update_sets_timestamp(self, store):
        await store.update_heartbeat("bot1")
        hb = await store.get_heartbeat("bot1")
        assert hb is not None
        assert "T" in hb  # ISO format has "T" between date and time

    @pytest.mark.asyncio
    async def test_has_ttl(self, store, fake_redis):
        from ib_trader.redis.state import StateKeys
        await store.update_heartbeat("bot1")
        ttl = await fake_redis.ttl(StateKeys.bot_heartbeat("bot1"))
        assert 0 < ttl <= StateKeys.BOT_HEARTBEAT_TTL


class TestLastAction:
    @pytest.mark.asyncio
    async def test_set_and_get(self, store):
        await store.set_last_action("bot1", "FORCE_BUY")
        data = await store.get_last_action("bot1")
        assert data["action"] == "FORCE_BUY"

    @pytest.mark.asyncio
    async def test_clear(self, store):
        await store.set_last_action("bot1", "FORCE_BUY")
        await store.clear_last_action("bot1")
        assert await store.get_last_action("bot1") is None

    @pytest.mark.asyncio
    async def test_has_ttl(self, store, fake_redis):
        from ib_trader.redis.state import StateKeys
        await store.set_last_action("bot1", "X")
        ttl = await fake_redis.ttl(StateKeys.bot_last_action("bot1"))
        assert 0 < ttl <= StateKeys.BOT_LAST_ACTION_TTL


class TestKillSwitch:
    @pytest.mark.asyncio
    async def test_default_not_engaged(self, store):
        assert await store.is_kill_switch_engaged("bot1") is False

    @pytest.mark.asyncio
    async def test_engage_then_engaged(self, store):
        await store.engage_kill_switch("bot1", reason="daily loss cap")
        assert await store.is_kill_switch_engaged("bot1") is True

    @pytest.mark.asyncio
    async def test_release(self, store):
        await store.engage_kill_switch("bot1")
        await store.release_kill_switch("bot1")
        assert await store.is_kill_switch_engaged("bot1") is False

    @pytest.mark.asyncio
    async def test_fail_closed_no_redis(self):
        # The cardinal safety test: no Redis configured → kill switch
        # reported as engaged, BUYs will be rejected by RiskMiddleware.
        store = BotStateStore(None)
        assert await store.is_kill_switch_engaged("bot1") is True

    @pytest.mark.asyncio
    async def test_fail_closed_on_read_error(self, monkeypatch, store):
        # Simulate a Redis transient failure — read raises, we must
        # still treat it as engaged.
        async def _raises(*args, **kwargs):
            raise RuntimeError("redis connection reset")
        monkeypatch.setattr(store._store, "get", _raises)
        assert await store.is_kill_switch_engaged("bot1") is True


class TestNoRedisNoop:
    @pytest.mark.asyncio
    async def test_set_and_get_status(self):
        # Most writes become no-ops when Redis is absent so the runner
        # doesn't have to branch on redis=None at every call site.
        store = BotStateStore(None)
        await store.set_status("bot1", STATUS_RUNNING)
        assert await store.get_status("bot1") == STATUS_STOPPED  # default

    @pytest.mark.asyncio
    async def test_heartbeat_noop(self):
        store = BotStateStore(None)
        await store.update_heartbeat("bot1")  # no error
        assert await store.get_heartbeat("bot1") is None


class TestSnapshot:
    @pytest.mark.asyncio
    async def test_empty_bots_returns_empty(self, store):
        assert await store.snapshot_runtime_state([]) == {}

    @pytest.mark.asyncio
    async def test_defaults_for_unknown_bot(self, store):
        snap = await store.snapshot_runtime_state(["unknown"])
        assert snap["unknown"] == {
            "status": STATUS_STOPPED,
            "heartbeat": None,
            "last_action": None,
            "error_message": None,
            "kill_switch": False,
        }

    @pytest.mark.asyncio
    async def test_composes_all_fields(self, store):
        await store.set_status("bot1", STATUS_ERROR, error_message="boom")
        await store.update_heartbeat("bot1")
        await store.set_last_action("bot1", "FORCE_BUY")
        await store.engage_kill_switch("bot1", reason="test")

        snap = await store.snapshot_runtime_state(["bot1"])
        row = snap["bot1"]
        assert row["status"] == STATUS_ERROR
        assert row["heartbeat"] is not None
        assert row["last_action"]["action"] == "FORCE_BUY"
        assert row["error_message"] == "boom"
        assert row["kill_switch"] is True
