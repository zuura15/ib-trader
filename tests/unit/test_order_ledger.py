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


def test_partial_fill_then_cancel_is_held(ledger):
    """Partial fill followed by Cancelled is HELD, not terminal — IB's
    re-route across venues looks identical to a real partial cancel at
    the moment the cancel arrives. The ledger holds; a follow-up status
    (more fills + Filled, or a second Cancelled) finalizes. If no
    follow-up arrives the watchdog raises a WARNING after
    ``order_terminal_timeout_seconds`` per the no-self-derive rule.

    Regression: GLD bot bought 114 shares, IB sent Cancelled at filled=34
    while re-routing the remaining 80, then continued filling. The old
    eager-terminate logic dropped the post-cancel fills and the bot
    state recorded only 34 shares (real position: 114).
    """
    ledger.register("100", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    ledger.record_fill(
        "100", qty=Decimal("4"), price=Decimal("12.70"),
        commission=Decimal("0.10"), remaining=Decimal("6"),
    )
    events = ledger.record_status("100", "Cancelled")
    assert len(events) == 1
    assert events[0]["terminal"] is False
    assert events[0]["status"] == "CancelHeld"
    # Entry must remain so further fills can still attach.
    assert ledger.get("100") is not None


def test_preroute_cancel_after_partial_then_more_fills(ledger):
    """The actual GLD-bug shape end-to-end: register 114, partial fill 34,
    spurious Cancelled, 80 more fills across multiple execDetails, IB
    closes with Filled. The ledger must accumulate every fill and emit
    a single terminal Filled with filled_qty=114."""
    ledger.register("880", "ref", "GLD", "STK", 51529211, "BUY", Decimal("114"))
    ledger.record_status("880", "Submitted")
    ledger.record_fill(
        "880", qty=Decimal("34"), price=Decimal("435.08"),
        commission=Decimal("0"), remaining=Decimal("80"),
    )
    held = ledger.record_status("880", "Cancelled")
    assert held[0]["terminal"] is False
    assert held[0]["status"] == "CancelHeld"
    # Re-route delivers more fills + status flips back to Submitted.
    for qty in (Decimal("15"), Decimal("15"), Decimal("5"), Decimal("45")):
        evs = ledger.record_fill(
            "880", qty=qty, price=Decimal("435.08"),
            commission=Decimal("0"), remaining=Decimal("0"),
        )
        for ev in evs:
            assert ev["terminal"] is False
    term = ledger.record_status("880", "Filled")
    assert len(term) == 1
    assert term[0]["terminal"] is True
    assert term[0]["status"] == "Filled"
    assert term[0]["filled_qty"] == "114"
    assert ledger.get("880") is None  # evicted on real terminal


def test_double_cancel_after_partial_finalizes(ledger):
    """If IB sends Cancelled twice (the second one is the genuine cancel
    rather than a re-route), the second one finalizes — prev_status is
    now ``Cancelled`` so the preroute guard does NOT re-fire."""
    ledger.register("200", "ref", "F", "STK", 1, "BUY", Decimal("10"))
    ledger.record_status("200", "Submitted")
    ledger.record_fill(
        "200", qty=Decimal("4"), price=Decimal("12.70"),
        commission=Decimal("0.10"), remaining=Decimal("6"),
    )
    held = ledger.record_status("200", "Cancelled")
    assert held[0]["terminal"] is False
    final = ledger.record_status("200", "Cancelled")
    assert final[0]["terminal"] is True
    assert final[0]["status"] == "PartialFillCancelled"
    assert final[0]["filled_qty"] == "4"


# ---------------------------------------------------------------------------
# Position-diff reconcile
# ---------------------------------------------------------------------------


def _make_ledger_with_position(qty: Decimal):
    """Build a ledger whose position_getter returns the given fixed qty."""
    from ib_trader.engine.order_ledger import OrderLedger
    holder = {"qty": qty}

    def getter(symbol: str, sec_type: str = "STK") -> Decimal:
        return holder["qty"]

    return OrderLedger(position_getter=getter), holder


def test_position_diff_upgrades_partial_terminal_to_full():
    """The GLD bug shape end-to-end via the position diff: BUY 114 with
    pre_position=3, only 84 fills tracked, IB's net position now shows
    117 (pre 3 + actual 114). At terminal time the ledger upgrades the
    emitted filled_qty from 84 to 114 because the broker confirms it."""
    ledger, position = _make_ledger_with_position(Decimal("3"))  # pre-place
    ledger.register(
        "886", "ref-gld", "GLD", "STK", 51529211, "BUY", Decimal("114"),
        pre_position=Decimal("3"),
    )
    ledger.record_status("886", "Submitted")
    ledger.record_fill(
        "886", qty=Decimal("84"), price=Decimal("435.61"),
        commission=Decimal("0.34"), remaining=Decimal("30"),
    )
    # Broker-side: full 114 actually filled (IB just dropped fill events
    # for the last 30 during a re-route).
    position["qty"] = Decimal("117")
    term = ledger.record_status("886", "Filled")
    assert len(term) == 1
    assert term[0]["terminal"] is True
    assert term[0]["filled_qty"] == "114"
    assert term[0]["status"] == "Filled"


def test_position_diff_caps_at_target_qty():
    """If the broker's position moved by MORE than target_qty (e.g. a
    concurrent manual buy on the same symbol), the upgrade is capped at
    target_qty so we never over-attribute."""
    ledger, position = _make_ledger_with_position(Decimal("0"))
    ledger.register(
        "300", "ref", "F", "STK", 1, "BUY", Decimal("10"),
        pre_position=Decimal("0"),
    )
    ledger.record_status("300", "Submitted")
    ledger.record_fill(
        "300", qty=Decimal("4"), price=Decimal("12.70"),
        commission=Decimal("0"), remaining=Decimal("6"),
    )
    # Position shows 25 — way more than our 10-share target. Capped.
    position["qty"] = Decimal("25")
    term = ledger.record_status("300", "Filled")
    assert term[0]["filled_qty"] == "10"


def test_position_diff_does_not_downgrade():
    """If the broker's position moved by LESS than tracked fills (we
    counted more than IB shows), trust tracked fills — never silently
    lose qty."""
    ledger, position = _make_ledger_with_position(Decimal("0"))
    ledger.register(
        "301", "ref", "F", "STK", 1, "BUY", Decimal("10"),
        pre_position=Decimal("0"),
    )
    ledger.record_status("301", "Submitted")
    ledger.record_fill(
        "301", qty=Decimal("8"), price=Decimal("12.70"),
        commission=Decimal("0"), remaining=Decimal("2"),
    )
    # Broker shows only 5 long despite our 8 tracked. Don't downgrade.
    position["qty"] = Decimal("5")
    term = ledger.record_status("301", "Filled")
    assert term[0]["filled_qty"] == "8"


def test_position_diff_no_upgrade_when_zero_tracked_fills():
    """Refuses to upgrade an entry with zero tracked fills even if the
    broker shows the position moved — synthesizing a fill price out of
    thin air is too dangerous. Operator must reconcile manually."""
    ledger, position = _make_ledger_with_position(Decimal("0"))
    ledger.register(
        "302", "ref", "F", "STK", 1, "BUY", Decimal("10"),
        pre_position=Decimal("0"),
    )
    ledger.record_status("302", "Submitted")
    # No fills tracked at all.
    position["qty"] = Decimal("10")
    term = ledger.record_status("302", "Filled")
    assert term[0]["filled_qty"] == "0"


def test_position_diff_sell_side_uses_signed_delta():
    """For a SELL, the diff is ``pre - current``, not ``current - pre``."""
    ledger, position = _make_ledger_with_position(Decimal("100"))
    ledger.register(
        "303", "ref", "F", "STK", 1, "SELL", Decimal("100"),
        pre_position=Decimal("100"),
    )
    ledger.record_status("303", "Submitted")
    ledger.record_fill(
        "303", qty=Decimal("60"), price=Decimal("12.70"),
        commission=Decimal("0"), remaining=Decimal("40"),
    )
    # Broker says position is now 0 — we sold all 100 even though we
    # only saw 60 fill events.
    position["qty"] = Decimal("0")
    term = ledger.record_status("303", "Filled")
    assert term[0]["filled_qty"] == "100"


def test_position_diff_skipped_without_position_getter():
    """A ledger constructed without a position_getter behaves exactly as
    before — terminals reflect tracked fills only."""
    from ib_trader.engine.order_ledger import OrderLedger
    plain = OrderLedger()
    plain.register(
        "304", "ref", "F", "STK", 1, "BUY", Decimal("10"),
        pre_position=Decimal("0"),
    )
    plain.record_status("304", "Submitted")
    plain.record_fill(
        "304", qty=Decimal("4"), price=Decimal("12.70"),
        commission=Decimal("0"), remaining=Decimal("6"),
    )
    term = plain.record_status("304", "Filled")
    assert term[0]["filled_qty"] == "4"


def test_register_after_auto_create_backfills_pre_position():
    """If a fill auto-creates the entry before our explicit register
    runs, the explicit register backfills target_qty + pre_position
    rather than overwriting the accumulated fills."""
    ledger, position = _make_ledger_with_position(Decimal("0"))
    # A fill arrives first (race) — ledger auto-creates with target=4.
    ledger.record_fill(
        "305", qty=Decimal("4"), price=Decimal("12.70"),
        commission=Decimal("0"), remaining=Decimal("6"),
        symbol="F", sec_type="STK", con_id=1, side="BUY",
    )
    # Then our register call lands with the real target + snapshot.
    ledger.register(
        "305", "ref", "F", "STK", 1, "BUY", Decimal("10"),
        pre_position=Decimal("0"),
    )
    # Broker position confirms the full 10 filled.
    position["qty"] = Decimal("10")
    term = ledger.record_status("305", "Filled")
    assert term[0]["filled_qty"] == "10"


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
