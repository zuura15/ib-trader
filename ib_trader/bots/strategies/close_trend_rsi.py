"""Close-Price Trend RSI Reversal Strategy.

Buys dips within confirmed uptrends detected on the close-price line graph.
More responsive than sawtooth detection in tight price ranges.

Uses the same exit logic as sawtooth_rsi: client-side trailing stop.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from signals_lib.indicators import add_rsi, add_bollinger_bands
from signals_lib.price_action import add_price_action_features
from signals_lib.channels import add_channel_features
from signals_lib.time_filters import passes_session_filter, add_time_of_day_features
from signals_lib.trend import add_close_trend_features

from ib_trader.bots.fsm import BotState
from ib_trader.bots.strategy import (
    StrategyManifest, Subscription, StrategyContext,
    MarketEvent, Action,
    BarCompleted, QuoteUpdate, OrderFilled, OrderRejected,
    PlaceOrder, UpdateState, LogSignal,
    ExitType, LogEventType, QuoteField,
)

logger = logging.getLogger(__name__)


def _parse_aware_dt(s: str) -> datetime:
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _safe_float(row: pd.Series, col: str) -> float:
    if col not in row.index:
        return float("nan")
    val = row[col]
    try:
        return float(val)
    except (ValueError, TypeError):
        return float("nan")


class CloseTrendRsiStrategy:
    """Buys dips in close-price uptrends with RSI confirmation."""

    def __init__(self, config: dict) -> None:
        self.config = config
        symbol = config["symbol"]
        bar_seconds = config.get("bar_size_seconds", 180)
        lookback = config.get("lookback_bars", 20)

        self.manifest = StrategyManifest(
            name="close_trend_rsi_reversal",
            subscriptions=[
                Subscription("bars", [symbol], {"bar_seconds": bar_seconds, "lookback": lookback}),
            ],
            capabilities=["execution", "state_store"],
            state_schema={
                "trade_serial": "int|null",
                "entry_price": "decimal|null",
                "entry_time": "str|null",
                "high_water_mark": "decimal|null",
                "current_stop": "decimal|null",
                "trail_activated": "bool",
            },
            version="1.0",
        )

    async def on_start(self, ctx: StrategyContext) -> list[Action]:
        if not ctx.state:
            ctx.state = {
                "trade_serial": None,
                "entry_price": None,
                "entry_time": None,
                "high_water_mark": None,
                "current_stop": None,
                "trail_activated": False,
            }
        return [LogSignal(
            event_type=LogEventType.STATE,
            message=f"Close-trend strategy started: fsm_state={ctx.fsm_state.value}",
            payload={"detector": "close_trend", "symbol": self.config["symbol"]},
        )]

    async def on_event(self, event: MarketEvent, ctx: StrategyContext) -> list[Action]:
        pos = ctx.fsm_state

        if isinstance(event, BarCompleted):
            return self._on_bar(event, ctx, pos)
        if isinstance(event, QuoteUpdate):
            return self._on_quote(event, ctx, pos)
        if isinstance(event, OrderFilled):
            return self._on_fill(event, ctx, pos)
        if isinstance(event, OrderRejected):
            return self._on_rejected(event, ctx, pos)
        return []

    async def on_stop(self, ctx: StrategyContext) -> list[Action]:
        return [LogSignal(event_type=LogEventType.STATE, message="Close-trend strategy stopped")]

    # -------------------------------------------------------------------
    # Bar processing — entry signals
    # -------------------------------------------------------------------

    def _on_bar(self, event: BarCompleted, ctx: StrategyContext,
                pos: BotState) -> list[Action]:
        actions: list[Action] = []
        bar = event.bar
        symbol = self.config["symbol"]
        entry_cfg = self.config.get("entry", {})

        # Build features
        window_df = pd.DataFrame(event.window)
        if "timestamp_utc" in window_df.columns:
            window_df["timestamp_utc"] = pd.to_datetime(window_df["timestamp_utc"], utc=True)

        featured = window_df.copy()
        featured = add_rsi(featured)
        featured = add_bollinger_bands(featured)
        featured = add_price_action_features(featured)
        featured = add_channel_features(featured)
        if "timestamp_utc" in featured.columns:
            featured = add_time_of_day_features(featured)

        # Close-price trend detection
        peak_window = entry_cfg.get("peak_window", 2)
        trend_points = entry_cfg.get("trend_points", 2)
        featured = add_close_trend_features(featured, peak_window=peak_window, trend_points=trend_points)

        last = featured.iloc[-1]

        rsi_val = _safe_float(last, "rsi")
        trend_up = _safe_float(last, "close_trend_up")
        valley_ago = _safe_float(last, "close_valley_bars_ago")
        near_valley = _safe_float(last, "close_near_valley")
        strength = _safe_float(last, "close_trend_strength")

        actions.append(LogSignal(
            event_type=LogEventType.BAR,
            message=f"{symbol} 3min close={bar.get('close')} rsi={rsi_val:.1f} "
                    f"trend={'UP' if trend_up == 1.0 else 'DOWN'} "
                    f"valley_ago={valley_ago:.0f} strength={strength:.0f}",
            payload={
                "symbol": symbol, "close": str(bar.get("close")),
                "rsi": rsi_val, "close_trend_up": trend_up,
                "close_valley_bars_ago": valley_ago,
                "close_near_valley": near_valley,
                "close_trend_strength": strength,
                "bar_count": event.bar_count,
            },
        ))

        if pos == BotState.AWAITING_ENTRY_TRIGGER:
            entry_actions = self._check_entry(last, bar, ctx)
            actions.extend(entry_actions)

        return actions

    def _check_entry(self, last: pd.Series, bar: dict,
                     ctx: StrategyContext) -> list[Action]:
        actions: list[Action] = []
        entry_cfg = self.config.get("entry", {})
        session_cfg = self.config.get("session_filter", {})
        symbol = self.config["symbol"]

        conditions: dict[str, tuple[bool, str]] = {}

        # 1. Close-price uptrend
        trend_up = _safe_float(last, "close_trend_up")
        conditions["close_trend_up"] = (
            trend_up == 1.0,
            f"trend={'UP' if trend_up == 1.0 else 'DOWN'}",
        )

        # 2. Near a valley
        valley_ago = _safe_float(last, "close_valley_bars_ago")
        max_valley_bars = entry_cfg.get("max_valley_bars_ago", 5)
        near_valley = _safe_float(last, "close_near_valley")
        near = valley_ago <= max_valley_bars or near_valley == 1.0
        conditions["near_valley"] = (
            near,
            f"valley_ago={valley_ago:.0f} (max={max_valley_bars}), near_valley={near_valley}",
        )

        # 3. RSI below threshold
        rsi_val = _safe_float(last, "rsi")
        max_rsi = entry_cfg.get("max_rsi", 60)
        conditions["rsi_below_max"] = (
            rsi_val < max_rsi,
            f"rsi={rsi_val:.1f} (max={max_rsi})",
        )

        # 4. Session filter
        ts = bar.get("timestamp_utc")
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if ts:
            passes, reason = passes_session_filter(
                ts,
                skip_close_transition=session_cfg.get("skip_close_transition", True),
                skip_turn_minutes=session_cfg.get("skip_turn_minutes", 5),
            )
            conditions["session_filter"] = (passes, reason if not passes else "OK")
        else:
            conditions["session_filter"] = (False, "no timestamp")

        all_pass = all(ok for ok, _ in conditions.values())

        if not all_pass:
            failed = {k: detail for k, (ok, detail) in conditions.items() if not ok}
            actions.append(LogSignal(
                event_type=LogEventType.SKIP,
                message=" | ".join(f"{k}=FAIL ({detail})" for k, detail in failed.items()),
                payload={"conditions": {k: {"pass": ok, "detail": d}
                                         for k, (ok, d) in conditions.items()}},
            ))
            return actions

        # All conditions met
        close_price = Decimal(str(bar.get("close", 0)))
        max_value = Decimal(str(self.config.get("max_position_value", "10000")))
        max_shares = self.config.get("max_shares", 20)

        if close_price > 0:
            qty = min(int(max_value / close_price), max_shares)
            qty = max(qty, 1)
        else:
            qty = 1

        order_strategy = self.config.get("order_strategy", "mid")

        actions.append(LogSignal(
            event_type=LogEventType.SIGNAL,
            message=f"BUY — close-trend UP (rsi={rsi_val:.1f}, "
                    f"valley_ago={valley_ago:.0f}, "
                    f"strength={_safe_float(last, 'close_trend_strength'):.0f})",
            payload={"conditions": {k: {"pass": ok, "detail": d}
                                     for k, (ok, d) in conditions.items()},
                     "qty": qty, "price": str(close_price)},
        ))

        actions.append(PlaceOrder(
            symbol=symbol, side="BUY", qty=Decimal(str(qty)),
            order_type=order_strategy,
        ))

        actions.append(UpdateState({
            "entry_time": datetime.now(timezone.utc).isoformat(),
        }))

        return actions

    # -------------------------------------------------------------------
    # Exit logic — identical to sawtooth (trailing stop)
    # -------------------------------------------------------------------

    def _on_quote(self, event: QuoteUpdate, ctx: StrategyContext,
                  pos: BotState) -> list[Action]:
        if pos != BotState.AWAITING_EXIT_TRIGGER:
            return []

        state = ctx.state
        _redis = ctx.config.get("_redis") if isinstance(ctx.config, dict) else None

        # Invariant guard: AWAITING_EXIT_TRIGGER implies we hold a
        # non-zero position. If qty is 0/negative/invalid here, FSM
        # state and position fields are out of sync (Apr 19 bug: FSM
        # stuck at AWAITING_EXIT_TRIGGER while qty collapsed to 0,
        # causing zero-qty SELLs to fire on every tick). Alert + bail.
        from decimal import InvalidOperation
        from ib_trader.logging_.alerts import fire_and_forget_alert
        qty_raw = state.get("qty", "0")
        try:
            position_qty = Decimal(str(qty_raw))
        except (ValueError, TypeError, InvalidOperation):
            position_qty = Decimal("0")
        if position_qty <= 0:
            fire_and_forget_alert(
                redis=_redis,
                trigger="BOT_INVARIANT_VIOLATED_QTY_ZERO",
                message=(
                    f"Bot in AWAITING_EXIT_TRIGGER with qty={qty_raw!r}. "
                    f"State doc is inconsistent — refusing to emit zero-qty SELL."
                ),
                severity="WARNING",
                bot_id=ctx.bot_id,
                symbol=state.get("symbol"),
                extra={"field": "qty", "value": str(qty_raw),
                       "entry_price": str(state.get("entry_price"))},
            )
            return []

        entry_price = Decimal(str(state.get("entry_price", "0")))
        if entry_price <= 0:
            # Invariant: AWAITING_EXIT_TRIGGER implies a filled entry with
            # a positive entry_price. If we got here without one, state is
            # inconsistent — surface it loudly (UI + log) instead of
            # silently eating quote ticks forever.
            fire_and_forget_alert(
                redis=_redis,
                trigger="BOT_INVARIANT_VIOLATED",
                message=(
                    f"Bot in AWAITING_EXIT_TRIGGER without a positive entry_price "
                    f"(value={state.get('entry_price')!r}). Cannot evaluate exit logic."
                ),
                severity="WARNING",
                bot_id=ctx.bot_id,
                symbol=state.get("symbol"),
                extra={"field": "entry_price", "value": str(state.get("entry_price"))},
            )
            return []

        exit_cfg = self.config.get("exit", {})
        price_field = exit_cfg.get("exit_price", QuoteField.BID.value)
        current_price = getattr(event, price_field, event.bid)
        if current_price <= 0:
            return []

        actions: list[Action] = []
        pnl_pct = (current_price - entry_price) / entry_price

        # Always persist latest price for UI display
        actions.append(UpdateState({"last_price": str(current_price)}))

        # Hard stop loss
        hard_sl_pct = Decimal(str(exit_cfg.get("hard_stop_loss_pct", "0.001")))
        hard_sl_price = entry_price * (1 - hard_sl_pct)
        if current_price <= hard_sl_price:
            return actions + self._trigger_exit(ctx, ExitType.HARD_STOP_LOSS,
                f"bid={current_price} <= hard_sl={hard_sl_price} (pnl={float(pnl_pct):.4%})")

        # Time stop — opt-in per strategy config. If time_stop_minutes is
        # absent, the whole check is skipped. If it's present, entry_time
        # MUST be set (invariant of AWAITING_EXIT_TRIGGER).
        time_stop_minutes = exit_cfg.get("time_stop_minutes")
        if time_stop_minutes is not None:
            entry_time_str = state.get("entry_time")
            if not entry_time_str:
                logger.warning(
                    '{"event": "INVARIANT_VIOLATED", "bot_id": "%s", '
                    '"field": "entry_time", "note": "time_stop configured but '
                    'entry_time missing — skipping time-stop check this tick"}',
                    ctx.bot_id,
                )
            else:
                entry_time = _parse_aware_dt(entry_time_str)
                elapsed = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
                if elapsed >= int(time_stop_minutes):
                    return actions + self._trigger_exit(ctx, ExitType.TIME_STOP,
                        f"elapsed={elapsed:.0f}min >= {int(time_stop_minutes)}min")

        # Trailing stop
        trail_activation = Decimal(str(exit_cfg.get("trail_activation_pct", "0.0005")))
        trail_width = Decimal(str(exit_cfg.get("trail_width_pct", "0.0015")))
        trail_activated = state.get("trail_activated", False)
        hwm = Decimal(str(state.get("high_water_mark") or current_price))

        if not trail_activated:
            if pnl_pct >= trail_activation:
                hwm = current_price
                trail_stop = hwm * (1 - trail_width)
                actions.append(LogSignal(
                    event_type=LogEventType.EXIT_CHECK,
                    message=f"TRAIL ACTIVATED hwm={hwm} stop={trail_stop}",
                    payload={"hwm": str(hwm), "trail_stop": str(trail_stop),
                             "pnl_pct": f"{float(pnl_pct):.4%}"},
                ))
                actions.append(UpdateState({
                    "trail_activated": True,
                    "high_water_mark": str(hwm),
                    "current_stop": str(trail_stop),
                }))
        else:
            if current_price > hwm:
                hwm = current_price
                trail_stop = hwm * (1 - trail_width)
                actions.append(UpdateState({
                    "high_water_mark": str(hwm),
                    "current_stop": str(trail_stop),
                }))
            else:
                trail_stop = Decimal(str(state.get("current_stop", "0")))
                if trail_stop > 0 and current_price <= trail_stop:
                    return actions + self._trigger_exit(ctx, ExitType.TRAILING_STOP,
                        f"bid={current_price} <= trail_stop={trail_stop} "
                        f"(hwm={hwm}, pnl={float(pnl_pct):.4%})")

        return actions

    def _trigger_exit(self, ctx: StrategyContext, exit_type: ExitType,
                      detail: str) -> list[Action]:
        """Generate actions for an exit trigger.

        Always places the SELL order — the bot has symbol and qty,
        that's all it needs. No serial gating.
        """
        symbol = self.config["symbol"]

        return [
            LogSignal(
                event_type=LogEventType.EXIT_CHECK,
                message=f"{exit_type.value}: {detail}",
                payload={"exit_type": exit_type.value},
            ),
            PlaceOrder(
                symbol=symbol, side="SELL",
                qty=Decimal(str(ctx.state.get("qty", 1))),
                # Session-aware aggressive-mid exit (see sawtooth_rsi
                # for the why; same rationale applies here).
                order_type="smart_market",
                origin="exit",
            ),
        ]

    # -------------------------------------------------------------------
    # Fill / Reject
    # -------------------------------------------------------------------

    def _on_fill(self, event: OrderFilled, ctx: StrategyContext,
                 pos: BotState) -> list[Action]:
        actions: list[Action] = []

        if pos == BotState.ENTRY_ORDER_PLACED and event.side == "BUY":
            entry_price = event.fill_price
            exit_cfg = self.config.get("exit", {})
            hard_sl = entry_price * (1 - Decimal(str(exit_cfg.get("hard_stop_loss_pct", "0.001"))))

            actions.extend([
                LogSignal(
                    event_type=LogEventType.FILL,
                    message=f"BUY {event.qty} {event.symbol} @ {event.fill_price}",
                    payload={"fill_price": str(event.fill_price), "qty": str(event.qty)},
                    trade_serial=event.trade_serial,
                ),
                LogSignal(
                    event_type=LogEventType.STATE,
                    message=f"entry={entry_price} hard_sl={hard_sl} trail=INACTIVE",
                    trade_serial=event.trade_serial,
                ),
                UpdateState({
                    "trade_serial": event.trade_serial,
                    "entry_price": str(entry_price),
                    "entry_time": datetime.now(timezone.utc).isoformat(),
                    "high_water_mark": str(entry_price),
                    "current_stop": str(hard_sl),
                    "trail_activated": False,
                    "qty": str(event.qty),
                }),
            ])

        elif pos == BotState.EXIT_ORDER_PLACED and event.side == "SELL":
            entry_price = Decimal(str(ctx.state.get("entry_price", "0")))
            pnl = (event.fill_price - entry_price) * event.qty if entry_price > 0 else Decimal("0")

            actions.extend([
                LogSignal(
                    event_type=LogEventType.CLOSED,
                    message=f"{event.symbol} @ {event.fill_price} pnl={pnl:+.2f}",
                    trade_serial=ctx.state.get("trade_serial"),
                ),
                UpdateState({
                    "trade_serial": None, "entry_price": None,
                    "entry_time": None, "high_water_mark": None,
                    "current_stop": None, "trail_activated": False,
                    "qty": None,
                }),
            ])

        return actions

    def _on_rejected(self, event: OrderRejected, ctx: StrategyContext,
                     pos: BotState) -> list[Action]:
        actions: list[Action] = [
            LogSignal(event_type=LogEventType.ERROR, message=f"Order rejected: {event.reason}"),
        ]
        if pos == BotState.ENTRY_ORDER_PLACED:
            actions.append(UpdateState({
                "trade_serial": None, "entry_time": None,
            }))
        return actions
