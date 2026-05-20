"""Unit tests for TickFuzzyStrategy — dollar-denominated SL + trail.

Focus: the exit lifecycle ($10 hard SL, $20 trail activation,
$5 give-back, ratchet up only). Entry FSM is deferred to a later
iteration; we test exit math under simulated tick streams while the
bot is in AWAITING_EXIT_TRIGGER.
"""
from datetime import datetime, timezone
from decimal import Decimal

import pytest

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategies.tick_fuzzy import TickFuzzyStrategy
from ib_trader.bots.strategy import (
    OrderFilled, PlaceOrder, QuoteUpdate, StrategyContext, UpdateState,
)


def _ctx(state: dict | None = None,
         fsm: BotState = BotState.AWAITING_EXIT_TRIGGER) -> StrategyContext:
    return StrategyContext(
        state=state or {},
        fsm_state=fsm,
        bot_id="test-bot",
        config={},
    )


def _quote(symbol: str, mid: Decimal) -> QuoteUpdate:
    # We pass mid as both bid and ask so the QuoteUpdate.mid property
    # returns exactly mid.
    return QuoteUpdate(
        symbol=symbol, bid=mid, ask=mid, last=mid,
        timestamp=datetime.now(timezone.utc),
    )


@pytest.fixture
def long_strategy():
    return TickFuzzyStrategy({
        "symbol": "MNQM6",
        "contract_multiplier": 2,
        "initial_sl_dollars": 10,
        "trail_activation_dollars": 20,
        "trail_giveback_dollars": 5,
    })


@pytest.fixture
def long_state_entered_at_29000():
    """A LONG @ 29000.00 with qty=1, no HWM / trail yet (fresh entry)."""
    return {
        "entry_price": "29000.00",
        "qty": "1",
        "position_direction": "LONG",
        "hwm_pnl_dollars": "0",
        "trail_active": False,
        "trail_stop_pnl_dollars": "0",
    }


# ---------------------------------------------------------------------------
# Pre-condition gates — non-AWAITING_EXIT_TRIGGER, missing fields.
# ---------------------------------------------------------------------------

class TestPreconditions:
    def test_does_nothing_when_not_in_position(self, long_strategy,
                                               long_state_entered_at_29000):
        ctx = _ctx(long_state_entered_at_29000,
                   fsm=BotState.AWAITING_ENTRY_TRIGGER)
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("28000")), ctx)
        assert out == []

    def test_does_nothing_when_entry_price_missing(self, long_strategy):
        ctx = _ctx({"qty": "1", "position_direction": "LONG"})
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29000")), ctx)
        assert out == []

    def test_does_nothing_when_qty_zero(self, long_strategy):
        ctx = _ctx({"entry_price": "29000", "qty": "0",
                    "position_direction": "LONG"})
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29000")), ctx)
        assert out == []


# ---------------------------------------------------------------------------
# P&L math — verify direction handling on a single tick.
# ---------------------------------------------------------------------------

class TestPnlMath:
    def test_long_profit_at_higher_price(self, long_strategy):
        # LONG @ 29000, qty 1, mult 2. Tick at 29005 → +5 pts × 1 × 2 = +$10
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "0",
                    "trail_active": False,
                    "trail_stop_pnl_dollars": "0"})
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29005")), ctx)
        # Not at threshold, just a state update.
        assert len(out) == 1 and isinstance(out[0], UpdateState)
        assert Decimal(out[0].state["unrealized_pnl"]) == Decimal("10")
        assert Decimal(out[0].state["hwm_pnl_dollars"]) == Decimal("10")

    def test_short_profit_at_lower_price(self, long_strategy):
        # SHORT @ 29000, qty 1, mult 2. Tick at 28995 → +5 × 1 × 2 = +$10
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "SHORT",
                    "hwm_pnl_dollars": "0",
                    "trail_active": False,
                    "trail_stop_pnl_dollars": "0"})
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("28995")), ctx)
        assert len(out) == 1 and isinstance(out[0], UpdateState)
        assert Decimal(out[0].state["unrealized_pnl"]) == Decimal("10")

    def test_long_loss_at_lower_price(self, long_strategy):
        # LONG @ 29000, tick 28998 → -2 × 1 × 2 = -$4 (not yet SL)
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "0",
                    "trail_active": False,
                    "trail_stop_pnl_dollars": "0"})
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("28998")), ctx)
        assert len(out) == 1
        assert Decimal(out[0].state["unrealized_pnl"]) == Decimal("-4")
        # HWM stays at 0 (max of prior 0 and current -4)
        assert Decimal(out[0].state["hwm_pnl_dollars"]) == Decimal("0")


# ---------------------------------------------------------------------------
# Hard SL — fires when pnl ≤ -initial_sl_dollars and trail inactive.
# ---------------------------------------------------------------------------

class TestHardSL:
    def test_fires_exactly_at_minus_sl(self, long_strategy):
        # LONG @ 29000, qty 1, mult 2. Tick at 28995 → -5 × 2 = -$10 = SL boundary
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "0",
                    "trail_active": False,
                    "trail_stop_pnl_dollars": "0"})
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("28995")), ctx)
        # Should emit LogSignal + UpdateState(exit_reason) + PlaceOrder
        assert any(isinstance(a, PlaceOrder) for a in out)
        place = next(a for a in out if isinstance(a, PlaceOrder))
        assert place.origin == "exit"
        assert place.side == "SELL"  # closing a long
        # exit_reason in the UpdateState should be hard_sl
        upd = next(a for a in out if isinstance(a, UpdateState))
        assert upd.state["exit_reason"] == "hard_sl"

    def test_fires_for_short(self, long_strategy):
        # SHORT @ 29000, tick at 29005 → entry-last = -5 → -$10 → SL
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "SHORT",
                    "hwm_pnl_dollars": "0",
                    "trail_active": False,
                    "trail_stop_pnl_dollars": "0"})
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29005")), ctx)
        place = next(a for a in out if isinstance(a, PlaceOrder))
        assert place.side == "BUY"  # buy-to-cover

    def test_does_not_fire_below_threshold(self, long_strategy):
        # LONG @ 29000, tick 28998 → -$4, no fire.
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "0",
                    "trail_active": False,
                    "trail_stop_pnl_dollars": "0"})
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("28998")), ctx)
        assert not any(isinstance(a, PlaceOrder) for a in out)


# ---------------------------------------------------------------------------
# Trail activation — flip to trail mode when HWM crosses +$20.
# ---------------------------------------------------------------------------

class TestTrailActivation:
    def test_activates_at_threshold(self, long_strategy):
        # LONG @ 29000, tick at 29010 → +10 × 2 = +$20 = activation boundary
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "15",  # prior HWM was already +15
                    "trail_active": False,
                    "trail_stop_pnl_dollars": "0"})
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29010")), ctx)
        upd = next(a for a in out if isinstance(a, UpdateState))
        assert upd.state["trail_active"] is True
        # Initial trail_stop = HWM - giveback = 20 - 5 = 15
        assert Decimal(upd.state["trail_stop_pnl_dollars"]) == Decimal("15")

    def test_does_not_activate_below_threshold(self, long_strategy):
        # LONG, hwm reaches +18 — still below activation threshold of +20.
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "0",
                    "trail_active": False,
                    "trail_stop_pnl_dollars": "0"})
        # Tick at 29009 → +9 pts × 2 = +$18, not enough.
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29009")), ctx)
        upd = next(a for a in out if isinstance(a, UpdateState))
        assert upd.state["trail_active"] is False


# ---------------------------------------------------------------------------
# Trail ratchet — stop moves UP only.
# ---------------------------------------------------------------------------

class TestTrailRatchet:
    def test_ratchets_up_with_higher_hwm(self, long_strategy):
        # Trail active, prior HWM +$25, trail_stop +$20.
        # New tick reaches +$40 → new trail_stop = 40 - 5 = +$35.
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "25",
                    "trail_active": True,
                    "trail_stop_pnl_dollars": "20"})
        # Tick at 29020 → +20 × 2 = +$40
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29020")), ctx)
        upd = next(a for a in out if isinstance(a, UpdateState))
        assert Decimal(upd.state["hwm_pnl_dollars"]) == Decimal("40")
        assert Decimal(upd.state["trail_stop_pnl_dollars"]) == Decimal("35")

    def test_does_not_loosen_on_pullback(self, long_strategy):
        # Trail active, HWM +$50, trail_stop +$45.
        # Pullback to +$30 — HWM stays at +$50, trail_stop stays at +$45.
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "50",
                    "trail_active": True,
                    "trail_stop_pnl_dollars": "45"})
        # Tick at 29015 → +15 × 2 = +$30 (pullback)
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29015")), ctx)
        # +30 > +45? no. Trail fires.
        place = next(a for a in out if isinstance(a, PlaceOrder))
        assert place.origin == "exit"

    def test_continues_with_partial_pullback(self, long_strategy):
        # HWM +$50, trail_stop +$45. Pullback only to +$48 (still above stop).
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "50",
                    "trail_active": True,
                    "trail_stop_pnl_dollars": "45"})
        # Tick at 29024 → +24 × 2 = +$48
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29024")), ctx)
        # Doesn't fire (48 > 45)
        assert not any(isinstance(a, PlaceOrder) for a in out)
        # HWM unchanged (48 < 50), trail_stop unchanged
        upd = out[0]
        assert upd.state["hwm_pnl_dollars"] == "50"
        assert Decimal(upd.state["trail_stop_pnl_dollars"]) == Decimal("45")


# ---------------------------------------------------------------------------
# Trail fire — exit when pnl drops to trail_stop after trail active.
# ---------------------------------------------------------------------------

class TestTrailFire:
    def test_fires_when_pnl_hits_trail_stop(self, long_strategy):
        # Trail active, trail_stop = +$15. Tick brings pnl to exactly +$15.
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "20",
                    "trail_active": True,
                    "trail_stop_pnl_dollars": "15"})
        # Tick at 29007.5 → +7.5 × 2 = +$15 = exactly at trail_stop
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29007.5")), ctx)
        place = next(a for a in out if isinstance(a, PlaceOrder))
        assert place.origin == "exit"
        upd = next(a for a in out if isinstance(a, UpdateState))
        assert upd.state["exit_reason"] == "trail_stop"

    def test_hard_sl_does_not_fire_when_trail_active(self, long_strategy):
        # Trail active, trail_stop = +$15. Tick brings pnl to -$30
        # (way past hard SL). The TRAIL fire path should claim it,
        # not the hard_sl one.
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "20",
                    "trail_active": True,
                    "trail_stop_pnl_dollars": "15"})
        # Tick at 28985 → -15 × 2 = -$30
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("28985")), ctx)
        upd = next(a for a in out if isinstance(a, UpdateState))
        # exit_reason is trail_stop (trail won), not hard_sl
        assert upd.state["exit_reason"] == "trail_stop"


# ---------------------------------------------------------------------------
# Lifecycle sequence — simulate a full LONG entry → ride → trail-stop exit.
# ---------------------------------------------------------------------------

class TestFullLifecycle:
    def test_long_round_trip(self, long_strategy):
        # Start: just entered LONG @ 29000.
        state = {
            "entry_price": "29000", "qty": "1",
            "position_direction": "LONG",
            "hwm_pnl_dollars": "0",
            "trail_active": False,
            "trail_stop_pnl_dollars": "0",
        }
        # LONG @ 29000, mult 2. Walk:
        #   29002.5 → +$5   (no SL, hwm=5)
        #   29006   → +$12  (no SL, hwm=12)
        #   29012.5 → +$25  (activate trail, trail_stop=20, hwm=25)
        #   29015   → +$30  (ratchet trail_stop to 25, hwm=30)
        #   29014   → +$28  (above trail_stop 25, no fire, hwm=30)
        #   29013   → +$26  (above trail_stop 25, no fire, hwm=30)
        #   29012.5 → +$25  (= trail_stop 25 → FIRE trail_stop)
        ticks = [29002.5, 29006, 29012.5, 29015, 29014, 29013, 29012.5]
        for price in ticks[:-1]:
            out = long_strategy._on_quote(_quote("MNQM6", Decimal(str(price))), _ctx(state))
            assert all(not isinstance(a, PlaceOrder) for a in out), (
                f"Unexpected exit at price {price} with state {state}"
            )
            for a in out:
                if isinstance(a, UpdateState):
                    state.update(a.state)

        # By now: HWM = +30, trail_active=True, trail_stop=25.
        assert state["trail_active"] is True
        assert Decimal(state["hwm_pnl_dollars"]) == Decimal("30")
        assert Decimal(state["trail_stop_pnl_dollars"]) == Decimal("25")

        # Last tick (29012.5) → +$25, at trail_stop → fire.
        out = long_strategy._on_quote(_quote("MNQM6", Decimal(str(ticks[-1]))), _ctx(state))
        place = next(a for a in out if isinstance(a, PlaceOrder))
        assert place.origin == "exit"
        assert place.side == "SELL"
        upd = next(a for a in out if isinstance(a, UpdateState))
        assert upd.state["exit_reason"] == "trail_stop"


# ---------------------------------------------------------------------------
# active_stop — the PRICE the bot would exit at, surfaced for the
# PositionStrip UI. Must be set at entry-fill time AND ratchet up as
# the trail moves. Operator-visible feedback that "an SL exists".
# ---------------------------------------------------------------------------

class TestActiveStop:
    def test_seeded_at_entry_fill_long(self, long_strategy):
        # LONG entry @ 29000, qty 1, mult 2, SL=$10 → stop_price = 29000 - 5
        ctx = _ctx({"position_direction": "LONG"})
        out = long_strategy._on_fill(
            OrderFilled(trade_serial=1, symbol="MNQM6", side="BUY",
                        fill_price=Decimal("29000"), qty=Decimal("1"),
                        commission=Decimal("0"), ib_order_id="1"),
            ctx,
        )
        upd = next(a for a in out if isinstance(a, UpdateState))
        assert Decimal(upd.state["active_stop"]) == Decimal("28995")

    def test_seeded_at_entry_fill_short(self, long_strategy):
        # SHORT entry @ 29000 → stop_price = 29000 + 5
        ctx = _ctx({"position_direction": "SHORT"})
        out = long_strategy._on_fill(
            OrderFilled(trade_serial=1, symbol="MNQM6", side="SELL",
                        fill_price=Decimal("29000"), qty=Decimal("1"),
                        commission=Decimal("0"), ib_order_id="1"),
            ctx,
        )
        upd = next(a for a in out if isinstance(a, UpdateState))
        assert Decimal(upd.state["active_stop"]) == Decimal("29005")

    def test_quote_updates_active_stop_before_trail(self, long_strategy):
        # Trail inactive: active_stop should be the initial SL price.
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "8",
                    "trail_active": False,
                    "trail_stop_pnl_dollars": "0"})
        # Tick at 29004 → +$8, no SL/trail trigger.
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29004")), ctx)
        upd = out[0]
        # active_stop still anchored at initial SL price (28995)
        assert Decimal(upd.state["active_stop"]) == Decimal("28995")

    def test_active_stop_ratchets_with_trail(self, long_strategy):
        # Trail just activated with HWM=$25, trail_stop_pnl=$20.
        # active_stop should be trail_stop in PRICE = entry + 10.
        ctx = _ctx({"entry_price": "29000", "qty": "1",
                    "position_direction": "LONG",
                    "hwm_pnl_dollars": "20",
                    "trail_active": False,
                    "trail_stop_pnl_dollars": "0"})
        # Tick at 29012.5 → +$25 → activates trail, trail_stop_pnl = $20.
        out = long_strategy._on_quote(_quote("MNQM6", Decimal("29012.5")), ctx)
        upd = out[0]
        assert upd.state["trail_active"] is True
        # active_stop = entry + 20/(1*2) = 29010 (locked profit)
        assert Decimal(upd.state["active_stop"]) == Decimal("29010")
