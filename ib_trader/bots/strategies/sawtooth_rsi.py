"""Sawtooth RSI Reversal Strategy.

Buys intraday dips within confirmed sawtooth uptrends on 3-minute bars.
Exit via client-side trailing stop (hard SL, percentage trail, time stop).
Uses streaming quotes (bid) for ~1-second exit monitoring.

Lifecycle is owned by the bot FSM (ib_trader/bots/fsm.py). Strategies
route event handling on ctx.fsm_state (AWAITING_ENTRY_TRIGGER /
ENTRY_ORDER_PLACED / AWAITING_EXIT_TRIGGER / EXIT_ORDER_PLACED).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

import pandas as pd

from signals_lib.time_filters import passes_session_filter

from ib_trader.bots.fsm import BotState
from ib_trader.bots.strategy import (
    StrategyManifest, Subscription, StrategyContext,
    MarketEvent, Action,
    BarCompleted, QuoteUpdate, OrderFilled, OrderRejected,
    PlaceOrder, UpdateState, LogSignal,
    ExitType, LogEventType, QuoteField,
)


def _parse_aware_dt(s: str) -> datetime:
    """Parse an ISO datetime string, ensuring it's timezone-aware (UTC)."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt

logger = logging.getLogger(__name__)


class SawtoothRsiStrategy:
    """Sawtooth RSI Reversal ��� buys dips in confirmed uptrends."""

    def __init__(self, config: dict) -> None:
        self.config = config
        symbol = config["symbol"]
        bar_seconds = config.get("bar_size_seconds", 180)
        lookback = config.get("lookback_bars", 100)

        self.manifest = StrategyManifest(
            name="sawtooth_rsi_reversal",
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
                "entry_command_id": "str|null",
                "exit_command_id": "str|null",
            },
            version="1.0",
        )

    async def on_start(self, ctx: StrategyContext) -> list[Action]:
        """Initialize or restore state."""
        actions: list[Action] = []
        if not ctx.state:
            ctx.state = {
                "trade_serial": None,
                "entry_price": None,
                "entry_time": None,
                "high_water_mark": None,
                "current_stop": None,
                "trail_activated": False,
                "entry_command_id": None,
                "exit_command_id": None,
            }
        actions.append(LogSignal(
            event_type=LogEventType.STATE,
            message=f"Strategy started: fsm_state={ctx.fsm_state.value}",
            payload={"config": {k: str(v) for k, v in self.config.items()
                                if k not in ("risk",)}},
        ))
        return actions

    async def on_event(self, event: MarketEvent, ctx: StrategyContext) -> list[Action]:
        """Route events based on FSM state."""
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
        """Cleanup on stop."""
        return [LogSignal(event_type=LogEventType.STATE, message="Strategy stopped")]

    # -------------------------------------------------------------------
    # Bar processing (every 3 minutes) — drives ENTRY signals
    # -------------------------------------------------------------------

    def _on_bar(self, event: BarCompleted, ctx: StrategyContext,
                pos: BotState) -> list[Action]:
        """Process a completed 3-min bar. Only enters when awaiting entry."""
        actions: list[Action] = []
        bar = event.bar
        symbol = self.config["symbol"]

        # Build features on the full window
        window_df = pd.DataFrame(event.window)
        if "timestamp_utc" in window_df.columns:
            window_df["timestamp_utc"] = pd.to_datetime(window_df["timestamp_utc"], utc=True)

        # Use pipeline for most features, then override sawtooth with our params
        from signals_lib.indicators import add_rsi, add_bollinger_bands
        from signals_lib.price_action import add_price_action_features
        from signals_lib.volume import add_volume_features
        from signals_lib.channels import add_channel_features
        from signals_lib.time_filters import add_time_of_day_features
        from signals_lib.sawtooth import add_sawtooth_features

        entry_cfg = self.config.get("entry", {})
        swing_window = entry_cfg.get("swing_window", 3)
        trend_swings = entry_cfg.get("sawtooth_trend_swings", 2)

        featured = window_df.copy()
        featured = add_rsi(featured)
        featured = add_bollinger_bands(featured)
        featured = add_price_action_features(featured)
        if "volume" in featured.columns:
            featured = add_volume_features(featured)
        featured = add_channel_features(featured)
        if "timestamp_utc" in featured.columns:
            featured = add_time_of_day_features(featured)
        featured = add_sawtooth_features(featured, swing_window=swing_window, trend_swings=trend_swings)
        last = featured.iloc[-1]

        # Log bar with key features
        rsi_val = _safe_float(last, "rsi")
        sawtooth = _safe_float(last, "sawtooth_uptrend")
        bars_since = _safe_float(last, "bars_since_swing_low")
        chan_pos = _safe_float(last, "channel_position")
        bounce = _safe_float(last, "bounce_after_dip")

        actions.append(LogSignal(
            event_type=LogEventType.BAR,
            message=f"{symbol} 3min close={bar.get('close')} rsi={rsi_val:.1f} "
                    f"sawtooth={'UP' if sawtooth == 1.0 else 'DOWN'} "
                    f"bars_since_low={bars_since:.0f}",
            payload={
                "symbol": symbol, "close": str(bar.get("close")),
                "rsi": rsi_val, "sawtooth_uptrend": sawtooth,
                "bars_since_swing_low": bars_since,
                "channel_position": chan_pos, "bounce_after_dip": bounce,
                "bar_count": event.bar_count,
            },
        ))

        # Only check entry while awaiting an entry trigger.
        # (Entry timeout is handled by runtime every tick, not here.)
        if pos == BotState.AWAITING_ENTRY_TRIGGER:
            entry_actions = self._check_entry(last, bar, ctx)
            actions.extend(entry_actions)

        return actions

    def _check_entry(self, last: pd.Series, bar: dict,
                     ctx: StrategyContext) -> list[Action]:
        """Evaluate entry conditions on the latest featured bar."""
        actions: list[Action] = []
        entry_cfg = self.config.get("entry", {})
        session_cfg = self.config.get("session_filter", {})
        symbol = self.config["symbol"]

        # Collect condition results
        conditions: dict[str, tuple[bool, str]] = {}

        # 1. Sawtooth uptrend
        sawtooth = _safe_float(last, "sawtooth_uptrend")
        conditions["sawtooth_uptrend"] = (
            sawtooth == 1.0,
            f"sawtooth={'UP' if sawtooth == 1.0 else 'DOWN'}",
        )

        # 2. Near swing low
        bars_since = _safe_float(last, "bars_since_swing_low")
        max_bars = entry_cfg.get("max_bars_since_swing_low", 5)
        at_local_low = _safe_float(last, "at_local_low")
        near_low = bars_since <= max_bars or at_local_low == 1.0
        conditions["near_swing_low"] = (
            near_low,
            f"bars_since_low={bars_since:.0f} (max={max_bars}), at_local_low={at_local_low}",
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

        # Evaluate
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

        # All conditions met — place entry order
        close_price = Decimal(str(bar.get("close", 0)))
        max_value = Decimal(str(self.config.get("max_position_value", "10000")))
        max_shares = self.config.get("max_shares", 20)

        # Calculate quantity
        if close_price > 0:
            qty_by_value = int(max_value / close_price)
            qty = min(qty_by_value, max_shares)
            qty = max(qty, 1)
        else:
            qty = 1

        order_strategy = self.config.get("order_strategy", "mid")

        actions.append(LogSignal(
            event_type=LogEventType.SIGNAL,
            message=f"BUY — all conditions met (rsi={rsi_val:.1f}, "
                    f"bars_since_low={bars_since:.0f}, channel_pos="
                    f"{_safe_float(last, 'channel_position'):.2f})",
            payload={"conditions": {k: {"pass": ok, "detail": d}
                                     for k, (ok, d) in conditions.items()},
                     "qty": qty, "price": str(close_price)},
        ))

        actions.append(PlaceOrder(
            symbol=symbol,
            side="BUY",
            qty=Decimal(str(qty)),
            order_type=order_strategy,
        ))

        actions.append(UpdateState({
            "entry_time": datetime.now(timezone.utc).isoformat(),
        }))

        return actions

    # -------------------------------------------------------------------
    # Quote processing (~1 second) — drives EXIT monitoring
    # -------------------------------------------------------------------

    def _on_quote(self, event: QuoteUpdate, ctx: StrategyContext,
                  pos: BotState) -> list[Action]:
        """Check trailing stop on every streaming quote while awaiting exit."""
        if pos != BotState.AWAITING_EXIT_TRIGGER:
            return []

        state = ctx.state
        entry_price = Decimal(str(state.get("entry_price", "0")))
        if entry_price <= 0:
            # Invariant: AWAITING_EXIT_TRIGGER implies a filled entry with
            # a positive entry_price. If we got here without one, state is
            # inconsistent — surface it loudly instead of silently eating
            # quote ticks forever.
            logger.warning(
                '{"event": "INVARIANT_VIOLATED", "bot_id": "%s", '
                '"field": "entry_price", "value": "%s", '
                '"fsm_state": "AWAITING_EXIT_TRIGGER"}',
                ctx.bot_id, state.get("entry_price"),
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

        # Hard stop loss — always active, never moves
        hard_sl_pct = Decimal(str(exit_cfg.get("hard_stop_loss_pct", "0.001")))
        hard_sl_price = entry_price * (1 - hard_sl_pct)

        if current_price <= hard_sl_price:
            return actions + self._trigger_exit(
                ctx, ExitType.HARD_STOP_LOSS,
                f"bid={current_price} <= hard_sl={hard_sl_price} (pnl={float(pnl_pct):.4%})",
            )

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
                elapsed_minutes = (datetime.now(timezone.utc) - entry_time).total_seconds() / 60
                if elapsed_minutes >= int(time_stop_minutes):
                    return actions + self._trigger_exit(
                        ctx, ExitType.TIME_STOP,
                        f"elapsed={elapsed_minutes:.0f}min >= {int(time_stop_minutes)}min",
                    )

        # Trailing stop
        trail_activation = Decimal(str(exit_cfg.get("trail_activation_pct", "0.0005")))
        trail_width = Decimal(str(exit_cfg.get("trail_width_pct", "0.0015")))
        trail_activated = state.get("trail_activated", False)
        hwm = Decimal(str(state.get("high_water_mark") or current_price))

        if not trail_activated:
            if pnl_pct >= trail_activation:
                # Activate the trail
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
            # Trail is active — ratchet up or trigger
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
                    return actions + self._trigger_exit(
                        ctx, ExitType.TRAILING_STOP,
                        f"bid={current_price} <= trail_stop={trail_stop} "
                        f"(hwm={hwm}, pnl={float(pnl_pct):.4%})",
                    )

        return actions

    def _trigger_exit(self, ctx: StrategyContext, exit_type: ExitType,
                      detail: str) -> list[Action]:
        """Generate actions for an exit trigger.

        Always places the SELL order — the bot has symbol and qty,
        that's all it needs. No serial gating.
        """
        symbol = self.config["symbol"]
        entry_price = ctx.state.get("entry_price")

        return [
            LogSignal(
                event_type=LogEventType.EXIT_CHECK,
                message=f"{exit_type.value}: {detail}",
                payload={"exit_type": exit_type.value,
                         "entry_price": str(entry_price)},
            ),
            PlaceOrder(
                symbol=symbol,
                side="SELL",
                qty=Decimal(str(ctx.state.get("qty", 1))),
                order_type="market",
                origin="exit",
            ),
        ]

    # -------------------------------------------------------------------
    # Fill / Reject handling
    # -------------------------------------------------------------------

    def _on_fill(self, event: OrderFilled, ctx: StrategyContext,
                 pos: BotState) -> list[Action]:
        """Handle order fill events."""
        actions: list[Action] = []
        state = ctx.state

        if pos == BotState.ENTRY_ORDER_PLACED and event.side == "BUY":
            # Entry filled — the FSM will transition to AWAITING_EXIT_TRIGGER
            # on ENTRY_FILLED; strategy only writes trade-scoped state.
            entry_price = event.fill_price
            exit_cfg = self.config.get("exit", {})
            hard_sl = entry_price * (1 - Decimal(str(exit_cfg.get("hard_stop_loss_pct", "0.001"))))

            actions.append(LogSignal(
                event_type=LogEventType.FILL,
                message=f"BUY {event.qty} {event.symbol} @ {event.fill_price} "
                        f"(serial={event.trade_serial})",
                payload={"fill_price": str(event.fill_price), "qty": str(event.qty),
                         "commission": str(event.commission),
                         "ib_order_id": event.ib_order_id},
                trade_serial=event.trade_serial,
            ))

            actions.append(LogSignal(
                event_type=LogEventType.STATE,
                message=f"entry={entry_price} hard_sl={hard_sl} trail=INACTIVE",
                payload={"entry_price": str(entry_price), "hard_sl": str(hard_sl)},
                trade_serial=event.trade_serial,
            ))

            actions.append(UpdateState({
                "trade_serial": event.trade_serial,
                "entry_price": str(entry_price),
                "entry_time": datetime.now(timezone.utc).isoformat(),
                "high_water_mark": str(entry_price),
                "current_stop": str(hard_sl),
                "trail_activated": False,
                "qty": str(event.qty),
            }))

        elif pos == BotState.EXIT_ORDER_PLACED and event.side == "SELL":
            # Exit filled — the FSM will transition to AWAITING_ENTRY_TRIGGER
            # on EXIT_FILLED; strategy clears trade-scoped state.
            entry_price = Decimal(str(state.get("entry_price", "0")))
            pnl = (event.fill_price - entry_price) * event.qty
            entry_time_str = state.get("entry_time")
            held_seconds: float = 0.0
            if entry_time_str:
                entry_time = _parse_aware_dt(entry_time_str)
                held_seconds = (datetime.now(timezone.utc) - entry_time).total_seconds()

            actions.append(LogSignal(
                event_type=LogEventType.CLOSED,
                message=f"{event.symbol} serial={state.get('trade_serial')} "
                        f"@ {event.fill_price} pnl={pnl:+.2f} "
                        f"held={held_seconds / 60:.1f}min",
                payload={"exit_price": str(event.fill_price),
                         "entry_price": str(entry_price),
                         "pnl": str(pnl), "held_seconds": held_seconds,
                         "commission": str(event.commission)},
                trade_serial=state.get("trade_serial"),
            ))

            actions.append(UpdateState({
                "trade_serial": None,
                "entry_price": None,
                "entry_time": None,
                "high_water_mark": None,
                "current_stop": None,
                "trail_activated": False,
                "entry_command_id": None,
                "exit_command_id": None,
                "qty": None,
            }))

        return actions

    def _on_rejected(self, event: OrderRejected, ctx: StrategyContext,
                     pos: BotState) -> list[Action]:
        """Handle order rejection — FSM transitions the lifecycle; the
        strategy just clears any trade-scoped state it seeded."""
        actions: list[Action] = [
            LogSignal(
                event_type=LogEventType.ERROR,
                message=f"Order rejected: {event.reason}",
                payload={"reason": event.reason, "command_id": event.command_id},
            ),
        ]
        if pos == BotState.ENTRY_ORDER_PLACED:
            actions.append(UpdateState({
                "trade_serial": None,
                "entry_time": None,
            }))
        return actions


def _safe_float(row: pd.Series, col: str) -> float:
    """Extract a float from a pandas Series, returning NaN if missing."""
    if col not in row.index:
        return float("nan")
    val = row[col]
    try:
        return float(val)
    except (ValueError, TypeError):
        return float("nan")
