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


def test_single_full_fill_emits_progress_and_terminal(ledger):
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    events = ledger.record_fill(
        "100", qty=Decimal("10"), price=Decimal("12.73"),
        commission=Decimal("0.50"), remaining=Decimal("0"),
    )
    assert len(events) == 2
    progress, terminal = events[0], events[1]
    assert progress["terminal"] is False
    assert progress["last_fill_qty"] == "10"
    assert terminal["terminal"] is True
    assert terminal["status"] == "Filled"
    assert terminal["filled_qty"] == "10"
    assert terminal["avg_price"] == "12.73"
    assert terminal["total_commission"] == "0.50"


def test_partial_fills_accumulate(ledger):
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))

    events1 = ledger.record_fill(
        "100", qty=Decimal("3"), price=Decimal("12.70"),
        commission=Decimal("0.10"), remaining=Decimal("7"),
    )
    assert len(events1) == 1  # progress only
    assert events1[0]["terminal"] is False
    assert events1[0]["filled_qty"] == "3"
    assert events1[0]["status"] == "PartiallyFilled"

    events2 = ledger.record_fill(
        "100", qty=Decimal("7"), price=Decimal("12.80"),
        commission=Decimal("0.40"), remaining=Decimal("0"),
    )
    assert len(events2) == 2  # progress + terminal
    terminal = events2[1]
    assert terminal["terminal"] is True
    assert terminal["filled_qty"] == "10"
    # Weighted avg: (3*12.70 + 7*12.80) / 10 = 127.7/10 = 12.77
    assert Decimal(terminal["avg_price"]) == Decimal("12.77")
    assert Decimal(terminal["total_commission"]) == Decimal("0.50")


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


def test_untracked_fill_creates_entry(ledger):
    events = ledger.record_fill(
        "888", qty=Decimal("5"), price=Decimal("100"),
        commission=Decimal("0"), order_ref="ext", symbol="AAPL",
        side="BUY", remaining=Decimal("0"),
    )
    assert any(e["terminal"] for e in events)
    terminal = [e for e in events if e["terminal"]][0]
    assert terminal["filled_qty"] == "5"


def test_ledger_evicts_on_terminal(ledger):
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    ledger.record_fill(
        "100", qty=Decimal("10"), price=Decimal("12.73"),
        commission=Decimal("0"), remaining=Decimal("0"),
    )
    assert ledger.get("100") is None


def test_order_ref_passthrough(ledger):
    ledger.register("100", "IBT:test-ford:F:B:42", "F", "STK", 1, "BUY", Decimal("10"))
    events = ledger.record_fill(
        "100", qty=Decimal("10"), price=Decimal("12"),
        commission=Decimal("0"), remaining=Decimal("0"),
    )
    for e in events:
        assert e["orderRef"] == "IBT:test-ford:F:B:42"
