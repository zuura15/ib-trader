"""Regression: IB Gateway disconnect → backoff reconnect, escalating
alert.

A scheduled IB Gateway restart on 2026-05-10 left the engine stuck —
the disconnect handler fired a CATASTROPHIC alert immediately and
never tried to reconnect, so the daemon sat idle even though the
Gateway came back online a minute later.

Fix: ``_raise_ib_disconnect_alert`` now kicks off an async loop that
retries ``ctx.ib.connect()`` with exponential backoff (2s → 4s → ...
→ 60s) and fires only a WARNING ``IB_GATEWAY_RECONNECTING`` alert up
front. After 5 minutes of unsuccessful retries it ALSO fires the
CATASTROPHIC ``IB_GATEWAY_DISCONNECTED`` (which blocks the UI as
before). On reconnect the connectedEvent callback clears both
triggers.

These tests lock that contract:
  - WARNING alert raised immediately on the first call.
  - ``ctx.ib.connect()`` retried with backoff.
  - Loop returns cleanly when connect eventually succeeds.
  - CATASTROPHIC alert raised once elapsed time crosses 5 min.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

import ib_trader.engine.main as engine_main


def _make_ctx(connect_mock: AsyncMock):
    """Minimal AppContext-like stub for the reconnect loop."""
    ib = SimpleNamespace(connect=connect_mock)
    redis = SimpleNamespace(
        hgetall=AsyncMock(return_value={}),  # no existing alerts
    )
    return SimpleNamespace(ib=ib, redis=redis)


@pytest.fixture(autouse=True)
def fast_sleep():
    """Replace asyncio.sleep with a no-op so the backoff loop runs
    instantaneously in tests. The loop's *elapsed* clock comes from
    ``asyncio.get_event_loop().time()``, which we patch separately
    where time-based escalation matters."""
    with patch.object(engine_main.asyncio, "sleep", new=AsyncMock(return_value=None)):
        yield


async def test_reconnect_loop_fires_warning_then_succeeds():
    """Happy path: WARNING alert raised once, connect succeeds after a
    couple of retries, loop returns without firing CATASTROPHIC."""
    connect = AsyncMock(side_effect=[ConnectionRefusedError("dead"),
                                     ConnectionRefusedError("dead"),
                                     None])
    ctx = _make_ctx(connect)

    fired: list[tuple[str, str]] = []

    def fake_alert(*, redis, trigger, severity, message, **_):
        fired.append((trigger, severity))

    with patch(
        "ib_trader.logging_.alerts.fire_and_forget_alert",
        side_effect=fake_alert,
    ):
        await engine_main._reconnect_with_backoff(ctx)

    assert connect.await_count == 3
    assert ("IB_GATEWAY_RECONNECTING", "WARNING") in fired
    assert not any(t == "IB_GATEWAY_DISCONNECTED" for t, _ in fired), (
        "CATASTROPHIC should not fire when reconnect succeeds before "
        "the 5-min escalation threshold"
    )


async def test_reconnect_loop_escalates_after_threshold():
    """After 5 min of failures the loop fires the CATASTROPHIC alert
    on top of the WARNING — and only fires it once even if retries
    continue for longer."""
    # Simulate: fail many times, succeed on the 6th call. Elapsed
    # time advances past the threshold between attempts 3 and 4.
    threshold = engine_main._RECONNECT_CATASTROPHIC_AFTER_SEC
    # ``time()`` is called once at loop start plus once per failure
    # (for ``elapsed``) plus once after success (for the info log).
    # Seed enough values that the loop never runs out, then clamp.
    times = [0.0, 1.0, 2.0, 3.0,
             threshold + 1.0,
             threshold + 2.0,
             threshold + 3.0,
             threshold + 4.0]
    time_idx = {"i": 0}

    def fake_time() -> float:
        i = time_idx["i"]
        time_idx["i"] = min(i + 1, len(times) - 1)
        return times[i]

    loop_stub = SimpleNamespace(time=fake_time)

    connect = AsyncMock(side_effect=[
        ConnectionRefusedError("dead")] * 5 + [None])
    ctx = _make_ctx(connect)

    fired: list[tuple[str, str]] = []

    def fake_alert(*, redis, trigger, severity, message, **_):
        fired.append((trigger, severity))

    with patch.object(
        engine_main.asyncio, "get_event_loop",
        lambda: loop_stub,
    ), patch(
        "ib_trader.logging_.alerts.fire_and_forget_alert",
        side_effect=fake_alert,
    ):
        await engine_main._reconnect_with_backoff(ctx)

    triggers_seen = [t for t, _ in fired]
    assert triggers_seen.count("IB_GATEWAY_RECONNECTING") == 1
    assert triggers_seen.count("IB_GATEWAY_DISCONNECTED") == 1, (
        f"Expected exactly one CATASTROPHIC escalation, got {fired!r}"
    )
    # CATASTROPHIC must come after WARNING
    assert triggers_seen.index("IB_GATEWAY_DISCONNECTED") \
        > triggers_seen.index("IB_GATEWAY_RECONNECTING")


async def test_reconnect_loop_cancellation_propagates():
    """The reconnect loop honours asyncio cancellation so engine
    shutdown isn't blocked by an in-flight retry."""
    connect = AsyncMock(side_effect=asyncio.CancelledError())
    ctx = _make_ctx(connect)

    with patch(
        "ib_trader.logging_.alerts.fire_and_forget_alert",
        side_effect=lambda **_: None,
    ):
        with pytest.raises(asyncio.CancelledError):
            await engine_main._reconnect_with_backoff(ctx)
