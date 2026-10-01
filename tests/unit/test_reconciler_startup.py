"""Startup-reconcile resilience tests for the engine Reconciler.

The startup reconcile runs before the engine is fully serving; a hung
IB snapshot here used to block the internal API from ever binding
(live wedge, 2026-10-01). These tests lock the degraded path: a
snapshot that exceeds ``snapshot_timeout`` yields an empty order
snapshot plus a WARNING alert, and startup completes.
"""
from __future__ import annotations

import asyncio

import pytest

import ib_trader.engine.reconciler as reconciler_mod
from ib_trader.engine.reconciler import Reconciler


class _HangingIB:
    """IB stub whose open-orders snapshot never returns."""

    async def get_open_orders(self) -> list[dict]:
        await asyncio.sleep(60)
        return []


class _FastIB:
    """IB stub that answers immediately with no orders."""

    async def get_open_orders(self) -> list[dict]:
        return []


@pytest.fixture
def alert_calls(monkeypatch):
    calls: list[dict] = []

    async def fake_log_and_alert(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(reconciler_mod, "log_and_alert", fake_log_and_alert)
    return calls


async def test_startup_snapshot_timeout_degrades(alert_calls):
    r = Reconciler(_HangingIB(), redis=None, snapshot_timeout=0.05)
    # Neither IB stub has req_positions_async, so positions resolve to
    # [] and the degraded path must complete well under a second.
    await asyncio.wait_for(r.startup_reconcile(), timeout=2)

    assert len(alert_calls) == 1
    assert alert_calls[0]["trigger"] == "RECONCILER_STARTUP_ORDERS_TIMEOUT"
    assert alert_calls[0]["severity"] == "WARNING"


async def test_startup_fast_snapshot_no_alert(alert_calls):
    r = Reconciler(_FastIB(), redis=None, snapshot_timeout=5)
    await asyncio.wait_for(r.startup_reconcile(), timeout=2)
    assert alert_calls == []
