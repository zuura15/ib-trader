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
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ib_trader.bots.lifecycle import BotState
from ib_trader.bots.strategy import (
    Action,
    BarCompleted,
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
    detect_lines,
    in_futures_deadzone,
)

logger = logging.getLogger(__name__)


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
                "entry_line": "dict|null",  # {slope_per_sec, anchor_time, anchor_price, touches, kind, slope_per_bar, intercept, anchor_b_idx}
                "qty": "decimal|null",
                "last_price": "decimal|null",
                "unrealized_pnl": "decimal|null",
                "last_deadzone_alert_date": "str|null",
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
            return await self._on_bar(event, ctx)
        if isinstance(event, QuoteUpdate):
            return self._on_quote(event, ctx)
        if isinstance(event, OrderFilled):
            return self._on_fill(event, ctx)
        if isinstance(event, OrderRejected):
            return self._on_rejected(event, ctx)
        return []

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
            return actions + self._evaluate_exit(event, ctx, bar_time)

        # Entry check: gated on armed + deadzone + FSM state.
        if pos != BotState.AWAITING_ENTRY_TRIGGER:
            return actions
        if not ctx.state.get("armed", False):
            return actions
        if in_futures_deadzone(bar_time):
            # Quiet skip — logging every bar inside the deadzone is noise.
            return actions

        # Pull bars from /engine/history so the bot and the view-only
        # chart see identical input. The runtime hands us
        # ``event.window`` from the in-process aggregator, which lags
        # warmup dedup; we ignore it here on the entry path. Fall back
        # to the runtime window only if the HTTP fetch comes up empty
        # (engine down / network blip).
        fetched = await self._fetch_history()
        if fetched:
            window = fetched
        else:
            window = event.window or []
        if not window:
            return actions
        closes = [float(b.get("close", 0)) for b in window]
        if len(closes) < 4:
            return actions
        last_idx = len(closes) - 1

        # Scan both sides. Long candidates from uptrending support
        # (slope > 0); short candidates from downtrending resistance
        # (slope < 0). Both filtered to 3-touch, not broken.
        supports = detect_lines(closes, up_to=last_idx, type_="support")
        resistances = detect_lines(closes, up_to=last_idx, type_="resistance")
        longs = [ln for ln in supports
                 if ln.touches >= MIN_TOUCHES and ln.slope > 0
                 and ln.break_idx is None]
        shorts = [ln for ln in resistances
                  if ln.touches >= MIN_TOUCHES and ln.slope < 0
                  and ln.break_idx is None]
        longs.sort(key=lambda ln: (-ln.touches, -ln.slope))
        shorts.sort(key=lambda ln: (-ln.touches, ln.slope))   # most-negative slope first

        # If both sides offer a candidate (rare but possible in a noisy
        # range), pick whichever has more touches; tie → prefer long
        # (entry on dip is the bias the user originally tuned for).
        long_line = longs[0] if longs else None
        short_line = shorts[0] if shorts else None
        # Diagnostic — emit ONE compact record per bar even when nothing
        # qualifies. The visibility gap during testing was: "frontend
        # shows a B but no bot trade fires" — without this, the logs
        # were silent on every barbarewhere the algorithm decided not to
        # act. Touches per side (best of) makes algorithm divergence
        # vs the frontend obvious.
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
                "best_short_touches": short_line.touches if short_line else 0,
                "best_short_slope": short_line.slope if short_line else None,
                "supports_found": len(supports),
                "resistances_found": len(resistances),
            },
        ))
        if long_line and short_line:
            if short_line.touches > long_line.touches:
                chosen, direction, kind = short_line, "short", "resistance"
            else:
                chosen, direction, kind = long_line, "long", "support"
        elif long_line:
            chosen, direction, kind = long_line, "long", "support"
        elif short_line:
            chosen, direction, kind = short_line, "short", "resistance"
        else:
            return actions

        # Freeze the line in (time, price) space so future evaluations
        # survive the window sliding forward.
        anchor_b_time = _parse_ts(
            window[chosen.anchor_b_idx].get("timestamp_utc")
        ) if 0 <= chosen.anchor_b_idx < len(window) else bar_time
        anchor_b_price = float(window[chosen.anchor_b_idx].get(
            "close", closes[chosen.anchor_b_idx]
        )) if 0 <= chosen.anchor_b_idx < len(window) else closes[chosen.anchor_b_idx]
        slope_per_sec = chosen.slope / self.bar_seconds

        entry_line_doc = {
            "kind": kind,
            "direction": direction,
            "slope_per_bar": chosen.slope,
            "intercept": chosen.intercept,
            "slope_per_sec": slope_per_sec,
            "anchor_time": (anchor_b_time or bar_time).isoformat(),
            "anchor_price": anchor_b_price,
            "anchor_b_idx": chosen.anchor_b_idx,
            "from_idx": chosen.from_idx,
            "touches": chosen.touches,
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
        actions.append(LogSignal(
            event_type=LogEventType.SIGNAL,
            message=(
                f"{label} 3-touch (touches={chosen.touches}, "
                f"slope/bar={chosen.slope:.4f})"
            ),
            payload={"qty": qty, "entry_line": entry_line_doc,
                     "bar_time": bar_time.isoformat()},
        ))
        actions.append(PlaceOrder(
            symbol=self.config["symbol"],
            side=side,
            qty=Decimal(str(qty)),
            order_type=self.config.get("order_strategy", "mid"),
        ))
        actions.append(UpdateState({
            "entry_line": entry_line_doc,
            "entry_bar_time": bar_time.isoformat(),
        }))
        return actions

    def _evaluate_exit(self, event: BarCompleted, ctx: StrategyContext,
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

        line_value = anchor_p + slope_per_sec * (
            bar_time - anchor_t
        ).total_seconds()

        # Deadzone holding alert — only for FUT/FOP.
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

        direction = str(entry_line.get("direction", "long")).lower()
        actions.append(LogSignal(
            event_type=LogEventType.EXIT_CHECK,
            message=(
                f"bar close={bar_close:.4f} vs {direction} line="
                f"{line_value:.4f}"
            ),
            payload={"close": bar_close, "line_value": line_value,
                     "direction": direction,
                     "bar_time": bar_time.isoformat()},
        ))

        # Long: break = close below the entry support.
        # Short: break = close above the entry resistance.
        broken = (bar_close < line_value) if direction == "long" \
            else (bar_close > line_value)
        if broken:
            verb = "support" if direction == "long" else "resistance"
            cmp = "<" if direction == "long" else ">"
            return actions + self.build_exit_actions(
                ctx, ExitType.TRAILING_STOP,
                f"3-min bar close {bar_close:.4f} {cmp} entry "
                f"{verb} {line_value:.4f}",
            )
        return actions

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
        # Long: profit when last > entry. Short: profit when last < entry.
        unrealized = (last - entry_price) * qty if direction == "long" \
            else (entry_price - last) * qty
        return [UpdateState({
            "last_price": str(last),
            "unrealized_pnl": str(unrealized),
        })]

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
        # directions. Long entry = BUY; long exit = SELL. Short entry =
        # SELL; short exit = BUY (buy-to-cover).
        is_entry_leg = pos == BotState.ENTRY_ORDER_PLACED and (
            (direction == "long" and event.side == "BUY")
            or (direction == "short" and event.side == "SELL")
        )
        is_exit_leg = pos == BotState.EXIT_ORDER_PLACED and (
            (direction == "long" and event.side == "SELL")
            or (direction == "short" and event.side == "BUY")
        )
        if is_entry_leg:
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
                    # Server-local (PT) per CLAUDE.md; the UI / PositionStrip
                    # parses with ``new Date(iso)`` which respects the offset.
                    "entry_time": datetime.now().astimezone().isoformat(),
                    "qty": str(event.qty),
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
            # Long P/L = (exit - entry) * qty. Short P/L = (entry - exit) * qty.
            if entry_price > 0:
                pnl = (event.fill_price - entry_price) * event.qty \
                    if direction == "long" \
                    else (entry_price - event.fill_price) * event.qty
            else:
                pnl = Decimal("0")
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
                    "armed": False,
                    "entry_line": None,
                    "entry_price": None,
                    "entry_time": None,
                    "entry_bar_time": None,
                    "unrealized_pnl": None,
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
