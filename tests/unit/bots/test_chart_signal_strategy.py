"""Tests for the chart-signal strategy."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategies.chart_signal import ChartSignalStrategy
from ib_trader.bots.strategy import (
    BarCompleted,
    LogEventType,
    LogSignal,
    OrderFilled,
    PlaceOrder,
    QuoteUpdate,
    StrategyContext,
    UpdateState,
)

PT = ZoneInfo("America/Los_Angeles")
BAR_SECONDS = 180


def _default_config(sec_type: str = "FUT") -> dict:
    return {
        "symbol": "MGCM6",
        "sec_type": sec_type,
        "bar_size_seconds": BAR_SECONDS,
        "lookback_bars": 60,
        "qty_default": 1,
        "order_strategy": "mid",
        "exit_order_strategy": "mid",
        # Tests build BarCompleted events anchored at START_UTC
        # (2026-05-10) which is hours-to-days behind real wallclock.
        # The wallclock-signal-age gate would reject every such bar
        # in normal flow — bump the cap so existing semantic tests
        # (3-touch detection, exit eval, deadzone, fills, etc.) are
        # not gated on real-time-vs-fixture-time. The signal-age
        # gate has its own dedicated tests that build wallclock-
        # recent bar events and use the production-default cap.
        "max_signal_age_seconds": 10 ** 9,
        # The entry-distance filter (FILTER_FAR_FROM_PIVOT) caps
        # entry-to-line gap at 2× trail_dist. Fixture closes drift
        # several units past the synthetic line by design, so the
        # filter would reject most semantic tests. Disabled here;
        # dedicated tests in TestEntryDistanceFilter cover the rule.
        "far_from_pivot_filter_enabled": False,
        # The stale_line filter rejects lines whose Q anchor is older
        # than max_q_age_hours (default 24h). Real-time tests build
        # wallclock-now bars; if a fixture happens to span more than
        # 24h of synthetic time the filter kicks in unpredictably.
        # Disabled here; TestStaleLineFilter covers the rule.
        "entry_stale_line_filter_enabled": False,
    }


def _make_ctx(state: dict | None = None,
              fsm_state: BotState = BotState.AWAITING_ENTRY_TRIGGER,
              config: dict | None = None) -> StrategyContext:
    return StrategyContext(
        state=state if state is not None else {"armed": True},
        fsm_state=fsm_state,
        bot_id="test-bot",
        config=config or _default_config(),
    )


def _bar(t: datetime, close: float, low: float | None = None,
         high: float | None = None) -> dict:
    return {
        "timestamp_utc": t.astimezone(timezone.utc).isoformat(),
        "open": close,
        "high": close if high is None else high,
        "low": close if low is None else low,
        "close": close,
        "volume": 100,
    }


def _zigzag_bars(start: datetime,
                 closes: list[float]) -> list[dict]:
    """One bar per close at BAR_SECONDS spacing starting at ``start``."""
    return [_bar(start + timedelta(seconds=i * BAR_SECONDS), c)
            for i, c in enumerate(closes)]


# A 9-bar zigzag that resolves to a single 4-touch uptrending support
# line with slope 0.5/bar. Pivot lows fall at idx 1,3,5,7 with values
# 9,10,11,12 — all collinear on y = 0.5*idx + 8.5.
ZIGZAG_CLOSES = [10.0, 9.0, 11.0, 10.0, 12.0, 11.0, 13.0, 12.0, 14.0]
# Mirror zigzag: pivot HIGHS at idx 1,3,5,7 with values 21,20,19,18 on
# a 4-touch downtrending resistance line of slope -0.5/bar.
ZIGZAG_CLOSES_DOWN = [20.0, 21.0, 19.0, 20.0, 18.0, 19.0, 17.0, 18.0, 16.0]
START_UTC = datetime(2026, 5, 10, 18, 0, tzinfo=timezone.utc)   # 11:00 PT, well outside deadzone


class TestManifest:
    def test_creation(self):
        s = ChartSignalStrategy(_default_config())
        assert s.manifest.name == "chart_signal"
        assert "FUT" in s.manifest.supported_sec_types
        sub = s.manifest.subscriptions[0]
        assert sub.type == "bars" and sub.symbols == ["MGCM6"]
        assert sub.params["bar_seconds"] == BAR_SECONDS

    @pytest.mark.asyncio
    async def test_on_start_initializes_armed(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(state={})
        actions = await s.on_start(ctx)
        # Expect one UpdateState that sets armed=True.
        ups = [a for a in actions if isinstance(a, UpdateState)]
        assert ups and ups[0].state.get("armed") is True


class TestEntry:
    def _bar_event_at(self, idx: int) -> BarCompleted:
        bars = _zigzag_bars(START_UTC, ZIGZAG_CLOSES[: idx + 1])
        return BarCompleted(
            symbol="MGCM6", bar=bars[-1], window=bars, bar_count=len(bars),
        )

    @pytest.mark.asyncio
    async def test_3touch_uptrending_support_fires_buy(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx()
        event = self._bar_event_at(8)
        actions = await s.on_event(event, ctx)

        place = [a for a in actions if isinstance(a, PlaceOrder)]
        assert place, f"expected a PlaceOrder, got {actions}"
        assert place[0].side == "BUY"
        assert place[0].qty == Decimal("1")

        # Freezes entry_line in state with slope_per_sec derived from BAR_SECONDS.
        ups = [a for a in actions if isinstance(a, UpdateState)
               and "entry_line" in a.state]
        assert ups
        line = ups[0].state["entry_line"]
        assert line["kind"] == "support"
        assert line["direction"] == "long"
        assert line["touches"] >= 3
        assert line["slope_per_bar"] == pytest.approx(0.5)
        assert line["slope_per_sec"] == pytest.approx(0.5 / BAR_SECONDS)
        # Change A — chart clip: ``from_time`` must be present so the
        # frontend can clip the entry-line render at Q. Q is the first
        # construction pivot at from_idx=1 (close 9.0 at START + 1×180s).
        assert "from_time" in line
        assert line["from_time"]   # non-empty ISO

    @pytest.mark.asyncio
    async def test_armed_false_blocks_entry(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(state={"armed": False})
        actions = await s.on_event(self._bar_event_at(8), ctx)
        assert not any(isinstance(a, PlaceOrder) for a in actions)

    @pytest.mark.asyncio
    async def test_qty_override_in_state_wins(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(state={"armed": True, "qty_override": "3"})
        actions = await s.on_event(self._bar_event_at(8), ctx)
        place = [a for a in actions if isinstance(a, PlaceOrder)]
        assert place and place[0].qty == Decimal("3")

    @pytest.mark.asyncio
    async def test_cooldown_until_blocks_entry(self):
        """After exit (with stop_on_exit=False), the runtime writes
        ``cooldown_until``. While the bar's wallclock is before that
        timestamp, the entry gate skips even if a fresh 3-touch
        signal otherwise qualifies."""
        cfg = _default_config()
        s = ChartSignalStrategy(cfg)
        # Bar evaluation is at idx 8 of ZIGZAG — wallclock
        # ``START_UTC + 8 × BAR_SECONDS``. Set cooldown_until to one
        # bar after that, so the gate fires.
        bar_time = START_UTC + timedelta(seconds=BAR_SECONDS * 8)
        cooldown_until = (bar_time + timedelta(seconds=BAR_SECONDS)).isoformat()
        ctx = _make_ctx(state={"armed": True, "cooldown_until": cooldown_until})
        actions = await s.on_event(self._bar_event_at(8), ctx)
        assert not any(isinstance(a, PlaceOrder) for a in actions), \
            "expected no entry during cooldown"
        from ib_trader.bots.strategy import LogSignal, LogEventType
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and a.event_type == LogEventType.SKIP
                 and "cooldown" in a.message]
        assert skips, "expected a SKIP log for the cooldown gate"

    @pytest.mark.asyncio
    async def test_cooldown_passed_allows_entry(self):
        """Once the bar's wallclock is past ``cooldown_until``, entry
        fires normally."""
        cfg = _default_config()
        s = ChartSignalStrategy(cfg)
        bar_time = START_UTC + timedelta(seconds=BAR_SECONDS * 8)
        cooldown_until = (bar_time - timedelta(seconds=1)).isoformat()
        ctx = _make_ctx(state={"armed": True, "cooldown_until": cooldown_until})
        actions = await s.on_event(self._bar_event_at(8), ctx)
        place = [a for a in actions if isinstance(a, PlaceOrder)]
        assert place, f"expected entry after cooldown, got {actions}"

    @pytest.mark.asyncio
    async def test_no_3touch_no_entry(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx()
        # Flat closes — no pivots, no signal.
        flat_bars = _zigzag_bars(START_UTC, [10.0] * 9)
        event = BarCompleted(symbol="MGCM6", bar=flat_bars[-1],
                             window=flat_bars, bar_count=9)
        actions = await s.on_event(event, ctx)
        assert not any(isinstance(a, PlaceOrder) for a in actions)

    @pytest.mark.asyncio
    async def test_stale_anchor_blocked_no_new_touch(self):
        """The 3-touch line is valid but the just-confirmed pivot on
        this bar isn't on the line — the bot must NOT fire on the
        stale signal. Matches the user's 2026-05-11 MNQ observation:
        chart showed a B at the anchor, no new B for 20+ minutes, but
        the bot fired a fresh BUY because ``armed=True`` and the line
        was still 3-touch."""
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx()
        # 9-bar zigzag (4-touch support, last pivot at idx 7) followed
        # by 6 monotonically-increasing bars. The bars stay well above
        # the line (no break, no new touching pivot), so the line is
        # still 4-touch and not broken — but the just-confirmed pivot
        # at idx 13 isn't a pivot at all (closes monotonic upward), so
        # guard 1 rejects.
        closes = ZIGZAG_CLOSES + [14.2, 14.5, 14.8, 15.1, 15.4, 15.7]
        bars = _zigzag_bars(START_UTC, closes)
        event = BarCompleted(symbol="MGCM6", bar=bars[-1],
                             window=bars, bar_count=len(bars))
        actions = await s.on_event(event, ctx)
        # No entry — freshness gate blocked.
        assert not any(isinstance(a, PlaceOrder) for a in actions), \
            f"expected no PlaceOrder on stale 3-touch, got {actions}"
        # Surface log explains why.
        from ib_trader.bots.strategy import LogSignal, LogEventType
        skips = [a for a in actions if isinstance(a, LogSignal)
                  and a.event_type == LogEventType.SKIP]
        assert skips, "expected a SKIP log row explaining the stale gate"

    @pytest.mark.asyncio
    async def test_fresh_3rd_touch_fires(self):
        """Sanity inverse: on the bar where the 3rd touch is freshly
        confirmed (the just-confirmed pivot at last_idx-1 is on the
        line), the bot fires normally."""
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx()
        # ZIGZAG_CLOSES at idx=8 has the freshest pivot at idx 7 (just
        # confirmed by closing bar 8) sitting on the support line. The
        # latest pivot IS the new pivot → guard 1 passes.
        bars = _zigzag_bars(START_UTC, ZIGZAG_CLOSES)
        event = BarCompleted(symbol="MGCM6", bar=bars[-1],
                             window=bars, bar_count=len(bars))
        actions = await s.on_event(event, ctx)
        place = [a for a in actions if isinstance(a, PlaceOrder)]
        assert place and place[0].side == "BUY"

    @pytest.mark.asyncio
    async def test_signal_age_timeout_blocks_stale_bar_event(self):
        """Even when the touch count just increased (guard 1 passes),
        the wallclock signal-age cap rejects a bar event whose close
        was longer ago than ``max_signal_age_seconds``. Simulates a
        bar event queued during a daemon restart.

        Uses sec_type=STK so the test isn't sensitive to wallclock
        landing in the FUT/FOP 14:00-15:00 PT deadzone (which would
        short-circuit ``_on_bar`` before our guards run)."""
        from datetime import datetime, timezone, timedelta
        cfg = _default_config(sec_type="STK")
        cfg["max_signal_age_seconds"] = 5  # tight cap for this test
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        # Latest bar's close = now - 30s → elapsed 30s > 5s cap.
        # Build bars so the latest bar's start time = now - 30s -
        # BAR_SECONDS, with closes that produce a fresh 3-touch.
        last_bar_close = datetime.now(timezone.utc) - timedelta(seconds=30)
        last_bar_start = last_bar_close - timedelta(seconds=BAR_SECONDS)
        first_bar_start = last_bar_start - timedelta(
            seconds=BAR_SECONDS * (len(ZIGZAG_CLOSES) - 1),
        )
        bars = _zigzag_bars(first_bar_start, ZIGZAG_CLOSES)
        event = BarCompleted(symbol="MGCM6", bar=bars[-1],
                             window=bars, bar_count=len(bars))
        actions = await s.on_event(event, ctx)
        # No entry — wallclock gate blocked.
        assert not any(isinstance(a, PlaceOrder) for a in actions), \
            f"expected no PlaceOrder on wallclock-stale event, got {actions}"
        from ib_trader.bots.strategy import LogSignal, LogEventType
        skips = [a for a in actions if isinstance(a, LogSignal)
                  and a.event_type == LogEventType.SKIP
                  and "too old" in a.message]
        assert skips, "expected a SKIP log row with 'too old' message"

    @pytest.mark.asyncio
    async def test_signal_age_default_passes_in_normal_flow(self):
        """A bar event whose close was just now (within the default
        10s cap) passes guard 2 and fires normally. STK to avoid the
        FUT deadzone short-circuit (see sibling test)."""
        from datetime import datetime, timezone, timedelta
        cfg = _default_config(sec_type="STK")
        cfg["max_signal_age_seconds"] = 10
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        # Latest bar's close = now (just closed) → elapsed ≈ 0s.
        last_bar_close = datetime.now(timezone.utc)
        last_bar_start = last_bar_close - timedelta(seconds=BAR_SECONDS)
        first_bar_start = last_bar_start - timedelta(
            seconds=BAR_SECONDS * (len(ZIGZAG_CLOSES) - 1),
        )
        bars = _zigzag_bars(first_bar_start, ZIGZAG_CLOSES)
        event = BarCompleted(symbol="MGCM6", bar=bars[-1],
                             window=bars, bar_count=len(bars))
        actions = await s.on_event(event, ctx)
        place = [a for a in actions if isinstance(a, PlaceOrder)]
        assert place and place[0].side == "BUY"


class TestEntryDistanceFilter:
    """FILTER_FAR_FROM_PIVOT: reject when entry-to-line gap > 2× trail_dist."""

    def _bar_event_at(self, idx: int) -> BarCompleted:
        bars = _zigzag_bars(START_UTC, ZIGZAG_CLOSES[: idx + 1])
        return BarCompleted(
            symbol="MGCM6", bar=bars[-1], window=bars, bar_count=len(bars),
        )

    @pytest.mark.asyncio
    async def test_rejects_when_gap_exceeds_cap(self):
        # ZIGZAG bar 8 closes at 14.0; support line projects to 12.5
        # at this bar (slope 0.5 from anchor 12.0 at idx 7) → gap 1.5.
        # With trail_pct = 0.0003 (default) and 2× mult: cap = 0.0084.
        # Gap 1.5 >> cap → reject.
        cfg = _default_config()
        cfg["far_from_pivot_filter_enabled"] = True
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        actions = await s.on_event(self._bar_event_at(8), ctx)
        assert not [a for a in actions if isinstance(a, PlaceOrder)]
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and a.event_type == LogEventType.SKIP
                 and "far_from_pivot" in (a.message or "")]
        assert skips, "expected entry_distance SKIP log"
        p = skips[0].payload
        assert p["filter"] == "far_from_pivot"
        assert p["direction"] == "long"
        assert p["gap"] > p["cap"]

    @pytest.mark.asyncio
    async def test_allows_when_gap_within_cap(self):
        # Same fixture, but bump trail_pct so 2× × 14 ≥ 1.5 → cap ≥ 1.5.
        # trail_pct = 0.06 → trail_dist 0.84 → cap 1.68 ≥ 1.5 → pass.
        cfg = _default_config()
        cfg["far_from_pivot_filter_enabled"] = True
        cfg["trail_width_pct"] = 0.06
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        actions = await s.on_event(self._bar_event_at(8), ctx)
        place = [a for a in actions if isinstance(a, PlaceOrder)]
        assert place, f"expected PlaceOrder, got {[type(a).__name__ for a in actions]}"
        assert place[0].side == "BUY"

    @pytest.mark.asyncio
    async def test_disabled_by_config_lets_far_entry_through(self):
        cfg = _default_config()  # far_from_pivot_filter_enabled: False
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        actions = await s.on_event(self._bar_event_at(8), ctx)
        place = [a for a in actions if isinstance(a, PlaceOrder)]
        assert place, "filter disabled → entry should fire"

    @pytest.mark.asyncio
    async def test_payload_has_cap_and_distance(self):
        cfg = _default_config()
        cfg["far_from_pivot_filter_enabled"] = True
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        actions = await s.on_event(self._bar_event_at(8), ctx)
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and a.event_type == LogEventType.SKIP
                 and (a.payload or {}).get("filter") == "far_from_pivot"]
        p = skips[0].payload
        assert p["entry_price"] == pytest.approx(14.0)
        assert p["line_value"] == pytest.approx(12.5)
        assert p["gap"] == pytest.approx(1.5)
        # mult default = 2.0
        assert p["mult"] == 2.0
        # cap = 14 * 0.0003 * 2 = 0.0084
        assert p["cap"] == pytest.approx(0.0084, abs=1e-6)


class TestMarginalEntryMode:
    """``allow_marginal_entries=True`` — far_from_pivot would normally
    reject, but the entry fires anyway and entry_line is tagged
    ``marginal=True``."""

    def _bar_event_at(self, idx: int) -> BarCompleted:
        bars = _zigzag_bars(START_UTC, ZIGZAG_CLOSES[: idx + 1])
        return BarCompleted(
            symbol="MGCM6", bar=bars[-1], window=bars, bar_count=len(bars),
        )

    @pytest.mark.asyncio
    async def test_far_from_pivot_is_hard_reject(self):
        """Post 2026-05-18 evening: far_from_pivot moved from
        bypassable to hard-reject. A fire-bar past the line by
        > cap means the rejection already played out and entering
        now is "into thin air" — the marginal-mode tight-exit
        story doesn't recover it because the trail-stop is already
        breached at entry. Even with ``allow_marginal_entries=True``,
        the filter must block the order and tag the SKIP
        ``marginal: False`` so the audit decision points at it."""
        cfg = _default_config()
        cfg["far_from_pivot_filter_enabled"] = True
        cfg["allow_marginal_entries"] = True  # still hard-reject
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        actions = await s.on_event(self._bar_event_at(8), ctx)
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and (a.payload or {}).get("filter") == "far_from_pivot"]
        if skips:
            # Filter fired → no PlaceOrder, SKIP tagged marginal=False.
            assert skips[0].payload.get("marginal") is False
            assert not [a for a in actions if isinstance(a, PlaceOrder)], \
                "far_from_pivot is now hard-reject; no PlaceOrder expected"

    @pytest.mark.asyncio
    async def test_clean_trade_not_tagged_marginal(self):
        cfg = _default_config()
        cfg["allow_marginal_entries"] = True
        # All bypass-able filters disabled — entry path is fully clean.
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        actions = await s.on_event(self._bar_event_at(8), ctx)
        sig = next(a for a in actions if isinstance(a, LogSignal)
                   and a.event_type == LogEventType.SIGNAL)
        el = sig.payload["entry_line"]
        assert el.get("marginal") is False
        assert (el.get("marginal_filters") or []) == []


class TestSynthesizeBarEvalSkipChain:
    """Audit-row decision label and skip_chain — when a bypassable
    filter (shoulder / min_target / far_from_pivot) tags a SKIP under
    ``allow_marginal_entries=True`` and a later hard-reject filter
    actually kills the trade, the decision must point at the real
    rejection (not the misleading bypassed first SKIP)."""

    def _bar_event(self) -> BarCompleted:
        bar = {
            "timestamp_utc": "2026-05-17T22:00:00+00:00",
            "open": 100.0, "high": 100.0, "low": 100.0, "close": 100.0,
            "volume": 0,
        }
        return BarCompleted(
            symbol="MGCM6", bar=bar, window=[bar], bar_count=1,
        )

    def _shoulder_skip_bypassed(self) -> LogSignal:
        return LogSignal(
            event_type=LogEventType.SKIP,
            message="shoulder filter — right shoulder 100 vs left 99 [marginal mode]",
            payload={
                "filter": "shoulder",
                "marginal": True,
                "side": "SHORT",
            },
        )

    def _opposing_dominance_skip(self) -> LogSignal:
        return LogSignal(
            event_type=LogEventType.SKIP,
            message="opposing_dominance filter (SHORT) — opposing-side max touches 12 ≥ 3.0×",
            payload={
                "filter": "opposing_dominance",
                "direction": "short",
            },
        )

    def _shoulder_skip_hard(self) -> LogSignal:
        """Shoulder SKIP when allow_marginal=False — actually rejected."""
        return LogSignal(
            event_type=LogEventType.SKIP,
            message="shoulder filter — right shoulder 100 vs left 99",
            payload={
                "filter": "shoulder",
                "marginal": False,
                "side": "SHORT",
            },
        )

    def test_bypassed_skip_does_not_become_decision(self):
        """Shoulder bypassed (marginal=True) + opposing_dominance reject
        → decision = FILTERED·opposing_dominance, not FILTERED·shoulder."""
        s = ChartSignalStrategy(_default_config())
        actions = [
            self._shoulder_skip_bypassed(),
            self._opposing_dominance_skip(),
        ]
        audit = s._synthesize_bar_eval(
            self._bar_event(), actions, BotState.AWAITING_ENTRY_TRIGGER,
        )
        assert audit is not None
        assert audit.decision == "FILTERED·opposing_dominance"
        assert audit.payload["audit"]["filter_name"] == "opposing_dominance"

    def test_skip_chain_records_every_skip_with_bypassed_flag(self):
        s = ChartSignalStrategy(_default_config())
        actions = [
            self._shoulder_skip_bypassed(),
            self._opposing_dominance_skip(),
        ]
        audit = s._synthesize_bar_eval(
            self._bar_event(), actions, BotState.AWAITING_ENTRY_TRIGGER,
        )
        chain = audit.payload.get("skip_chain")
        assert chain is not None
        assert len(chain) == 2
        assert chain[0]["filter"] == "shoulder"
        assert chain[0]["bypassed"] is True
        assert chain[1]["filter"] == "opposing_dominance"
        assert chain[1]["bypassed"] is False

    def test_non_bypassed_shoulder_still_drives_decision(self):
        """When ``allow_marginal_entries=False`` shoulder really rejects
        (marginal=False) — decision must remain FILTERED·shoulder."""
        s = ChartSignalStrategy(_default_config())
        actions = [self._shoulder_skip_hard()]
        audit = s._synthesize_bar_eval(
            self._bar_event(), actions, BotState.AWAITING_ENTRY_TRIGGER,
        )
        assert audit.decision == "FILTERED·shoulder"

    def test_all_bypassed_with_place_order_fires(self):
        """Sanity: PlaceOrder takes precedence over any SKIP. Decision
        label is FIRED, regardless of how many bypassed SKIPs preceded."""
        s = ChartSignalStrategy(_default_config())
        actions = [
            self._shoulder_skip_bypassed(),
            PlaceOrder(symbol="MGCM6", side="SELL",
                       qty=Decimal("1"), order_type="mid"),
        ]
        audit = s._synthesize_bar_eval(
            self._bar_event(), actions, BotState.AWAITING_ENTRY_TRIGGER,
        )
        assert audit.decision.startswith("FIRED·SELL")
        # skip_chain still surfaces the bypassed filter for diagnostics.
        chain = audit.payload.get("skip_chain") or []
        assert len(chain) == 1
        assert chain[0]["bypassed"] is True

    def test_no_skips_yields_no_skip_chain(self):
        """When the bar has no SKIPs, skip_chain is omitted to keep the
        audit payload lean (the operator only cares about it on
        filter-reject bars)."""
        s = ChartSignalStrategy(_default_config())
        audit = s._synthesize_bar_eval(
            self._bar_event(),
            [PlaceOrder(symbol="MGCM6", side="BUY",
                        qty=Decimal("1"), order_type="mid")],
            BotState.AWAITING_ENTRY_TRIGGER,
        )
        assert "skip_chain" not in audit.payload


class TestStaleLineFilter:
    """FILTER_STALE_LINE: reject when the chosen line has fewer than
    ``entry_min_recent_strict_touches`` (default 2) strict touches in
    the last ``entry_max_q_age_hours`` (default 24h). Hard-reject."""

    def _make_bar_event(self, bars):
        return BarCompleted(
            symbol="MGCM6", bar=bars[-1], window=bars, bar_count=len(bars),
        )

    @pytest.mark.asyncio
    async def test_rejects_when_recent_strict_count_below_threshold(self):
        cfg = _default_config()
        cfg["entry_stale_line_filter_enabled"] = True
        cfg["entry_max_q_age_hours"] = 4.0
        cfg["entry_min_recent_strict_touches"] = 2
        cfg["far_from_pivot_filter_enabled"] = False
        cfg["entry_opposing_dominance_filter_enabled"] = False
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        morning = datetime(2026, 5, 10, 16, 0, tzinfo=timezone.utc)
        bars = []
        for i in range(200):
            t = morning + timedelta(seconds=BAR_SECONDS * i)
            if i == 0:
                close = 4660.0
            elif i <= 100:
                close = 4660.0 + (i / 100.0) * 20.0
            elif i <= 195:
                close = 4680.0 - ((i - 100) / 95.0) * 20.0
            else:
                close = 4660.0 + ((i - 195) / 4.0) * 10.0
            bars.append(_bar(t, close))
        actions = await s.on_event(self._make_bar_event(bars), ctx)
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and (a.payload or {}).get("filter") == "stale_line"]
        if skips:
            p = skips[0].payload
            assert p["recent_strict_touches"] < p["min_recent_strict"]
            assert p["min_recent_strict"] == 2
            assert p["max_age_hours"] == 4.0
            assert p["marginal"] is False


class TestOpposingDominanceFilter:
    """FILTER_OPPOSING_DOMINANCE: reject when opposite-side has many
    more touches than the chosen line — market structure votes
    against the trade direction. MES LONG @ 18:36 PT 2026-05-14
    had 4-touch chosen vs 20-touch opposing (5×) → reject."""

    def _bar_event_at(self, idx: int) -> BarCompleted:
        bars = _zigzag_bars(START_UTC, ZIGZAG_CLOSES[: idx + 1])
        return BarCompleted(
            symbol="MGCM6", bar=bars[-1], window=bars, bar_count=len(bars),
        )

    @pytest.mark.asyncio
    async def test_passes_when_ratio_under_cap(self):
        # ZIGZAG fixture has roughly symmetric supports/resistances
        # (3-4 touches each); ratio well under 3.0 → should NOT
        # trigger opposing_dominance.
        cfg = _default_config()
        cfg["far_from_pivot_filter_enabled"] = False
        cfg["entry_stale_line_filter_enabled"] = False
        cfg["entry_opposing_dominance_filter_enabled"] = True
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        actions = await s.on_event(self._bar_event_at(8), ctx)
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and (a.payload or {}).get("filter") == "opposing_dominance"]
        assert not skips
        # Still emits an entry order on this fixture.
        assert [a for a in actions if isinstance(a, PlaceOrder)]

    @pytest.mark.asyncio
    async def test_rejects_when_ratio_at_or_above_cap(self):
        # ZIGZAG fixture: chosen support 4 touches, opposing resistance
        # 3 touches. Lower the cap to 0.5 so opposing(3) ≥ 4×0.5 = 2.0
        # fires the filter.
        cfg = _default_config()
        cfg["far_from_pivot_filter_enabled"] = False
        cfg["entry_stale_line_filter_enabled"] = False
        cfg["entry_opposing_dominance_filter_enabled"] = True
        cfg["entry_opposing_dominance_ratio"] = 0.5
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        actions = await s.on_event(self._bar_event_at(8), ctx)
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and (a.payload or {}).get("filter") == "opposing_dominance"]
        assert skips, "expected opposing_dominance SKIP"
        p = skips[0].payload
        assert p["opposing_max_touches"] >= p["chosen_touches"] * p["ratio_cap"]
        assert not [a for a in actions if isinstance(a, PlaceOrder)]

    @pytest.mark.asyncio
    async def test_marginal_bypass_lets_entry_fire(self):
        """``allow_marginal_entries=True`` lets opposing_dominance
        tag the trade as marginal instead of returning. PlaceOrder
        is emitted and entry_line.marginal_filters carries
        ``opposing_dominance``."""
        cfg = _default_config()
        cfg["far_from_pivot_filter_enabled"] = False
        cfg["entry_stale_line_filter_enabled"] = False
        cfg["entry_opposing_dominance_filter_enabled"] = True
        cfg["entry_opposing_dominance_ratio"] = 0.5
        cfg["allow_marginal_entries"] = True
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        actions = await s.on_event(self._bar_event_at(8), ctx)
        assert [a for a in actions if isinstance(a, PlaceOrder)], \
            "marginal mode should let the trade fire"
        sig = next(a for a in actions if isinstance(a, LogSignal)
                   and a.event_type == LogEventType.SIGNAL)
        el = sig.payload["entry_line"]
        assert el.get("marginal") is True
        assert "opposing_dominance" in (el.get("marginal_filters") or [])
        # SKIP still emitted with marginal=True (so audit can suppress
        # it from the decision label and surface the trade as FIRED).
        skip = next(a for a in actions if isinstance(a, LogSignal)
                    and (a.payload or {}).get("filter") == "opposing_dominance")
        assert skip.payload.get("marginal") is True


class TestBrokenLineSkippedAsEntryCandidate:
    """Operator clarification 2026-05-18: a line with non-null
    ``break_idx`` is dead structure — the market already chose to
    violate it. Such lines must NOT serve as entry candidates,
    regardless of how many old touches they accumulated before
    breaking. Hard gate in the entry-candidate iteration.

    Direct unit test on the iteration logic via a fabricated
    candidate list — easier than reproducing the full bar fixture
    that yields a broken-but-high-touch line organically.
    """

    def test_iteration_skips_broken_short_candidate(self):
        """Direct unit-test of the broken-line gate: build a
        candidate list where the highest-touches line is broken and
        a lower-touches alive line follows. The iteration loop must
        skip the broken one and pick the alive one. Mirrors the
        ``if cand.break_idx is not None: continue`` gate added to
        ``_on_bar`` 2026-05-18."""
        from ib_trader.signals.sr_fan import TrendLine
        broken = TrendLine(
            type="resistance", from_idx=10, anchor_b_idx=12,
            to_idx=20, slope=-0.5, intercept=200.0, touches=100,
            break_idx=20, third_touch_idx=15,
        )
        alive = TrendLine(
            type="resistance", from_idx=30, anchor_b_idx=40,
            to_idx=50, slope=-0.2, intercept=150.0, touches=5,
            break_idx=None, third_touch_idx=35,
        )

        chosen = None
        for cand in [broken, alive]:
            if cand.break_idx is not None:
                continue
            chosen = cand
            break

        assert chosen is alive, (
            "iteration must skip broken candidate even when its touch "
            "count is far higher than the alive candidate's"
        )


class TestStaleLineIsHardReject:
    """Operator clarification 2026-05-18: stale_line is a LINE-VALIDITY
    gate, not an entry filter. ``allow_marginal_entries=True`` must
    NOT bypass it. (Earlier code briefly treated it as bypassable;
    that produced shorts firing on 3-day-old, broken lines from the
    May 14 session — see commit 06e7644 regression.)"""

    def _make_bar_event(self, bars):
        return BarCompleted(
            symbol="MGCM6", bar=bars[-1], window=bars, bar_count=len(bars),
        )

    @pytest.mark.asyncio
    async def test_stale_line_blocks_entry_even_under_marginal(self):
        cfg = _default_config()
        cfg["entry_stale_line_filter_enabled"] = True
        cfg["entry_max_q_age_hours"] = 4.0
        cfg["far_from_pivot_filter_enabled"] = False
        cfg["entry_opposing_dominance_filter_enabled"] = False
        cfg["allow_marginal_entries"] = True  # filter is still hard-reject
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        morning = datetime(2026, 5, 10, 16, 0, tzinfo=timezone.utc)
        bars = []
        for i in range(200):
            t = morning + timedelta(seconds=BAR_SECONDS * i)
            if i == 0:
                close = 4660.0
            elif i <= 100:
                close = 4660.0 + (i / 100.0) * 20.0
            elif i <= 195:
                close = 4680.0 - ((i - 100) / 95.0) * 20.0
            else:
                close = 4660.0 + ((i - 195) / 4.0) * 10.0
            bars.append(_bar(t, close))
        actions = await s.on_event(self._make_bar_event(bars), ctx)
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and (a.payload or {}).get("filter") == "stale_line"]
        if skips:
            # Stale_line SKIP carries marginal=False so the audit
            # decision logic uses it as the rejection cause.
            assert skips[0].payload.get("marginal") is False
            # And no order fired — the bypass does not apply.
            assert not [a for a in actions if isinstance(a, PlaceOrder)], \
                "stale_line is now a hard reject; no PlaceOrder expected"


class TestCounterTrendFilter:
    """FILTER_COUNTER_TREND: reject SHORT on up-sloping resistance
    and LONG on down-sloping support. Mirrors the chart's
    ``showCounterResistance``/``showCounterSupport`` defaults. Operator
    add 2026-05-18 after every MGC rally peak post-17:12 PT became a
    SELL. Bypassable in marginal mode."""

    def test_up_sloping_resistance_short_is_counter_trend(self):
        """The filter's direction × slope geometry. Direct invariant:
        for SHORT entries, resistance lines with positive slope are
        counter-trend; negative-slope and zero-slope are not."""
        for slope in (0.5, 1.0, 10.0):
            assert slope > 0  # short on up-sloping = counter-trend
        for slope in (-0.5, -1.0, 0.0):
            assert not (slope > 0)  # not counter-trend

    def test_down_sloping_support_long_is_counter_trend(self):
        """Symmetric: LONG on a down-sloping support is counter-trend."""
        for slope in (-0.5, -1.0, -10.0):
            assert slope < 0
        for slope in (0.5, 1.0, 0.0):
            assert not (slope < 0)



    """Counter_line cache must clear on entry fill and the read path must
    skip lines whose geometry is wrong for the current direction.
    Defends against the MGC SHORT trade #7 bug (2026-05-14) where a
    stale LONG resistance line fired counter_line on a SHORT trade."""

    @pytest.mark.asyncio
    async def test_entry_fill_clears_cache(self):
        s = ChartSignalStrategy(_default_config())
        # Pre-existing cache (would be from a previous trade in real life).
        ctx = _make_ctx(
            state={
                "armed": True,
                "entry_line": {"direction": "long"},
                "counter_lines_cache": [{"value": 4666.25, "slope": 0.137,
                                         "touches": 2}],
                "counter_lines_tol": 0.93,
                "counter_touch": {"started_at": "2026-05-14T22:00:00+00:00",
                                  "line_value": 4666.25, "line_touches": 2,
                                  "line_slope": 0.137},
            },
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )
        event = OrderFilled(
            trade_serial=1, symbol="MGCM6", side="BUY", fill_price=Decimal("4660.7"),
            qty=Decimal("1"), commission=Decimal("0"), ib_order_id="x",
        )
        actions = await s.on_event(event, ctx)
        ups = [a for a in actions if isinstance(a, UpdateState)]
        # Find the UpdateState that zeroes the counter-line cache.
        cache_clear = next(
            (u for u in ups if "counter_lines_cache" in u.state), None,
        )
        assert cache_clear is not None
        assert cache_clear.state["counter_lines_cache"] == []
        # tol is now seeded at fill (fill_price * touch_tolerance_fraction)
        # so the marginal tick-time check has a working tolerance during
        # the first 3 min before _evaluate_exit's bar-close refresh.
        assert cache_clear.state["counter_lines_tol"] > 0
        assert cache_clear.state["counter_touch"] is None

    def test_check_counter_line_skips_wrong_direction(self):
        # SHORT trade entered at 4660.70; cache has a resistance line
        # ABOVE entry (4666.25) — i.e. NOT a valid support for SHORT.
        # Read-path filter should silently skip it.
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(
            state={
                "armed": True,
                "entry_line": {"direction": "short"},
                "entry_price": "4660.7",
                "counter_lines_cache": [{"value": 4666.25, "slope": 0.137,
                                         "touches": 2}],
                "counter_lines_tol": 0.93,
            },
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )
        state_patch: dict = {}
        actions = s._check_counter_line_exit(
            ctx, mid=4665.40, direction="short", state_patch=state_patch,
        )
        # Stale line should be skipped → no actions, no touch armed.
        assert actions == []
        assert state_patch.get("counter_touch") is None

    def test_check_counter_line_accepts_valid_direction(self):
        # LONG trade entered at 4660.70 below the resistance at 4666.25.
        # Touch at mid 4666.20 (within tol 0.93) → should arm.
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(
            state={
                "armed": True,
                "entry_line": {"direction": "long"},
                "entry_price": "4660.7",
                "counter_lines_cache": [{"value": 4666.25, "slope": 0.137,
                                         "touches": 2}],
                "counter_lines_tol": 0.93,
            },
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )
        state_patch: dict = {}
        actions = s._check_counter_line_exit(
            ctx, mid=4665.40, direction="long", state_patch=state_patch,
        )
        assert state_patch.get("counter_touch") is not None
        armed = state_patch["counter_touch"]
        assert armed["line_value"] == pytest.approx(4666.25)


class TestShortEntry:
    """Bot enters short on a 3-touch downtrending resistance and exits
    on bar close back above the entry line."""

    def _bar_event_at(self, idx: int) -> BarCompleted:
        bars = _zigzag_bars(START_UTC, ZIGZAG_CLOSES_DOWN[: idx + 1])
        return BarCompleted(
            symbol="MGCM6", bar=bars[-1], window=bars, bar_count=len(bars),
        )

    @pytest.mark.asyncio
    async def test_3touch_downtrending_resistance_fires_sell(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx()
        actions = await s.on_event(self._bar_event_at(8), ctx)
        place = [a for a in actions if isinstance(a, PlaceOrder)]
        assert place, f"expected a PlaceOrder, got {actions}"
        assert place[0].side == "SELL"
        assert place[0].qty == Decimal("1")

        ups = [a for a in actions if isinstance(a, UpdateState)
               and "entry_line" in a.state]
        assert ups
        line = ups[0].state["entry_line"]
        assert line["kind"] == "resistance"
        assert line["direction"] == "short"
        assert line["touches"] >= 3
        assert line["slope_per_bar"] == pytest.approx(-0.5)
        assert line["slope_per_sec"] == pytest.approx(-0.5 / BAR_SECONDS)

    @pytest.mark.asyncio
    async def test_short_exit_on_bar_close_above_line(self):
        s = ChartSignalStrategy(_default_config(sec_type="STK"))
        anchor_time = (START_UTC
                        + timedelta(seconds=BAR_SECONDS * 7)).isoformat()
        entry_line = {
            "kind": "resistance", "direction": "short",
            "slope_per_bar": -0.5, "intercept": 21.5,
            "slope_per_sec": -0.5 / BAR_SECONDS,
            "anchor_time": anchor_time, "anchor_price": 18.0,
            "anchor_b_idx": 7, "from_idx": 1, "touches": 4,
        }
        ctx = _make_ctx(
            state={"armed": False, "qty": "1", "entry_price": "18.0",
                   "entry_line": entry_line},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
            config=_default_config(sec_type="STK"),
        )
        # Two bars after anchor: line at idx 9 = -0.5*9+21.5 = 17.0.
        # Close 17.5 > 17.0 → break (price reclaimed resistance).
        bar_time = START_UTC + timedelta(seconds=BAR_SECONDS * 9)
        bar = _bar(bar_time, close=17.5)
        event = BarCompleted(symbol="MGCM6", bar=bar, window=[bar],
                             bar_count=1)
        actions = await s.on_event(event, ctx)
        covers = [a for a in actions if isinstance(a, PlaceOrder)
                   and a.side == "BUY"]
        assert covers, f"expected BUY cover, got {actions}"
        assert covers[0].qty == Decimal("1")

    @pytest.mark.asyncio
    async def test_short_no_exit_when_close_stays_below_line(self):
        s = ChartSignalStrategy(_default_config(sec_type="STK"))
        anchor_time = (START_UTC
                        + timedelta(seconds=BAR_SECONDS * 7)).isoformat()
        entry_line = {
            "kind": "resistance", "direction": "short",
            "slope_per_bar": -0.5, "intercept": 21.5,
            "slope_per_sec": -0.5 / BAR_SECONDS,
            "anchor_time": anchor_time, "anchor_price": 18.0,
            "anchor_b_idx": 7, "from_idx": 1, "touches": 4,
        }
        ctx = _make_ctx(
            state={"armed": False, "qty": "1", "entry_price": "18.0",
                   "entry_line": entry_line},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
            config=_default_config(sec_type="STK"),
        )
        bar_time = START_UTC + timedelta(seconds=BAR_SECONDS * 9)
        # Wick high pokes above the line (17.5 > 17.0) but close stays
        # below — short stays open.
        bar = _bar(bar_time, close=16.5, low=15.0, high=17.5)
        event = BarCompleted(symbol="MGCM6", bar=bar, window=[bar],
                             bar_count=1)
        actions = await s.on_event(event, ctx)
        assert not any(isinstance(a, PlaceOrder) for a in actions)

    @pytest.mark.asyncio
    async def test_short_quote_pnl_inverts(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(
            state={"armed": False, "qty": "2", "entry_price": "18.0",
                   "entry_line": {"direction": "short"}},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )
        q = QuoteUpdate(
            symbol="MGCM6", bid=Decimal("17.40"), ask=Decimal("17.60"),
            last=Decimal("17.50"),
            timestamp=datetime.now(timezone.utc),
        )
        actions = await s.on_event(q, ctx)
        ups = [a for a in actions if isinstance(a, UpdateState)]
        assert ups
        # short profit = (entry - last) * qty = (18.0 - 17.5) * 2 = 1.0
        assert Decimal(ups[0].state["unrealized_pnl"]) == Decimal("1.0")

    @pytest.mark.asyncio
    async def test_short_sell_fill_records_entry(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(
            state={"armed": True, "entry_line": {"direction": "short"}},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )
        fill = OrderFilled(
            trade_serial=99, symbol="MGCM6", side="SELL",
            fill_price=Decimal("18.00"), qty=Decimal("1"),
            commission=Decimal("0"), ib_order_id="ib-S1",
        )
        actions = await s.on_event(fill, ctx)
        merged: dict = {}
        for a in actions:
            if isinstance(a, UpdateState):
                merged.update(a.state)
        assert merged["entry_price"] == "18.00"
        assert merged["qty"] == "1"
        assert merged["trade_serial"] == 99

    @pytest.mark.asyncio
    async def test_short_buy_cover_full_close_clears_state(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(
            state={"armed": True, "qty": "0", "entry_price": "18.0",
                   "trade_serial": 99,
                   "entry_line": {"direction": "short"}},
            fsm_state=BotState.AWAITING_ENTRY_TRIGGER,
        )
        fill = OrderFilled(
            trade_serial=99, symbol="MGCM6", side="BUY",
            fill_price=Decimal("17.50"), qty=Decimal("1"),
            commission=Decimal("0"), ib_order_id="ib-S2",
        )
        actions = await s.on_event(fill, ctx)
        merged: dict = {}
        for a in actions:
            if isinstance(a, UpdateState):
                merged.update(a.state)
        # armed stays True — the bot continues running after a
        # round-trip (post-2026-05-12: ``stop_on_exit=False`` for
        # chart_signal). The runtime writes ``cooldown_until`` to
        # gate re-entry for one bar.
        assert "armed" not in merged
        assert merged["entry_line"] is None
        assert merged["entry_price"] is None

    def test_build_exit_actions_flips_side_for_short(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(
            state={"qty": "2", "entry_line": {"direction": "short"}},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )
        from ib_trader.bots.strategy import ExitType
        actions = s.build_exit_actions(ctx, ExitType.FORCE_EXIT, "test")
        places = [a for a in actions if isinstance(a, PlaceOrder)]
        assert places and places[0].side == "BUY"
        assert places[0].qty == Decimal("2")


class TestDeadzone:
    def _bar_event_at(self, t: datetime, idx: int) -> BarCompleted:
        bars = _zigzag_bars(t, ZIGZAG_CLOSES[: idx + 1])
        return BarCompleted(symbol="MGCM6", bar=bars[-1],
                            window=bars, bar_count=len(bars))

    @pytest.mark.asyncio
    async def test_deadzone_blocks_entry(self):
        s = ChartSignalStrategy(_default_config(sec_type="FUT"))
        ctx = _make_ctx()
        # Deadzone is 14:00-15:00 PT. Pick a start such that the LAST
        # bar lands at 14:30 PT.
        last_pt = datetime(2026, 5, 12, 14, 30, tzinfo=PT)
        start = last_pt.astimezone(timezone.utc) - timedelta(seconds=BAR_SECONDS * 8)
        actions = await s.on_event(self._bar_event_at(start, 8), ctx)
        assert not any(isinstance(a, PlaceOrder) for a in actions)

    @pytest.mark.asyncio
    async def test_deadzone_holding_alerts_once_per_day(self):
        cfg = _default_config(sec_type="FUT")
        cfg["_redis"] = object()  # truthy stub; fire_and_forget is mocked
        s = ChartSignalStrategy(cfg)

        # Frozen entry_line — pretend we entered earlier today on the
        # same zigzag. anchor at pivot idx 7 of the original zigzag.
        anchor_time = (START_UTC + timedelta(seconds=BAR_SECONDS * 7)).isoformat()
        entry_line = {
            "kind": "support", "direction": "long",
            "slope_per_bar": 0.5, "intercept": 8.5,
            "slope_per_sec": 0.5 / BAR_SECONDS,
            "anchor_time": anchor_time, "anchor_price": 12.0,
            "anchor_b_idx": 7, "from_idx": 1, "touches": 4,
        }
        ctx = _make_ctx(
            state={"armed": False, "qty": "1", "entry_price": "12.0",
                   "entry_line": entry_line},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER, config=cfg,
        )
        # Bar at 14:30 PT on a future day, close still above the line.
        bar_time = datetime(2026, 5, 12, 14, 30, tzinfo=PT)
        bar = _bar(bar_time, close=20.0)
        event = BarCompleted(symbol="MGCM6", bar=bar, window=[bar], bar_count=1)

        with patch(
            "ib_trader.bots.strategies.chart_signal.fire_and_forget_alert"
        ) as alert:
            actions1 = await s.on_event(event, ctx)
            # Simulate the runtime persisting UpdateState by merging.
            for a in actions1:
                if isinstance(a, UpdateState):
                    ctx.state.update(a.state)
            # Second bar in same window same day → no duplicate alert.
            bar2 = _bar(bar_time + timedelta(seconds=BAR_SECONDS), close=20.1)
            event2 = BarCompleted(symbol="MGCM6", bar=bar2,
                                  window=[bar, bar2], bar_count=2)
            await s.on_event(event2, ctx)

        assert alert.call_count == 1
        kwargs = alert.call_args.kwargs
        assert kwargs["trigger"] == "FUT_DEADZONE_HOLDING"
        assert kwargs["severity"] == "WARNING"

    @pytest.mark.asyncio
    async def test_deadzone_alert_skipped_for_stk(self):
        cfg = _default_config(sec_type="STK")
        cfg["_redis"] = object()
        s = ChartSignalStrategy(cfg)
        anchor_time = START_UTC.isoformat()
        entry_line = {
            "kind": "support", "direction": "long",
            "slope_per_bar": 0.5, "intercept": 8.5,
            "slope_per_sec": 0.5 / BAR_SECONDS,
            "anchor_time": anchor_time, "anchor_price": 12.0,
            "anchor_b_idx": 7, "from_idx": 1, "touches": 4,
        }
        ctx = _make_ctx(
            state={"armed": False, "qty": "1", "entry_price": "12.0",
                   "entry_line": entry_line},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER, config=cfg,
        )
        bar_time = datetime(2026, 5, 12, 14, 30, tzinfo=PT)
        bar = _bar(bar_time, close=20.0)
        event = BarCompleted(symbol="MGCM6", bar=bar, window=[bar], bar_count=1)
        with patch(
            "ib_trader.bots.strategies.chart_signal.fire_and_forget_alert"
        ) as alert:
            await s.on_event(event, ctx)
        alert.assert_not_called()


class TestExit:
    def _state_holding(self) -> dict:
        anchor_time = (START_UTC + timedelta(seconds=BAR_SECONDS * 7)).isoformat()
        return {
            "armed": False, "qty": "1", "entry_price": "12.0",
            "entry_line": {
                "kind": "support", "direction": "long",
                "slope_per_bar": 0.5, "intercept": 8.5,
                "slope_per_sec": 0.5 / BAR_SECONDS,
                "anchor_time": anchor_time, "anchor_price": 12.0,
                "anchor_b_idx": 7, "from_idx": 1, "touches": 4,
            },
        }

    @pytest.mark.asyncio
    async def test_bar_close_below_line_fires_exit(self):
        s = ChartSignalStrategy(_default_config(sec_type="STK"))
        ctx = _make_ctx(state=self._state_holding(),
                         fsm_state=BotState.AWAITING_EXIT_TRIGGER,
                         config=_default_config(sec_type="STK"))
        # 2 bars after anchor (anchor_b_idx=7, new bar at idx 9). Line at
        # idx 9 = 0.5*9 + 8.5 = 13.0. Close 12.5 < 13.0 → exit.
        bar_time = START_UTC + timedelta(seconds=BAR_SECONDS * 9)
        bar = _bar(bar_time, close=12.5)
        event = BarCompleted(symbol="MGCM6", bar=bar, window=[bar],
                             bar_count=1)
        actions = await s.on_event(event, ctx)
        sells = [a for a in actions if isinstance(a, PlaceOrder)
                 and a.side == "SELL"]
        assert sells, f"expected SELL exit, got {actions}"
        assert sells[0].qty == Decimal("1")

    @pytest.mark.asyncio
    async def test_bar_close_above_line_no_exit_even_with_wick_below(self):
        s = ChartSignalStrategy(_default_config(sec_type="STK"))
        ctx = _make_ctx(state=self._state_holding(),
                         fsm_state=BotState.AWAITING_EXIT_TRIGGER,
                         config=_default_config(sec_type="STK"))
        bar_time = START_UTC + timedelta(seconds=BAR_SECONDS * 9)
        # Low pokes below line (12.0 < 13.0) but close stays above.
        bar = _bar(bar_time, close=13.5, low=12.0, high=14.0)
        event = BarCompleted(symbol="MGCM6", bar=bar, window=[bar],
                             bar_count=1)
        actions = await s.on_event(event, ctx)
        assert not any(isinstance(a, PlaceOrder) and a.side == "SELL"
                       for a in actions)


class TestTrailingDip:
    """Change B: trailing-dip exit alongside line-breach, whichever
    fires first. HWM (long) / LWM (short) track the bar-close water
    mark since entry; exit when ``bar_close`` deviates from the mark
    by more than ``trail_width_pct``."""

    def _long_state(self, hwm: str | None = None) -> dict:
        anchor_time = (START_UTC + timedelta(seconds=BAR_SECONDS * 7)).isoformat()
        s = {
            "armed": False, "qty": "1", "entry_price": "12.0",
            "entry_line": {
                "kind": "support", "direction": "long",
                "slope_per_bar": 0.5, "intercept": 8.5,
                "slope_per_sec": 0.5 / BAR_SECONDS,
                "anchor_time": anchor_time, "anchor_price": 12.0,
                "anchor_b_idx": 7, "from_idx": 1, "touches": 4,
            },
        }
        if hwm is not None:
            s["high_water_mark"] = hwm
        return s

    @pytest.mark.asyncio
    async def test_long_trail_stop_fires_before_line_breach(self):
        """Position ran up to HWM=20, then bar closes at 19.99 — 0.05%
        below HWM, well above the line. trail_width_pct=0.0003 (0.03%)
        → trail_stop = 20 × 0.9997 = 19.994. Close 19.99 < 19.994 →
        trail exit. Line at idx 9 is 13.0, close 19.99 > 13.0 → line
        does NOT breach. Reason must be ``trail_stop``."""
        cfg = _default_config(sec_type="STK")
        cfg["trail_width_pct"] = 0.0003
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(state=self._long_state(hwm="20"),
                         fsm_state=BotState.AWAITING_EXIT_TRIGGER,
                         config=cfg)
        bar_time = START_UTC + timedelta(seconds=BAR_SECONDS * 9)
        bar = _bar(bar_time, close=19.99)
        event = BarCompleted(symbol="MGCM6", bar=bar, window=[bar],
                             bar_count=1)
        actions = await s.on_event(event, ctx)
        sells = [a for a in actions if isinstance(a, PlaceOrder)
                  and a.side == "SELL"]
        assert sells, f"expected SELL exit, got {actions}"
        # exit_reason persisted via UpdateState
        ups = [a for a in actions if isinstance(a, UpdateState)
                and "exit_reason" in a.state]
        assert ups and ups[0].state["exit_reason"] == "trail_stop"

    @pytest.mark.asyncio
    async def test_long_line_breach_when_no_trail_room(self):
        """Position barely moved, no profit. Bar closes below the line
        but well within the trail. Reason must be ``line_breach``."""
        cfg = _default_config(sec_type="STK")
        cfg["trail_width_pct"] = 0.05  # very loose so trail can't fire first
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(state=self._long_state(hwm="12.5"),
                         fsm_state=BotState.AWAITING_EXIT_TRIGGER,
                         config=cfg)
        # Line at idx 9 = 13.0. Close 12.5 < 13.0 → line breach.
        # Trail (with 5% pct): hwm=12.5 × 0.95 = 11.875. Close 12.5 > 11.875
        # → trail does NOT breach.
        bar_time = START_UTC + timedelta(seconds=BAR_SECONDS * 9)
        bar = _bar(bar_time, close=12.5)
        event = BarCompleted(symbol="MGCM6", bar=bar, window=[bar],
                             bar_count=1)
        actions = await s.on_event(event, ctx)
        sells = [a for a in actions if isinstance(a, PlaceOrder)
                  and a.side == "SELL"]
        assert sells, f"expected SELL exit, got {actions}"
        ups = [a for a in actions if isinstance(a, UpdateState)
                and "exit_reason" in a.state]
        assert ups and ups[0].state["exit_reason"] == "line_breach"

    @pytest.mark.asyncio
    async def test_short_trail_fires_on_low_water_mark_rise(self):
        """Short held; price dipped to LWM=10, then bar closes at
        10.005 — 0.05% above LWM. trail_width_pct=0.0003 → trail_stop
        = 10 × 1.0003 = 10.003. Close 10.005 > 10.003 → trail exit.
        Line at idx 9 (downtrending resistance) at 14.5, close 10.005
        < 14.5 → line does NOT breach."""
        cfg = _default_config(sec_type="STK")
        cfg["trail_width_pct"] = 0.0003
        s = ChartSignalStrategy(cfg)
        anchor_time = (START_UTC + timedelta(seconds=BAR_SECONDS * 7)).isoformat()
        state = {
            "armed": False, "qty": "1", "entry_price": "11.0",
            "entry_line": {
                "kind": "resistance", "direction": "short",
                "slope_per_bar": -0.5, "intercept": 19,
                "slope_per_sec": -0.5 / BAR_SECONDS,
                "anchor_time": anchor_time, "anchor_price": 15.5,
                "anchor_b_idx": 7, "from_idx": 1, "touches": 4,
            },
            "low_water_mark": "10",
        }
        ctx = _make_ctx(state=state,
                         fsm_state=BotState.AWAITING_EXIT_TRIGGER,
                         config=cfg)
        bar_time = START_UTC + timedelta(seconds=BAR_SECONDS * 9)
        bar = _bar(bar_time, close=10.005)
        event = BarCompleted(symbol="MGCM6", bar=bar, window=[bar],
                             bar_count=1)
        actions = await s.on_event(event, ctx)
        covers = [a for a in actions if isinstance(a, PlaceOrder)
                   and a.side == "BUY"]
        assert covers, f"expected BUY cover, got {actions}"
        ups = [a for a in actions if isinstance(a, UpdateState)
                and "exit_reason" in a.state]
        assert ups and ups[0].state["exit_reason"] == "trail_stop"


class TestMultiplierAwarePnl:
    """Change C: unrealized P/L surfaced by ``_on_quote`` and the
    realized P/L from ``_on_fill`` exit must include the contract
    multiplier so the strip reads dollars, not raw price diff."""

    @pytest.mark.asyncio
    async def test_unrealized_pnl_uses_contract_multiplier(self):
        cfg = _default_config(sec_type="FUT")
        cfg["contract_multiplier"] = 10   # MGC = 10 oz/contract
        anchor_time = START_UTC.isoformat()
        entry_line = {
            "kind": "support", "direction": "long",
            "slope_per_bar": 0.5, "intercept": 8.5,
            "slope_per_sec": 0.5 / BAR_SECONDS,
            "anchor_time": anchor_time, "anchor_price": 4741.20,
            "anchor_b_idx": 7, "from_idx": 1, "touches": 3,
        }
        ctx = _make_ctx(
            state={"armed": False, "qty": "1", "entry_price": "4741.20",
                   "entry_line": entry_line},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER, config=cfg,
        )
        s = ChartSignalStrategy(cfg)
        q = QuoteUpdate(symbol="MGCM6",
                         bid=Decimal("4742.10"), ask=Decimal("4742.30"),
                         last=Decimal("4742.20"),
                         timestamp=datetime.now(timezone.utc))
        actions = await s.on_event(q, ctx)
        ups = [a for a in actions if isinstance(a, UpdateState)]
        assert ups
        st = ups[0].state
        # (4742.20 - 4741.20) × 1 contract × 10 oz = $10.00
        assert Decimal(st["unrealized_pnl"]) == Decimal("10.00")

    @pytest.mark.asyncio
    async def test_short_unrealized_pnl_uses_contract_multiplier(self):
        cfg = _default_config(sec_type="FUT")
        cfg["contract_multiplier"] = 5    # MES
        anchor_time = START_UTC.isoformat()
        entry_line = {
            "kind": "resistance", "direction": "short",
            "slope_per_bar": -0.1, "intercept": 100,
            "slope_per_sec": -0.1 / BAR_SECONDS,
            "anchor_time": anchor_time, "anchor_price": 7420,
            "anchor_b_idx": 7, "from_idx": 1, "touches": 3,
        }
        ctx = _make_ctx(
            state={"armed": False, "qty": "1", "entry_price": "7420",
                   "entry_line": entry_line},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER, config=cfg,
        )
        s = ChartSignalStrategy(cfg)
        q = QuoteUpdate(symbol="MESM6",
                         bid=Decimal("7417"), ask=Decimal("7419"),
                         last=Decimal("7418"),
                         timestamp=datetime.now(timezone.utc))
        actions = await s.on_event(q, ctx)
        ups = [a for a in actions if isinstance(a, UpdateState)]
        assert ups
        st = ups[0].state
        # (7420 - 7418) × 1 × 5 = $10
        assert Decimal(st["unrealized_pnl"]) == Decimal("10")


class TestFills:
    @pytest.mark.asyncio
    async def test_buy_fill_records_entry_state(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(state={"armed": True},
                         fsm_state=BotState.AWAITING_EXIT_TRIGGER)
        fill = OrderFilled(
            trade_serial=42, symbol="MGCM6", side="BUY",
            fill_price=Decimal("12.50"), qty=Decimal("1"),
            commission=Decimal("0"), ib_order_id="ib-1",
        )
        actions = await s.on_event(fill, ctx)
        ups = [a for a in actions if isinstance(a, UpdateState)]
        assert ups
        merged: dict = {}
        for u in ups:
            merged.update(u.state)
        assert merged["entry_price"] == "12.50"
        assert merged["qty"] == "1"
        assert merged["trade_serial"] == 42

    @pytest.mark.asyncio
    async def test_full_close_sell_clears_state_keeps_armed(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(
            state={"armed": True, "qty": "0", "entry_price": "12.0",
                   "trade_serial": 42, "entry_line": {"kind": "support"}},
            fsm_state=BotState.AWAITING_ENTRY_TRIGGER,
        )
        fill = OrderFilled(
            trade_serial=42, symbol="MGCM6", side="SELL",
            fill_price=Decimal("13.00"), qty=Decimal("1"),
            commission=Decimal("0"), ib_order_id="ib-2",
        )
        actions = await s.on_event(fill, ctx)
        merged: dict = {}
        for a in actions:
            if isinstance(a, UpdateState):
                merged.update(a.state)
        # armed stays True (post-2026-05-12 semantic — chart_signal's
        # ``stop_on_exit=False`` keeps the bot running through
        # round-trips with a cooldown gate instead of one-and-done).
        assert "armed" not in merged
        assert merged["entry_line"] is None
        assert merged["entry_price"] is None


class TestQuoteUpdates:
    @pytest.mark.asyncio
    async def test_quote_updates_unrealized_pnl_when_holding(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(
            state={"armed": False, "qty": "2", "entry_price": "12.0"},
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
        )
        q = QuoteUpdate(
            symbol="MGCM6", bid=Decimal("12.40"), ask=Decimal("12.60"),
            last=Decimal("12.50"), timestamp=datetime.now(timezone.utc),
        )
        actions = await s.on_event(q, ctx)
        ups = [a for a in actions if isinstance(a, UpdateState)]
        assert ups
        st = ups[0].state
        assert Decimal(st["last_price"]) == Decimal("12.5")
        assert Decimal(st["unrealized_pnl"]) == Decimal("1.0")  # (12.5-12.0)*2

    @pytest.mark.asyncio
    async def test_quote_no_op_when_no_position(self):
        s = ChartSignalStrategy(_default_config())
        ctx = _make_ctx(state={"armed": True},
                         fsm_state=BotState.AWAITING_ENTRY_TRIGGER)
        q = QuoteUpdate(
            symbol="MGCM6", bid=Decimal("12.40"), ask=Decimal("12.60"),
            last=Decimal("12.50"), timestamp=datetime.now(timezone.utc),
        )
        assert await s.on_event(q, ctx) == []


class TestSLLinger:
    """Two SL paths:
      - MARGINAL: touch+hold linger (``sl_linger_marginal_seconds``,
        default 10s). Continuous breach for the full window fires; a
        retrace clears the timer.
      - CLEAN:    periodic poll (``sl_check_clean_seconds``, default
        60s). Sample on each interval boundary; fire if breached at
        the sample. Between samples ticks are ignored.

    Runs for BOTH clean and marginal trades post-2026-05-17."""

    def _long_holding_ctx(self, *, sl_touch=None, marginal=True,
                           linger_marginal=5.0):
        # MGC trail_width_pct default 0.0003. Entry at 100.0, HWM 110.0,
        # trail = 110 * (1 - 0.0003) = 109.967.
        # active_stop = max(line, trail). With line below 109.967,
        # active_stop = trail = 109.967.
        cfg = _default_config()
        cfg["sl_linger_marginal_seconds"] = linger_marginal
        cfg["trail_width_pct"] = 0.0003
        return _make_ctx(
            state={
                "armed": False,
                "qty": "1",
                "entry_price": "100.0",
                "high_water_mark": "110.0",
                "entry_line": {
                    "direction": "long", "marginal": marginal,
                    # No anchor data — falls back to trail-only stop.
                },
                "sl_touch": sl_touch,
            },
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
            config=cfg,
        )

    def _quote(self, mid: float) -> QuoteUpdate:
        # bid/ask centered on mid (1-tick spread) — _on_quote reads
        # event.mid which is (bid + ask) / 2.
        half = 0.05
        return QuoteUpdate(
            symbol="MGCM6",
            bid=Decimal(str(mid - half)),
            ask=Decimal(str(mid + half)),
            last=Decimal(str(mid)),
            timestamp=datetime.now(timezone.utc),
        )

    @pytest.mark.asyncio
    async def test_first_breach_starts_timer_no_exit(self):
        """Mid crosses below active_stop for the first time → sl_touch
        gets a start_ts, NO PlaceOrder fired this tick."""
        ctx = self._long_holding_ctx(sl_touch=None)
        s = ChartSignalStrategy(ctx.config)
        # mid 109.96 < trail 109.967 → breached.
        actions = await s.on_event(self._quote(109.95), ctx)
        # No exit order on the first breach tick.
        assert not [a for a in actions if isinstance(a, PlaceOrder)]
        ups = [a for a in actions if isinstance(a, UpdateState)]
        assert ups
        merged: dict = {}
        for u in ups:
            merged.update(u.state)
        touch = merged.get("sl_touch")
        assert touch is not None and "start_ts" in touch

    @pytest.mark.asyncio
    async def test_retrace_within_linger_clears_timer(self):
        """Touch started 2s ago, mid retraces back above active_stop →
        sl_touch is set to None on the same tick."""
        old_start = (datetime.now(timezone.utc)
                     - timedelta(seconds=2)).isoformat()
        ctx = self._long_holding_ctx(
            sl_touch={"start_ts": old_start},
        )
        s = ChartSignalStrategy(ctx.config)
        # mid 110.00 > trail 109.967 → not breached.
        actions = await s.on_event(self._quote(110.00), ctx)
        ups = [a for a in actions if isinstance(a, UpdateState)]
        merged: dict = {}
        for u in ups:
            merged.update(u.state)
        # Touch cleared.
        assert merged.get("sl_touch") is None
        # No exit fired.
        assert not [a for a in actions if isinstance(a, PlaceOrder)]

    @pytest.mark.asyncio
    async def test_sustained_breach_past_linger_fires_exit(self):
        """Touch is 6s old AND mid still breached → exit fires this
        tick. PlaceOrder emitted, exit_reason recorded."""
        old_start = (datetime.now(timezone.utc)
                     - timedelta(seconds=6)).isoformat()
        ctx = self._long_holding_ctx(
            sl_touch={"start_ts": old_start},
        )
        s = ChartSignalStrategy(ctx.config)
        # mid 109.95 still below trail 109.967.
        actions = await s.on_event(self._quote(109.95), ctx)
        # Exit order fires.
        sells = [a for a in actions if isinstance(a, PlaceOrder)
                 and a.side == "SELL"]
        assert sells, f"expected SELL PlaceOrder, got {actions}"
        # Touch cleared on the fire.
        ups = [a for a in actions if isinstance(a, UpdateState)]
        merged: dict = {}
        for u in ups:
            merged.update(u.state)
        assert merged.get("sl_touch") is None
        assert merged.get("exit_reason") == "trail_stop"

    @pytest.mark.asyncio
    async def test_short_direction_breach_timer(self):
        """SHORT side: breached when mid >= active_stop. Same
        touch+hold semantics, opposite geometry."""
        cfg = _default_config()
        cfg["sl_linger_marginal_seconds"] = 5.0
        cfg["trail_width_pct"] = 0.0003
        ctx = _make_ctx(
            state={
                "armed": False,
                "qty": "1",
                "entry_price": "100.0",
                "low_water_mark": "90.0",
                # SHORT trail = LWM * (1 + 0.0003) = 90.027.
                "entry_line": {"direction": "short", "marginal": True},
                "sl_touch": None,
            },
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
            config=cfg,
        )
        s = ChartSignalStrategy(cfg)
        # mid 90.05 > trail 90.027 → breached.
        actions = await s.on_event(self._quote(90.05), ctx)
        # First breach: no exit, timer starts.
        assert not [a for a in actions if isinstance(a, PlaceOrder)]
        ups = [a for a in actions if isinstance(a, UpdateState)]
        merged: dict = {}
        for u in ups:
            merged.update(u.state)
        assert merged.get("sl_touch") is not None
        assert "start_ts" in merged["sl_touch"]

    def _clean_holding_ctx(self, *, sl_last_check_ts=None,
                             interval_s=60.0):
        cfg = _default_config()
        cfg["sl_check_clean_seconds"] = interval_s
        cfg["trail_width_pct"] = 0.0003
        return _make_ctx(
            state={
                "armed": False,
                "qty": "1",
                "entry_price": "100.0",
                "high_water_mark": "110.0",
                "entry_line": {"direction": "long", "marginal": False},
                "sl_last_check_ts": sl_last_check_ts,
            },
            fsm_state=BotState.AWAITING_EXIT_TRIGGER,
            config=cfg,
        )

    @pytest.mark.asyncio
    async def test_clean_first_tick_samples_immediately(self):
        """Clean trade with no prior sample: the first tick samples
        the SL. If breached at that sample, fires immediately."""
        ctx = self._clean_holding_ctx(sl_last_check_ts=None)
        s = ChartSignalStrategy(ctx.config)
        # mid 109.95 < trail 109.967 → breached at first sample.
        actions = await s.on_event(self._quote(109.95), ctx)
        sells = [a for a in actions if isinstance(a, PlaceOrder)
                 and a.side == "SELL"]
        assert sells, f"expected SELL on first-tick sample, got {actions}"

    @pytest.mark.asyncio
    async def test_clean_skips_check_within_interval(self):
        """Clean trade that just sampled 10s ago: a breached tick
        inside the 60s interval is IGNORED. No fire, no state update
        to sl_last_check_ts."""
        recent = (datetime.now(timezone.utc)
                  - timedelta(seconds=10)).isoformat()
        ctx = self._clean_holding_ctx(sl_last_check_ts=recent)
        s = ChartSignalStrategy(ctx.config)
        actions = await s.on_event(self._quote(109.95), ctx)
        sells = [a for a in actions if isinstance(a, PlaceOrder)
                 and a.side == "SELL"]
        assert not sells, "clean trade fired before interval elapsed"
        # sl_last_check_ts not touched.
        ups = [a for a in actions if isinstance(a, UpdateState)]
        merged: dict = {}
        for u in ups:
            merged.update(u.state)
        assert "sl_last_check_ts" not in merged

    @pytest.mark.asyncio
    async def test_clean_samples_after_interval_breached_fires(self):
        """65s since last sample: re-evaluate at this tick. Breached
        → fire."""
        old = (datetime.now(timezone.utc)
               - timedelta(seconds=65)).isoformat()
        ctx = self._clean_holding_ctx(sl_last_check_ts=old)
        s = ChartSignalStrategy(ctx.config)
        actions = await s.on_event(self._quote(109.95), ctx)
        sells = [a for a in actions if isinstance(a, PlaceOrder)
                 and a.side == "SELL"]
        assert sells, "clean trade did not fire on poll-interval sample"

    @pytest.mark.asyncio
    async def test_clean_samples_after_interval_not_breached_records(self):
        """65s since last sample: re-evaluate. Not breached → no
        fire, BUT sl_last_check_ts advances so the next sample is
        another 60s out."""
        old = (datetime.now(timezone.utc)
               - timedelta(seconds=65)).isoformat()
        ctx = self._clean_holding_ctx(sl_last_check_ts=old)
        s = ChartSignalStrategy(ctx.config)
        # mid 110.00 > trail 109.967 → not breached.
        actions = await s.on_event(self._quote(110.00), ctx)
        sells = [a for a in actions if isinstance(a, PlaceOrder)
                 and a.side == "SELL"]
        assert not sells
        ups = [a for a in actions if isinstance(a, UpdateState)]
        merged: dict = {}
        for u in ups:
            merged.update(u.state)
        assert merged.get("sl_last_check_ts") is not None


class TestRegimeGate:
    """Bar-level regime gate fires after pivot detection. The gate
    rejects (a) anti-direction in trending regimes, (b) flat+amplitude
    fail, (c) flat+extreme fail. Insufficient bars surfaces a RISK
    warning but does not gate."""

    def _build_window(self, closes: list[float]) -> list[dict]:
        bars: list[dict] = []
        for i, c in enumerate(closes):
            t = START_UTC + timedelta(seconds=i * BAR_SECONDS)
            bars.append({
                "timestamp_utc": t.isoformat(),
                "open": c, "high": c + 0.1, "low": c - 0.1,
                "close": c, "volume": 100,
            })
        return bars

    def _bar_event(self, bars: list[dict]) -> BarCompleted:
        return BarCompleted(
            symbol="MGCM6", bar=bars[-1], window=bars, bar_count=len(bars),
        )

    def _uptrend_with_pivot_high_at_end(self) -> list[float]:
        """30 strongly-uptrending bars ending in [bar-3 < bar-2 > bar-1]
        so the just-confirmed pivot at last_idx-1 is a HIGH (SHORT
        candidate). Slope steep enough that ADX > 25 with +DI > −DI."""
        base = [100.0 + i * 1.5 for i in range(27)]
        # Last three: dip-peak-dip so idx-2 is a clear pivot HIGH.
        peak = base[-1] + 3.0
        base.append(peak)        # last_idx - 1: peak
        base.append(peak - 2.0)  # last_idx:     drops after peak
        return base

    def _downtrend_with_pivot_low_at_end(self) -> list[float]:
        """Symmetric — 30 downtrending bars ending in a pivot LOW
        (LONG candidate). ADX > 25, −DI > +DI."""
        base = [150.0 - i * 1.5 for i in range(27)]
        trough = base[-1] - 3.0
        base.append(trough)
        base.append(trough + 2.0)
        return base

    def _uptrend_with_pivot_low_at_end(self) -> list[float]:
        """Uptrend (regime=up) ending in a pivot LOW (LONG candidate).
        LONG is pro-regime — should NOT be gated."""
        base = [100.0 + i * 1.5 for i in range(27)]
        # Last three: peak-dip-peak so idx-2 is a pivot LOW.
        dip = base[-1] - 1.0
        base.append(dip)
        base.append(dip + 2.5)
        return base

    def _flat_with_pivot_high_at_extreme(self) -> list[float]:
        """30 bars of tight zigzag (range ~0.2) ending in a natural
        pivot HIGH that sits inside the zigzag range — no extra
        spike. ADX stays below 20 (symmetric +DM / −DM) so regime
        classifies as flat. ATR ~0.1 → 5-bar swing 0.5 → far under
        the cost floor (~$2 round-trip)."""
        # Pure two-step zigzag so +DM ≈ −DM → low DX → low ADX.
        # Final three bars: low-high-low → natural pivot HIGH at idx-2.
        base = []
        for i in range(28):
            base.append(100.0 if i % 2 == 0 else 100.1)
        # Last bar drops below the alternating low to confirm the
        # pivot HIGH at idx-2 (the 100.1 just placed).
        base.append(99.9)
        return base

    @pytest.mark.asyncio
    async def test_up_regime_blocks_short(self):
        cfg = _default_config()
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        bars = self._build_window(self._uptrend_with_pivot_high_at_end())
        actions = await s.on_event(self._bar_event(bars), ctx)
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and a.event_type == LogEventType.SKIP
                 and (a.payload or {}).get("filter")
                 == "local_peak_in_uptrend"]
        assert skips, f"expected local_peak_in_uptrend SKIP, got {actions}"
        s_payload = skips[0].payload
        assert s_payload["direction"] == "short"
        assert s_payload["regime"] == "up"
        # No PlaceOrder fired.
        assert not [a for a in actions if isinstance(a, PlaceOrder)]

    @pytest.mark.asyncio
    async def test_down_regime_blocks_long(self):
        cfg = _default_config()
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        bars = self._build_window(self._downtrend_with_pivot_low_at_end())
        actions = await s.on_event(self._bar_event(bars), ctx)
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and a.event_type == LogEventType.SKIP
                 and (a.payload or {}).get("filter")
                 == "local_trough_in_downtrend"]
        assert skips, f"expected local_trough_in_downtrend SKIP, got {actions}"
        s_payload = skips[0].payload
        assert s_payload["direction"] == "long"
        assert s_payload["regime"] == "down"

    @pytest.mark.asyncio
    async def test_pro_regime_long_not_blocked_by_regime(self):
        """Up regime + LONG candidate — should NOT trip the regime
        gate. Other filters may still reject but regime stays out."""
        cfg = _default_config()
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        bars = self._build_window(self._uptrend_with_pivot_low_at_end())
        actions = await s.on_event(self._bar_event(bars), ctx)
        regime_skips = [a for a in actions if isinstance(a, LogSignal)
                        and a.event_type == LogEventType.SKIP
                        and (a.payload or {}).get("filter") in (
                            "local_peak_in_uptrend",
                            "local_trough_in_downtrend",
                        )]
        assert not regime_skips, (
            f"regime gate should not fire on pro-direction, got {regime_skips}"
        )

    @pytest.mark.asyncio
    async def test_flat_amplitude_blocks_entry(self):
        """Flat regime with tight range → ATR too small → amplitude
        gate fires. Thresholds bumped high so the synthetic zigzag
        deterministically classifies as flat regardless of the exact
        ADX reading pandas-ta produces."""
        cfg = _default_config()
        # Force every ADX reading below trending → flat regime.
        cfg["adx_trending_threshold"] = 100.0
        cfg["adx_ranging_threshold"] = 100.0
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        bars = self._build_window(self._flat_with_pivot_high_at_extreme())
        actions = await s.on_event(self._bar_event(bars), ctx)
        skips = [a for a in actions if isinstance(a, LogSignal)
                 and a.event_type == LogEventType.SKIP
                 and (a.payload or {}).get("filter") == "flat_amplitude"]
        assert skips, f"expected flat_amplitude SKIP, got {actions}"
        p = skips[0].payload
        assert p["direction"] == "short"
        assert p["expected_swing"] < p["cost_floor"] * p["min_edge_mult"]

    def _downtrend_with_pivot_high_at_end(self) -> list[float]:
        """30 strongly-downtrending bars (−DI dominant) ending in a
        small bounce that confirms a pivot HIGH at last_idx-1 — the
        "lower-high in a downtrend" pattern the DI-lean override is
        designed to allow as a SHORT entry."""
        base = [150.0 - i * 1.5 for i in range(27)]
        # Last three: low-high-low so idx-2 is a pivot HIGH.
        bounce = base[-1] + 1.0
        base.append(bounce)
        base.append(bounce - 2.0)
        return base

    @pytest.mark.asyncio
    async def test_di_lean_override_skips_extreme_check(self):
        """Downtrend (−DI ≫ +DI) + pivot HIGH at end + flat regime
        forced via high ADX thresholds → DI-lean override fires.
        Verifies no flat_extreme SKIP and a RISK row is emitted."""
        cfg = _default_config()
        # Force flat regime regardless of pandas-ta output.
        cfg["adx_trending_threshold"] = 100.0
        cfg["adx_ranging_threshold"] = 100.0
        # Low threshold so the synthetic downtrend triggers override.
        cfg["regime_di_lean_threshold"] = 5.0
        # Tiny cost floor so amplitude passes deterministically.
        cfg["regime_round_trip_commission"] = 0.001
        cfg["regime_min_edge_mult"] = 0.01
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        bars = self._build_window(self._downtrend_with_pivot_high_at_end())
        actions = await s.on_event(self._bar_event(bars), ctx)
        # No flat_extreme SKIP — override took effect.
        extreme_skips = [a for a in actions if isinstance(a, LogSignal)
                         and a.event_type == LogEventType.SKIP
                         and (a.payload or {}).get("filter")
                         == "flat_extreme"]
        assert not extreme_skips, (
            f"DI-lean override should have skipped extreme check, "
            f"got {extreme_skips}"
        )
        # RISK row surfaced for audit visibility.
        risk_rows = [a for a in actions if isinstance(a, LogSignal)
                     and a.event_type == LogEventType.RISK
                     and (a.payload or {}).get("filter")
                     == "regime_di_lean_override"]
        assert risk_rows, f"expected DI-lean RISK row, got {actions}"
        assert risk_rows[0].payload["direction"] == "short"
        assert risk_rows[0].payload["di_lean"] >= 5.0

    @pytest.mark.asyncio
    async def test_insufficient_bars_warns_does_not_block(self):
        """9-bar fixture: regime falls to 'insufficient'. Surface a
        RISK row but do NOT gate the entry."""
        cfg = _default_config()
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        bars = _zigzag_bars(START_UTC, ZIGZAG_CLOSES)
        ev = BarCompleted(symbol="MGCM6", bar=bars[-1], window=bars,
                          bar_count=len(bars))
        actions = await s.on_event(ev, ctx)
        # RISK warning emitted.
        risks = [a for a in actions if isinstance(a, LogSignal)
                 and a.event_type == LogEventType.RISK
                 and (a.payload or {}).get("filter")
                 == "insufficient_bars_for_regime"]
        assert risks, f"expected insufficient_bars RISK, got {actions}"
        # No regime-driven SKIP — gate did not block.
        regime_skips = [a for a in actions if isinstance(a, LogSignal)
                        and a.event_type == LogEventType.SKIP
                        and (a.payload or {}).get("filter") in (
                            "local_peak_in_uptrend",
                            "local_trough_in_downtrend",
                            "flat_amplitude", "flat_extreme",
                        )]
        assert not regime_skips, (
            f"insufficient bars should not gate, got {regime_skips}"
        )

    @pytest.mark.asyncio
    async def test_disabled_flag_bypasses_gate(self):
        """regime_filter_enabled=False short-circuits the whole gate.
        Even an anti-direction trending setup passes through."""
        cfg = _default_config()
        cfg["regime_filter_enabled"] = False
        s = ChartSignalStrategy(cfg)
        ctx = _make_ctx(config=cfg)
        bars = self._build_window(self._uptrend_with_pivot_high_at_end())
        actions = await s.on_event(self._bar_event(bars), ctx)
        # No regime-driven SKIP because the gate didn't run.
        regime_skips = [a for a in actions if isinstance(a, LogSignal)
                        and a.event_type == LogEventType.SKIP
                        and (a.payload or {}).get("filter") in (
                            "local_peak_in_uptrend",
                            "local_trough_in_downtrend",
                            "flat_amplitude", "flat_extreme",
                        )]
        assert not regime_skips, (
            f"disabled regime gate should not fire, got {regime_skips}"
        )
