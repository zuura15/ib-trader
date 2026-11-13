"""Serialization tests for InsyncClient.get_open_orders.

ib_async keys order-snapshot requests (openOrders / completedOrders)
globally, not per-call: when two callers overlap, the second call's
future replaces the first's, and the first awaiter hangs forever on an
end-marker that no longer resolves it. Observed live 2026-10-01: the
startup reconciler and the #98 orders sweep collided and wedged engine
startup before the internal API could bind.

These tests lock the invariant: concurrent get_open_orders calls never
overlap — the second waits for the first to finish.
"""
from __future__ import annotations

import asyncio

from ib_trader.ib.insync_client import InsyncClient


def _make_client() -> InsyncClient:
    return InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
    )


async def test_concurrent_get_open_orders_serialize():
    client = _make_client()
    active = 0
    max_active = 0
    calls = 0

    async def fake_unlocked() -> list[dict]:
        nonlocal active, max_active, calls
        active += 1
        calls += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.02)
        active -= 1
        return []

    client._get_open_orders_unlocked = fake_unlocked

    results = await asyncio.gather(
        client.get_open_orders(),
        client.get_open_orders(),
        client.get_open_orders(),
    )

    assert calls == 3
    assert max_active == 1, "snapshot calls overlapped — lock not held"
    assert results == [[], [], []]


async def test_lock_released_after_inner_failure():
    """A failing snapshot must release the lock for the next caller."""
    client = _make_client()

    async def boom() -> list[dict]:
        raise RuntimeError("gateway hiccup")

    client._get_open_orders_unlocked = boom
    try:
        await client.get_open_orders()
    except RuntimeError:
        pass

    async def ok() -> list[dict]:
        return []

    client._get_open_orders_unlocked = ok
    assert await asyncio.wait_for(client.get_open_orders(), timeout=1) == []


class _FakeIB:
    """ib_async stand-in whose snapshot requests can be made to never answer."""

    def __init__(self, *, hang_all: bool = False, hang_bind: bool = False,
                 hang_completed: bool = False) -> None:
        self.hang_all = hang_all
        self.hang_bind = hang_bind
        self.hang_completed = hang_completed

    @staticmethod
    def _answer(hang: bool, value):
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        if not hang:
            fut.set_result(value)
        return fut

    def reqOpenOrdersAsync(self):
        return self._answer(self.hang_bind, [])

    def reqAllOpenOrdersAsync(self):
        return self._answer(self.hang_all, [])

    def reqCompletedOrdersAsync(self, apiOnly: bool):
        return self._answer(self.hang_completed, [])


def _client_with(fake: _FakeIB) -> InsyncClient:
    client = InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
        min_call_interval_ms=0, order_snapshot_timeout=0.05,
    )
    client._InsyncClient__ib = fake
    return client


async def test_unanswered_open_orders_request_times_out_and_frees_the_lock():
    """IB dropping the end marker (connection blip, live 2026-11-13) must not
    wedge every later snapshot behind the lock."""
    from ib_trader.ib.insync_client import IBOrderSnapshotTimeout

    fake = _FakeIB(hang_all=True)
    client = _client_with(fake)
    try:
        await asyncio.wait_for(client.get_open_orders(), timeout=2)
        raise AssertionError("expected IBOrderSnapshotTimeout")
    except IBOrderSnapshotTimeout:
        pass

    fake.hang_all = False
    assert await asyncio.wait_for(client.get_open_orders(), timeout=2) == []


async def test_unanswered_bind_or_completed_request_still_returns_a_snapshot():
    """The bind and completed-orders requests are best-effort: a timeout on
    either degrades to the plain open-orders snapshot."""
    client = _client_with(_FakeIB(hang_bind=True, hang_completed=True))
    assert await asyncio.wait_for(client.get_open_orders(), timeout=2) == []


async def test_completed_orders_timeout_is_skipped_during_cooldown():
    """Once completed-orders times out, later snapshots skip it rather than
    waiting the timeout out again (it was adding 15 s to every close)."""
    import time as _time

    fake = _FakeIB(hang_completed=True)
    calls = 0
    real = fake.reqCompletedOrdersAsync

    def counting(apiOnly: bool):
        nonlocal calls
        calls += 1
        return real(apiOnly)

    fake.reqCompletedOrdersAsync = counting
    client = _client_with(fake)
    client._completed_orders_cooldown = 60

    await asyncio.wait_for(client.get_open_orders(), timeout=2)
    started = _time.monotonic()
    await asyncio.wait_for(client.get_open_orders(), timeout=2)
    assert calls == 1
    assert _time.monotonic() - started < 0.05

    client._completed_orders_retry_at = 0.0
    fake.hang_completed = False
    await asyncio.wait_for(client.get_open_orders(), timeout=2)
    assert calls == 2
