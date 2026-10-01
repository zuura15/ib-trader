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
