"""Per-order serialization tests for InsyncClient._dispatch_ordered.

ib_async dispatches IB events (execDetails, orderStatus,
commissionReport) synchronously in arrival order, but our handlers
schedule async callbacks that may yield. Without a per-order lock,
a status=Filled handler could overtake a still-queued fill handler
and terminalize the ledger with under-counted fills — the live PSQ
incident on 2026-04-21 left 246 orphan shares this way.

These tests lock the invariant: callbacks for the same ``ib_order_id``
run in scheduling order regardless of their internal await pattern,
while callbacks for different orders remain concurrent.
"""
from __future__ import annotations

import asyncio

from ib_trader.ib.insync_client import InsyncClient


def _make_client() -> InsyncClient:
    return InsyncClient(
        host="127.0.0.1", port=4002, client_id=9999, account_id="DU0",
    )


async def test_dispatch_ordered_serializes_same_order():
    """Three callbacks scheduled in order with DECREASING internal
    delays — without serialization they'd complete 3, 2, 1. The
    per-order lock must force them to complete 1, 2, 3."""
    client = _make_client()
    observed: list[int] = []

    async def cb(seq: int, delay: float) -> None:
        await asyncio.sleep(delay)
        observed.append(seq)

    t1 = asyncio.create_task(client._dispatch_ordered("A", cb, 1, 0.03))
    t2 = asyncio.create_task(client._dispatch_ordered("A", cb, 2, 0.02))
    t3 = asyncio.create_task(client._dispatch_ordered("A", cb, 3, 0.01))
    await asyncio.gather(t1, t2, t3)

    assert observed == [1, 2, 3]


async def test_dispatch_ordered_different_orders_run_concurrently():
    """Orders are isolated — one blocking handler for order A must not
    prevent order B's handler from starting."""
    client = _make_client()
    a_started = asyncio.Event()
    b_started = asyncio.Event()
    release = asyncio.Event()

    async def blocker(ev: asyncio.Event) -> None:
        ev.set()
        await release.wait()

    t1 = asyncio.create_task(client._dispatch_ordered("A", blocker, a_started))
    t2 = asyncio.create_task(client._dispatch_ordered("B", blocker, b_started))

    # If locks were shared across orders, b_started.wait() would hang.
    await asyncio.wait_for(a_started.wait(), timeout=1.0)
    await asyncio.wait_for(b_started.wait(), timeout=1.0)
    release.set()
    await asyncio.gather(t1, t2)


async def test_dispatch_ordered_reuses_lock_for_same_order():
    """Same ib_order_id must get the same Lock instance across calls —
    otherwise serialization is broken."""
    client = _make_client()
    lock1 = client._get_order_lock("X")
    lock2 = client._get_order_lock("X")
    lock_other = client._get_order_lock("Y")
    assert lock1 is lock2
    assert lock1 is not lock_other


async def test_dispatch_ordered_releases_lock_on_exception():
    """A raising callback must still release the lock, otherwise every
    subsequent event for that order stalls forever."""
    client = _make_client()

    async def boom() -> None:
        raise RuntimeError("simulated handler failure")

    async def good() -> None:
        pass

    t1 = asyncio.create_task(client._dispatch_ordered("A", boom))
    t2 = asyncio.create_task(client._dispatch_ordered("A", good))

    # First task propagates; second must still complete.
    raised = False
    try:
        await t1
    except RuntimeError:
        raised = True
    assert raised
    await asyncio.wait_for(t2, timeout=1.0)
    assert t2.done() and t2.exception() is None


async def test_simulated_fill_then_status_race_preserves_order():
    """End-to-end-ish: simulate the PSQ ordering — a fill handler that
    yields twice, followed immediately by a status=Filled handler.
    Without the lock, the status handler would finish first; with it,
    the fill handler finishes first every time."""
    client = _make_client()
    events: list[str] = []

    async def fill_handler() -> None:
        await asyncio.sleep(0.02)
        events.append("fill")
        await asyncio.sleep(0.02)
        events.append("fill-done")

    async def status_handler() -> None:
        events.append("status")

    tf = asyncio.create_task(client._dispatch_ordered("1899", fill_handler))
    ts = asyncio.create_task(client._dispatch_ordered("1899", status_handler))
    await asyncio.gather(tf, ts)

    assert events == ["fill", "fill-done", "status"]
