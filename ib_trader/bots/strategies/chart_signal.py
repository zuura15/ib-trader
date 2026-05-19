"""Chart-signal strategy.

Mirrors the frontend chart's 3-touch SR detector: when an uptrending
support line collects three pivot-touches the bot opens a long; the
position closes when a 3-min bar closes through the line. The same
algorithm runs in the frontend (``supportResistance.ts``) and in the
backtest harness (``scripts/backtest/sr_backtest.py``) — the shared
Python implementation lives in ``ib_trader.signals.sr_fan`` and is the
single source of truth.

Operational rules (per the approved plan):

- **Both directions.** Long entry on a 3-touch uptrending support (BUY,
  exit on bar close BELOW the line). Short entry on a 3-touch downtrending
  resistance (SELL, exit on bar close ABOVE the line). If both sides
  qualify on the same bar, the side with more touches wins; ties prefer
  long (the bias the user originally tuned for).
- **One-and-done per arm**. After a completed round-trip the state field
  ``armed`` flips to ``False``. The strategy refuses to enter again until
  ``armed`` is flipped back to ``True`` via the ``/rearm`` HTTP endpoint.
- **Futures deadzone** (14:00-15:00 PT). Entries are blocked inside the
  window; an open FUT/FOP position is **not** auto-flattened, but a
  ``FUT_DEADZONE_HOLDING`` WARNING fires once per day on first
  entry-into-window crossings while a position is open.

Why ignore S signals here even though the chart renders them? The chart
is a discretionary surface — the human can see both sides. The bot only
trades the side the runtime currently supports cleanly. The plan calls
this out explicitly; a future PR can lift the short path.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategy import (
    Action,
    BarCompleted,
    EmitAudit,
    ExitType,
    LogEventType,
    LogSignal,
    MarketEvent,
    OrderFilled,
    OrderRejected,
    PlaceOrder,
    QuoteUpdate,
    StrategyContext,
    StrategyManifest,
    Subscription,
    UpdateState,
)
from ib_trader.logging_.alerts import fire_and_forget_alert
from ib_trader.signals.sr_fan import (
    MIN_TOUCHES,
    TOUCH_TOLERANCE_FRACTION,
    detect_lines,
    find_pivot_highs,
    find_pivot_lows,
    find_wedges,
    in_futures_deadzone,
)

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------
# Named entry filters. Each constant is the canonical identifier for a
# gate that can independently reject an otherwise-qualifying B/S
# signal. Names appear in skip-log payloads under ``"filter"`` and are
# referenced in operational docs.
#
#   shoulder        — right-shoulder close must beat the left in the
#                     trend direction; rejects "bad shape" pivots.
#   tight_triangle  — most-immediate wedge apex within N bars of the
#                     current bar; wedge resolution imminent in either
#                     direction.
#   min_target      — distance from entry price to the most-immediate
#                     wedge's opposing edge (resistance for LONG,
#                     support for SHORT) must be ≥ stop distance.
# --------------------------------------------------------------------
FILTER_SHOULDER = "shoulder"
FILTER_TIGHT_TRIANGLE = "tight_triangle"
FILTER_MIN_TARGET = "min_target"
FILTER_FAR_FROM_PIVOT = "far_from_pivot"
FILTER_STALE_LINE = "stale_line"
FILTER_OPPOSING_DOMINANCE = "opposing_dominance"
# Inter-touch spacing rule. Rejects the candidate line when the
# new touch lands "right after" the previous touch on a line that
# took much longer to build — a structural sign of tolerance-
# attack rather than real respect (the long line's tol band
# catches near-misses by mathematical coincidence). Operator rule
# 2026-05-18. Line-validity gate, NOT bypassable in marginal mode.
FILTER_INTER_TOUCH_SPACING = "inter_touch_spacing"
# Marginal-bypass cap. When too many bypassable filters would have
# rejected the entry, marginal mode is suppressed — entry rejected
# outright. Operator rule 2026-05-18: 1-2 bypassed = OK; 3+ = too
# much uncertainty, abandon the entry.
FILTER_TOO_MANY_MARGINAL_BYPASSES = "too_many_marginal_bypasses"
# Counter-trend entry filter. Up-sloping resistance shorts and
# down-sloping support longs fade their own prevailing direction —
# bad fit during clear trends, especially after a strong move. The
# chart frontend hides these lines by default
# (``showCounterResistance``/``showCounterSupport`` toggles); the
# bot mirrors that default here. Bypassable in marginal mode.
FILTER_COUNTER_TREND = "counter_trend"

# Local-regime gates (2026-05-17). Bar-level, run AFTER pivot
# detection and BEFORE 3-touch line search. Each is hard-reject —
# these are market-regime decisions, not entry-quality bypasses.
#   local_peak_in_uptrend    — SHORT candidate, but local regime is
#                              UP (ADX > 25, +DI > −DI). Don't sell
#                              into a confirmed uptrend.
#   local_trough_in_downtrend — LONG candidate, but local regime is
#                              DOWN (ADX > 25, −DI > +DI). Don't buy
#                              into a confirmed downtrend.
#   flat_amplitude    — flat regime + ATR × N-bars < edge-mult × costs.
#   flat_extreme      — flat regime + pivot not at the Donchian
#                       N-bar extreme (within tol).
#   insufficient_bars — bar window too small to derive a regime;
#                       falls through to flat-conservative gates.
FILTER_LOCAL_PEAK_IN_UPTREND = "local_peak_in_uptrend"
FILTER_LOCAL_TROUGH_IN_DOWNTREND = "local_trough_in_downtrend"
FILTER_FLAT_AMPLITUDE = "flat_amplitude"
FILTER_FLAT_EXTREME = "flat_extreme"
FILTER_INSUFFICIENT_BARS_FOR_REGIME = "insufficient_bars_for_regime"

# Named exit triggers (alongside the bar-close line_breach / trail_stop).
#   counter_line  — tick-time touch-and-hold: mid touches an opposing
#                   trendline (2+ touches, unbroken) and price hasn't
#                   broken through within ``counter_exit_hold_seconds``
#                   (default 10) → immediate close.
EXIT_COUNTER_LINE = "counter_line"
EXIT_TIGHT_COUNTER_LINE = "tight_counter_line"


def _parse_ts(ts: Any) -> datetime | None:
    """Bars carry ``timestamp_utc`` as either an ISO string or a datetime.
    Normalise to an aware UTC datetime or ``None`` when missing."""
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(float(ts), tz=timezone.utc)
    try:
        out = datetime.fromisoformat(str(ts))
        return out if out.tzinfo else out.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


class ChartSignalStrategy:
    """3-touch SR fan strategy. Long on uptrending support; exit on bar
    close below the entry line."""

    def __init__(self, config: dict) -> None:
        self.config = config
        symbol = config["symbol"]
        bar_seconds = int(config.get("bar_size_seconds", 180))
        lookback = int(config.get("lookback_bars", 60))
        self.bar_seconds = bar_seconds

        self.manifest = StrategyManifest(
            name="chart_signal",
            subscriptions=[
                Subscription("bars", [symbol],
                             {"bar_seconds": bar_seconds, "lookback": lookback}),
            ],
            capabilities=["execution", "state_store"],
            state_schema={
                "trade_serial": "int|null",
                "armed": "bool",
                "entry_price": "decimal|null",
                "entry_time": "str|null",
                "entry_bar_time": "str|null",
                "entry_line": "dict|null",  # {slope_per_sec, anchor_time, anchor_price, touches, kind, slope_per_bar, intercept, anchor_b_idx, from_time, from_idx}
                "qty": "decimal|null",
                "last_price": "decimal|null",
                "unrealized_pnl": "decimal|null",
                # Trailing-dip exit (Change B). HWM = highest bar
                # close since entry (long); LWM = lowest (short).
                # active_stop = max(line, trail) for long, min for
                # short. exit_reason recorded on close.
                "high_water_mark": "decimal|null",
                "low_water_mark": "decimal|null",
                "active_stop": "decimal|null",
                "exit_reason": "str|null",
                # ``cooldown_until`` is a wallclock ISO set by the
                # runtime on exit when ``stop_on_exit=False``. The
                # entry gate in _on_bar skips firing while now <
                # cooldown_until — gives one bar of breathing room
                # after a round-trip before the bot enters again.
                "cooldown_until": "str|null",
                "last_deadzone_alert_date": "str|null",
                # Counter-line exit (EXIT_COUNTER_LINE). Cache is a
                # snapshot of opposing trendlines written at every bar
                # close in _evaluate_exit; tick-time _on_quote reads
                # it and runs the touch-and-hold rule.
                "counter_lines_cache": "list|null",
                "counter_lines_tol": "decimal|null",
                "counter_touch": "dict|null",
                # Per-trade ATR snapshot for the counter-line exit's
                # proximity check. Refreshed at every bar close while
                # in position. ``None`` until the first refresh.
                "trade_atr": "decimal|null",
                # SL touch+hold timer — MARGINAL trades only.
                # ``{"start_ts": iso}`` when mid first crossed
                # ``active_stop`` in the bad direction; cleared on
                # retrace. Fires when elapsed >=
                # ``sl_linger_marginal_seconds`` (default 10).
                "sl_touch": "dict|null",
                # SL periodic-poll timestamp — CLEAN trades only.
                # ``_on_quote`` re-evaluates the SL once every
                # ``sl_check_clean_seconds`` (default 60); if the
                # sample is breached, fires immediately. Updated on
                # each sample; None at entry (first tick samples).
                "sl_last_check_ts": "str|null",
            },
            version="1.0",
            supported_sec_types=("STK", "ETF", "FUT", "FOP"),
        )

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def on_start(self, ctx: StrategyContext) -> list[Action]:
        if not ctx.state:
            ctx.state = {}
        # Initialise state lazily. ``armed`` defaults to True on a fresh
        # bot start; if the runtime is restoring after a crash mid-round
        # the persisted state already carries the right ``armed`` value.
        actions: list[Action] = []
        if "armed" not in ctx.state:
            actions.append(UpdateState({"armed": True}))
        actions.append(LogSignal(
            event_type=LogEventType.STATE,
            message=(
                f"chart_signal started: fsm={ctx.fsm_state.value} "
                f"armed={ctx.state.get('armed', True)}"
            ),
            payload={"symbol": self.config["symbol"]},
        ))
        return actions

    async def on_stop(self, ctx: StrategyContext) -> list[Action]:
        return [LogSignal(event_type=LogEventType.STATE,
                          message="chart_signal stopped")]

    async def on_event(self, event: MarketEvent,
                       ctx: StrategyContext) -> list[Action]:
        if isinstance(event, BarCompleted):
            pos_before = ctx.fsm_state
            actions = await self._on_bar(event, ctx)
            # Append a BAR_EVAL audit row synthesized from the bar's
            # action list. One row per 3-min bar evaluation regardless
            # of outcome (entry side, exit side, gated, or holding).
            audit = self._synthesize_bar_eval(event, actions, pos_before)
            if audit is not None:
                actions = list(actions) + [audit]
            return actions
        if isinstance(event, QuoteUpdate):
            return self._on_quote(event, ctx)
        if isinstance(event, OrderFilled):
            return self._on_fill(event, ctx)
        if isinstance(event, OrderRejected):
            return self._on_rejected(event, ctx)
        return []

    def _synthesize_bar_eval(
        self, event: BarCompleted, actions: list[Action],
        pos_before: BotState,
    ) -> EmitAudit | None:
        """Derive the BAR_EVAL row from the bar's resulting action list.

        Headline fields:
          - pivot_status: from the BAR LogSignal payload (best touches
            tell us if a pivot landed on a line; we resolve the side
            from the chosen line if one was picked, else NO_PIVOT).
          - line_status: from best_long_touches / best_short_touches.
          - decision: tiered priority —
              PlaceOrder           → FIRED·<BUY/SELL>
              TRAILING_STOP signal → EXIT_FIRED·<reason>
              SKIP w/ filter       → FILTERED·<filter_name>
              SKIP w/o filter      → SKIP·<short reason>
              else, in-position    → HOLDING
              else                 → GATED·<gate>  (no BAR event = early gate)

        Returns None if the bar produced literally no actions (e.g.,
        the strategy bailed before emitting anything) — we skip the
        audit row in that case to keep the feed clean.
        """
        # Bar-vs-eval-bar semantics: the bot evaluates at the close of
        # bar X (last_idx) and the pivot under consideration is at
        # bar X-1 (last_idx-1). The audit row labels and contents
        # should all refer to the PIVOT BAR (X-1) — that's the chart
        # tick the operator sees with the pivot on it. The eval
        # itself ran 3 min later when bar X closed.
        #
        # /engine/history labels bars by slot-START. event.bar is
        # bar X (slot-start T_eval, slot-end T_eval+180s). The pivot
        # bar X-1 has slot-end = T_eval = pivot's chart-tick.
        # So event_ts_utc = event.bar.timestamp_utc gives us the
        # pivot bar's chart-tick (matches the chart's slot-end
        # convention).
        from datetime import timedelta as _td
        bar_ts = _parse_ts(event.bar.get("timestamp_utc"))  # pivot bar's chart-tick
        bar_seconds = int(self.config.get("bar_size_seconds", 180))
        eval_ts = (bar_ts + _td(seconds=bar_seconds)
                    if bar_ts is not None else None)
        # bar_close = the PIVOT bar's close, sourced from the BAR
        # payload (which the strategy emits using its own closes
        # series). Falls back to event.bar.close (= eval bar's close,
        # off by one bar) only if the BAR LogSignal hasn't been
        # collected yet.
        bar_close_d: "Decimal | None" = None
        symbol = self.config.get("symbol", "")

        # BAR signal carries the line/pivot universe at evaluation time.
        bar_sig: LogSignal | None = None
        # PlaceOrder = the order that fired.
        place_order: PlaceOrder | None = None
        # Exit signal (in-position bar that triggered TRAILING_STOP).
        exit_sig: LogSignal | None = None
        # First non-bypassed SKIP wins for the decision. SKIPs from the
        # bypassable filters (shoulder / min_target / far_from_pivot)
        # under ``allow_marginal_entries=True`` carry ``marginal=True``
        # in their payload — these were tagged for diagnostics but the
        # trade continued past them, so they're NOT the rejection cause.
        # Without this filter the audit label misreports the first
        # bypassed filter (e.g. ``FILTERED·shoulder``) when the trade
        # was actually killed downstream (e.g. opposing_dominance,
        # stale_line, max_signal_age).
        skip_sig: LogSignal | None = None
        # Full chain of SKIPs for the operator-facing entry_decision
        # diag (every filter that fired this bar, bypassed or not).
        skip_chain: list[dict] = []
        for a in actions:
            if isinstance(a, PlaceOrder):
                if place_order is None:
                    place_order = a
            elif isinstance(a, LogSignal):
                et = a.event_type.value if hasattr(a.event_type, "value") \
                    else str(a.event_type)
                if et == "BAR" and bar_sig is None:
                    bar_sig = a
                elif et == "EXIT_CHECK" and "TRAILING_STOP" in (a.message or ""):
                    exit_sig = a
                elif et == "SKIP":
                    p = a.payload or {}
                    bypassed = bool(p.get("marginal", False))
                    skip_chain.append({
                        "filter": p.get("filter"),
                        "bypassed": bypassed,
                        "message_head": (a.message or "")[:80],
                    })
                    if skip_sig is None and not bypassed:
                        skip_sig = a

        # Pivot status — canonical source is the BAR payload's
        # ``pivot_detected`` field (filled from find_pivot_lows/highs
        # at last_idx-1). Independent of whether a 3-touch line
        # accepted the pivot — operator sees "pivot existed but got
        # filtered" cases. Falls back to NO_PIVOT when BAR carried
        # an explicit None.
        pivot_status = "NO_PIVOT"
        line_status = "LINES_NONE"
        if bar_sig and bar_sig.payload:
            pd = bar_sig.payload.get("pivot_detected")
            if pd == "low":
                pivot_status = "PIVOT_LOW"
            elif pd == "high":
                pivot_status = "PIVOT_HIGH"
            blt = int(bar_sig.payload.get("best_long_touches") or 0)
            bst = int(bar_sig.payload.get("best_short_touches") or 0)
            if blt > 0 and bst > 0:
                line_status = "LINES_BOTH"
            elif blt > 0:
                line_status = "LINES_LONG"
            elif bst > 0:
                line_status = "LINES_SHORT"
            # Pull pivot bar's close from the BAR payload — this is
            # the price tag for the audit row (matches the chart-tick
            # the row is labeled with).
            pbc = bar_sig.payload.get("pivot_bar_close")
            if pbc is not None:
                try:
                    bar_close_d = Decimal(str(pbc))
                except (TypeError, ValueError, ArithmeticError):
                    pass
        else:
            # No BAR signal in this action list — gated path (cooldown,
            # warmup, etc.). Pivot status unknown.
            pivot_status = "NONE"
        # Fallback for bar_close: if BAR payload didn't carry it
        # (gated/early-bail path), use event.bar.close — the eval bar's
        # close. Off by one bar but better than NULL.
        if bar_close_d is None:
            try:
                bar_close_d = Decimal(str(event.bar.get("close", "0")))
            except Exception:  # noqa: BLE001
                bar_close_d = None

        # Detect marginal entry from the SIGNAL payload's entry_line.
        # When ``allow_marginal_entries=True`` lets a filter-rejected
        # trade fire, entry_line.marginal == True propagates through.
        signal_sig_for_marginal: LogSignal | None = next(
            (a for a in actions if isinstance(a, LogSignal)
             and (a.event_type.value if hasattr(a.event_type, "value")
                  else str(a.event_type)) == "SIGNAL"),
            None,
        )
        is_marginal_entry = False
        if signal_sig_for_marginal and signal_sig_for_marginal.payload:
            el = signal_sig_for_marginal.payload.get("entry_line") or {}
            is_marginal_entry = bool(el.get("marginal", False))

        # Decision — tiered.
        decision = ""
        if place_order is not None:
            tag = "·marginal" if is_marginal_entry else ""
            decision = f"FIRED·{place_order.side}{tag}"
            pivot_status = ("PIVOT_LOW" if place_order.side == "BUY"
                            else "PIVOT_HIGH")
        elif exit_sig is not None:
            # TRAILING_STOP message starts with the exit reason; pull
            # the short form (e.g., "counter-line held" → "counter_line").
            payload = exit_sig.payload or {}
            exit_type = str(payload.get("exit_type", "")).lower()
            direction = str(payload.get("direction", "")).lower()
            # The detail message carries the reason after the colon.
            msg = exit_sig.message or ""
            short = msg.split(":", 1)[-1].strip()[:30] if ":" in msg else exit_type
            decision = f"EXIT_FIRED·{short}" if short else f"EXIT_FIRED·{exit_type}"
            pivot_status = "PIVOT_LOW" if direction == "short" else "PIVOT_HIGH"
        elif skip_sig is not None:
            payload = skip_sig.payload or {}
            filt = payload.get("filter")
            if filt:
                decision = f"FILTERED·{filt}"
                side = str(payload.get("direction", "")).lower()
                if side == "long":
                    pivot_status = "PIVOT_LOW"
                elif side == "short":
                    pivot_status = "PIVOT_HIGH"
            else:
                # Cooldown / signal-too-old / no-new-pivot / etc.
                msg = (skip_sig.message or "").lower()
                if "cooldown" in msg:
                    decision = "GATED·cooldown"
                elif "deadzone" in msg:
                    decision = "GATED·deadzone"
                elif "too old" in msg:
                    decision = "SKIP·stale_bar"
                elif "no new pivot" in msg:
                    decision = "SKIP·no_new_pivot"
                    pivot_status = "NO_PIVOT"
                else:
                    decision = "SKIP·other"
        else:
            # No PlaceOrder, no exit signal, no SKIP — either the bar
            # was processed silently (gated before any signal emit) or
            # the bot is holding a position. We still emit a row so
            # the audit feed has one entry per bot per bar — silent
            # windows otherwise look like the system is broken.
            if pos_before == BotState.AWAITING_EXIT_TRIGGER:
                decision = "HOLDING"
            elif pos_before == BotState.AWAITING_ENTRY_TRIGGER:
                decision = "GATED·armed_false"
            else:
                # OFF / ERRORED / ENTRY_ORDER_PLACED / etc — the bot
                # isn't actively evaluating but the bar still closed.
                # Emit a row with the FSM state name so the operator
                # can see what was happening at that bar.
                state_name = getattr(pos_before, "name", "UNKNOWN") \
                    if pos_before is not None else "UNKNOWN"
                decision = f"GATED·{state_name.lower()}"

        # Structured audit fields — what the frontend headline + detail
        # actually consume. Keeping them as top-level dict entries so
        # the renderer doesn't have to re-parse the BAR/SIGNAL payloads.
        #
        #   pivot         : "low" | "high" | None
        #   touch         : { line_kind, touches, slope, intercept,
        #                     q_anchor_time, p_anchor_time,
        #                     line_value_at_now } | None
        #   filter_name   : str | None    (e.g. "shoulder")
        #   filter_detail : str | None    (one-line human form)
        #   outcome       : "B" | "S" | "—"
        #   prior_bar_close : decimal | None  (the bar BEFORE this one)
        audit: dict = {
            "pivot": None,
            "touch": None,
            "filter_name": None,
            "filter_detail": None,
            "outcome": "—",
            "prior_bar_close": None,
            # ``eval_ts_utc`` = the chart-tick when the bot actually
            # processed this pivot (always pivot's chart-tick + bar_seconds).
            # ``eval_bar_close`` = the close of the eval bar = "next close"
            # relative to the pivot bar.
            "eval_ts_utc": eval_ts.isoformat() if eval_ts is not None else None,
            "eval_bar_close": None,
        }
        # prior_bar_close and pivot — sourced from the BAR payload
        # (which the strategy emits using its own /engine/history
        # closes series, the canonical source). event.window's bar
        # dicts vary in shape across runtime paths so we don't rely
        # on them here.
        if bar_sig and bar_sig.payload:
            pc = bar_sig.payload.get("prior_bar_close")
            if pc is not None:
                try:
                    audit["prior_bar_close"] = float(pc)
                except (TypeError, ValueError):
                    pass
            ebc = bar_sig.payload.get("eval_bar_close")
            if ebc is not None:
                try:
                    audit["eval_bar_close"] = float(ebc)
                except (TypeError, ValueError):
                    pass
            pd = bar_sig.payload.get("pivot_detected")
            if pd in ("low", "high"):
                audit["pivot"] = pd

        # Backfill pivot from chosen-side info if BAR didn't carry it
        # (older bot_events rows pre-payload-extension).
        if audit["pivot"] is None:
            if pivot_status == "PIVOT_LOW":
                audit["pivot"] = "low"
            elif pivot_status == "PIVOT_HIGH":
                audit["pivot"] = "high"

        # Touch info — primary source is the BAR payload (which is
        # emitted on EVERY bar, regardless of whether SIGNAL fires or
        # filters reject). The SIGNAL payload (if present) refines it
        # with anchor timestamps the BAR payload doesn't carry.
        signal_sig: LogSignal | None = next(
            (a for a in actions if isinstance(a, LogSignal)
             and (a.event_type.value if hasattr(a.event_type, "value")
                  else str(a.event_type)) == "SIGNAL"),
            None,
        )
        # Touch info — the BAR payload carries the authoritative list
        # ``pivot_touching_lines``: every current-session 3+touch line
        # (matching the pivot's side) that the just-confirmed pivot
        # lies on within tol. TOUCH·N counts N = len(this list).
        if bar_sig and bar_sig.payload:
            touching = bar_sig.payload.get("pivot_touching_lines") or []
            if isinstance(touching, list) and len(touching) > 0:
                # Pick the highest-touch line as the "primary" for the
                # touch chip (matches the bot's tie-break).
                primary = touching[0]
                audit["touch"] = {
                    "line_kind": primary.get("kind"),
                    "direction": (
                        "long" if primary.get("kind") == "support"
                        else "short"
                    ),
                    "touches": int(primary.get("touches", 0) or 0),
                    "slope_per_bar": primary.get("slope_per_bar"),
                    "intercept": primary.get("intercept"),
                    "anchor_b_idx": primary.get("anchor_b_idx"),
                    "anchor_q_idx": primary.get("from_idx"),
                    "anchor_b_time": primary.get("anchor_b_time"),
                    "anchor_q_time": primary.get("anchor_q_time"),
                    "anchor_b_price": primary.get("anchor_b_close"),
                    "count": len(touching),
                    "lines": touching,
                }
        # Step 2: refine with SIGNAL payload anchor timestamps (only
        # present when entry actually fired).
        if signal_sig and signal_sig.payload and audit["touch"] is not None:
            el = signal_sig.payload.get("entry_line") or {}
            if el:
                audit["touch"].update({
                    "anchor_b_time": el.get("anchor_time"),
                    "anchor_q_time": el.get("from_time"),
                    "anchor_b_price": el.get("anchor_price"),
                })
        # Filter info — only set when a filter explicitly fired.
        if skip_sig and skip_sig.payload:
            sp = skip_sig.payload or {}
            filt = sp.get("filter")
            if filt:
                audit["filter_name"] = str(filt)
                audit["filter_detail"] = (skip_sig.message or "")[:200]
        # Outcome.
        if place_order is not None:
            audit["outcome"] = "B" if place_order.side == "BUY" else "S"
        elif exit_sig is not None:
            audit["outcome"] = "exit"

        # Payload: structured audit fields + the existing raw blocks so
        # the operator can still drill into the full diag via the
        # "raw JSON" modal in the UI.
        payload: dict = {"audit": audit}
        if bar_sig and bar_sig.payload:
            payload["bar"] = bar_sig.payload
        if skip_sig and skip_sig.payload:
            payload["skip"] = skip_sig.payload
        if signal_sig and signal_sig.payload:
            payload["signal"] = signal_sig.payload
        if exit_sig and exit_sig.payload:
            payload["exit"] = exit_sig.payload
        if place_order is not None:
            payload["fired"] = {
                "side": place_order.side,
                "qty": str(place_order.qty),
                "order_type": getattr(place_order, "order_type", None),
            }
        # Full SKIP chain — every filter that fired this bar, in order,
        # with whether marginal-mode bypassed it. Lets the operator
        # answer "why didn't this fire?" without log digging. Empty
        # list when no SKIPs occurred (clean fire / pre-pivot gate).
        if skip_chain:
            payload["skip_chain"] = skip_chain

        return EmitAudit(
            event_type="BAR_EVAL",
            decision=decision,
            symbol=symbol,
            event_ts_utc=bar_ts,
            pivot_status=pivot_status,
            line_status=line_status,
            bar_close=bar_close_d,
            payload=payload,
        )

    # ------------------------------------------------------------------
    # Bar — entry & exit decisions both fire on 3-min bar close
    # ------------------------------------------------------------------

    async def _fetch_history(self) -> list[dict]:
        """Pull 3-min bars from the engine, the same way the view-only
        chart does (``/engine/history`` is the same endpoint the
        frontend's ``/api/history`` proxies to). Keeps the bot and the
        chart's SR detector on identical input data — without this the
        bot ran on the in-process aggregator's view, which lags warmup
        dedup and never catches up on a restart."""
        engine_url = self.config.get("_engine_url")
        if not engine_url:
            return []
        import httpx
        symbol = self.config["symbol"]
        sec_type = str(self.config.get("sec_type", "STK")).upper()
        hours = int(self.config.get("history_hours", 2))
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"{engine_url}/engine/history",
                    params={"symbol": symbol, "sec_type": sec_type,
                            "hours": hours, "bar_size": "3 mins"},
                )
                resp.raise_for_status()
                return resp.json() or []
        except Exception as e:  # noqa: BLE001 — broad on purpose
            logger.warning(
                '{"event": "CHART_SIGNAL_HISTORY_FETCH_FAILED", '
                '"symbol": "%s", "error": "%s"}',
                self.config.get("symbol"), e,
            )
            return []

    async def _on_bar(self, event: BarCompleted,
                      ctx: StrategyContext) -> list[Action]:
        actions: list[Action] = []
        bar_time = _parse_ts(event.bar.get("timestamp_utc"))
        if bar_time is None:
            return actions

        pos = ctx.fsm_state

        # Exit check (long): if we hold a position, evaluate against the
        # frozen entry line on this 3-min close. Independent of window
        # length — the entry line is already frozen.
        if pos == BotState.AWAITING_EXIT_TRIGGER:
            return actions + await self._evaluate_exit(event, ctx, bar_time)

        # Entry check: gated on armed + deadzone + cooldown + FSM state.
        if pos != BotState.AWAITING_ENTRY_TRIGGER:
            return actions
        if not ctx.state.get("armed", False):
            return actions
        # Cooldown gate: when ``stop_on_exit=false``, the runtime
        # writes a ``cooldown_until`` wallclock ISO on the bot doc
        # after each round-trip. Skip entries while that's in the
        # future. Default cooldown = 180s (one 3-min bar) so the
        # bot doesn't immediately re-fire on the bar right after
        # exit.
        cooldown_until = ctx.state.get("cooldown_until")
        if cooldown_until:
            cu = _parse_ts(cooldown_until)
            if cu is not None and cu > bar_time:
                actions.append(LogSignal(
                    event_type=LogEventType.SKIP,
                    message=(
                        f"in cooldown until {cu.isoformat()} "
                        f"(bar_time={bar_time.isoformat()})"
                    ),
                    payload={"cooldown_until": cu.isoformat(),
                             "bar_time": bar_time.isoformat()},
                ))
                return actions
        # Futures deadzone only applies to FUT/FOP — the docstring and
        # the holding-alert path scope to those sec types, but the
        # entry gate was unconditional and silently blocked STK bots
        # during 14:00-15:00 PT for no real reason.
        sec_type = str(self.config.get("sec_type", "STK")).upper()
        if sec_type in ("FUT", "FOP") and in_futures_deadzone(bar_time):
            # Quiet skip — logging every bar inside the deadzone is noise.
            return actions

        # Pull bars from /engine/history so the bot and the view-only
        # chart see identical input. The runtime hands us
        # ``event.window`` from the in-process aggregator; we prefer
        # the HTTP fetch (IB-canonical, matches the chart) and fall
        # back to the runtime window only if the fetch comes up empty.
        #
        # IB's historical-data API typically lags the live bar close
        # by a slot: when the 09:48-09:51 bar closes at 09:51:00, the
        # HTTP fetch at 09:51:06 still returns data through 09:48 —
        # so the bot's "just-confirmed pivot" sits one bar older than
        # what the chart shows, and clean low-touch entries like the
        # 2026-05-12 MGCM6 09:48 setup were missed. Bridge the lag by
        # appending the local aggregator's just-closed bar when it's
        # newer than ``fetched``'s last entry.
        fetched = await self._fetch_history()
        local_window = event.window or []
        # ``/engine/history`` stores timestamps as ISO strings;
        # ``event.window`` stores them as datetime objects. Normalize
        # both to ISO strings before comparing so the append loop
        # doesn't trip a ``TypeError: '>' not supported between
        # datetime and str``.
        def _ts_iso(b: dict) -> str:
            # ``/engine/history`` returns bars with the timestamp under
            # the ``ts`` key (ISO string) while ``event.window`` bars
            # use ``timestamp_utc`` (datetime). Read both so the
            # dedup/append comparison works regardless of source.
            # Earlier oversight: with only ``timestamp_utc`` the fetched
            # latest ts came back as "" → the loop appended every
            # event.window bar (a duplicate of the fetched data).
            ts = b.get("timestamp_utc") or b.get("ts")
            if ts is None:
                return ""
            if hasattr(ts, "isoformat"):
                return ts.isoformat()
            return str(ts)
        if fetched:
            window = list(fetched)
            if local_window:
                latest_ts_fetched = _ts_iso(window[-1])
                for bar in local_window:
                    bar_ts = _ts_iso(bar)
                    if bar_ts and bar_ts > latest_ts_fetched:
                        window.append(bar)
                        latest_ts_fetched = bar_ts
        else:
            window = list(local_window)
        if not window:
            return actions
        closes = [float(b.get("close", 0)) for b in window]
        if len(closes) < 4:
            return actions
        last_idx = len(closes) - 1

        # Pivot lookup early — the BAR audit row needs to know whether
        # the just-confirmed pivot at last_idx-1 was a LOW or a HIGH,
        # regardless of whether a 3-touch line ended up accepting it.
        # Cheap O(n) walk; reused later by the line-accept loop.
        support_pivots = find_pivot_lows(closes)
        resistance_pivots = find_pivot_highs(closes)
        new_pivot_idx_for_audit = last_idx - 1

        # Scan both sides. Long candidates from uptrending support
        # (slope > 0); short candidates from downtrending resistance
        # (slope < 0). Both filtered to 3-touch, not broken.
        #
        # ``near_touch_tolerance_fraction`` (optional, default 5× of
        # the strict 0.0002 = 0.001): once a line accumulates the
        # first three strict touches, further pivots within this
        # wider band count as 4th/5th/… touches. This recovers entries
        # on lines that are visually-established but where the new
        # pivot's strict tol misses by a hair — the kind of pivot the
        # operator's eye reads as "on the line" but the math doesn't.
        # Set to ``None`` to disable (= legacy strict behavior).
        near_frac = self.config.get(
            "near_touch_tolerance_fraction", 5 * TOUCH_TOLERANCE_FRACTION,
        )
        if near_frac is not None:
            near_frac = float(near_frac)
        # ``break_stale_bars`` extends the lifetime of broken lines in
        # the detect output. Default 480 bars = 24 h of 3-min bars,
        # so a line that briefly "breaks" at a session-rollover gap
        # stays in the universe long enough for the post-rollover
        # entry path to consider it. 24 h sits inside the same
        # window as the stale_line filter so the two are consistent.
        # Observed on MNQ 2026-05-15 13:00 PT — after the session gap
        # detect_lines was returning 0 lines because every prior line
        # broke at the gap and aged past the old 20-bar (1 h) window.
        break_stale_bars = int(self.config.get(
            "detect_break_stale_bars", 480,
        ))
        supports = detect_lines(
            closes, up_to=last_idx, type_="support",
            near_touch_tolerance_fraction=near_frac,
            break_stale_bars=break_stale_bars,
        )
        resistances = detect_lines(
            closes, up_to=last_idx, type_="resistance",
            near_touch_tolerance_fraction=near_frac,
            break_stale_bars=break_stale_bars,
        )
        # Note: ``break_idx`` is no longer a hard gate here. The
        # ``stale_line`` filter further down checks "most recent
        # strict touch within 24 h" which subsumes the broken-line
        # case (a line with no recent touch — including one that
        # broke long ago — fails that check).
        longs = [ln for ln in supports
                 if ln.touches >= MIN_TOUCHES and ln.slope > 0]
        shorts = [ln for ln in resistances
                  if ln.touches >= MIN_TOUCHES and ln.slope < 0]
        longs.sort(key=lambda ln: (-ln.touches, -ln.slope))
        shorts.sort(key=lambda ln: (-ln.touches, ln.slope))   # most-negative slope first

        # If both sides offer a candidate (rare but possible in a noisy
        # range), pick whichever has more touches; tie → prefer long
        # (entry on dip is the bias the user originally tuned for).
        long_line = longs[0] if longs else None
        short_line = shorts[0] if shorts else None

        # Touch tolerances — computed early so the BAR audit row can
        # surface ``has_new_touch`` (the strict "this bar's pivot
        # actually lies on the line within tol" check) alongside the
        # raw line presence flag. The bot uses these same values
        # later in the entry-decision path.
        TOUCH_FRAC_EARLY = float(self.config.get(
            "touch_tolerance_fraction", TOUCH_TOLERANCE_FRACTION,
        ))
        avg_close_early = sum(closes) / max(1, len(closes))
        touch_tol_early = max(1e-6, avg_close_early * TOUCH_FRAC_EARLY)
        near_tol_early: float | None = None
        if near_frac is not None and near_frac > TOUCH_FRAC_EARLY:
            near_tol_early = max(touch_tol_early,
                                  avg_close_early * near_frac)
        np_idx = last_idx - 1

        def _strict_new_touch(line, side_pivots) -> bool:
            """``_has_new_touch`` minus the 4th-loose path — used by
            the audit row to determine whether THIS bar's pivot
            actually landed on the line. Same gate the entry uses;
            cached to avoid duplicate work at line 805."""
            if line is None:
                return False
            if np_idx < 0 or np_idx not in side_pivots:
                return False
            line_at = line.intercept + line.slope * np_idx
            delta = abs(closes[np_idx] - line_at)
            if delta <= touch_tol_early:
                return True
            if near_tol_early is None or delta > near_tol_early:
                return False
            # 4th+ near touch — line must already hold MIN_TOUCHES
            # strict touches from older pivots.
            strict_old = 0
            for piv in side_pivots:
                if piv == np_idx or piv < line.from_idx:
                    continue
                if piv > last_idx:
                    continue
                if abs(closes[piv]
                       - (line.intercept + line.slope * piv)) <= touch_tol_early:
                    strict_old += 1
            return strict_old >= MIN_TOUCHES

        long_has_new_touch = _strict_new_touch(long_line, support_pivots)
        short_has_new_touch = _strict_new_touch(short_line, resistance_pivots)

        # Enumerate ALL current-session 3+touch lines (on the
        # pivot's matching side) that the just-confirmed pivot lies
        # on within touch_tol. This is what the audit feed's
        # TOUCH·N chip counts: "N = number of trendlines this
        # pivot landed on". Stale-line cap: Q anchor must be within
        # ``entry_max_q_age_hours`` (default 24h) of the fire bar —
        # same threshold the bot's stale_line entry filter uses.
        pivot_touching_lines: list[dict] = []
        if np_idx >= 0:
            from datetime import timedelta as _td_audit
            # Bar timestamps come from /engine/history into ``window``.
            def _bar_dt(i):
                if 0 <= i < len(window):
                    ts = (window[i].get("timestamp_utc")
                          or window[i].get("ts"))
                    return _parse_ts(ts)
                return None

            fire_dt = _bar_dt(last_idx)
            max_q_age_hours_audit = float(self.config.get(
                "entry_max_q_age_hours", 24.0,
            ))

            def _enumerate_touching(lines, side_pivots, pivot_kind):
                out: list[dict] = []
                if np_idx not in side_pivots:
                    return out
                pivot_close = closes[np_idx]
                for ln in lines:
                    if ln.touches < MIN_TOUCHES:
                        continue
                    if ln.break_idx is not None:
                        continue
                    # Side-correct slope check.
                    if pivot_kind == "low" and ln.slope <= 0:
                        continue
                    if pivot_kind == "high" and ln.slope >= 0:
                        continue
                    line_at = ln.intercept + ln.slope * np_idx
                    delta = abs(pivot_close - line_at)
                    if delta > touch_tol_early:
                        # Allow near-touch only if line already holds
                        # MIN_TOUCHES strict from older pivots — same
                        # rule as ``_has_new_touch``.
                        if (near_tol_early is None
                                or delta > near_tol_early):
                            continue
                        strict_old = 0
                        for piv in side_pivots:
                            if (piv == np_idx or piv < ln.from_idx
                                    or piv > last_idx):
                                continue
                            d = abs(closes[piv]
                                    - (ln.intercept + ln.slope * piv))
                            if d <= touch_tol_early:
                                strict_old += 1
                        if strict_old < MIN_TOUCHES:
                            continue
                    # Stale-line gate: the line's MOST RECENT strict
                    # touch must be within ``max_q_age_hours`` of the
                    # fire bar (same rule as the entry filter). Lines
                    # built long ago but still being touched recently
                    # remain valid; lines whose only touches were >24 h
                    # ago drop out.
                    if fire_dt is not None:
                        latest_idx = ln.anchor_b_idx
                        line_to_idx = (
                            ln.break_idx if ln.break_idx is not None
                            else last_idx
                        )
                        for piv in side_pivots:
                            if piv <= ln.anchor_b_idx or piv > line_to_idx:
                                continue
                            if abs(closes[piv]
                                   - (ln.intercept + ln.slope * piv)) <= touch_tol_early:
                                if piv > latest_idx:
                                    latest_idx = piv
                        latest_dt = _bar_dt(latest_idx)
                        if latest_dt is not None:
                            age_h = (fire_dt - latest_dt).total_seconds() / 3600.0
                            if age_h > max_q_age_hours_audit:
                                continue
                    # Two prior strict touches that justify the new
                    # pivot as a 3rd touch (the spacing-helper view).
                    # When anchor_b_idx == np_idx, detect_lines used
                    # the firing pivot as P — so the audit's anchor_b
                    # is misleading as "the second touch." Surface the
                    # actual last-two strict touches BEFORE np_idx so
                    # the operator can see what the line really rested
                    # on at fire time.
                    prior_strict: list[int] = []
                    for piv in side_pivots:
                        if piv >= np_idx:
                            break
                        if piv < ln.from_idx:
                            continue
                        if abs(closes[piv]
                               - (ln.intercept + ln.slope * piv)) <= touch_tol_early:
                            prior_strict.append(piv)
                    pt1_idx = prior_strict[-2] if len(prior_strict) >= 2 else None
                    pt2_idx = prior_strict[-1] if len(prior_strict) >= 1 else None
                    out.append({
                        "kind": ln.type,
                        "touches": int(ln.touches),
                        "slope_per_bar": round(ln.slope, 6),
                        "intercept": round(ln.intercept, 6),
                        "from_idx": int(ln.from_idx),
                        "anchor_b_idx": int(ln.anchor_b_idx),
                        "anchor_q_time": (
                            _bar_dt(ln.from_idx).isoformat()
                            if _bar_dt(ln.from_idx) else None
                        ),
                        "anchor_b_time": (
                            _bar_dt(ln.anchor_b_idx).isoformat()
                            if _bar_dt(ln.anchor_b_idx) else None
                        ),
                        "anchor_q_close": round(closes[ln.from_idx], 4),
                        "anchor_b_close": round(closes[ln.anchor_b_idx], 4),
                        "prior_touch_1_idx": pt1_idx,
                        "prior_touch_2_idx": pt2_idx,
                        "prior_touch_1_time": (
                            _bar_dt(pt1_idx).isoformat()
                            if pt1_idx is not None and _bar_dt(pt1_idx)
                            else None
                        ),
                        "prior_touch_2_time": (
                            _bar_dt(pt2_idx).isoformat()
                            if pt2_idx is not None and _bar_dt(pt2_idx)
                            else None
                        ),
                        "prior_touch_1_close": (
                            round(closes[pt1_idx], 4)
                            if pt1_idx is not None else None
                        ),
                        "prior_touch_2_close": (
                            round(closes[pt2_idx], 4)
                            if pt2_idx is not None else None
                        ),
                        "prior_strict_count": len(prior_strict),
                    })
                # Sort by touches desc so the highest-confidence
                # line lands first in the detail pane.
                out.sort(key=lambda d: -d["touches"])
                return out

            if np_idx in support_pivots:
                pivot_touching_lines = _enumerate_touching(
                    supports, support_pivots, "low",
                )
            elif np_idx in resistance_pivots:
                pivot_touching_lines = _enumerate_touching(
                    resistances, resistance_pivots, "high",
                )
        # Diagnostic — emit ONE compact record per bar even when nothing
        # qualifies. The visibility gap during testing was: "frontend
        # shows a B but no bot trade fires" — without this, the logs
        # were silent on every barbarewhere the algorithm decided not to
        # act. Touches per side (best of) makes algorithm divergence
        # vs the frontend obvious.
        # Dump the top raw candidates per side so we can tell which
        # filter rejected an "obviously valid" line. Sort by touches
        # desc; tiebreak by absolute slope desc (steeper first). Per-
        # line payload fields: touches / slope_per_bar / break_idx
        # (None = not broken). When ``longs=0`` but ``supports_found>0``
        # the top_supports list explains why nothing qualified.
        def _top(lines, k=5):
            ranked = sorted(lines, key=lambda l: (-l.touches, -abs(l.slope)))
            return [
                {"touches": l.touches,
                 "slope": round(l.slope, 4),
                 "break_idx": l.break_idx,
                 "from": l.from_idx, "anchor": l.anchor_b_idx}
                for l in ranked[:k]
            ]

        actions.append(LogSignal(
            event_type=LogEventType.BAR,
            message=(
                f"chart_signal bar close={closes[-1]:.4f} "
                f"longs={len(longs)} shorts={len(shorts)} "
                f"best_long_touches={long_line.touches if long_line else 0} "
                f"best_short_touches={short_line.touches if short_line else 0}"
            ),
            payload={
                "n_bars": len(closes),
                "best_long_touches": long_line.touches if long_line else 0,
                "best_long_slope": long_line.slope if long_line else None,
                "best_long_intercept": long_line.intercept if long_line else None,
                "best_long_from_idx": long_line.from_idx if long_line else None,
                "best_long_anchor_b_idx": long_line.anchor_b_idx if long_line else None,
                "best_short_touches": short_line.touches if short_line else 0,
                "best_short_slope": short_line.slope if short_line else None,
                "best_short_intercept": short_line.intercept if short_line else None,
                "best_short_from_idx": short_line.from_idx if short_line else None,
                "best_short_anchor_b_idx": short_line.anchor_b_idx if short_line else None,
                "supports_found": len(supports),
                "resistances_found": len(resistances),
                "top_supports": _top(supports),
                "top_resistances": _top(resistances),
                # Prior-bar close for the audit feed's expanded view.
                # The audit row is now labeled with the PIVOT bar's
                # chart-tick (= bar X-1), so ``prior_bar_close`` here
                # is the bar BEFORE the pivot bar (= bar X-2, two
                # 3-min slots before the eval). The pivot bar's own
                # close is ``pivot_bar_close`` below.
                "prior_bar_close": (closes[-3] if len(closes) >= 3 else None),
                # The pivot bar's close — the chart-tick price the
                # audit row's label refers to. Sourced from closes[-2]
                # (= last_idx - 1) because the pivot is at last_idx-1
                # and the bot evaluates at last_idx.
                "pivot_bar_close": (closes[-2] if len(closes) >= 2 else None),
                # Eval bar's close — the bar that just closed and
                # triggered evaluation. Displayed in the detail pane
                # as "next close" relative to the pivot.
                "eval_bar_close": (closes[-1] if len(closes) >= 1 else None),
                # Pivot bit derived from the just-confirmed pivot
                # detection at last_idx-1. Independent of whether a
                # 3-touch line accepted it — operator can see "a
                # pivot existed here but got filtered" cases.
                "pivot_detected": (
                    "low" if (last_idx - 1) in support_pivots
                    else "high" if (last_idx - 1) in resistance_pivots
                    else None
                ),
                # Strict "this bar's pivot actually landed on the
                # chosen line within touch tolerance" flags. The audit
                # feed's TOUCH·N chip uses these instead of just the
                # raw line-presence — a pivot HIGH at this bar with
                # best_short_touches > 0 ≠ a touch on the line unless
                # the pivot's close is within tol of the line.
                "long_has_new_touch": long_has_new_touch,
                "short_has_new_touch": short_has_new_touch,
                # Authoritative list for the audit's TOUCH·N chip:
                # every CURRENT-SESSION 3+touch line (matching the
                # pivot's side) that the just-confirmed pivot lies on
                # within touch_tol. N = len(this list). When N == 0,
                # the audit shows NO_3RD_TOUCH (2-touch lines may exist
                # on the chart but aren't entry-eligible); when N >= 1,
                # shows TOUCH·N with the list rendered in the detail.
                "pivot_touching_lines": pivot_touching_lines,
            },
        ))
        # ----------------------------------------------------------
        # Local-regime gate (2026-05-17). Bar-level filter applied
        # AFTER the BAR audit row (so the audit feed always carries
        # the bar's geometric context) and BEFORE the entry-filter
        # chain (so we save the per-line filter work on regime
        # rejection and keep the rejection reason as a single clean
        # SKIP row).
        #
        # The line search itself runs unconditionally so the BAR
        # audit's top_supports/top_resistances payload is always
        # complete for operator review. The few-ms saving from
        # gating detect_lines isn't worth losing that visibility.
        # ----------------------------------------------------------
        np_idx_rg = last_idx - 1
        is_pivot_high_rg = np_idx_rg in resistance_pivots
        is_pivot_low_rg = np_idx_rg in support_pivots
        if (
            self.config.get("regime_filter_enabled", True)
            and (is_pivot_high_rg or is_pivot_low_rg)
        ):
            from .regime import (
                compute_regime, passes_amplitude, at_donchian_extreme,
            )
            side_in_play = "short" if is_pivot_high_rg else "long"
            # Donchian-extreme check uses the PIVOT bar's close, not
            # the eval bar's close. The pivot is what defines the
            # signal — checking the eval bar (one bar later) biases
            # toward rejection because price has already moved off
            # the rejection point by then. For SHORTs after a pivot
            # HIGH the eval close is typically below DCU; for LONGs
            # after a pivot LOW it's typically above DCL. Fixed
            # 2026-05-18 after a MNQ SHORT was rejected at 29116
            # while the pivot itself sat right at DCU (29139 vs
            # DCU 29143 within $5.84 tol).
            entry_close_rg = closes[np_idx_rg]
            reading = compute_regime(
                window,
                adx_period=int(self.config.get("adx_period", 14)),
                atr_period=int(self.config.get("atr_period", 14)),
                donchian_period=int(self.config.get("donchian_period", 20)),
                trending_threshold=float(self.config.get(
                    "adx_trending_threshold", 25.0,
                )),
                ranging_threshold=float(self.config.get(
                    "adx_ranging_threshold", 20.0,
                )),
            )

            # 1) Trending regime — reject anti-direction outright.
            if reading.regime == "up" and side_in_play == "short":
                actions.append(LogSignal(
                    event_type=LogEventType.SKIP,
                    message=(
                        f"{FILTER_LOCAL_PEAK_IN_UPTREND} — local pivot "
                        f"high but the broader regime is UP "
                        f"(ADX={reading.adx:.1f}, "
                        f"+DI={reading.dmp:.1f} > −DI={reading.dmn:.1f}); "
                        f"don't sell into an uptrend"
                    ),
                    payload={
                        "filter": FILTER_LOCAL_PEAK_IN_UPTREND,
                        "marginal": False,
                        "direction": "short",
                        **reading.to_audit_payload(),
                    },
                ))
                return actions
            if reading.regime == "down" and side_in_play == "long":
                actions.append(LogSignal(
                    event_type=LogEventType.SKIP,
                    message=(
                        f"{FILTER_LOCAL_TROUGH_IN_DOWNTREND} — local "
                        f"pivot low but the broader regime is DOWN "
                        f"(ADX={reading.adx:.1f}, "
                        f"−DI={reading.dmn:.1f} > +DI={reading.dmp:.1f}); "
                        f"don't buy into a downtrend"
                    ),
                    payload={
                        "filter": FILTER_LOCAL_TROUGH_IN_DOWNTREND,
                        "marginal": False,
                        "direction": "long",
                        **reading.to_audit_payload(),
                    },
                ))
                return actions

            # 2) Insufficient bars (cold-start / test) — surface a
            # RISK row but DON'T gate. Pre-2024-fetch in real prod
            # we always have 24h = 480 bars so this branch rarely
            # fires; in unit tests with synthetic short windows it's
            # the common path. The existing per-line filters still
            # apply downstream.
            if reading.regime == "insufficient":
                actions.append(LogSignal(
                    event_type=LogEventType.RISK,
                    message=(
                        f"{FILTER_INSUFFICIENT_BARS_FOR_REGIME} — only "
                        f"{reading.n_bars} bars; regime gate skipped"
                    ),
                    payload={
                        "filter": FILTER_INSUFFICIENT_BARS_FOR_REGIME,
                        "marginal": False,
                        "direction": side_in_play,
                        **reading.to_audit_payload(),
                    },
                ))
            # 3) Flat / uncertain (sufficient bars) — apply
            # amplitude and Donchian-extreme gates. Pivot must sit
            # at the extreme of the recent N-bar range AND recent
            # ATR must give enough room to clear costs.
            elif reading.regime in ("flat", "uncertain"):
                TOUCH_FRAC_RG = float(self.config.get(
                    "touch_tolerance_fraction", TOUCH_TOLERANCE_FRACTION,
                ))
                avg_close_rg = sum(closes) / max(1, len(closes))
                touch_tol_rg = max(1e-6, avg_close_rg * TOUCH_FRAC_RG)
                mult_rg = float(self.config.get("contract_multiplier", 1.0))
                # Symbol-specific round-trip commission default; falls
                # back to 1.0 for unknown symbols. ATR is in price
                # units, so divide commission by multiplier to compare.
                round_trip_default = {
                    "MGCQ6": 1.94, "MESM6": 1.24, "MNQM6": 1.24,
                }.get(str(self.config.get("symbol", "")), 1.0)
                round_trip_comm_rg = float(self.config.get(
                    "regime_round_trip_commission", round_trip_default,
                ))
                cost_floor_rg = (
                    (round_trip_comm_rg / max(mult_rg, 1e-6))
                    + (2.0 * touch_tol_rg)
                )
                typ_bars_rg = float(self.config.get(
                    "regime_typical_bars_in_trade", 5.0,
                ))
                edge_mult_rg = float(self.config.get(
                    "regime_min_edge_mult", 2.0,
                ))

                # 2a) Amplitude — expected swing vs cost floor.
                if not passes_amplitude(
                    reading,
                    cost_floor=cost_floor_rg,
                    typical_bars_in_trade=typ_bars_rg,
                    min_edge_mult=edge_mult_rg,
                ):
                    expected_swing_rg = (reading.atr or 0.0) * typ_bars_rg
                    actions.append(LogSignal(
                        event_type=LogEventType.SKIP,
                        message=(
                            f"{FILTER_FLAT_AMPLITUDE} ({side_in_play.upper()}) "
                            f"— flat regime, expected swing "
                            f"${expected_swing_rg:.4f} < required "
                            f"${cost_floor_rg * edge_mult_rg:.4f}"
                        ),
                        payload={
                            "filter": FILTER_FLAT_AMPLITUDE,
                            "marginal": False,
                            "direction": side_in_play,
                            "expected_swing": round(expected_swing_rg, 4),
                            "cost_floor": round(cost_floor_rg, 4),
                            "min_edge_mult": edge_mult_rg,
                            "typical_bars_in_trade": typ_bars_rg,
                            **reading.to_audit_payload(),
                        },
                    ))
                    return actions

                # 2b) Donchian extreme — pivot must be near the
                # regime-side band.
                # DI-lean override (2026-05-18): in flat/uncertain
                # regime, if DI dominance toward the entry direction
                # is strong enough, skip the Donchian extreme check.
                # ADX is laggy — Wilder smoothing means a fresh
                # downtrend can have −DI clearly above +DI well
                # before ADX climbs past 25. When the directional
                # signal is unambiguous, the "must be at the recent
                # peak/trough" rule is too strict — lower-highs in a
                # confirmed bearish drift (and lower-lows in a
                # confirmed bullish drift) are valid entries. The
                # amplitude gate above still applies.
                di_lean_threshold = float(self.config.get(
                    "regime_di_lean_threshold", 7.0,
                ))
                dmp_val = reading.dmp or 0.0
                dmn_val = reading.dmn or 0.0
                di_lean = (
                    (dmn_val - dmp_val) if side_in_play == "short"
                    else (dmp_val - dmn_val)
                )
                if di_lean >= di_lean_threshold:
                    # Skip the extreme check entirely — emit a RISK
                    # row so the audit trail captures that the gate
                    # was relaxed (and why).
                    actions.append(LogSignal(
                        event_type=LogEventType.RISK,
                        message=(
                            f"regime DI-lean override "
                            f"({side_in_play.upper()}) — DI sep "
                            f"{di_lean:.1f} ≥ {di_lean_threshold:.1f}, "
                            f"skipping Donchian extreme check"
                        ),
                        payload={
                            "filter": "regime_di_lean_override",
                            "marginal": False,
                            "direction": side_in_play,
                            "di_lean": round(di_lean, 2),
                            "threshold": di_lean_threshold,
                            **reading.to_audit_payload(),
                        },
                    ))
                else:
                    ext_tol_rg = touch_tol_rg * float(self.config.get(
                        "regime_extreme_tol_mult", 1.0,
                    ))
                    if not at_donchian_extreme(
                        reading, price=entry_close_rg,
                        side=side_in_play, tol=ext_tol_rg,
                    ):
                        band_val = (
                            reading.dcu if side_in_play == "short"
                            else reading.dcl
                        )
                        actions.append(LogSignal(
                            event_type=LogEventType.SKIP,
                            message=(
                                f"{FILTER_FLAT_EXTREME} "
                                f"({side_in_play.upper()}) — flat regime, "
                                f"entry @ {entry_close_rg:.4f} not at "
                                f"Donchian "
                                f"{'upper' if side_in_play == 'short' else 'lower'} "
                                f"band {band_val:.4f} "
                                f"(tol ${ext_tol_rg:.4f}); "
                                f"DI sep {di_lean:.1f} below override "
                                f"threshold {di_lean_threshold:.1f}"
                            ),
                            payload={
                                "filter": FILTER_FLAT_EXTREME,
                                "marginal": False,
                                "direction": side_in_play,
                                "entry_price": round(entry_close_rg, 4),
                                "band_value": (
                                    round(band_val, 4)
                                    if band_val is not None else None
                                ),
                                "tol": round(ext_tol_rg, 4),
                                "di_lean": round(di_lean, 2),
                                "di_lean_threshold": di_lean_threshold,
                                **reading.to_audit_payload(),
                            },
                        ))
                        return actions

        # ----------------------------------------------------------
        # End regime gate.
        # ----------------------------------------------------------
        # Freshness gates — two guards, both must pass.
        #
        # Guard 1: TOUCH COUNT INCREASED ON THIS BAR.
        # ``find_pivot_lows`` / ``find_pivot_highs`` walk i in
        # [1, len-2] — a pivot at bar X is confirmed when bar X+1
        # closes (X+1 is the right neighbor). So the just-confirmed
        # pivot on this evaluation is at ``last_idx - 1``. The bot
        # may only fire if that newly-confirmed pivot lies ON the
        # chosen line (within touch_tolerance) — otherwise the touch
        # count didn't go up on this bar and we're reacting to a
        # stale 3-touch line that already existed for several bars.
        #
        # A future retest — a later pivot landing on the same line
        # within touch_tol — also satisfies this guard, so the user's
        # "price bounces off the line again → fresh trigger" semantic
        # works for free.
        #
        # Guard 2: WALLCLOCK SIGNAL AGE.
        # The indicator appears on the chart at bar close. Default
        # ``max_signal_age_seconds = 10`` (5 is the tighter
        # alternative). If the BAR event was queued during a daemon
        # restart and we're processing it more than that many seconds
        # after the bar closed, the user-visible "B/S just lit up on
        # the chart" window has passed. Drop the signal.
        #
        # Background: pre-fix, the bot fired on every bar a 3-touch
        # line remained valid (no anchor-freshness check), and on
        # 2026-05-11 chart-bot-4 fired three BUYs at 13:45 / 13:49 /
        # 13:52 on a line anchored at 13:24. The chart showed a
        # single B at 13:24; the user reasonably expected no further
        # entries on that same line. These two guards align bot
        # firing with the chart's NEW-badge semantic.
        TOUCH_FRAC = float(self.config.get(
            "touch_tolerance_fraction", TOUCH_TOLERANCE_FRACTION,
        ))
        avg_close = sum(closes) / max(1, len(closes))
        touch_tol = max(1e-6, avg_close * TOUCH_FRAC)
        # Loose band for 4th+ touches — only applied when the line
        # already has MIN_TOUCHES strict touches from OLDER pivots
        # (i.e. the just-confirmed pivot is a follow-up confirmation
        # on a line the prior bars already established).
        near_tol: float | None = None
        if near_frac is not None and near_frac > TOUCH_FRAC:
            near_tol = max(touch_tol, avg_close * near_frac)
        # support_pivots / resistance_pivots already computed earlier
        # for the BAR audit row; reuse them here.
        new_pivot_idx = last_idx - 1   # the just-confirmed pivot

        # Acceleration-continuation entry: opt-in, off by default.
        # Fires when the new pivot lies STRICTLY OUTSIDE the line in
        # the favorable direction (above for support, below for
        # resistance) by more than near_touch_tol AND the implied
        # slope new_pivot→P is at least ``min_slope_ratio`` × the
        # line's slope. Trail-only exit when this path fires (line
        # is no longer a useful breach-stop because price is well
        # away from it).
        accel_enabled = bool(self.config.get(
            "acceleration_entry_enabled", False,
        ))
        min_slope_ratio = float(self.config.get("min_slope_ratio", 1.5))

        def _has_new_touch(line, side_pivots) -> bool:
            if new_pivot_idx < 0 or new_pivot_idx not in side_pivots:
                return False
            line_at = line.intercept + line.slope * new_pivot_idx
            delta = abs(closes[new_pivot_idx] - line_at)
            if delta <= touch_tol:
                return True
            if near_tol is None or delta > near_tol:
                return False
            # 4th+ near touch — accept only if the line already holds
            # MIN_TOUCHES strict touches from pivots OTHER than the
            # new one. ``line.touches`` already includes loose-counted
            # pivots when ``near_touch_tolerance_fraction`` is in
            # effect, so we recount strict-old here to enforce the
            # "first three must be strict" rule.
            strict_old = 0
            for piv in side_pivots:
                if piv == new_pivot_idx or piv < line.from_idx:
                    continue
                if piv > last_idx:
                    continue
                if abs(closes[piv]
                       - (line.intercept + line.slope * piv)) <= touch_tol:
                    strict_old += 1
            return strict_old >= MIN_TOUCHES

        # Inter-touch spacing gate (FILTER_INTER_TOUCH_SPACING).
        # Rejects a candidate line whose spacing between the last two
        # strict touches and the new pivot is grossly asymmetric in
        # either direction — both directions are degenerate geometry,
        # not just one.
        #
        # Rule (symmetric, tightened 2026-05-19):
        #   g_prev = T_{n-1} - T_{n-2}  (bars between the last two
        #                                 strict touches before new_pivot)
        #   g_new  = new_pivot - T_{n-1}
        #   ratio  = max(g_prev, g_new) / min(g_prev, g_new)
        # Reject when ratio > max_ratio (default 3).
        #
        # Direction A — "mountain attack on valley" (g_prev >> g_new):
        #   long line + new touch lands right after the previous one.
        #   Long line's wide projected band catches the near-miss by
        #   coincidence, not respect.
        # Direction B — "tight-cluster projection" (g_new >> g_prev):
        #   T_{n-2} and T_{n-1} are near-adjacent (e.g. 3 bars apart).
        #   The line is anchored on noise and projected forward 100+
        #   bars to catch a coincidence pivot. Observed live on MNQ
        #   2026-05-19 00:33 — Q at idx 272, T_{n-1} at idx 275 (3
        #   bars later), new pivot at idx 478 (203 bars later). The
        #   original asymmetric rule passed this with ratio 0.015.
        #
        # For a freshly-built Q→P line with no other strict touches,
        # T_{n-2} = from_idx (Q), T_{n-1} = anchor_b_idx (P). For
        # multi-touch lines we find the two most recent strict
        # touches before new_pivot_idx.
        spacing_enabled = bool(self.config.get(
            "entry_inter_touch_spacing_filter_enabled", True,
        ))
        spacing_max_ratio = float(self.config.get(
            "entry_line_max_inter_gap_ratio", 3.0,
        ))

        def _inter_touch_spacing_ok(line, side_pivots) -> tuple[bool, dict]:
            """Return (passes, payload). Payload always carries the
            metrics so the SKIP audit row is self-explaining."""
            if new_pivot_idx < 0:
                return True, {"reason": "no_new_pivot"}
            strict_touches: list[int] = []
            for piv in side_pivots:
                if piv >= new_pivot_idx:
                    break  # side_pivots is ascending
                if piv < line.from_idx:
                    continue
                line_at_piv = line.intercept + line.slope * piv
                if abs(closes[piv] - line_at_piv) <= touch_tol:
                    strict_touches.append(piv)
            if len(strict_touches) < 2:
                return True, {
                    "reason": "insufficient_prior_touches",
                    "prior_strict_count": len(strict_touches),
                }
            t_prev = strict_touches[-1]
            t_prev_prev = strict_touches[-2]
            g_prev = t_prev - t_prev_prev
            g_new = new_pivot_idx - t_prev
            if g_prev <= 0 or g_new <= 0:
                return True, {"reason": "degenerate_gaps"}
            ratio = max(g_prev, g_new) / max(min(g_prev, g_new), 1)
            passes = ratio <= spacing_max_ratio
            return passes, {
                "g_prev": g_prev,
                "g_new": g_new,
                "ratio": round(ratio, 3),
                "max_ratio": spacing_max_ratio,
                "t_prev_prev_idx": t_prev_prev,
                "t_prev_idx": t_prev,
                "new_pivot_idx": new_pivot_idx,
            }

        def _find_2pivot_accel():
            """2-pivot accel: 3 consecutive same-kind pivots, the
            most recent being ``new_pivot_idx``. The older 2 form
            a base line; the new pivot is the "3rd point" that
            overshoots the line in the favorable direction.

            Why 2-pivot (not 3+touch line) — operator decision
            2026-05-18: a 3+touch base line means accel fires at
            the 4th+ pivot, by which time the trend is established
            (often stale). Lowering the base to 2 consecutive
            pivots means accel fires at the 3rd pivot — the
            earliest the directional pattern is recognizable. The
            "consecutive" rule ensures we're working with current
            local structure: no other same-kind pivot between Q
            and P, AND no other between P and new_pivot.

            Conditions (all must hold):
              1. new_pivot_idx is a strict 1/1 pivot of either side.
              2. The 2 immediately-preceding same-side pivots (Q, P)
                 both lie within ``entry_max_q_age_hours`` of the
                 fire bar — local, not weeks-old structure.
              3. Q→P slope has the right sign: positive for support,
                 negative for resistance.
              4. closes[new_pivot] is strictly outside the Q→P line
                 in the favorable direction beyond ``near_tol``.
              5. Implied slope P→new_pivot is at least
                 ``min_slope_ratio`` × steeper than the Q→P slope
                 in the same direction.

            Returns (synthetic_line, direction_str) on hit, None
            otherwise.
            """
            if near_tol is None:
                return None
            if new_pivot_idx < 0:
                return None
            if new_pivot_idx in support_pivots:
                side_pivots = support_pivots
                direction_str = "long"
                line_kind = "support"
            elif new_pivot_idx in resistance_pivots:
                side_pivots = resistance_pivots
                direction_str = "short"
                line_kind = "resistance"
            else:
                return None

            # Last 2 same-kind pivots strictly before new_pivot_idx.
            # ``side_pivots`` is sorted ascending; "consecutive" is
            # implicit (no other same-kind pivot can appear between
            # adjacent list entries).
            earlier = [p for p in side_pivots if p < new_pivot_idx]
            if len(earlier) < 2:
                return None
            P_idx = earlier[-1]
            Q_idx = earlier[-2]

            # Freshness window — both Q and P must lie within
            # entry_max_q_age_hours of the fire bar.
            max_q_age_hours_local = float(self.config.get(
                "entry_max_q_age_hours", 24.0,
            ))
            window_bars_local = int(
                max_q_age_hours_local * 3600.0 / self.bar_seconds
            )
            if Q_idx < last_idx - window_bars_local:
                return None

            if P_idx <= Q_idx or new_pivot_idx <= P_idx:
                return None

            base_span = P_idx - Q_idx
            base_slope = (closes[P_idx] - closes[Q_idx]) / base_span
            base_intercept = closes[P_idx] - base_slope * P_idx

            # Direction-sign check.
            if direction_str == "long" and base_slope <= 0:
                return None
            if direction_str == "short" and base_slope >= 0:
                return None

            # Overshoot check at new_pivot_idx.
            line_at_new = base_intercept + base_slope * new_pivot_idx
            delta = closes[new_pivot_idx] - line_at_new
            if direction_str == "long":
                if delta <= near_tol:
                    return None
            else:
                if delta >= -near_tol:
                    return None

            # Implied-slope check (P → new_pivot).
            implied_span = new_pivot_idx - P_idx
            implied_slope = (closes[new_pivot_idx] - closes[P_idx]) / implied_span
            if direction_str == "long":
                if implied_slope < base_slope * min_slope_ratio:
                    return None
            else:
                if implied_slope > base_slope * min_slope_ratio:
                    return None

            # Synthetic TrendLine — touches=2 (Q + P).
            from ib_trader.signals.sr_fan import TrendLine
            synthetic = TrendLine(
                type=line_kind,
                from_idx=Q_idx,
                anchor_b_idx=P_idx,
                to_idx=last_idx,
                slope=base_slope,
                intercept=base_intercept,
                touches=2,
                break_idx=None,
                third_touch_idx=None,
            )
            return (synthetic, direction_str)

        # Track which path each line passed: ``"touch"`` (strict /
        # 4th+ loose, the original 3rd-touch entry) or ``"accel"``
        # (acceleration continuation). The chosen line's path
        # propagates into the entry_line state so exit eval can
        # skip the line-breach check on accel entries.
        #
        # Iterate the sorted candidate lists (already touches-desc)
        # and pick the FIRST line that passes the touch/accel gate.
        # Previously only longs[0] / shorts[0] were tested; if the
        # universe-best line wasn't the one the pivot lay on,
        # entries silently skipped — observed on MGC 2026-05-15
        # 11:00 PIVOT·H TOUCH·9 PASSED: shorts[0] anchored at idx
        # 108 (line ~$44 away from pivot, no touch) shadowed a
        # different 9-touch line at idx 268 that the pivot strictly
        # touched. After the fix we fall through to that one.
        long_path: str | None = None
        short_path: str | None = None
        long_line = None
        short_line = None
        # Line-validity gate: a broken line is dead structure. The
        # market already chose to violate it; treating new pivots near
        # its projected level as fresh touches would fire entries on
        # zombie lines. ``break_stale_bars`` controls how long the
        # engine RETAINS broken lines for rendering; for ENTRY
        # candidates the answer is "never". Hard-reject — not a
        # marginal-bypassable filter (operator clarification
        # 2026-05-18: validity gates ≠ entry filters).
        # Same reasoning for ``stale_line`` — if the latest strict
        # touch is more than ``entry_max_q_age_hours`` (24h default)
        # old, the line isn't being honored by current participants
        # and shouldn't trigger entries. Stale_line is enforced
        # downstream in its own filter block; broken-line is gated
        # right here at iteration time.
        # Collect spacing-rejected candidates so we can emit a SKIP
        # audit row when the rejection was the reason no entry
        # fired. Without surfacing this, a stale "mountain attack"
        # line would just go silently — operator wouldn't know the
        # bot considered it.
        spacing_rejections: list[dict] = []
        for cand in longs:
            if cand.break_idx is not None:
                continue
            if not _has_new_touch(cand, support_pivots):
                continue
            if spacing_enabled:
                ok, sp_payload = _inter_touch_spacing_ok(
                    cand, support_pivots,
                )
                if not ok:
                    spacing_rejections.append({
                        "dir": "long", "path": "touch",
                        "payload": sp_payload,
                    })
                    continue
            long_line, long_path = cand, "touch"
            break
        for cand in shorts:
            if cand.break_idx is not None:
                continue
            if not _has_new_touch(cand, resistance_pivots):
                continue
            if spacing_enabled:
                ok, sp_payload = _inter_touch_spacing_ok(
                    cand, resistance_pivots,
                )
                if not ok:
                    spacing_rejections.append({
                        "dir": "short", "path": "touch",
                        "payload": sp_payload,
                    })
                    continue
            short_line, short_path = cand, "touch"
            break

        # Accel path: try the 2-pivot consecutive-peaks pattern when
        # no 3+touch TOUCH candidate was found on the matching side.
        # The 2-pivot accel uses the last 2 same-kind pivots as the
        # base line and treats the just-confirmed pivot as the
        # overshoot — fires at the 3rd pivot, not the 4th+ which
        # the old 3+touch-base accel required.
        if accel_enabled:
            accel_result = _find_2pivot_accel()
            if accel_result is not None:
                synth_line, accel_dir = accel_result
                # Apply spacing gate to accel too — the 2-pivot
                # base (Q→P) is vulnerable to the same tolerance
                # attack if Q-P is long and new_pivot is right
                # after P.
                accel_side_pivots = (
                    support_pivots if accel_dir == "long"
                    else resistance_pivots
                )
                accel_passes = True
                accel_sp_payload: dict = {}
                if spacing_enabled:
                    accel_passes, accel_sp_payload = (
                        _inter_touch_spacing_ok(
                            synth_line, accel_side_pivots,
                        )
                    )
                if not accel_passes:
                    spacing_rejections.append({
                        "dir": accel_dir, "path": "accel",
                        "payload": accel_sp_payload,
                    })
                elif accel_dir == "long" and long_line is None:
                    long_line, long_path = synth_line, "accel"
                elif accel_dir == "short" and short_line is None:
                    short_line, short_path = synth_line, "accel"

        # If no candidate survived AND we have spacing rejections,
        # emit a single SKIP for the first one so the operator can
        # see the gate fired. Multiple rejections per bar collapse
        # into one row; the payload's metrics describe the strongest
        # rejection (first encountered).
        if (long_line is None and short_line is None
                and spacing_rejections):
            rej = spacing_rejections[0]
            p = rej["payload"]
            actions.append(LogSignal(
                event_type=LogEventType.SKIP,
                message=(
                    f"{FILTER_INTER_TOUCH_SPACING} "
                    f"({rej['dir'].upper()}) — prior gap "
                    f"{p.get('g_prev', '?')} bars vs new gap "
                    f"{p.get('g_new', '?')} bars (ratio "
                    f"{p.get('ratio', '?')} > "
                    f"{p.get('max_ratio', '?')}); spacing too "
                    f"asymmetric — line is either tolerance-attacked "
                    f"or anchored on a tight cluster projected forward"
                ),
                payload={
                    "filter": FILTER_INTER_TOUCH_SPACING,
                    "marginal": False,
                    "direction": rej["dir"],
                    "path": rej["path"],
                    **p,
                },
            ))
            return actions

        if long_path is None:
            long_line = None
        if short_path is None:
            short_line = None

        # Marginal-entry mode (``allow_marginal_entries`` config flag).
        # When enabled, the per-side filters (shoulder, min_target) and
        # the post-chosen filter (far_from_pivot) tag the trade as
        # "marginal" instead of nuking the line. The entry still fires
        # but uses the tighter counter_line exit (cache contains both
        # sides' lines, not just opposing). Operator can A/B clean vs
        # marginal at trade level via the audit feed.
        allow_marginal = bool(self.config.get(
            "allow_marginal_entries", False,
        ))
        marginal_filters_long: list[str] = []
        marginal_filters_short: list[str] = []

        # Entry-decision diagnostic — one structured block captured on
        # EVERY eval (including when no entry fires). Lets us answer
        # "why didn't this bar fire?" without restarting with a more
        # verbose log level. Kept compact so payload stays cheap.
        def _line_diag(line, side: str) -> dict | None:
            if line is None:
                return None
            line_at = line.intercept + line.slope * new_pivot_idx
            delta = closes[new_pivot_idx] - line_at if new_pivot_idx >= 0 else None
            return {
                "touches": line.touches,
                "slope": round(line.slope, 4),
                "from_idx": line.from_idx,
                "anchor_b_idx": line.anchor_b_idx,
                "q_close": round(closes[line.from_idx], 4)
                    if 0 <= line.from_idx < len(closes) else None,
                "p_close": round(closes[line.anchor_b_idx], 4)
                    if 0 <= line.anchor_b_idx < len(closes) else None,
                "line_at_new_pivot": round(line_at, 4),
                "delta_from_line": round(delta, 4) if delta is not None else None,
            }
        decision_diag = {
            "new_pivot_idx": new_pivot_idx,
            "new_pivot_close": (
                round(closes[new_pivot_idx], 4)
                if 0 <= new_pivot_idx < len(closes) else None
            ),
            "new_is_pivot_low": new_pivot_idx in support_pivots,
            "new_is_pivot_high": new_pivot_idx in resistance_pivots,
            "left_shoulder_close": (
                round(closes[new_pivot_idx - 1], 4)
                if 1 <= new_pivot_idx < len(closes) else None
            ),
            "right_shoulder_close": (
                round(closes[last_idx], 4)
                if 0 <= last_idx < len(closes) else None
            ),
            "touch_tol": round(touch_tol, 4),
            "near_tol": round(near_tol, 4) if near_tol is not None else None,
            "long_path_pre_filter": long_path,
            "short_path_pre_filter": short_path,
            "candidate_long": _line_diag(long_line, "LONG"),
            "candidate_short": _line_diag(short_line, "SHORT"),
        }

        # Bad-shoulder entry filter. Operator's visual rule:
        #   B (LONG)  — the bar AFTER the pivot must close HIGHER than
        #               the bar BEFORE the pivot (right shoulder > left).
        #   S (SHORT) — the bar AFTER the pivot must close LOWER than
        #               the bar BEFORE the pivot (right shoulder < left).
        # Pivot at p = new_pivot_idx; left shoulder = bar p−1; right
        # shoulder = bar p+1 = the entry bar (last_idx). Inclusive
        # comparison so flat right-shoulders (=) are also rejected.
        # 30d TRADES sweep showed this filter saves $14.6k on MGC
        # (clear edge) but INVERTS on MNQ — opt MNQ out via per-bot
        # YAML (``entry_shoulder_filter_enabled: false``).
        filt_enabled = bool(self.config.get(
            "entry_shoulder_filter_enabled", True,
        ))
        if filt_enabled and (long_line is not None or short_line is not None):
            if new_pivot_idx >= 1 and last_idx <= len(closes) - 1:
                left_shoulder = closes[new_pivot_idx - 1]
                right_shoulder = closes[last_idx]
                rejected_by_filter = False
                rejected_payload: dict = {}
                if long_line is not None and right_shoulder <= left_shoulder:
                    rejected_payload = {
                        "side": "LONG",
                        "left_shoulder": round(left_shoulder, 4),
                        "right_shoulder": round(right_shoulder, 4),
                    }
                    # Marginal mode: tag but don't nuke. Else: hard-reject.
                    if allow_marginal:
                        marginal_filters_long.append(FILTER_SHOULDER)
                    else:
                        long_line = None
                    rejected_by_filter = True
                if short_line is not None and right_shoulder >= left_shoulder:
                    rejected_payload = {
                        "side": "SHORT",
                        "left_shoulder": round(left_shoulder, 4),
                        "right_shoulder": round(right_shoulder, 4),
                    }
                    if allow_marginal:
                        marginal_filters_short.append(FILTER_SHOULDER)
                    else:
                        short_line = None
                    rejected_by_filter = True
                if rejected_by_filter:
                    actions.append(LogSignal(
                        event_type=LogEventType.SKIP,
                        message=(
                            f"{FILTER_SHOULDER} filter — right shoulder "
                            f"{rejected_payload['right_shoulder']:.4f} "
                            f"vs left {rejected_payload['left_shoulder']:.4f}"
                            + (" [marginal mode]" if allow_marginal else "")
                        ),
                        payload={"filter": FILTER_SHOULDER,
                                 "marginal": allow_marginal,
                                 **rejected_payload,
                                 "entry_decision": decision_diag},
                    ))

        # Tight-triangle entry block. If we're inside a converging
        # triangle whose apex is within ``entry_triangle_block_distance``
        # bars of the current bar, suppress the entry — the wedge is
        # about to resolve in EITHER direction and a fresh position
        # is exposed to the breakout going the wrong way. Set to 0 to
        # disable. Mirrors the on-chart wedge overlay's apex math.
        #
        # 15-mo BID_ASK backtest (run_15mo_tight_triangle.py):
        #   block ≤1  → −$10k aggregate vs baseline (mild cost)
        #   block ≤3  → −$33k aggregate vs baseline (too aggressive)
        #   block ≤5  → −$45k
        # Default 2 is a compromise: it blocks the most-imminent
        # ("apex inside the next 6 min") wedges where the operator
        # is most likely to get whipsawed, while still entering at
        # apex-3-bars-away where there's room to manage the trade.
        # Compute wedges once — shared by the tight-triangle and
        # min-target filters below.
        wedges: list = []
        if long_line is not None or short_line is not None:
            wedge_max_apex = int(self.config.get(
                "wedge_max_apex_bars_ahead", 200,
            ))
            wedge_min_overlap = int(self.config.get(
                "wedge_min_overlap_bars", 5,
            ))
            # Flat-slope threshold so the bot and chart agree on
            # which wedges to ignore (same-direction trendlines —
            # see find_wedges docstring). Tied to TOUCH_TOLERANCE,
            # matches /engine/sr.
            avg_close = (
                sum(closes) / len(closes) if closes else 0.0
            )
            flat_eps = avg_close * TOUCH_TOLERANCE_FRACTION / 20.0
            wedges = find_wedges(
                supports, resistances, last_idx,
                max_apex_bars_ahead=wedge_max_apex,
                min_overlap_bars=wedge_min_overlap,
                flat_slope_threshold=flat_eps,
            )

        tri_block_dist = int(self.config.get(
            "entry_triangle_block_distance", 2,
        ))
        if tri_block_dist > 0 and (
            long_line is not None or short_line is not None
        ):
            apex_min = wedges[0].apex_bars_ahead if wedges else None
            if apex_min is not None and apex_min <= tri_block_dist:
                actions.append(LogSignal(
                    event_type=LogEventType.SKIP,
                    message=(
                        f"{FILTER_TIGHT_TRIANGLE} filter — apex {apex_min} "
                        f"bars ahead (≤ {tri_block_dist}); waiting for "
                        f"resolution"
                        + (" [marginal mode]" if allow_marginal else "")
                    ),
                    payload={"filter": FILTER_TIGHT_TRIANGLE,
                             "marginal": allow_marginal,
                             "apex_bars_ahead": apex_min,
                             "threshold": tri_block_dist,
                             "entry_decision": decision_diag},
                ))
                # Marginal mode: tag whichever direction(s) have a
                # candidate and let the trade fire. Tight-exit
                # machinery (counter_line linger + trigger-line
                # linger + tick-time SL on marginals) handles the
                # whipsaw risk that this filter was guarding against.
                if allow_marginal:
                    if long_line is not None:
                        marginal_filters_long.append(FILTER_TIGHT_TRIANGLE)
                    if short_line is not None:
                        marginal_filters_short.append(FILTER_TIGHT_TRIANGLE)
                else:
                    long_line = None
                    short_line = None

        # Min-target entry filter (FILTER_MIN_TARGET). Uses the
        # opposing edge of the nearest WEDGE — the structural
        # ceiling/floor that price is converging toward. If no wedge
        # exists, the filter doesn't apply (entry allowed).
        #
        # Apex distance is NOT capped here: even a far-future apex
        # implies a near-term opposing edge worth measuring against.
        # We rely on ``find_wedges``' default ``max_apex_bars_ahead``
        # only for sanity (very-far-future apexes are usually
        # nearly-parallel lines and uninteresting).
        #
        # Geometry — evaluated at the current bar's x:
        #   LONG  : opposing edge = nearest wedge's resistance line.
        #   SHORT : opposing edge = nearest wedge's support    line.
        # Entry price proxy: closes[last_idx] (mid-fill assumption).
        #
        # Stop distance: max(trail_dist, line_breach_dist). The trail
        # is ``entry * trail_width_pct``; the line-breach distance is
        # ``|entry - chosen_line_at_last_idx|`` — usually small at a
        # fresh touch but recorded for completeness. Whichever is wider
        # is the worst-case loss the trade can take.
        mt_enabled = bool(self.config.get(
            "entry_min_target_filter_enabled", True,
        ))
        if mt_enabled and wedges and (
            long_line is not None or short_line is not None
        ):
            nearest_wedge = wedges[0]
            entry_price = closes[last_idx]
            trail_pct = float(self.config.get("trail_width_pct", 0.0003))
            trail_dist = abs(entry_price) * trail_pct

            def _min_target_reject(side_line, side: str) -> dict | None:
                if side == "LONG":
                    opp = nearest_wedge.resistance
                else:
                    opp = nearest_wedge.support
                opp_at = opp.intercept + opp.slope * last_idx
                raw_target_dist = (
                    opp_at - entry_price if side == "LONG"
                    else entry_price - opp_at
                )
                # Wedge-already-broken case: if the opposing edge has
                # crossed past the entry direction (resistance below a
                # LONG entry / support above a SHORT entry), the wedge
                # is structurally behind us — price has already broken
                # out of the converging structure. min_target's
                # "is the wedge target worth the stop" question is
                # MOOT in that case. Skip the filter entirely so the
                # entry isn't blocked on a wedge that no longer exists
                # as a constraint. Per operator decision 2026-05-14
                # after MNQ 21:42 PT setup got killed this way.
                if raw_target_dist <= 0:
                    return None
                target_dist = raw_target_dist
                line_at = (
                    side_line.intercept + side_line.slope * last_idx
                )
                line_breach_dist = abs(entry_price - line_at)
                stop_dist = max(trail_dist, line_breach_dist)
                if target_dist >= stop_dist:
                    return None
                return {
                    "filter": FILTER_MIN_TARGET,
                    "side": side,
                    "entry_price": round(entry_price, 4),
                    "opposing_edge_price": round(opp_at, 4),
                    "opposing_line_touches": opp.touches,
                    "opposing_line_slope": round(opp.slope, 4),
                    "wedge_apex_bars_ahead": nearest_wedge.apex_bars_ahead,
                    "target_distance": round(target_dist, 4),
                    "stop_distance": round(stop_dist, 4),
                    "trail_distance": round(trail_dist, 4),
                    "line_breach_distance": round(line_breach_dist, 4),
                }

            if long_line is not None:
                rej = _min_target_reject(long_line, "LONG")
                if rej is not None:
                    actions.append(LogSignal(
                        event_type=LogEventType.SKIP,
                        message=(
                            f"{FILTER_MIN_TARGET} filter (LONG) — target "
                            f"${rej['target_distance']:.4f} < stop "
                            f"${rej['stop_distance']:.4f} (opposing R at "
                            f"{rej['opposing_edge_price']:.4f}, entry "
                            f"{rej['entry_price']:.4f})"
                            + (" [marginal mode]" if allow_marginal else "")
                        ),
                        payload={**rej, "marginal": allow_marginal,
                                  "entry_decision": decision_diag},
                    ))
                    if allow_marginal:
                        marginal_filters_long.append(FILTER_MIN_TARGET)
                    else:
                        long_line = None
            if short_line is not None:
                rej = _min_target_reject(short_line, "SHORT")
                if rej is not None:
                    actions.append(LogSignal(
                        event_type=LogEventType.SKIP,
                        message=(
                            f"{FILTER_MIN_TARGET} filter (SHORT) — target "
                            f"${rej['target_distance']:.4f} < stop "
                            f"${rej['stop_distance']:.4f} (opposing S at "
                            f"{rej['opposing_edge_price']:.4f}, entry "
                            f"{rej['entry_price']:.4f})"
                            + (" [marginal mode]" if allow_marginal else "")
                        ),
                        payload={**rej, "marginal": allow_marginal,
                                  "entry_decision": decision_diag},
                    ))
                    if allow_marginal:
                        marginal_filters_short.append(FILTER_MIN_TARGET)
                    else:
                        short_line = None

        if long_line is None and short_line is None:
            actions.append(LogSignal(
                event_type=LogEventType.SKIP,
                message=(
                    f"3-touch line(s) found but no new pivot landed on "
                    f"any of them this bar (new_pivot_idx={new_pivot_idx})"
                ),
                payload={"new_pivot_idx": new_pivot_idx,
                         "last_idx": last_idx,
                         "new_is_pivot_low": new_pivot_idx in support_pivots,
                         "new_is_pivot_high": new_pivot_idx in resistance_pivots,
                         "entry_decision": decision_diag},
            ))
            return actions

        # Guard 2 — wallclock signal age. Compare bar-close wallclock
        # to ``datetime.now()``; if the gap exceeds the configured
        # cap we drop the signal (queued/stale bar event).
        from datetime import timedelta as _td
        max_signal_age_s = float(self.config.get(
            "max_signal_age_seconds", 10,
        ))
        bar_close_dt = bar_time + _td(seconds=self.bar_seconds)
        now_dt = datetime.now(timezone.utc)
        elapsed_s = (now_dt - bar_close_dt).total_seconds()
        if elapsed_s > max_signal_age_s:
            actions.append(LogSignal(
                event_type=LogEventType.SKIP,
                message=(
                    f"signal too old to act on — bar closed "
                    f"{elapsed_s:.1f}s ago (cap {max_signal_age_s:.1f}s)"
                ),
                payload={"elapsed_seconds": round(elapsed_s, 2),
                         "max_signal_age_seconds": max_signal_age_s,
                         "bar_close": bar_close_dt.isoformat(),
                         "entry_decision": decision_diag},
            ))
            return actions

        if long_line and short_line:
            if short_line.touches > long_line.touches:
                chosen, direction, kind = short_line, "short", "resistance"
                chosen_path = short_path
            else:
                chosen, direction, kind = long_line, "long", "support"
                chosen_path = long_path
        elif long_line:
            chosen, direction, kind = long_line, "long", "support"
            chosen_path = long_path
        elif short_line:
            chosen, direction, kind = short_line, "short", "resistance"
            chosen_path = short_path
        else:
            return actions
        is_acceleration_entry = chosen_path == "accel"

        # Counter-trend filter (FILTER_COUNTER_TREND). Reject SHORT on
        # an up-sloping resistance and LONG on a down-sloping support
        # — those entries fade their own direction (e.g., a resistance
        # whose touches are STEPPING UP is the rally's upper envelope,
        # not failed sellers). Slope == 0 (horizontal) is kept either
        # way; an honest level is not counter-trend.
        # Mirrors the chart frontend's ``showCounterResistance``
        # /``showCounterSupport`` defaults so the bot doesn't enter on
        # lines the operator can't even see by default.
        # Bypassable in marginal mode; emits a SKIP either way so the
        # audit feed records the counter-trend identification.
        # Operator-driven 2026-05-18 after observing every MGC rally
        # peak post-17:12 PT fire a SHORT.
        ct_enabled = bool(self.config.get(
            "entry_counter_trend_filter_enabled", True,
        ))
        if ct_enabled:
            ct_violation = (
                (direction == "long" and chosen.slope < 0)
                or (direction == "short" and chosen.slope > 0)
            )
            if ct_violation:
                actions.append(LogSignal(
                    event_type=LogEventType.SKIP,
                    message=(
                        f"{FILTER_COUNTER_TREND} filter "
                        f"({direction.upper()}) — chosen "
                        f"{kind} line slope={chosen.slope:+.4f}/bar "
                        f"is counter-trend to entry direction"
                        + (" [marginal mode]" if allow_marginal else "")
                    ),
                    payload={
                        "filter": FILTER_COUNTER_TREND,
                        "marginal": allow_marginal,
                        "direction": direction,
                        "line_kind": kind,
                        "line_slope_per_bar": round(chosen.slope, 6),
                        "entry_decision": decision_diag,
                    },
                ))
                if allow_marginal:
                    if direction == "long":
                        marginal_filters_long.append(FILTER_COUNTER_TREND)
                    else:
                        marginal_filters_short.append(FILTER_COUNTER_TREND)
                else:
                    return actions

        # Far-from-pivot filter (FILTER_FAR_FROM_PIVOT).
        # Hard-reject (NOT bypassable, regardless of allow_marginal
        # or entry_path — operator decisions 2026-05-18). A fire-bar
        # close that's multiple-trail-widths past the line means the
        # rejection already played out on a prior bar; entering now
        # is structurally "into thin air" and the trail-stop will
        # breach on any normal reversal. The "tight exit will save
        # us" argument doesn't apply when the entry is already past
        # the exit threshold.
        #
        # 2026-05-18: removed the acceleration-entry exemption AND
        # changed the reference point from the LINE to the just-
        # confirmed PIVOT itself. The filter's name was always "far
        # from pivot" but the math was ``|entry - line_at_fire_bar|``,
        # which for touch entries (pivot on line) is approximately
        # the same number but for accel entries (pivot beyond line by
        # design) measured the overshoot itself and rejected every
        # accel.
        #
        # Now: ``gap = |entry_price - pivot_close|`` — the drift
        # between the just-confirmed pivot bar's close and the fire
        # bar's close. Same intent ("did the rejection play out on a
        # prior bar?") applied uniformly to both detection paths. A
        # touch pivot followed by a tight fire bar passes. An accel
        # pivot followed by a tight fire bar also passes. A late
        # fire bar (drifted from the pivot) gets rejected regardless
        # of how the pivot was detected.
        #
        # Cap = trail_dist × ``far_from_pivot_max_trail_mult`` (default
        # 2.0). MGC trail $0.93 → cap $1.86; MES $2.26 → $4.52;
        # MNQ $5.94 → $11.88.
        ffp_enabled = bool(self.config.get(
            "far_from_pivot_filter_enabled",
            # Back-compat with the old config key name.
            self.config.get("entry_distance_filter_enabled", True),
        ))
        if ffp_enabled:
            entry_price = closes[last_idx]
            pivot_close = closes[last_idx - 1]
            trail_pct = float(self.config.get("trail_width_pct", 0.0003))
            trail_dist = abs(entry_price) * trail_pct
            mult = float(self.config.get(
                "far_from_pivot_max_trail_mult",
                self.config.get("entry_distance_max_trail_mult", 2.0),
            ))
            cap = trail_dist * mult
            line_at = chosen.intercept + chosen.slope * last_idx
            # Slope-compensated pivot reference (2026-05-18 fix).
            # Raw |entry − pivot_close| ignored the line's natural
            # per-bar drift. On a steep up-sloping support
            # (+$1.22/bar on MGC), the line itself rises by that
            # amount per bar — the eval bar's close is expected to
            # be ~$1.22 above the pivot just from the line rising,
            # before any actual price motion. Pivot-based gap with
            # no compensation rejected touch entries on steep lines
            # for normal bar-to-bar drift.
            #
            # Now: project the pivot's "natural" position one bar
            # forward by the line slope, and measure the gap from
            # THAT. For touch entries (pivot on line),
            # ``expected_pivot_pos == line_at_eval_bar`` so this is
            # geometrically identical to the original line-based
            # FFP. For accel entries (pivot beyond line),
            # ``expected_pivot_pos`` stays close to pivot_close,
            # so the filter still catches "fire bar drifted too far
            # from the overshoot pivot" (the MNQ 14:54 case).
            bars_offset = last_idx - (last_idx - 1)  # = 1 in normal flow
            expected_pivot_pos = (
                pivot_close + chosen.slope * bars_offset
            )
            gap = abs(entry_price - expected_pivot_pos)
            if gap > cap:
                actions.append(LogSignal(
                    event_type=LogEventType.SKIP,
                    message=(
                        f"{FILTER_FAR_FROM_PIVOT} filter ({direction.upper()}) — "
                        f"fire bar drifted ${gap:.4f} past pivot's "
                        f"expected position (cap ${cap:.4f} = "
                        f"{mult:.1f}× trail). pivot @ "
                        f"{pivot_close:.4f} + slope×{bars_offset} = "
                        f"{expected_pivot_pos:.4f}, entry @ "
                        f"{entry_price:.4f}, line @ {line_at:.4f}"
                    ),
                    payload={
                        "filter": FILTER_FAR_FROM_PIVOT,
                        # Hard-reject — not marginal-bypassable.
                        "marginal": False,
                        "direction": direction,
                        "entry_price": round(entry_price, 4),
                        "pivot_close": round(pivot_close, 4),
                        "expected_pivot_pos": round(expected_pivot_pos, 4),
                        "line_value": round(line_at, 4),
                        "gap": round(gap, 4),
                        "cap": round(cap, 4),
                        "trail_distance": round(trail_dist, 4),
                        "mult": mult,
                        "entry_decision": decision_diag,
                    },
                ))
                return actions

        # Stale-line filter (FILTER_STALE_LINE).
        # Line-validity gate (hard-reject; not bypassable in marginal
        # mode — operator clarification 2026-05-18).
        #
        # Rule: require AT LEAST ``entry_min_recent_strict_touches``
        # (default 2) strict touches on the chosen line within the
        # last ``entry_max_q_age_hours`` (default 24h) of bars.
        sl_enabled = bool(self.config.get(
            "entry_stale_line_filter_enabled",
            # Back-compat with old key name.
            self.config.get("entry_q_session_filter_enabled", True),
        ))
        max_q_age_hours = float(self.config.get(
            "entry_max_q_age_hours", 24.0,
        ))
        min_recent_strict = int(self.config.get(
            "entry_min_recent_strict_touches", 2,
        ))
        if sl_enabled:
            side_pivots = (
                support_pivots if direction == "long"
                else resistance_pivots
            )
            to_idx = (
                chosen.break_idx if chosen.break_idx is not None
                else last_idx
            )
            window_bars = int(
                max_q_age_hours * 3600.0 / self.bar_seconds
            )
            window_start_idx = max(0, last_idx - window_bars)
            recent_strict = 0
            latest_touch_idx = -1
            for piv in side_pivots:
                if piv < window_start_idx or piv > to_idx:
                    continue
                line_val_at_piv = (
                    chosen.intercept + chosen.slope * piv
                )
                if abs(closes[piv] - line_val_at_piv) <= touch_tol:
                    recent_strict += 1
                    if piv > latest_touch_idx:
                        latest_touch_idx = piv
            if recent_strict < min_recent_strict:
                lt_time_iso = None
                if 0 <= latest_touch_idx < len(window):
                    wlt = window[latest_touch_idx]
                    lt = _parse_ts(
                        wlt.get("timestamp_utc") or wlt.get("ts"),
                    )
                    if lt is not None:
                        lt_time_iso = lt.isoformat()
                actions.append(LogSignal(
                    event_type=LogEventType.SKIP,
                    message=(
                        f"{FILTER_STALE_LINE} filter ({direction.upper()})"
                        f" — only {recent_strict} strict touch(es) in "
                        f"the last {max_q_age_hours:.0f}h (need "
                        f"≥ {min_recent_strict}); line is stale "
                        f"relative to current price action"
                    ),
                    payload={
                        "filter": FILTER_STALE_LINE,
                        "marginal": False,
                        "direction": direction,
                        "recent_strict_touches": recent_strict,
                        "min_recent_strict": min_recent_strict,
                        "max_age_hours": max_q_age_hours,
                        "latest_strict_touch_time": lt_time_iso,
                        "entry_decision": decision_diag,
                    },
                ))
                return actions

        # Opposing-dominance filter (FILTER_OPPOSING_DOMINANCE).
        # Reject when the OPPOSITE-side market structure has many more
        # touches than the chosen line — a signal that the prevailing
        # trend is against this trade. For LONG, opposing = strongest
        # resistance line; for SHORT, opposing = strongest support.
        # Cap ratio default 3.0 (configurable). 18:36 PT MES on
        # 2026-05-14 had 4-touch chosen support vs 20-touch dominant
        # resistance (5× ratio) → reject.
        od_enabled = bool(self.config.get(
            "entry_opposing_dominance_filter_enabled", True,
        ))
        if od_enabled:
            opp_pool = resistances if direction == "long" else supports
            opp_max_touches = max(
                (l.touches for l in opp_pool if l.break_idx is None),
                default=0,
            )
            ratio = float(self.config.get(
                "entry_opposing_dominance_ratio", 3.0,
            ))
            if (chosen.touches > 0
                    and opp_max_touches >= chosen.touches * ratio):
                actions.append(LogSignal(
                    event_type=LogEventType.SKIP,
                    message=(
                        f"{FILTER_OPPOSING_DOMINANCE} filter "
                        f"({direction.upper()}) — opposing-side max "
                        f"touches {opp_max_touches} ≥ {ratio:.1f}× "
                        f"chosen touches {chosen.touches} "
                        f"(market structure dominates against trade)"
                        + (" [marginal mode]" if allow_marginal else "")
                    ),
                    payload={
                        "filter": FILTER_OPPOSING_DOMINANCE,
                        "marginal": allow_marginal,
                        "direction": direction,
                        "chosen_touches": int(chosen.touches),
                        "opposing_max_touches": int(opp_max_touches),
                        "ratio_cap": ratio,
                        "actual_ratio": round(
                            opp_max_touches / chosen.touches, 2,
                        ),
                        "entry_decision": decision_diag,
                    },
                ))
                if allow_marginal:
                    if direction == "long":
                        marginal_filters_long.append(FILTER_OPPOSING_DOMINANCE)
                    else:
                        marginal_filters_short.append(FILTER_OPPOSING_DOMINANCE)
                else:
                    return actions

        # Freeze the line in (time, price) space so future evaluations
        # survive the window sliding forward.
        # ``/engine/history`` bars expose timestamps as ``ts`` whereas
        # the in-process aggregator's bars use ``timestamp_utc``.
        # Falling back keeps anchor / Q resolution correct regardless
        # of which feed populated the window (otherwise we silently
        # recorded ``anchor_time = bar_time`` for every entry).
        def _bar_ts(b: dict):
            return _parse_ts(b.get("timestamp_utc") or b.get("ts"))
        anchor_b_time = (
            _bar_ts(window[chosen.anchor_b_idx])
            if 0 <= chosen.anchor_b_idx < len(window)
            else bar_time
        )
        anchor_b_price = float(window[chosen.anchor_b_idx].get(
            "close", closes[chosen.anchor_b_idx]
        )) if 0 <= chosen.anchor_b_idx < len(window) else closes[chosen.anchor_b_idx]
        # Q (first construction pivot) timestamp — the chart clips the
        # rendered entry-line at this point so the operator only sees
        # the section the bot actually validated. Without ``from_time``
        # the chart extrapolates the line all the way back to ``bars[0]``
        # which looks like a long-running trend the bot never claimed.
        anchor_q_time = (
            _bar_ts(window[chosen.from_idx])
            if 0 <= chosen.from_idx < len(window)
            else None
        )
        slope_per_sec = chosen.slope / self.bar_seconds

        # Resolve marginal status for the chosen side. Used by the
        # counter_line cache build to decide between opposing-only
        # (clean) and all-sides-except-trigger (marginal/tight) caches.
        chosen_marginal_filters = (
            marginal_filters_long if direction == "long"
            else marginal_filters_short
        )
        is_marginal_entry = bool(chosen_marginal_filters)

        # Marginal-bypass cap (2026-05-18). When too many filters
        # would have been waved through by allow_marginal_entries,
        # the entry is structurally too uncertain — reject outright
        # instead of firing as marginal. Operator rule: 1-2 bypassed
        # filters acceptable, 3+ is too much uncertainty.
        marginal_cap = int(self.config.get(
            "entry_marginal_max_bypassed_filters", 2,
        ))
        if (is_marginal_entry
                and len(chosen_marginal_filters) > marginal_cap):
            actions.append(LogSignal(
                event_type=LogEventType.SKIP,
                message=(
                    f"{FILTER_TOO_MANY_MARGINAL_BYPASSES} "
                    f"({direction.upper()}) — "
                    f"{len(chosen_marginal_filters)} filters would "
                    f"be bypassed ({', '.join(chosen_marginal_filters)}); "
                    f"cap is {marginal_cap}, entry rejected"
                ),
                payload={
                    "filter": FILTER_TOO_MANY_MARGINAL_BYPASSES,
                    "marginal": False,
                    "direction": direction,
                    "bypassed_filters": list(chosen_marginal_filters),
                    "bypass_count": len(chosen_marginal_filters),
                    "cap": marginal_cap,
                    "entry_decision": decision_diag,
                },
            ))
            return actions

        # Trigger line value at the fire bar (last_idx). Frozen here
        # so ``_on_fill`` can seed the marginal-mode tight-zones cache
        # the moment the entry fill arrives — before the first bar
        # close has had a chance to populate it via _evaluate_exit.
        # Drift over the first 3 min is negligible vs the entry
        # cushion at typical slopes.
        trigger_line_value_at_entry = (
            chosen.intercept + chosen.slope * last_idx
        )
        # Two prior strict touches (the spacing-helper view) — what
        # the line actually rested on before the firing pivot.
        # Distinguishes "Q + P" (line construction) from
        # "Touch 1 + Touch 2" (last two strict touches before
        # new_pivot). When anchor_b == new_pivot, the audit's
        # construction P is the firing pivot itself; surfacing the
        # real prior touches lets the operator judge line quality.
        _chosen_side_pivots = (
            support_pivots if kind == "support" else resistance_pivots
        )
        _prior_strict: list[int] = []
        for _piv in _chosen_side_pivots:
            if _piv >= new_pivot_idx:
                break
            if _piv < chosen.from_idx:
                continue
            if abs(closes[_piv]
                   - (chosen.intercept + chosen.slope * _piv)) <= touch_tol:
                _prior_strict.append(_piv)
        _pt1 = _prior_strict[-2] if len(_prior_strict) >= 2 else None
        _pt2 = _prior_strict[-1] if len(_prior_strict) >= 1 else None
        _bar_ts_safe = lambda i: (
            _bar_ts(window[i])
            if i is not None and 0 <= i < len(window) else None
        )
        _pt1_dt = _bar_ts_safe(_pt1)
        _pt2_dt = _bar_ts_safe(_pt2)
        entry_line_doc = {
            "kind": kind,
            "direction": direction,
            "slope_per_bar": chosen.slope,
            "intercept": chosen.intercept,
            "slope_per_sec": slope_per_sec,
            "line_value_at_entry": trigger_line_value_at_entry,
            "anchor_time": (anchor_b_time or bar_time).isoformat(),
            "anchor_price": anchor_b_price,
            "anchor_b_idx": chosen.anchor_b_idx,
            "from_idx": chosen.from_idx,
            "from_time": (anchor_q_time or anchor_b_time or bar_time).isoformat(),
            "touches": chosen.touches,
            "prior_touch_1_idx": _pt1,
            "prior_touch_2_idx": _pt2,
            "prior_touch_1_time": (
                _pt1_dt.isoformat() if _pt1_dt is not None else None
            ),
            "prior_touch_2_time": (
                _pt2_dt.isoformat() if _pt2_dt is not None else None
            ),
            "prior_touch_1_close": (
                round(closes[_pt1], 4) if _pt1 is not None else None
            ),
            "prior_touch_2_close": (
                round(closes[_pt2], 4) if _pt2 is not None else None
            ),
            "prior_strict_count": len(_prior_strict),
            # Flag the exit path: ``acceleration`` entries skip the
            # line-breach gate (price is far from the line and the
            # line is no longer a useful stop reference).
            "entry_path": "accel" if is_acceleration_entry else "touch",
            # Marginal-mode bookkeeping. ``marginal=True`` means the
            # entry would have been rejected by at least one of the
            # bypassable filters (shoulder / far_from_pivot /
            # min_target) but ``allow_marginal_entries=True`` let it
            # fire anyway. Exit path uses the tight counter_line cache.
            "marginal": is_marginal_entry,
            "marginal_filters": list(chosen_marginal_filters),
        }

        qty = int(self.config.get("qty_default", 1))
        try:
            qty_override = ctx.state.get("qty_override")
            if qty_override not in (None, ""):
                qty = max(1, int(Decimal(str(qty_override))))
        except Exception:  # noqa: BLE001 — never block entry on a malformed override
            qty = int(self.config.get("qty_default", 1))

        side = "BUY" if direction == "long" else "SELL"
        label = "BUY — uptrending support" if direction == "long" \
            else "SELL — downtrending resistance"
        # Diagnostic snapshot: dump the closes window around the new
        # pivot + the bot's pivot-membership decision so post-hoc
        # investigations (``explain.py``) don't have to reverse-
        # engineer whether the strict-pivot gate was satisfied.
        diag_window = closes[max(0, new_pivot_idx - 2): new_pivot_idx + 3]
        actions.append(LogSignal(
            event_type=LogEventType.SIGNAL,
            message=(
                f"{label} 3-touch (touches={chosen.touches}, "
                f"slope/bar={chosen.slope:.4f})"
            ),
            payload={"qty": qty, "entry_line": entry_line_doc,
                     "bar_time": bar_time.isoformat(),
                     "diag": {
                         "n_bars": len(closes),
                         "last_idx": last_idx,
                         "new_pivot_idx": new_pivot_idx,
                         "closes_around_pivot": [
                             round(c, 4) for c in diag_window
                         ],
                         "new_is_pivot_low":
                             new_pivot_idx in support_pivots,
                         "new_is_pivot_high":
                             new_pivot_idx in resistance_pivots,
                     },
                     "entry_decision": decision_diag},
        ))
        actions.append(PlaceOrder(
            symbol=self.config["symbol"],
            side=side,
            qty=Decimal(str(qty)),
            order_type=self.config.get("order_strategy", "mid"),
        ))
        # ``entry_bar_time`` should mark the bar that triggered the
        # entry — i.e. the just-confirmed PIVOT at last_idx-1, NOT the
        # just-closed bar (last_idx) at which the bot evaluated. The
        # chart anchors its B/S badge here; storing bar_time made the
        # badge land one slot too late (visible in the 2026-05-12
        # MGCM6 15:21 entry where the operator's chart drew B at the
        # 15:18 bar but the actual pivot was the 15:15 bar).
        pivot_t = (
            _bar_ts(window[new_pivot_idx])
            if 0 <= new_pivot_idx < len(window)
            else None
        )
        if pivot_t is None:
            pivot_t = bar_time - timedelta(seconds=self.bar_seconds)
        actions.append(UpdateState({
            "entry_line": entry_line_doc,
            "entry_bar_time": pivot_t.isoformat(),
        }))
        return actions

    async def _evaluate_exit(self, event: BarCompleted, ctx: StrategyContext,
                              bar_time: datetime) -> list[Action]:
        actions: list[Action] = []
        entry_line = ctx.state.get("entry_line") or {}
        if not entry_line:
            # No line frozen — can't evaluate. Surface and bail.
            actions.append(LogSignal(
                event_type=LogEventType.ERROR,
                message="exit eval skipped — entry_line missing from state",
            ))
            return actions

        bar_close = float(event.bar.get("close", 0))
        anchor_t = _parse_ts(entry_line.get("anchor_time"))
        anchor_p = float(entry_line.get("anchor_price", 0))
        slope_per_sec = float(entry_line.get("slope_per_sec", 0))
        if anchor_t is None or anchor_p <= 0:
            actions.append(LogSignal(
                event_type=LogEventType.ERROR,
                message="exit eval skipped — entry_line anchor invalid",
                payload={"entry_line": entry_line},
            ))
            return actions

        # Deadzone holding alert — only for FUT/FOP. Fired BEFORE the
        # staleness check so the operator gets the "you're holding into
        # the danger zone" warning even when the frozen entry line is
        # too old to extrapolate safely.
        sec_type = str(self.config.get("sec_type", "STK")).upper()
        if sec_type in ("FUT", "FOP") and in_futures_deadzone(bar_time):
            today = bar_time.date().isoformat()
            last = ctx.state.get("last_deadzone_alert_date")
            if last != today:
                fire_and_forget_alert(
                    redis=self.config.get("_redis"),
                    trigger="FUT_DEADZONE_HOLDING",
                    message=(
                        f"{self.config.get('symbol')} held into 14-15 PT "
                        f"deadzone with {ctx.state.get('qty')} contracts. "
                        f"Bot will not auto-exit — review manually."
                    ),
                    severity="WARNING",
                    bot_id=ctx.bot_id,
                    symbol=self.config.get("symbol"),
                )
                actions.append(UpdateState({
                    "last_deadzone_alert_date": today,
                }))

        # Wallclock extrapolation cap. ``slope_per_sec`` is calibrated
        # on contiguous in-session bars; multiplying it by a weekend
        # gap (~60h) drifts the line to an absurd value and triggers a
        # spurious exit on the first Monday bar. If the gap between
        # the anchor and the current bar exceeds the cap we surface
        # the staleness as a WARNING and skip the exit evaluation —
        # the next bar will let the strategy re-detect a fresh line.
        elapsed_sec = (bar_time - anchor_t).total_seconds()
        max_age_hours = float(self.config.get(
            "entry_line_max_age_hours", 8.0,
        ))
        if elapsed_sec > max_age_hours * 3600:
            actions.append(LogSignal(
                event_type=LogEventType.RISK,
                message=(
                    f"entry line stale by {elapsed_sec / 3600:.1f}h "
                    f"(cap {max_age_hours:.1f}h) — skipping exit eval; "
                    f"line will re-detect on next bar"
                ),
                payload={"elapsed_hours": elapsed_sec / 3600,
                         "max_age_hours": max_age_hours},
            ))
            return actions

        line_value = anchor_p + slope_per_sec * elapsed_sec

        direction = str(entry_line.get("direction", "long")).lower()

        # Trailing-dip stop alongside the line-breach exit: track the
        # bar-close water mark since entry and exit whenever a bar
        # closes more than ``trail_width_pct`` away from it. Without
        # this the only exit was line-breach, which converges with
        # price over time (the line's slope moves it toward the
        # bar's close every bar) — bot could give back a multi-tick
        # favorable move while waiting for the line to catch up. Per-
        # bot ``trail_width_pct`` (default 0.0003) sized so the
        # dollar giveback per contract is ~$10 regardless of symbol.
        from decimal import Decimal as _D
        bar_close_d = _D(str(bar_close))
        trail_pct = _D(str(self.config.get("trail_width_pct", "0.0003")))
        line_value_d = _D(str(line_value))

        breach_trail = False
        trail_stop_d = _D("0")
        water_mark_update: dict = {}
        if direction == "long":
            hwm_raw = ctx.state.get("high_water_mark")
            try:
                cur_hwm = _D(str(hwm_raw)) if hwm_raw not in (None, "") \
                    else bar_close_d
            except Exception:  # noqa: BLE001
                cur_hwm = bar_close_d
            hwm = max(cur_hwm, bar_close_d)
            trail_stop_d = hwm * (_D("1") - trail_pct)
            active_stop = max(line_value_d, trail_stop_d)
            water_mark_update = {
                "high_water_mark": str(hwm),
                "active_stop": str(active_stop),
            }
            breach_trail = bar_close_d < trail_stop_d
        else:  # short
            lwm_raw = ctx.state.get("low_water_mark")
            try:
                cur_lwm = _D(str(lwm_raw)) if lwm_raw not in (None, "") \
                    else bar_close_d
            except Exception:  # noqa: BLE001
                cur_lwm = bar_close_d
            lwm = min(cur_lwm, bar_close_d)
            trail_stop_d = lwm * (_D("1") + trail_pct)
            active_stop = min(line_value_d, trail_stop_d)
            water_mark_update = {
                "low_water_mark": str(lwm),
                "active_stop": str(active_stop),
            }
            breach_trail = bar_close_d > trail_stop_d

        # Acceleration entries skip the line-breach gate — they were
        # entered ABOVE/BELOW the line by design, so a "breach" of
        # the original line carries no information. Trail does all
        # the exit work.
        #
        # ``breach_line`` only fires on bar close when bar_close is
        # CLEARLY past the line on the unfavorable side — beyond a
        # ``touch_tolerance_fraction`` band. A near-miss (bar close
        # within tol of the line) defers to the tick-time SL
        # touch+hold in ``_on_quote`` (the entry line is the floor of
        # ``active_stop``, so the SL linger handles the retest).
        entry_path = str(entry_line.get("entry_path", "touch"))
        breach_tol_frac = float(self.config.get(
            "touch_tolerance_fraction", TOUCH_TOLERANCE_FRACTION,
        ))
        breach_tol = abs(float(line_value)) * breach_tol_frac
        if entry_path == "accel":
            breach_line = False
        else:
            breach_line = (
                bar_close < (line_value - breach_tol)
                if direction == "long"
                else bar_close > (line_value + breach_tol)
            )

        actions.append(LogSignal(
            event_type=LogEventType.EXIT_CHECK,
            message=(
                f"bar close={bar_close:.4f} vs {direction} line="
                f"{line_value:.4f} trail={float(trail_stop_d):.4f}"
            ),
            payload={"close": bar_close, "line_value": line_value,
                     "trail_stop": float(trail_stop_d),
                     "direction": direction,
                     "bar_time": bar_time.isoformat()},
        ))
        actions.append(UpdateState(water_mark_update))

        if breach_line or breach_trail:
            if breach_line and breach_trail:
                reason = "both"
            elif breach_trail:
                reason = "trail_stop"
            else:
                reason = "line_breach"
            actions.append(UpdateState({"exit_reason": reason}))
            verb = "support" if direction == "long" else "resistance"
            cmp = "<" if direction == "long" else ">"
            detail = (
                f"3-min bar close {bar_close:.4f} {cmp} "
                + (f"trail {float(trail_stop_d):.4f}" if breach_trail
                   and not breach_line
                   else f"entry {verb} {line_value:.4f}")
                + f" [{reason}]"
            )
            return actions + self.build_exit_actions(
                ctx, ExitType.TRAILING_STOP, detail,
            )

        # Refresh the counter-line cache used by the tick-time
        # EXIT_COUNTER_LINE trigger in ``_on_quote``. Done at every
        # bar close while in position so the tick check always reads
        # a snapshot taken at the latest closed bar. Lines are static
        # within a bar (last_idx fixed; intercept/slope frozen) so
        # caching at bar close + reading on every tick is exact, not
        # an approximation.
        if bool(self.config.get("counter_exit_enabled", True)):
            actions.extend(
                await self._refresh_counter_lines_cache(direction, ctx)
            )

        return actions

    async def _refresh_counter_lines_cache(
        self, direction: str, ctx: StrategyContext,
    ) -> list[Action]:
        """Detect the opposing-side trendlines and write a flat cache
        to state for tick-time consumption. Counter lines = the lines
        the position would have to clear to keep running:
          LONG  → resistance lines (above current price)
          SHORT → support    lines (below current price)
        Includes 2+ touch unbroken lines (the user explicitly opted
        for the broader set; audit logs will tell us if it over-fires).
        """
        fetched = await self._fetch_history()
        if not fetched:
            return [UpdateState({"counter_lines_cache": [],
                                 "counter_lines_tol": 0.0})]
        closes = [float(b.get("close", 0)) for b in fetched]
        if len(closes) < 4:
            return [UpdateState({"counter_lines_cache": [],
                                 "counter_lines_tol": 0.0})]
        last_idx = len(closes) - 1
        bar_close = closes[last_idx]
        avg_close = sum(closes) / len(closes)
        near_frac = self.config.get(
            "near_touch_tolerance_fraction",
            5 * TOUCH_TOLERANCE_FRACTION,
        )
        if near_frac is not None:
            near_frac = float(near_frac)
        # Counter-line cache = OPPOSING-side 3+touch unbroken lines
        # on the right side of price. Same shape for clean and
        # marginal trades — the marginal-vs-clean distinction lives
        # at the entry tier (which filters get bypassed); the exit
        # logic is identical: bar-close line breach on the trigger
        # line + 10s mid-touch hold on the opposing line.
        from ib_trader.signals.sr_fan import (
            detect_lines,
        )
        opp_type = "resistance" if direction == "long" else "support"
        # Same break-stale window as the entry path so the opposing
        # line cache survives session rollovers symmetrically.
        break_stale_bars = int(self.config.get(
            "detect_break_stale_bars", 480,
        ))
        scanned = detect_lines(
            closes, up_to=last_idx, type_=opp_type,
            near_touch_tolerance_fraction=near_frac,
            break_stale_bars=break_stale_bars,
        )

        min_touches = int(self.config.get("counter_exit_min_touches", 2))
        cache: list[dict] = []
        for ln in scanned:
            if ln.break_idx is not None:
                continue
            if ln.touches < min_touches:
                continue
            value = ln.intercept + ln.slope * last_idx
            # Keep only the OPPOSING side relative to the trade
            # direction. (A "resistance" that sits below current
            # price has already been crossed.)
            if direction == "long" and value <= bar_close:
                continue
            if direction == "short" and value >= bar_close:
                continue
            cache.append({
                "value": round(value, 6),
                "slope": round(ln.slope, 6),
                "touches": int(ln.touches),
                "kind": ln.type,
            })
        # Sort strongest-first so the touch test prefers high-touch
        # lines when several sit near the same price.
        cache.sort(key=lambda x: (-x["touches"], abs(x["value"])))
        touch_frac = float(self.config.get(
            "touch_tolerance_fraction", TOUCH_TOLERANCE_FRACTION,
        ))
        tol = max(1e-6, avg_close * touch_frac)

        # Cache a simple ATR (avg high-low over last 14 bars) so the
        # tick-time counter-line exit can do a proximity check
        # against the trigger line. Operator rule 2026-05-18: if
        # the trigger is within 1× ATR of current price, tolerate
        # counter-line touches (the trade has structural backing).
        # If the trigger is FAR (> 1× ATR), counter-line touches
        # are a real failure signal — exit on linger as before.
        recent = fetched[-14:] if len(fetched) >= 14 else fetched
        ranges: list[float] = []
        for b in recent:
            hi, lo = b.get("high"), b.get("low")
            if hi is not None and lo is not None:
                try:
                    ranges.append(float(hi) - float(lo))
                except (TypeError, ValueError):
                    pass
        trade_atr = sum(ranges) / len(ranges) if ranges else None
        return [UpdateState({
            "counter_lines_cache": cache,
            "counter_lines_tol": round(tol, 6),
            "trade_atr": (round(trade_atr, 6)
                          if trade_atr is not None else None),
        })]

    # ------------------------------------------------------------------
    # Quote — surface last price + unrealised P/L for the UI strip
    # ------------------------------------------------------------------

    def _on_quote(self, event: QuoteUpdate,
                  ctx: StrategyContext) -> list[Action]:
        if ctx.fsm_state != BotState.AWAITING_EXIT_TRIGGER:
            return []
        try:
            entry_price = Decimal(str(ctx.state.get("entry_price", "0") or "0"))
            qty = Decimal(str(ctx.state.get("qty", "0") or "0"))
        except Exception:  # noqa: BLE001
            return []
        if entry_price <= 0 or qty <= 0:
            return []
        last = event.mid
        if last <= 0:
            return []
        entry_line = ctx.state.get("entry_line") or {}
        direction = str(entry_line.get("direction", "long")).lower()
        # Contract multiplier — futures contracts have a notional
        # multiplier (MGC = 10 oz, MCL = 100 bbl, MES = $5/pt, MNQ =
        # $2/pt). Without it the surfaced "P/L" was the raw price diff
        # — off by 2-100× depending on the symbol. STK defaults to 1.
        mult = Decimal(str(self.config.get("contract_multiplier", "1")))
        # Long: profit when last > entry. Short: profit when last < entry.
        unrealized = (last - entry_price) * qty * mult if direction == "long" \
            else (entry_price - last) * qty * mult

        state_patch: dict = {
            "last_price": str(last),
            "unrealized_pnl": str(unrealized),
        }

        # EXIT_COUNTER_LINE: tick-time touch-and-hold against the
        # nearest unbroken opposing trendline. Fires irrespective of
        # the bar-close trail/line-breach exits. The cache snapshot
        # was written at the last bar close (see
        # ``_refresh_counter_lines_cache``); lines are static within
        # a bar so the snapshot is exact.
        actions: list[Action] = []
        if bool(self.config.get("counter_exit_enabled", True)):
            actions.extend(self._check_counter_line_exit(
                ctx, float(last), direction, state_patch,
            ))
            # If the counter-line check fired an exit, ``actions``
            # already contains the LogSignal + UpdateState + PlaceOrder
            # and we should NOT also emit a redundant UpdateState.
            for a in actions:
                if isinstance(a, PlaceOrder):
                    return actions

        # Tick-time SL for BOTH clean and marginal trades (2026-05-17
        # operator spec). The trigger-line linger was removed entirely:
        # a SHORT entered AT the trigger line is by definition close
        # to that line, so a symmetric tol-band on the SAME line
        # armed immediately and exited within a few seconds. The
        # opposing line (counter-line) handles "did the entry idea
        # fail" via ``_check_counter_line_exit`` above; the SL
        # handles "price moved against us past the trail" here.
        #
        # ALWAYS PER TICK (no gating): ratchet HWM/LWM, re-project
        # the entry line via ``slope_per_sec``, compute
        # ``active_stop = max/min(line, trail)``, evaluate
        # ``sl_breached``. The trail is always current; the
        # marginal/clean split controls only WHEN to fire on a
        # breach (see below).
        #
        # FIRE DECISION — two different mechanics by entry confidence:
        #   - marginal (unchanged): touch+hold linger
        #     ``sl_linger_marginal_seconds`` (default 10s). First
        #     breach starts ``sl_touch.start_ts``; continuous breach
        #     for the full window fires; a retrace clears the timer
        #     so a re-breach starts a fresh window.
        #   - clean (new): periodic poll every
        #     ``sl_check_clean_seconds`` (default 60s). At each
        #     interval boundary, sample the current breach state;
        #     if breached → fire. Between samples, ticks are ignored
        #     by the fire decision (but HWM/active_stop still update
        #     above).
        try:
            trail_pct = Decimal(str(
                self.config.get("trail_width_pct", "0.0003")
            ))
        except Exception:  # noqa: BLE001
            trail_pct = Decimal("0.0003")
        mid_d = Decimal(str(last))
        wm_field = (
            "high_water_mark" if direction == "long"
            else "low_water_mark"
        )
        wm_raw = ctx.state.get(wm_field)
        try:
            cur_wm = (
                Decimal(str(wm_raw))
                if wm_raw not in (None, "")
                else mid_d
            )
        except Exception:  # noqa: BLE001
            cur_wm = mid_d
        new_wm = (
            max(cur_wm, mid_d) if direction == "long"
            else min(cur_wm, mid_d)
        )
        trail_stop = (
            new_wm * (Decimal("1") - trail_pct)
            if direction == "long"
            else new_wm * (Decimal("1") + trail_pct)
        )
        # Line component of active_stop is FROZEN at the entry-time
        # line value, not re-projected at each tick (2026-05-18 fix
        # after MGC 13:21 LONG was knifed at -$7 on a flat market).
        #
        # Why: re-projecting a steep up-sloping support (e.g.
        # +1.025/bar = +$0.34/min) makes the line ratchet above the
        # entry within seconds — even if price doesn't move, the
        # line catches up to the mid and the touch+hold linger fires.
        # That's an implicit "must rally as fast as the line slope"
        # condition the operator didn't ask for and can't see on the
        # chart.
        #
        # Freezing keeps the floor at the support level we entered
        # on. If price holds at or above that level → no breach. As
        # the trade works, HWM rises and trail eventually dominates
        # anyway (trail > frozen line). The bar-close ``breach_line``
        # check still re-projects, so we don't lose the "did the
        # support actually fail" semantic at bar boundaries.
        now_utc = datetime.now(timezone.utc)
        line_val_d: Decimal | None = None
        try:
            lv_at_entry = entry_line.get("line_value_at_entry")
            if lv_at_entry is not None:
                line_val_d = Decimal(str(lv_at_entry))
        except Exception:  # noqa: BLE001
            line_val_d = None
        if line_val_d is not None:
            active_stop = (
                max(line_val_d, trail_stop) if direction == "long"
                else min(line_val_d, trail_stop)
            )
        else:
            active_stop = trail_stop
        state_patch[wm_field] = str(new_wm)
        state_patch["active_stop"] = str(active_stop)

        sl_breached = (
            (mid_d <= active_stop) if direction == "long"
            else (mid_d >= active_stop)
        )
        is_marginal = bool(entry_line.get("marginal", False))

        fire = False
        detail = ""
        elapsed_log: float | None = None
        linger_log: float | None = None

        if is_marginal:
            linger_log = float(self.config.get(
                "sl_linger_marginal_seconds", 10.0,
            ))
            sl_touch_doc = ctx.state.get("sl_touch")
            elapsed_s: float | None = None
            if sl_breached:
                if not sl_touch_doc or not isinstance(sl_touch_doc, dict):
                    state_patch["sl_touch"] = {
                        "start_ts": now_utc.isoformat(),
                    }
                else:
                    start_ts = _parse_ts(sl_touch_doc.get("start_ts"))
                    if start_ts is not None:
                        elapsed_s = (now_utc - start_ts).total_seconds()
            elif sl_touch_doc:
                state_patch["sl_touch"] = None
                ctx.state["sl_touch"] = None

            if sl_breached and elapsed_s is not None \
                    and elapsed_s >= linger_log:
                fire = True
                elapsed_log = elapsed_s
                detail = (
                    f"mid {float(mid_d):.4f} "
                    + ("<= " if direction == "long" else ">= ")
                    + f"active_stop {float(active_stop):.4f} "
                    f"after {elapsed_s:.1f}s touch+hold "
                    f"(linger={linger_log:.0f}s, marginal)"
                )
                state_patch["sl_touch"] = None
        else:
            # Clean: poll every N seconds. Sample only when the
            # interval has elapsed since the last sample.
            interval_s = float(self.config.get(
                "sl_check_clean_seconds", 60.0,
            ))
            linger_log = interval_s
            last_check_iso = ctx.state.get("sl_last_check_ts")
            last_check = _parse_ts(last_check_iso) if last_check_iso else None
            since_last = (
                (now_utc - last_check).total_seconds()
                if last_check is not None
                else interval_s  # first tick — sample now
            )
            if since_last >= interval_s:
                state_patch["sl_last_check_ts"] = now_utc.isoformat()
                if sl_breached:
                    fire = True
                    elapsed_log = since_last
                    detail = (
                        f"mid {float(mid_d):.4f} "
                        + ("<= " if direction == "long" else ">= ")
                        + f"active_stop {float(active_stop):.4f} "
                        f"at {interval_s:.0f}s poll "
                        f"(interval={interval_s:.0f}s, clean)"
                    )

        if fire:
            state_patch["exit_reason"] = "trail_stop"
            actions.append(LogSignal(
                event_type=LogEventType.EXIT_CHECK,
                message=f"TRAILING_STOP [{direction}]: {detail}",
                payload={
                    "exit_type": ExitType.TRAILING_STOP.value,
                    "direction": direction,
                    "mid": float(mid_d),
                    "active_stop": float(active_stop),
                    "elapsed_s": elapsed_log,
                    "linger_s": linger_log,
                    "marginal_trade": is_marginal,
                },
            ))
            actions.append(UpdateState(state_patch))
            actions.extend(self.build_exit_actions(
                ctx, ExitType.TRAILING_STOP, detail,
            ))
            return actions

        actions.append(UpdateState(state_patch))
        return actions

    def _check_counter_line_exit(
        self, ctx: StrategyContext, mid: float, direction: str,
        state_patch: dict,
    ) -> list[Action]:
        """Tick-time check for the EXIT_COUNTER_LINE trigger.

        Touch starts on first tick whose mid reaches the opposing line
        within ``tol``. We then wait ``counter_exit_hold_seconds``.
        At elapsed >= hold:
          - if mid is STILL in the touch zone (no clean breakout) →
            exit immediately;
          - if mid is past the line by > tol (current breakout) →
            clear the touch and keep the trade.

        Brief breaches during the hold window do NOT reset state —
        only the end-of-hold snapshot decides. Matches the operator's
        "breach can be brief and come back down" intuition.
        """
        cache = ctx.state.get("counter_lines_cache") or []
        tol = float(ctx.state.get("counter_lines_tol", 0.0))
        if not cache or tol <= 0:
            return []
        hold_secs = float(self.config.get("counter_exit_hold_seconds", 10))
        now = datetime.now(timezone.utc)
        cur_touch = ctx.state.get("counter_touch")
        # Direction-aware re-filter on every read. The cache build
        # already drops geometrically-invalid lines (LONG: line below
        # price; SHORT: line above), but the cache can leak across
        # trades if it isn't rebuilt at entry (MGC SHORT trade #7 on
        # 2026-05-14 exited on a resistance line carried over from a
        # prior LONG). Belt-and-suspenders: validate against the
        # current trade's entry price so a stale line from the wrong
        # direction is silently skipped at consumption.
        try:
            entry_price = float(
                ctx.state.get("entry_price") or 0
            )
        except (TypeError, ValueError):
            entry_price = 0.0

        def _line_valid_for_direction(lv: float) -> bool:
            if entry_price <= 0:
                return True  # entry price unavailable — fall back to legacy behavior
            # LONG: opposing line must be ABOVE entry (resistance).
            # SHORT: opposing line must be BELOW entry (support).
            if direction == "long":
                return lv > entry_price
            return lv < entry_price

        if cur_touch is None:
            # No active touch — see if any line is currently touched.
            for ln in cache:
                lv = float(ln["value"])
                if not _line_valid_for_direction(lv):
                    continue
                touched = (mid >= lv - tol) if direction == "long" \
                    else (mid <= lv + tol)
                breakout = (mid > lv + tol) if direction == "long" \
                    else (mid < lv - tol)
                if touched and not breakout:
                    state_patch["counter_touch"] = {
                        "started_at": now.isoformat(),
                        "line_value": lv,
                        "line_touches": int(ln["touches"]),
                        "line_slope": float(ln.get("slope", 0)),
                    }
                    return [LogSignal(
                        event_type=LogEventType.EXIT_CHECK,
                        message=(
                            f"{EXIT_COUNTER_LINE} armed — line {lv:.4f} "
                            f"touched (touches={ln['touches']}, "
                            f"mid={mid:.4f}); hold {hold_secs:.0f}s "
                            f"for rejection confirm"
                        ),
                        payload={
                            "exit_trigger_armed": EXIT_COUNTER_LINE,
                            "line_value": lv,
                            "line_touches": int(ln["touches"]),
                            "line_slope": float(ln.get("slope", 0)),
                            "mid": mid,
                            "hold_seconds": hold_secs,
                        },
                    )]
            return []

        # Touch is active — check elapsed and current breakout status.
        started = _parse_ts(cur_touch.get("started_at"))
        if started is None:
            state_patch["counter_touch"] = None
            return []
        line_value = float(cur_touch["line_value"])
        # Validate the armed line still makes geometric sense for the
        # current direction. Same defense as the arm-time gate above —
        # an armed touch from a wrong-direction line should silently
        # clear, not exit.
        if not _line_valid_for_direction(line_value):
            state_patch["counter_touch"] = None
            return []
        elapsed = (now - started).total_seconds()
        cur_breakout = (mid > line_value + tol) if direction == "long" \
            else (mid < line_value - tol)
        if elapsed < hold_secs:
            return []
        if cur_breakout:
            # Hold elapsed but we're past the line — treat as a real
            # breakout; clear the touch and let the trade keep running.
            state_patch["counter_touch"] = None
            return [LogSignal(
                event_type=LogEventType.EXIT_CHECK,
                message=(
                    f"{EXIT_COUNTER_LINE} cleared — breakout past line "
                    f"{line_value:.4f} (mid={mid:.4f}); resetting"
                ),
                payload={
                    "exit_trigger_cleared": EXIT_COUNTER_LINE,
                    "line_value": line_value,
                    "mid": mid,
                    "elapsed_seconds": round(elapsed, 2),
                },
            )]
        # Counter touch persisted through linger. By itself NOT an
        # exit — operator rule 2026-05-18:
        #
        #   - If the trigger line (entry's support for LONG /
        #     resistance for SHORT) is NEARBY (|mid − trigger| ≤
        #     1× ATR), tolerate the counter touch — the trade has
        #     nearby structural backing and price can bounce off
        #     the counter and breach it on a second test. ONLY
        #     exit when the trigger itself ALSO fails.
        #   - If the trigger is FAR (> 1× ATR away), there's no
        #     nearby support to lean on. A counter rejection here
        #     means price will likely keep moving against us —
        #     exit on linger as before.
        entry_line = ctx.state.get("entry_line") or {}
        is_marginal = bool(entry_line.get("marginal", False))

        # Compute the trigger line value at the current tick.
        trigger_value: float | None = None
        try:
            anchor_t = _parse_ts(entry_line.get("anchor_time"))
            anchor_p = entry_line.get("anchor_price")
            slope_per_sec_t = entry_line.get("slope_per_sec")
            if (anchor_t is not None and anchor_p is not None
                    and slope_per_sec_t is not None):
                elapsed_t = (now - anchor_t).total_seconds()
                trigger_value = (
                    float(anchor_p) + float(slope_per_sec_t) * elapsed_t
                )
            else:
                lv_at_entry = entry_line.get("line_value_at_entry")
                if lv_at_entry is not None:
                    trigger_value = float(lv_at_entry)
        except Exception:  # noqa: BLE001
            trigger_value = None

        # Proximity check — is trigger nearby?
        trade_atr_raw = ctx.state.get("trade_atr")
        try:
            trade_atr = (
                float(trade_atr_raw)
                if trade_atr_raw not in (None, "")
                else None
            )
        except (TypeError, ValueError):
            trade_atr = None
        atr_mult = float(self.config.get(
            "counter_exit_trigger_nearby_atr_mult", 1.0,
        ))
        trigger_distance = (
            abs(mid - trigger_value)
            if trigger_value is not None else None
        )
        trigger_nearby = (
            trade_atr is not None
            and trigger_distance is not None
            and trigger_distance <= atr_mult * trade_atr
        )

        # Check whether the trigger is currently breached.
        trigger_breached = False
        if trigger_value is not None:
            breach_tol_frac = float(self.config.get(
                "touch_tolerance_fraction", TOUCH_TOLERANCE_FRACTION,
            ))
            breach_tol = abs(trigger_value) * breach_tol_frac
            if direction == "long":
                trigger_breached = mid < (trigger_value - breach_tol)
            else:
                trigger_breached = mid > (trigger_value + breach_tol)

        # Decision tree:
        #   NEARBY  + trigger holds   → clear, keep trade
        #   NEARBY  + trigger breached → exit
        #   FAR     + (regardless)    → exit (legacy behavior)
        if trigger_nearby and not trigger_breached:
            state_patch["counter_touch"] = None
            return [LogSignal(
                event_type=LogEventType.EXIT_CHECK,
                message=(
                    f"{EXIT_COUNTER_LINE} touched + trigger nearby "
                    f"and holding — counter @ {line_value:.4f}, "
                    f"mid {mid:.4f}, trigger @ "
                    f"{trigger_value:.4f}, distance "
                    f"{trigger_distance:.4f} ≤ "
                    f"{atr_mult:.1f}× ATR ({trade_atr:.4f}); "
                    f"clearing touch, keeping trade"
                ),
                payload={
                    "exit_trigger_cleared": EXIT_COUNTER_LINE,
                    "reason": "trigger_nearby_and_holds",
                    "line_value": line_value,
                    "mid": mid,
                    "trigger_value": trigger_value,
                    "trigger_distance": trigger_distance,
                    "trade_atr": trade_atr,
                    "elapsed_seconds": round(elapsed, 2),
                },
            )]

        # FAR from trigger OR trigger breached — exit. Reason flavor
        # depends on whether the trade was tagged marginal at entry.
        exit_label = (EXIT_TIGHT_COUNTER_LINE if is_marginal
                       else EXIT_COUNTER_LINE)
        detail = (
            f"{exit_label} held {elapsed:.1f}s "
            f"(line={line_value:.4f}, "
            f"touches={cur_touch['line_touches']}, mid={mid:.4f})"
        )
        state_patch["counter_touch"] = None
        state_patch["exit_reason"] = exit_label
        actions: list[Action] = [
            LogSignal(
                event_type=LogEventType.EXIT_CHECK,
                message=f"{exit_label} exit — {detail}",
                payload={
                    "exit_trigger": exit_label,
                    "line_value": line_value,
                    "line_touches": cur_touch["line_touches"],
                    "line_slope": cur_touch.get("line_slope"),
                    "mid": mid,
                    "elapsed_seconds": round(elapsed, 2),
                    "hold_seconds": hold_secs,
                    "marginal_trade": is_marginal,
                },
            ),
            UpdateState(state_patch),
        ]
        actions.extend(self.build_exit_actions(
            ctx, ExitType.TRAILING_STOP, detail,
        ))
        return actions

    # ------------------------------------------------------------------
    # Fills / Rejects
    # ------------------------------------------------------------------

    def _on_fill(self, event: OrderFilled,
                 ctx: StrategyContext) -> list[Action]:
        actions: list[Action] = []
        pos = ctx.fsm_state
        entry_line = ctx.state.get("entry_line") or {}
        direction = str(entry_line.get("direction", "long")).lower()
        # Map FSM-stage + fill side to (entry-leg vs exit-leg) for both
        # directions. The runtime transitions the FSM BEFORE notifying
        # the strategy, so by the time we get here the post-fill state
        # is what's in ``pos`` — AWAITING_EXIT_TRIGGER for an entry
        # fill, AWAITING_ENTRY_TRIGGER for an exit fill. The earlier
        # ENTRY_ORDER_PLACED / EXIT_ORDER_PLACED checks were never
        # truthy and the entry-leg seeding silently never ran. Side
        # vs direction still disambiguates the two legs:
        #   LONG  entry = BUY,  LONG  exit = SELL
        #   SHORT entry = SELL, SHORT exit = BUY (buy-to-cover)
        is_entry_leg = pos == BotState.AWAITING_EXIT_TRIGGER and (
            (direction == "long" and event.side == "BUY")
            or (direction == "short" and event.side == "SELL")
        )
        is_exit_leg = pos == BotState.AWAITING_ENTRY_TRIGGER and (
            (direction == "long" and event.side == "SELL")
            or (direction == "short" and event.side == "BUY")
        )
        if is_entry_leg:
            # Seed the trail-only stop at entry time so the position
            # strip has a real number from the first tick. Without
            # this, ``active_stop`` stays None until the next 3-min
            # bar's EXIT_CHECK fills it in. The bot's exit eval will
            # overwrite this with ``max(line, trail)`` (LONG) or
            # ``min(line, trail)`` (SHORT) at first close — but in
            # the interim the trail-only band is the conservative
            # initial stop and matches what the operator expects.
            trail_pct_d = Decimal(str(
                self.config.get("trail_width_pct", 0.0002)
            ))
            fill_price_d = Decimal(str(event.fill_price))
            if direction == "long":
                initial_stop = fill_price_d * (Decimal("1") - trail_pct_d)
                wm_field = "high_water_mark"
            else:
                initial_stop = fill_price_d * (Decimal("1") + trail_pct_d)
                wm_field = "low_water_mark"
            actions.extend([
                LogSignal(
                    event_type=LogEventType.FILL,
                    message=(
                        f"{event.side} {event.qty} {event.symbol} @ "
                        f"{event.fill_price} ({direction} entry)"
                    ),
                    payload={"fill_price": str(event.fill_price),
                             "qty": str(event.qty),
                             "direction": direction},
                    trade_serial=event.trade_serial,
                ),
                UpdateState({
                    "trade_serial": event.trade_serial,
                    "entry_price": str(event.fill_price),
                    # UTC to match the runtime's exit-leg ``now_iso()``
                    # convention. Mixing PT-with-offset here and UTC at
                    # exit caused SQL math on ``bot_trades.entry_time``
                    # vs ``exit_time`` to come out 7h off because
                    # SQLAlchemy strips the offset on naive DateTime
                    # columns. UI ``new Date(iso)`` handles either, so
                    # the renderer was fine — just the SQL math broke.
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "qty": str(event.qty),
                    "active_stop": str(initial_stop),
                    wm_field: str(fill_price_d),
                    # Counter-line state reset at entry. A previous
                    # trade's cache (built for the OPPOSITE direction)
                    # would otherwise leak into the new trade's
                    # ``_check_counter_line_exit`` reads — fired the
                    # MGC SHORT trade #7 immediate-exit at $5 loss on
                    # 2026-05-14. Empty cache here; first 3-min bar in
                    # ``_evaluate_exit`` rebuilds it for the new
                    # direction.
                    "counter_lines_cache": [],
                    # Seed ``counter_lines_tol`` at fill so the tight
                    # tick-time check can run during the first 3 min
                    # before _evaluate_exit's bar-close refresh fires.
                    # Without this the check returned early (tol == 0)
                    # and the bar-close line_breach always won the
                    # race — observed on MNQ 2026-05-15 10:51 marginal
                    # SHORT which exited at the bar close instead of
                    # at the trigger-line touch ~10 s after fill.
                    "counter_lines_tol": float(
                        Decimal(str(event.fill_price))
                        * Decimal(str(self.config.get(
                            "touch_tolerance_fraction",
                            TOUCH_TOLERANCE_FRACTION,
                        )))
                    ),
                    "counter_touch": None,
                    "trade_atr": None,
                    "sl_touch": None,
                    "sl_last_check_ts": None,
                }),
            ])
        elif is_exit_leg:
            from decimal import InvalidOperation
            cur_qty_raw = ctx.state.get("qty")
            try:
                cur_qty = Decimal(str(cur_qty_raw)) \
                    if cur_qty_raw not in (None, "") else Decimal("0")
            except (ValueError, TypeError, InvalidOperation):
                cur_qty = Decimal("0")
            if cur_qty != 0:
                # Partial — runtime has patched residual qty; do nothing.
                return [LogSignal(
                    event_type=LogEventType.EXIT_CHECK,
                    message=(
                        f"partial {event.side.lower()} {event.qty} @ "
                        f"{event.fill_price}, residual {cur_qty}"
                    ),
                    payload={"filled_qty": str(event.qty),
                             "residual_qty": str(cur_qty),
                             "direction": direction},
                    trade_serial=ctx.state.get("trade_serial"),
                )]
            entry_price = Decimal(
                str(ctx.state.get("entry_price", "0") or "0")
            )
            mult = Decimal(str(self.config.get("contract_multiplier", "1")))
            # Long P/L = (exit - entry) * qty * mult.
            # Short P/L = (entry - exit) * qty * mult.
            if entry_price > 0:
                pnl = (event.fill_price - entry_price) * event.qty * mult \
                    if direction == "long" \
                    else (entry_price - event.fill_price) * event.qty * mult
            else:
                pnl = Decimal("0")
            # Round-trip summary for the operator-facing audit feed.
            exit_reason = ctx.state.get("exit_reason") or "unknown"
            entry_time_iso = ctx.state.get("entry_time")
            duration_s: float | None = None
            entry_dt = _parse_ts(entry_time_iso) if entry_time_iso else None
            if entry_dt is not None:
                try:
                    duration_s = (datetime.now(timezone.utc)
                                  - entry_dt.astimezone(timezone.utc)
                                  ).total_seconds()
                except Exception:  # noqa: BLE001
                    duration_s = None
            # NOTE: the TRADE_CLOSED audit row used to be emitted here
            # with ``pnl_net=pnl`` (gross, no commission). It hit the
            # ``clear_position_fields``-wipes-``entry_price`` race in
            # ``ctx.state`` and always logged $0.00, so we suppressed
            # the row in the frontend (commit 37bc409).
            # The row is now emitted from ``runtime._handle_record_trade_closed``
            # where realized_pnl AND the seeded commission are both
            # correctly in scope. Keep the dev LogSignal for the
            # structured log only.
            actions.extend([
                LogSignal(
                    event_type=LogEventType.CLOSED,
                    message=(
                        f"{event.symbol} @ {event.fill_price} pnl={pnl:+.2f} "
                        f"({direction} close)"
                    ),
                    trade_serial=ctx.state.get("trade_serial"),
                ),
                UpdateState({
                    "trade_serial": None,
                    # ``armed`` stays True so the bot continues firing
                    # on future qualifying bars. The runtime's exit
                    # patch writes ``cooldown_until`` (when
                    # stop_on_exit=False) which gates re-entry for
                    # one bar; that replaces the old one-and-done
                    # disarm semantic.
                    "entry_line": None,
                    "entry_price": None,
                    "entry_time": None,
                    "entry_bar_time": None,
                    "unrealized_pnl": None,
                    # Trailing-dip state cleared so the next round
                    # starts fresh (water marks initialize from the
                    # first bar after re-arm).
                    "high_water_mark": None,
                    "low_water_mark": None,
                    "active_stop": None,
                    "exit_reason": None,
                    # Counter-line exit state cleared with the round.
                    "counter_touch": None,
                    "counter_lines_cache": [],
                    "counter_lines_tol": 0.0,
                    "trade_atr": None,
                    "sl_touch": None,
                    "sl_last_check_ts": None,
                }),
            ])
        return actions

    def _on_rejected(self, event: OrderRejected,
                     ctx: StrategyContext) -> list[Action]:
        return [LogSignal(
            event_type=LogEventType.ERROR,
            message=f"order rejected: {event.reason}",
        )]

    # ------------------------------------------------------------------
    # Force-exit hook used by the runtime's /force-quit and /force-sell
    # endpoints. Mid order for the full qty.
    # ------------------------------------------------------------------

    def build_exit_actions(self, ctx: StrategyContext, exit_type: ExitType,
                           detail: str) -> list[Action]:
        symbol = self.config["symbol"]
        qty = ctx.state.get("qty", 1)
        order_strategy = self.config.get("exit_order_strategy", "mid")
        # Direction drives the closing side. Long → SELL to flatten.
        # Short → BUY to cover. Read from the frozen entry_line; falls
        # back to long for legacy state docs that predate the field.
        entry_line = ctx.state.get("entry_line") or {}
        direction = str(entry_line.get("direction", "long")).lower()
        close_side = "SELL" if direction == "long" else "BUY"
        return [
            LogSignal(
                event_type=LogEventType.EXIT_CHECK,
                message=f"{exit_type.value} [{direction}]: {detail}",
                payload={"exit_type": exit_type.value,
                         "direction": direction},
            ),
            PlaceOrder(
                symbol=symbol, side=close_side,
                qty=Decimal(str(qty)),
                order_type=order_strategy,
                origin="exit",
            ),
        ]
