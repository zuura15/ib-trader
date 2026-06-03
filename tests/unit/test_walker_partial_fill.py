"""Regression: SMART_MARKET walker must keep walking on partial fills,
NOT exit and dispatch to the partial-cancel path.

Operator incident 2026-06-02 (order #894 MNQM6 smart_market BUY 19):
1 contract filled instantly, then ``_walk_limit_aggressive`` exited
because ``track.is_filled`` flipped True on the first partial fill.
The caller fell through to ``_handle_close_partial`` which CANCELLED
the remaining 18 — defeating the entire smart_market promise of
"walk aggressively until fully filled or capped." The user correctly
flagged this as "the real damaging bug" — the cancel-rejection issue
we were chasing was a symptom of the walker exiting too early, not
the root cause.

Fix: the walker now exits only on (a) full fill (qty_filled >= target),
(b) terminal cancel (track.is_canceled), (c) RTH duration_expired,
(d) ETH floor_price reached. A partial fill wakes the walker (via
``track.fill_event``) so it can re-amend toward the far side and
chase the next fill, but does NOT terminate it.

These tests pin both the exit conditions and the fill_event.clear()
behavior that keeps subsequent partial fills waking the walker
(without clear, asyncio.Event stays set and the walker would busy-
loop after the first fill).
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from ib_trader.engine.order import _walk_limit_aggressive


class _StubTrack:
    """Minimal track stand-in matching ctx.tracker.get(...) return shape."""

    def __init__(self):
        self.is_filled = False
        self.is_canceled = False
        self.fill_event = asyncio.Event()


def _make_ctx(*, qty_filled_sequence, status_sequence=None,
              prices=(Decimal("100"), Decimal("100.5"), Decimal("100.25"))):
    """Build a stub ctx whose get_order_status returns the given sequence,
    one call → one entry. Past-end calls return the last entry."""
    bid, ask, last = prices
    status_sequence = status_sequence or (["Submitted"] * len(qty_filled_sequence))
    call_idx = {"n": 0}

    async def _get_order_status(_ib_order_id):
        i = min(call_idx["n"], len(qty_filled_sequence) - 1)
        call_idx["n"] += 1
        return {
            "qty_filled": qty_filled_sequence[i],
            "status": status_sequence[i],
            "avg_fill_price": Decimal("0"),
            "commission": Decimal("0"),
        }

    async def _get_market_snapshot(_con_id):
        return {"bid": bid, "ask": ask, "last": last}

    ctx = SimpleNamespace(
        ib=SimpleNamespace(
            get_order_status=_get_order_status,
            get_market_snapshot=_get_market_snapshot,
            place_limit_order=AsyncMock(return_value="900"),
            amend_order=AsyncMock(return_value=None),
        ),
        tracker=SimpleNamespace(get=lambda _: track),
        redis=None,
    )
    track = _StubTrack()
    ctx.tracker.get = lambda _: track
    return ctx, track


@pytest.mark.asyncio
async def test_walker_does_not_exit_on_partial_fill():
    """1 of 19 fills, then full. Walker must KEEP walking after the
    partial, NOT exit with filled_or_canceled."""
    # Sequence: poll 1 → 1 filled, poll 2 → 1 filled, poll 3 → 19 filled (full).
    ctx, track = _make_ctx(qty_filled_sequence=[
        Decimal("1"), Decimal("1"), Decimal("19"),
    ])

    # Set fill_event True to simulate a partial-fill arriving — historically
    # this would have flipped is_filled and caused the walker to exit.
    track.fill_event.set()

    result = await _walk_limit_aggressive(
        ctx, con_id=1, ib_order_id="900", symbol="MNQM6", side="BUY",
        trigger_price=Decimal("100"),
        interval_seconds=0.01,
        total_duration_seconds=None,
        floor_price=Decimal("110"),
        target_qty=Decimal("19"),
    )
    assert result["status"] == "filled", (
        f"walker must exit on FULL fill, not partial; got {result}"
    )


@pytest.mark.asyncio
async def test_walker_still_exits_on_full_fill():
    """Sanity: full fill on the first poll exits cleanly."""
    ctx, track = _make_ctx(qty_filled_sequence=[Decimal("19")])
    result = await _walk_limit_aggressive(
        ctx, con_id=1, ib_order_id="900", symbol="MNQM6", side="BUY",
        trigger_price=Decimal("100"),
        interval_seconds=0.01,
        total_duration_seconds=None,
        floor_price=Decimal("110"),
        target_qty=Decimal("19"),
    )
    assert result["status"] == "filled"


@pytest.mark.asyncio
async def test_walker_exits_on_terminal_cancel():
    """track.is_canceled (operator or IB cancel) still terminates the
    walker — only partial fills should keep it going."""
    ctx, track = _make_ctx(qty_filled_sequence=[Decimal("0")])
    track.is_canceled = True
    result = await _walk_limit_aggressive(
        ctx, con_id=1, ib_order_id="900", symbol="MNQM6", side="BUY",
        trigger_price=Decimal("100"),
        interval_seconds=0.01,
        total_duration_seconds=None,
        floor_price=Decimal("110"),
        target_qty=Decimal("19"),
    )
    assert result["status"] == "filled_or_canceled"


@pytest.mark.asyncio
async def test_walker_exits_on_duration_expired():
    """RTH walker hits its time deadline with residual still working."""
    ctx, track = _make_ctx(qty_filled_sequence=[Decimal("1")] * 50)
    result = await _walk_limit_aggressive(
        ctx, con_id=1, ib_order_id="900", symbol="MNQM6", side="BUY",
        trigger_price=Decimal("100"),
        interval_seconds=0.01,
        # Very short deadline so the walker times out before fill.
        total_duration_seconds=0.05,
        floor_price=None,
        target_qty=Decimal("19"),
    )
    assert result["status"] == "duration_expired"


@pytest.mark.asyncio
async def test_walker_does_not_exit_at_preamend_guard_when_is_filled_set():
    """Order #916 incident 2026-06-03: SELL 50 MGCQ6 smart_market, 15
    filled on first partial, walker exited and ``_handle_partial``
    cancelled the residual 35 — operator saw ``PARTIAL: 15/50 filled
    | 35 not filled (cancel confirmed)`` when smart_market should
    have walked aggressively to fill the full 50.

    Root cause: ``de54ea2`` removed ``track.is_filled`` from the
    top-of-loop exit check, but the pre-amend guard at line ~1610
    still had it. ``track.is_filled`` is set by ``notify_filled``,
    which fires on EVERY fill callback including partials — so the
    second guard fired on the 15-contract partial and exited the
    walker with ``status="filled_or_canceled"``.

    The guard's actual job (avoid amending a terminal order) is
    already done correctly by the ``get_order_status`` block
    immediately below it, which checks the IB status string and the
    fill-vs-target ratio. The ``is_filled`` clause was both
    redundant and harmful; this test pins the fix.
    """
    # Two iterations needed:
    #   iter 1 mid-loop status read  → 15 (partial, not target)
    #   iter 1 pre-amend status read → 15 (still not terminal)
    #   iter 2 mid-loop status read  → 50 (target → exits "filled")
    ctx, track = _make_ctx(qty_filled_sequence=[
        Decimal("15"), Decimal("15"), Decimal("50"),
    ])
    # Real prod: on_fill calls notify_filled on every fill including
    # partials, so track.is_filled is True before the walker resumes
    # after the partial. fill_event also set so wait_for returns.
    track.is_filled = True
    track.fill_event.set()

    result = await _walk_limit_aggressive(
        ctx, con_id=1, ib_order_id="916", symbol="MGCQ6", side="BUY",
        trigger_price=Decimal("100"),
        interval_seconds=0.01,
        total_duration_seconds=None,
        floor_price=Decimal("110"),
        target_qty=Decimal("50"),
    )
    assert result["status"] == "filled", (
        f"walker must NOT exit at the pre-amend guard when "
        f"track.is_filled is True from a partial; got {result}. "
        f"Order #916 (SELL 50 MGCQ6 smart_market) incident 2026-06-03."
    )


@pytest.mark.asyncio
async def test_walker_clears_fill_event_after_consume():
    """asyncio.Event stays set forever once .set() is called. Without
    clearing in the walker, after a partial fill the wait_for returns
    immediately on every iteration → tight loop saturating IB rate
    limiter. Verify clear() runs."""
    ctx, track = _make_ctx(qty_filled_sequence=[
        Decimal("1"), Decimal("19"),
    ])
    track.fill_event.set()

    await _walk_limit_aggressive(
        ctx, con_id=1, ib_order_id="900", symbol="MNQM6", side="BUY",
        trigger_price=Decimal("100"),
        interval_seconds=0.01,
        total_duration_seconds=None,
        floor_price=Decimal("110"),
        target_qty=Decimal("19"),
    )
    # After the walker consumed the event signal once, it should have
    # been cleared back to unset so a future fill could wake it again.
    # (If clear didn't happen, the event would still be set.)
    assert not track.fill_event.is_set(), (
        "walker must clear fill_event after consuming so subsequent "
        "partial fills also wake it"
    )
