"""Tests for ib_trader.engine.order_ledger — fill accumulation + terminal events."""
from decimal import Decimal

import pytest

from ib_trader.engine.order_ledger import OrderLedger


@pytest.fixture
def ledger():
    return OrderLedger()


def test_register_emits_submitted_progress(ledger):
    events = ledger.register(
        ib_order_id="100", order_ref="IBT:test:F:B:1",
        symbol="F", sec_type="STK", con_id=9599491,
        side="BUY", target_qty=Decimal("10"),
    )
    assert len(events) == 1
    assert events[0]["terminal"] is False
    assert events[0]["status"] == "Submitted"
    assert events[0]["filled_qty"] == "0"


def test_record_fill_never_emits_terminal(ledger):
    """Invariant: the ledger NEVER derives a terminal event from fill
    data, even when the accumulated fills equal target_qty and IB
    reports remaining=0. Terminal events only come from IB via
    ``record_status``. This protects against event-dispatch races
    where ``trade.orderStatus.remaining`` reads stale across SMART-split
    fills — the previous behavior terminalized prematurely and left
    orphan positions in IB."""
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    events = ledger.record_fill(
        "100", qty=Decimal("10"), price=Decimal("12.73"),
        commission=Decimal("0.50"), remaining=Decimal("0"),
        total_qty=Decimal("10"),
    )
    assert len(events) == 1  # progress only — no terminal
    assert events[0]["terminal"] is False
    assert events[0]["filled_qty"] == "10"
    assert events[0]["status"] == "Filled"  # status-flag only, NOT terminal
    assert events[0]["last_fill_qty"] == "10"
    # Entry still present — terminal comes via record_status("Filled")
    assert ledger.get("100") is not None


def test_terminal_only_fires_on_status_filled(ledger):
    """Full lifecycle: fills accumulate as progress, then IB's
    orderStatus("Filled") produces the single terminal event."""
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    ledger.record_fill(
        "100", qty=Decimal("3"), price=Decimal("12.70"),
        commission=Decimal("0.10"), remaining=Decimal("7"),
    )
    ledger.record_fill(
        "100", qty=Decimal("7"), price=Decimal("12.80"),
        commission=Decimal("0.40"), remaining=Decimal("0"),
    )
    # No terminal yet — IB's Filled hasn't arrived.
    assert ledger.get("100") is not None

    events = ledger.record_status("100", "Filled")
    assert len(events) == 1
    assert events[0]["terminal"] is True
    assert events[0]["status"] == "Filled"
    assert events[0]["filled_qty"] == "10"
    # Weighted avg (3*12.70 + 7*12.80) / 10 = 12.77
    assert Decimal(events[0]["avg_price"]) == Decimal("12.77")
    assert Decimal(events[0]["total_commission"]) == Decimal("0.50")
    # Entry evicted on terminal.
    assert ledger.get("100") is None


def test_partial_fills_accumulate_as_progress(ledger):
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))

    events1 = ledger.record_fill(
        "100", qty=Decimal("3"), price=Decimal("12.70"),
        commission=Decimal("0.10"), remaining=Decimal("7"),
    )
    assert len(events1) == 1
    assert events1[0]["terminal"] is False
    assert events1[0]["filled_qty"] == "3"
    assert events1[0]["status"] == "PartiallyFilled"

    events2 = ledger.record_fill(
        "100", qty=Decimal("7"), price=Decimal("12.80"),
        commission=Decimal("0.40"), remaining=Decimal("0"),
    )
    assert len(events2) == 1
    assert events2[0]["terminal"] is False
    assert events2[0]["filled_qty"] == "10"


def test_cancel_with_zero_fills_from_submitted_is_held(ledger):
    """IB prerouting cancel: Submitted → Cancelled with 0 fills is held,
    not emitted as terminal. The entry stays in the ledger."""
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    # Simulate Submitted (default last_status is PendingSubmit)
    ledger.record_status("100", "Submitted")
    events = ledger.record_status("100", "Cancelled")
    assert len(events) == 1
    assert events[0]["terminal"] is False
    assert events[0]["status"] == "CancelHeld"
    # Entry still in ledger — not evicted
    assert ledger.get("100") is not None


def test_cancel_after_inactive_is_terminal(ledger):
    """A cancel after Inactive (not a preroute) IS terminal."""
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    ledger.record_status("100", "Inactive")
    # Inactive is itself terminal — should have emitted already
    assert ledger.get("100") is None


def test_partial_fill_then_cancel(ledger):
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    ledger.record_fill(
        "100", qty=Decimal("4"), price=Decimal("12.70"),
        commission=Decimal("0.10"), remaining=Decimal("6"),
    )
    events = ledger.record_status("100", "Cancelled")
    assert len(events) == 1
    assert events[0]["terminal"] is True
    assert events[0]["status"] == "PartialFillCancelled"
    assert events[0]["filled_qty"] == "4"


def test_inactive_maps_to_rejected(ledger):
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    events = ledger.record_status("100", "Inactive")
    assert events[0]["terminal"] is True
    assert events[0]["status"] == "Rejected"


def test_non_terminal_status_emits_progress(ledger):
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    events = ledger.record_status("100", "PreSubmitted")
    assert len(events) == 1
    assert events[0]["terminal"] is False
    assert events[0]["status"] == "PreSubmitted"


def test_untracked_cancel_with_orderref_is_held(ledger):
    """Untracked Cancelled with an orderRef = likely preroute. Held."""
    events = ledger.record_status(
        "999", "Cancelled",
        order_ref="IBT:test:QQQ:S:1", symbol="QQQ", side="SELL",
    )
    assert len(events) == 1
    assert events[0]["terminal"] is False
    assert events[0]["status"] == "CancelHeld"
    assert ledger.get("999") is not None


def test_untracked_inactive_no_orderref_is_terminal(ledger):
    """Truly untracked Inactive (manual TWS) with no orderRef = terminal."""
    events = ledger.record_status(
        "999", "Inactive",
        order_ref="", symbol="QQQ", side="SELL",
    )
    assert len(events) == 1
    assert events[0]["terminal"] is True


def test_untracked_fill_creates_entry_without_terminal(ledger):
    """Fill for an unregistered order synthesizes a ledger entry but
    does NOT terminalize. IB's orderStatus terminal is still required."""
    events = ledger.record_fill(
        "888", qty=Decimal("5"), price=Decimal("100"),
        commission=Decimal("0"), order_ref="ext", symbol="AAPL",
        side="BUY", remaining=Decimal("0"),
    )
    assert len(events) == 1
    assert events[0]["terminal"] is False
    assert events[0]["filled_qty"] == "5"
    assert ledger.get("888") is not None


def test_ledger_evicts_on_status_terminal(ledger):
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    ledger.record_fill(
        "100", qty=Decimal("10"), price=Decimal("12.73"),
        commission=Decimal("0"), remaining=Decimal("0"),
    )
    # Still present until IB says terminal.
    assert ledger.get("100") is not None
    ledger.record_status("100", "Filled")
    assert ledger.get("100") is None


def test_order_ref_passthrough(ledger):
    ledger.register("100", "IBT:test-ford:F:B:42", "F", "STK", 1, "BUY", Decimal("10"))
    events = ledger.record_fill(
        "100", qty=Decimal("10"), price=Decimal("12"),
        commission=Decimal("0"), remaining=Decimal("0"),
    )
    for e in events:
        assert e["orderRef"] == "IBT:test-ford:F:B:42"


def test_smart_split_fills_never_self_terminal(ledger):
    """Regression for the PSQ orphan bug. IB fires two execDetails for a
    SMART-split order back-to-back; both update trade.orderStatus.remaining
    to 0 synchronously before any on_fill task runs. The first fill's
    callback therefore reads remaining=0 prematurely. The ledger must NOT
    terminalize — fills are progress-only. Terminal flows from IB's
    orderStatus("Filled") that arrives afterward."""
    ledger.register("1899", "ref", "PSQ", "STK", 692767261, "BUY", Decimal("346"))
    events1 = ledger.record_fill(
        "1899", qty=Decimal("100"), price=Decimal("28.855"),
        commission=Decimal("0"),
        remaining=Decimal("0"),  # racy read, not authoritative
        total_qty=Decimal("346"),
    )
    assert len(events1) == 1
    assert events1[0]["terminal"] is False
    events2 = ledger.record_fill(
        "1899", qty=Decimal("246"), price=Decimal("28.855"),
        commission=Decimal("0"),
        remaining=Decimal("0"),
        total_qty=Decimal("346"),
    )
    assert len(events2) == 1
    assert events2[0]["terminal"] is False
    assert events2[0]["filled_qty"] == "346"
    # IB's terminal status is what actually evicts the entry.
    term = ledger.record_status("1899", "Filled")
    assert len(term) == 1
    assert term[0]["terminal"] is True
    assert term[0]["filled_qty"] == "346"


def test_check_stuck_surfaces_entries_past_timeout(ledger):
    """Orders IB hasn't terminalized within the timeout are returned
    by check_stuck so the engine watchdog can alert on them."""
    import time
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    # Backdate the entry so it looks stuck.
    ledger._entries["100"].created_at = time.monotonic() - 600
    stuck = ledger.check_stuck(timeout_seconds=300)
    assert len(stuck) == 1
    assert stuck[0].ib_order_id == "100"
    # Entry stays — stuck entries are NOT silently evicted.
    assert ledger.get("100") is not None


def test_check_stuck_alerts_each_entry_only_once(ledger):
    """Same stuck entry must not re-alert on every watchdog tick."""
    import time
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    ledger._entries["100"].created_at = time.monotonic() - 600
    assert len(ledger.check_stuck(timeout_seconds=300)) == 1
    # Second call — already alerted, suppressed.
    assert ledger.check_stuck(timeout_seconds=300) == []


def test_check_stuck_ignores_fresh_entries(ledger):
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    assert ledger.check_stuck(timeout_seconds=300) == []
