"""Tests for ib_trader.logging_.alerts.log_and_alert.

Pins the "catch → log at ERROR → surface to UI" helper that all engine,
middleware, bots, and strategy except blocks are expected to use. If
this helper silently regresses, the whole project invariant is at risk.
"""
from __future__ import annotations

import json

import pytest

from ib_trader.logging_.alerts import log_and_alert


class _FakeRedis:
    def __init__(self):
        self.hashes: dict[str, dict] = {}
        self.xadd_calls: list[tuple] = []

    async def hset(self, key, field, value):
        self.hashes.setdefault(key, {})[field] = value

    async def xadd(self, *args, **kwargs):
        self.xadd_calls.append((args, kwargs))
        return "0-0"


@pytest.fixture
def redis():
    return _FakeRedis()


@pytest.mark.asyncio
async def test_writes_alert_with_minimum_fields(redis, caplog):
    caplog.set_level("ERROR", logger="ib_trader.logging_.alerts")
    await log_and_alert(
        redis=redis,
        trigger="SOMETHING_BAD",
        message="boom",
        exc_info=False,  # no active exception in scope
    )
    assert "alerts:active" in redis.hashes
    stored = next(iter(redis.hashes["alerts:active"].values()))
    payload = json.loads(stored)
    assert payload["severity"] == "WARNING"
    assert payload["trigger"] == "SOMETHING_BAD"
    assert payload["message"] == "boom"
    # created_at always set.
    assert isinstance(payload["created_at"], str) and payload["created_at"]


@pytest.mark.asyncio
async def test_catastrophic_severity_passes_through(redis):
    await log_and_alert(
        redis=redis,
        trigger="IB_CONNECTION_LOST",
        message="IB gateway dropped",
        severity="CATASTROPHIC",
        symbol="QQQ",
        ib_order_id="123",
        exc_info=False,
    )
    stored = next(iter(redis.hashes["alerts:active"].values()))
    payload = json.loads(stored)
    assert payload["severity"] == "CATASTROPHIC"
    assert payload["symbol"] == "QQQ"
    assert payload["ib_order_id"] == "123"


@pytest.mark.asyncio
async def test_extra_fields_splat_into_payload(redis):
    await log_and_alert(
        redis=redis,
        trigger="WEIRD",
        message="m",
        extra={"retries": 3, "stage": "cancel"},
        exc_info=False,
    )
    payload = json.loads(next(iter(redis.hashes["alerts:active"].values())))
    assert payload["retries"] == 3
    assert payload["stage"] == "cancel"


@pytest.mark.asyncio
async def test_no_redis_logs_only(caplog):
    """When redis is None the helper must still log without crashing."""
    caplog.set_level("ERROR", logger="ib_trader.logging_.alerts")
    await log_and_alert(
        redis=None, trigger="NO_REDIS_CASE", message="m", exc_info=False,
    )
    # No assertion on records; completion without exception is the pass.


@pytest.mark.asyncio
async def test_publish_activity_fires(redis):
    """WS nudge goes out so the UI alerts panel refreshes immediately."""
    await log_and_alert(
        redis=redis, trigger="T", message="m", exc_info=False,
    )
    assert len(redis.xadd_calls) == 1
