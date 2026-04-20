"""Bot Runtime — orchestrates strategies via the Strategy Protocol.

The runtime:
1. Reads the strategy manifest to determine subscriptions
2. Polls market_bars table for new 5-sec bars
3. Aggregates bars to target size
4. Delivers typed events to the strategy
5. Passes returned actions through the middleware pipeline
6. Manages the quote-based exit monitoring loop

Integrates with the existing bot runner by subclassing BotBase.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

try:
    from redis.exceptions import ConnectionError as _RedisConnectionError
except ImportError:
    # Stand-in type that can never match if redis isn't installed.
    class _RedisConnectionError(Exception):  # type: ignore[no-redef]
        pass
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy.orm import scoped_session

from ib_trader.bots.base import BotBase
from ib_trader.bots.fsm import BotState
from ib_trader.bots.strategy import (
    Strategy, StrategyContext,
    BarCompleted, QuoteUpdate, OrderFilled, OrderRejected,
    PlaceOrder, LogSignal, UpdateState, LogEventType,
)
from ib_trader.bots.bar_aggregator import (
    BarAggregator, flush_state_to_file, load_state_from_file,
)
from ib_trader.bots.middleware import (
    MiddlewarePipeline, RiskMiddleware, LoggingMiddleware,
    PersistenceMiddleware, ExecutionMiddleware, ManualEntryMiddleware,
)
from ib_trader.data.repositories.bot_repository import BotEventRepository

logger = logging.getLogger(__name__)

STATE_DIR = Path.home() / ".ib-trader" / "bot-state"


def _parse_aware_dt(s: str) -> datetime:
    """Parse an ISO datetime string, ensuring it's timezone-aware (UTC)."""
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt
_QUOTE_CHECK_INTERVAL = 1.0  # seconds between exit quote checks
_STALE_QUOTE_WARN_SECONDS = 45   # engine polls every 30s, so 45s = missed one poll
_STALE_QUOTE_HALT_SECONDS = 120  # 2 minutes with no fresh data = halt


class StrategyBotRunner(BotBase):
    """BotBase adapter that runs a Strategy via the runtime.

    This is the bridge between the existing bot runner (which expects
    BotBase subclasses with on_tick) and the new Strategy protocol.
    """

    def __init__(self, bot_id: str, config: dict,
                 session_factory: scoped_session) -> None:
        super().__init__(bot_id, config, session_factory)

        # Load strategy config from YAML
        config_path = config.get("strategy_config",
                                  f"config/strategies/{config.get('strategy_name', 'sawtooth_rsi')}.yaml")
        with open(config_path) as f:
            self.strategy_config = yaml.safe_load(f)

        # Merge runtime overrides from bot config
        if "symbol" in config:
            self.strategy_config["symbol"] = config["symbol"]

        # Strategy instance
        self.strategy: Strategy | None = None
        self.ctx: StrategyContext | None = None
        self.pipeline: MiddlewarePipeline | None = None
        self.aggregator: BarAggregator | None = None
        self._warmup_complete: bool = False
        self._pending_cmd_id: str | None = None  # tracks the active command we're waiting on
        # Stoic mode: the bot ignores every non-order event (quote, bar,
        # position-change) while an order is in flight. The flag is set
        # synchronously when _run_pipeline detects a PlaceOrder action
        # and is only cleared when:
        #   (a) the order-stream handler consumes a terminal event for
        #       self._awaiting_terminal_ib_order_id, OR
        #   (b) the safety timeout elapses (stoic_mode_max_seconds), OR
        #   (c) the pipeline exited without producing an ib_order_id
        #       (nothing was actually placed).
        # Prevents the Apr 19 runaway where a duplicate SELL was fired
        # on every queued quote tick during the round-trip.
        self._order_submit_in_flight: bool = False
        self._awaiting_terminal_ib_order_id: str | None = None
        self._stoic_mode_set_at: float = 0.0  # time.monotonic() when flag was raised

        # Market data state
        self._last_bar_ts: datetime | None = None
        self._last_quote_time: float = time.monotonic()  # init to now, not 0
        self._quote_stale_logged: bool = False

        # Repos for middleware
        self._bot_events_repo = BotEventRepository(session_factory)
        self._session_factory = session_factory

    # ------------------------------------------------------------------
    # Single write path for bot state — Redis is the source of truth
    # ------------------------------------------------------------------

    async def _write_state(self, fields: dict) -> None:
        """Write fields to bot:<id> Redis key and refresh ctx.state.

        This is the ONLY method that writes to the bot's state key.
        PersistenceMiddleware and _apply_fill both call this — nobody
        calls store.set on bot:<id> directly.

        The "state" field (FSM lifecycle: OFF/AWAITING_ENTRY_TRIGGER/etc.)
        is stripped — only FSM.dispatch() may write it.
        """
        redis = self.config.get("_redis")
        if redis is None:
            raise RuntimeError("Redis not available for bot state write")
        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        key = f"bot:{self.bot_id}"
        existing = await store.get(key) or {}
        # "state" is owned by FSM.dispatch(). "position_state" is a
        # deleted concept — the FSM state is the single source of truth.
        # Strip both so a stray write fails closed.
        safe_fields = {k: v for k, v in fields.items()
                       if k not in ("state", "position_state")}
        merged = {**existing, **safe_fields}
        await store.set(key, merged)
        self.ctx.state = merged
        # Nudge WS so UI refreshes immediately
        try:
            from ib_trader.redis.streams import publish_activity
            await publish_activity(redis, "bots")
        except Exception as e:
            logger.debug("STATE_WRITE_NUDGE_FAILED", exc_info=e)

    async def _refresh_state(self) -> None:
        """Re-sync ctx.state and ctx.fsm_state from Redis — the single
        source of truth for state lives in the bot:<id> doc."""
        redis = self.config.get("_redis")
        if redis is None:
            return
        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        doc = await store.get(f"bot:{self.bot_id}") or {}
        self.ctx.state = doc
        self.ctx.fsm_state = BotState(doc.get("state", BotState.AWAITING_ENTRY_TRIGGER.value))

    # ------------------------------------------------------------------

    async def _apply_fill(self, *, bot_ref: str, symbol: str, side: str,
                          qty: Decimal, price: Decimal, commission: Decimal,
                          ib_order_id: str) -> None:
        """Apply a terminal fill to the bot's state and dispatch to strategy.

        The order ledger accumulates partials and emits one terminal event
        with cumulative qty/avg_price. Bot manages one position at a time
        — fills always apply to the current position.
        """
        await self._refresh_state()
        existing_qty = Decimal(str(self.ctx.state.get("qty", "0")))
        now_iso = datetime.now(timezone.utc).isoformat()

        if side == "B":
            new_qty = qty  # terminal fill — cumulative qty from ledger
            fill_event = OrderFilled(
                trade_serial=None, symbol=symbol, side="BUY",
                fill_price=price, qty=qty, commission=commission,
                ib_order_id=ib_order_id,
            )
            # Compute stop levels from strategy config so the UI shows
            # meaningful data immediately — before the first quote tick.
            exit_cfg = self.strategy_config.get("exit", {}) if isinstance(self.strategy_config.get("exit"), dict) else {}
            hard_sl_pct = Decimal(str(exit_cfg.get("hard_stop_loss_pct", 0.003)))
            trail_act_pct = Decimal(str(exit_cfg.get("trail_activation_pct", 0.00005)))
            trail_width = Decimal(str(exit_cfg.get("trail_width_pct", 0.0005)))
            hard_stop = price * (1 - hard_sl_pct)
            trail_activation_price = price * (1 + trail_act_pct)
            # Idempotency: preserve entry_price / entry_time / high_water_mark
            # if they're already set from a previous call on the same position.
            # Today _apply_fill only runs once per order, but any future
            # re-invocation (reconciler, replay) must not reset the entry
            # anchors. Stops + qty still refresh each call.
            keep_entry = bool(self.ctx.state.get("entry_time"))
            engine_fields = {
                "qty": str(new_qty),
                "avg_price": str(price),
                "entry_price": self.ctx.state["entry_price"] if keep_entry else str(price),
                "entry_time": self.ctx.state["entry_time"] if keep_entry else now_iso,
                "symbol": self.strategy_config.get("symbol", ""),
                "high_water_mark": self.ctx.state["high_water_mark"] if keep_entry else str(price),
                "current_stop": str(hard_stop.quantize(Decimal("0.01"))),
                "hard_stop": str(hard_stop.quantize(Decimal("0.01"))),
                "trail_activation_price": str(trail_activation_price.quantize(Decimal("0.01"))),
                "trail_width_pct": str(trail_width),
                "trail_activated": False,
            }
        else:
            new_qty = max(existing_qty - qty, Decimal("0"))
            fill_event = OrderFilled(
                trade_serial=None, symbol=symbol, side="SELL",
                fill_price=price, qty=qty, commission=commission,
                ib_order_id=ib_order_id,
            )
            engine_fields = {
                "qty": str(new_qty),
                "avg_price": str(price),
                "entry_price": self.ctx.state.get("entry_price"),
                "entry_time": self.ctx.state.get("entry_time"),
            }
        engine_fields["updated_at"] = now_iso
        await self._write_state(engine_fields)

        # Strategy tick — trail/exit bookkeeping runs inside on_event.
        actions = await self.strategy.on_event(fill_event, self.ctx)
        if actions:
            await self._run_pipeline(actions)

        # NOTE: realized-P&L recording and the daily-trade counter are
        # driven by the FSM's ``record_trade_closed`` side effect on
        # EXIT_FILLED. The previous in-line block here read entry_price
        # AFTER the FSM had already cleared it on full close, so the
        # daily loss cap could never trip — see code-review item #2.
        # FSM dispatch for fills is done by the caller in _dispatch_event
        # (the order:updates stream handler). Do NOT dispatch here —
        # that would double-count the qty.

    async def _apply_cancel(self, *, bot_ref: str, symbol: str) -> None:
        """Touch the bot doc's updated_at on cancel/rejection.

        The FSM state transition itself is dispatched by the caller
        (_dispatch_event) via EntryCancelled / ExitCancelled. This only
        bumps the timestamp so the UI notices the event.
        """
        redis = self.config.get("_redis")
        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        key = f"bot:{self.bot_id}"
        existing = await store.get(key) or {}
        if not existing:
            return
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        await store.set(key, existing)

    async def _apply_reconciled(self, *, bot_ref: str, symbol: str,
                                 new_state: str, reason: str,
                                 qty: Decimal | None, avg_price: Decimal | None) -> None:
        """Apply a hint from the reconciler — rare path, observability only.

        Lifecycle state remains owned by FSM.dispatch(). This path only
        records the qty/avg_price/reason so the UI shows the reconciled
        view; the FSM transition (if any) is the caller's responsibility.
        """
        redis = self.config.get("_redis")
        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        key = f"bot:{self.bot_id}"
        existing = await store.get(key) or {}
        if qty is not None:
            existing["qty"] = str(qty)
        if avg_price is not None:
            existing["avg_price"] = str(avg_price)
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        existing["reconciled_reason"] = reason
        await store.set(key, existing)
        logger.warning(
            '{"event": "BOT_RECONCILED", "bot_id": "%s", "symbol": "%s", '
            '"new_state": "%s", "reason": "%s"}',
            self.bot_id, symbol, new_state, reason,
        )

    async def _apply_position_event(self, *, bot_ref: str, symbol: str,
                                     ib_qty: Decimal, ib_avg_price: Decimal) -> None:
        """Apply the manual-close reconciliation rule on a positionEvent.

        Discipline contract: bot has exclusive control of a symbol while
        active. If IB's aggregate qty drops below what the bot tracks,
        the user manually closed part/all of the position. Update our
        state to match IB and log a MANUAL_CLOSE event.
        """
        redis = self.config.get("_redis")
        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        key = f"bot:{self.bot_id}"
        existing = await store.get(key) or {}
        cur_state = existing.get("state", BotState.OFF.value)
        if cur_state in (BotState.OFF.value, BotState.ERRORED.value,
                         BotState.AWAITING_ENTRY_TRIGGER.value):
            # No tracked position — nothing to reconcile.
            return

        expected = Decimal(existing.get("qty", "0"))
        actual = abs(ib_qty)   # bot tracks absolute qty; long/short is implicit

        if actual >= expected:
            # IB has at least as much as we expect — no manual reduction.
            # (actual > expected means manual add; bot doesn't claim those.)
            return

        reduction = expected - actual
        now_iso = datetime.now(timezone.utc).isoformat()
        existing["qty"] = str(actual)
        existing["updated_at"] = now_iso
        await store.set(key, existing)

        # Bolt-on FSM dispatch — if actual==0, MANUAL_CLOSE takes us
        # back to AWAITING_ENTRY_TRIGGER in the FSM.
        if actual == 0:
            from ib_trader.bots.fsm import FSM, BotEvent, EventType
            try:
                result = await FSM(self.bot_id, redis).dispatch(BotEvent(
                    EventType.MANUAL_CLOSE,
                    payload={
                        "message": f"IB qty dropped to 0 (bot had {expected})",
                        "reduction": str(reduction),
                    },
                ))
            except Exception:
                logger.exception(
                    '{"event": "FSM_DISPATCH_MANUAL_CLOSE_FAILED", "bot_id": "%s"}',
                    self.bot_id,
                )
            else:
                await self._execute_side_effects(result)

        self.log_event(
            "MANUAL_CLOSE",
            message=f"User manually reduced {symbol} by {reduction} "
                    f"(bot had {expected}, IB has {actual})",
            payload={
                "expected_qty": str(expected),
                "actual_qty": str(actual),
                "reduction": str(reduction),
                "full_close": actual == 0,
            },
        )

    async def _load_state_from_redis(self, redis, bot_ref: str, symbol: str) -> dict | None:
        """Load strategy state from Redis key.

        Returns the state dict if found, None otherwise.
        """
        from ib_trader.redis.state import StateStore
        try:
            store = StateStore(redis)
            state_doc = await store.get(f"bot:{self.bot_id}")
            if state_doc:
                logger.info(
                    '{"event": "STATE_LOADED_REDIS", "bot_id": "%s", "symbol": "%s"}',
                    self.bot_id, symbol,
                )
                return state_doc
        except Exception:
            logger.exception('{"event": "REDIS_STATE_LOAD_ERROR", "bot_ref": "%s"}', bot_ref)
        return None

    async def _run_pipeline(self, actions: list, ctx=None) -> None:
        """Run actions through pipeline and capture any submitted command ID.

        Dispatches FSM events for any PlaceOrder actions that made it
        through the pipeline so the bot's state machine reflects the
        order intent in Redis.
        """
        from ib_trader.bots.strategy import PlaceOrder
        place_orders = [a for a in actions if isinstance(a, PlaceOrder)]

        # Stoic-mode: set the flag synchronously so any quote/bar/
        # position event processed while we're in the HTTP round-trip
        # bails out (see quote-tick handler guard). Once an ib_order_id
        # is captured, the flag stays set until the order-stream handler
        # observes the terminal event for that id (see _dispatch_event).
        had_place_orders = bool(place_orders)
        if had_place_orders:
            self._order_submit_in_flight = True
            self._stoic_mode_set_at = time.monotonic()
        cmd_id: str | None = None
        try:
            await self.pipeline.process(actions, ctx or self.ctx)

            # Capture the command ID if the execution middleware placed an order
            cmd_id = self.pipeline.last_cmd_id
            if cmd_id is not None:
                self._pending_cmd_id = cmd_id
                self.pipeline.last_cmd_id = None

            # Dispatch FSM events whenever PlaceOrder actions went through
            # the pipeline AND produced a command ID (any truthy or "0" value).
            # The FSM transition is safe even on edge cases — cancel/timeout
            # reverts if the order didn't actually execute.
            if place_orders and cmd_id is not None:
                await self._dispatch_place_order_fsm(place_orders, cmd_id)
                logger.info(
                    '{"event": "FSM_PLACE_ORDER_DISPATCHED", "bot_id": "%s", '
                    '"cmd_id": "%s", "side": "%s"}',
                    self.bot_id, cmd_id,
                    place_orders[0].side if place_orders else "?",
                )
        finally:
            if had_place_orders:
                if cmd_id is not None:
                    # Order was placed — remain stoic until the terminal
                    # order-stream event releases the flag.
                    self._awaiting_terminal_ib_order_id = str(cmd_id)
                else:
                    # Pipeline returned without placing an order (e.g.
                    # middleware dropped it, or it raised before submit).
                    # Nothing is in flight; drop stoic mode so the bot
                    # can react to the next event.
                    self._order_submit_in_flight = False
                    self._awaiting_terminal_ib_order_id = None
                    self._stoic_mode_set_at = 0.0

    async def _dispatch_place_order_fsm(self, place_orders, cmd_id: str) -> None:
        """Emit PlaceEntryOrder / PlaceExitOrder FSM events for orders
        that committed through the pipeline.

        ``cmd_id`` is the IB order id returned by the execution middleware.
        It's stored in the FSM doc so the cancel side effect on STOP /
        ENTRY_TIMEOUT has a handle to the order that needs cancelling.
        """
        redis = self.config.get("_redis")
        if redis is None:
            return
        from ib_trader.bots.fsm import FSM, BotEvent, EventType
        fsm = FSM(self.bot_id, redis)
        for order in place_orders:
            event_type = (
                EventType.PLACE_ENTRY_ORDER if order.side == "BUY"
                else EventType.PLACE_EXIT_ORDER
            )
            payload = {
                "symbol": order.symbol,
                "qty": str(order.qty),
                "order_type": order.order_type,
                "origin": getattr(order, "origin", "strategy"),
                "serial": self.ctx.state.get("trade_serial"),
                "ib_order_id": cmd_id,
            }
            try:
                result = await fsm.dispatch(BotEvent(event_type, payload=payload))
            except Exception:
                logger.exception(
                    '{"event": "FSM_DISPATCH_PLACE_ORDER_FAILED", "bot_id": "%s"}',
                    self.bot_id,
                )
            else:
                await self._execute_side_effects(result)

    # ------------------------------------------------------------------
    # FSM side-effect executor
    # ------------------------------------------------------------------

    async def _execute_side_effects(self, result) -> None:
        """Execute the side effects an FSM transition emitted.

        The FSM is purely declarative: handlers in ``fsm.py`` produce a
        ``TransitionResult`` whose ``side_effects`` list captures the
        non-state actions (cancel an order, record realized P&L, log an
        audit event). The runtime is the only executor. Actions other
        than ``cancel_order`` / ``record_trade_closed`` / ``log_event``
        are intentionally no-ops here because the runtime drives them
        directly via the strategy / pipeline path (place orders) or the
        order-stream handler (emit_strategy_event, run_strategy_tick).
        """
        if result is None:
            return
        for se in result.side_effects:
            try:
                if se.action == "cancel_order":
                    await self._handle_cancel_order(se.args)
                elif se.action == "record_trade_closed":
                    await self._handle_record_trade_closed(se.args)
                elif se.action == "log_event":
                    self.log_event(
                        se.args.get("type", "EVENT"),
                        message=se.args.get("message"),
                        payload=se.args.get("payload"),
                    )
                elif se.action == "pager_alert":
                    await self._handle_pager_alert(se.args)
                elif se.action == "retry_exit_order":
                    await self._handle_retry_exit_order(se.args)
                # place_order / emit_strategy_event / run_strategy_tick
                # are owned by the runtime's existing direct paths.
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    '{"event": "SIDE_EFFECT_FAILED", "bot_id": "%s", '
                    '"action": "%s"}',
                    self.bot_id, se.action,
                )

    async def _handle_cancel_order(self, args: dict) -> None:
        """Cancel an in-flight order on STOP / ENTRY_TIMEOUT.

        Uses /engine/cancel-by-symbol because the bot owns one symbol at
        a time and the engine resolves both pre-fill and post-fill cases
        without us having to maintain a serial → ib_order_id map here.
        Pre-fill orders have no trade serial yet, so /engine/close (which
        keys on serial) wouldn't work for the ENTRY_ORDER_PLACED case.
        """
        symbol = args.get("symbol")
        engine_url = self.config.get("_engine_url")
        if not symbol or not engine_url:
            logger.warning(
                '{"event": "CANCEL_SKIPPED_NO_SYMBOL_OR_ENGINE", "bot_id": "%s"}',
                self.bot_id,
            )
            return
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"{engine_url}/engine/cancel-by-symbol",
                json={"symbol": symbol},
            )
            resp.raise_for_status()
            logger.info(
                '{"event": "BOT_CANCEL_BY_SYMBOL", "bot_id": "%s", '
                '"symbol": "%s", "ib_order_id": "%s"}',
                self.bot_id, symbol, args.get("ib_order_id"),
            )

    async def _handle_pager_alert(self, args: dict) -> None:
        """Raise a pager-class (CATASTROPHIC) alert that blocks the UI.

        Writes into the existing ``alerts:active`` Redis hash so the
        public /api/alerts endpoint and the WebSocket alerts channel
        pick it up without any new plumbing. The ``pager`` flag is what
        the frontend keys on to render a blocking modal / fatal banner
        (future wiring — the alert is visible today on the alerts panel
        in any case).

        This is a "model" of the pager integration — a real pager push
        (PagerDuty / Opsgenie / SMS) will attach later and consume the
        same CATASTROPHIC alerts feed, keyed off ``trigger`` and
        ``pager=true``.
        """
        redis = self.config.get("_redis")
        if redis is None:
            logger.error(
                '{"event": "PAGER_ALERT_NO_REDIS", "bot_id": "%s", "trigger": "%s"}',
                self.bot_id, args.get("trigger"),
            )
            return
        from ib_trader.redis.state import StateKeys
        import uuid as _uuid
        alert_id = str(_uuid.uuid4())
        alert_dict = {
            "id": alert_id,
            "severity": args.get("severity", "CATASTROPHIC"),
            "trigger": args.get("trigger", "BOT_PAGER"),
            "message": args.get("message", "Pager alert"),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "pager": True,
            "bot_id": self.bot_id,
            "symbol": args.get("symbol"),
            # Any extra context the caller passed (residual_qty, retries, etc.)
            **{k: v for k, v in args.items()
               if k not in ("severity", "trigger", "message", "symbol")},
        }
        try:
            await StateKeys.publish_alert(redis, alert_id, alert_dict)
            from ib_trader.redis.streams import publish_activity
            await publish_activity(redis, "alerts")
        except Exception:
            logger.exception(
                '{"event": "PAGER_ALERT_PUBLISH_FAILED", "bot_id": "%s", "trigger": "%s"}',
                self.bot_id, args.get("trigger"),
            )
            return
        logger.error(
            '{"event": "PAGER_ALERT_RAISED", "bot_id": "%s", "trigger": "%s", '
            '"symbol": "%s", "alert_id": "%s"}',
            self.bot_id, args.get("trigger"), args.get("symbol") or "", alert_id,
        )

    async def _handle_retry_exit_order(self, args: dict) -> None:
        """Place a follow-up SELL for the residual when an exit order
        terminated with unsold shares.

        Uses the engine's ``/engine/orders`` endpoint directly (bypassing
        the strategy pipeline) so the retry stays local to the FSM's
        decision. The orderRef is tagged with the bot_ref so the fill
        events route back into this bot's order-stream handler.
        """
        symbol = args.get("symbol")
        qty = args.get("qty")
        engine_url = self.config.get("_engine_url")
        bot_ref = self.config.get("bot_ref") or self.config.get("ref")
        if not (symbol and qty and engine_url):
            logger.warning(
                '{"event": "RETRY_EXIT_SKIPPED_INCOMPLETE", "bot_id": "%s"}',
                self.bot_id,
            )
            return
        logger.warning(
            '{"event": "BOT_EXIT_RETRY", "bot_id": "%s", "symbol": "%s", '
            '"qty": "%s", "attempt": %d, "reason": "%s"}',
            self.bot_id, symbol, qty, int(args.get("attempt") or 0),
            args.get("reason", ""),
        )
        # Session-aware aggressive-mid retry — matches the strategy
        # exit convention. The stoic-mode guard in _run_pipeline /
        # _dispatch_event prevents duplicate retries from firing while
        # this one is in flight.
        import httpx
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    f"{engine_url}/engine/orders",
                    json={
                        "symbol": symbol,
                        "side": "SELL",
                        "qty": str(qty),
                        "order_type": "smart_market",
                        "bot_ref": bot_ref,
                    },
                )
                resp.raise_for_status()
        except Exception as e:
            # Retry submission itself failed — escalate to pager. This
            # catches the "IB connection is dead" branch of the user's
            # requirement: if we can't even get the order across, page.
            logger.exception(
                '{"event": "BOT_EXIT_RETRY_FAILED", "bot_id": "%s", "symbol": "%s"}',
                self.bot_id, symbol,
            )
            await self._handle_pager_alert({
                "trigger": "BOT_EXIT_RETRY_SUBMIT_FAILED",
                "severity": "CATASTROPHIC",
                "symbol": symbol,
                "message": (
                    f"Failed to submit retry exit for {qty} {symbol}: {e}. "
                    f"Connection to engine may be down. Manual intervention required."
                ),
                "residual_qty": str(qty),
            })

    async def _handle_record_trade_closed(self, args: dict) -> None:
        """Record realized P&L + trade count when an exit fully fills.

        Driven by the FSM's ``_h_exit_filled`` handler which already
        computes ``realized_pnl = (price - entry_price) * order_qty``
        before clearing the position fields. This is the ONLY place that
        record_pnl / record_trade are called for closed trades — the
        legacy in-line record in ``_apply_fill`` has been removed so we
        don't double-count.

        Counters live in Redis (``bot:stats:<bot_id>``) so they survive
        a runner restart.
        """
        if not self._risk_mw:
            return
        realized_pnl_str = args.get("realized_pnl") or "0"
        try:
            pnl = Decimal(realized_pnl_str)
        except (ValueError, TypeError):
            pnl = Decimal("0")
        if pnl != 0:
            await self._risk_mw.record_pnl(pnl)
        await self._risk_mw.record_trade()

    async def on_startup(self, open_positions: list) -> None:
        """Initialize strategy, aggregator, middleware, and restore state."""
        # Create the strategy instance
        strategy_name = self.config.get("strategy_name", "sawtooth_rsi")
        self.strategy = _create_strategy(strategy_name, self.strategy_config)

        if self.strategy is None:
            raise ValueError(f"Unknown strategy: {strategy_name}")

        # Restore or initialize state from Redis
        symbol = self.strategy_config["symbol"]
        redis = self.config.get("_redis")
        engine_url = self.config.get("_engine_url")
        bot_ref = self.strategy_config.get("ref_id", self.bot_id)

        if redis is None:
            raise RuntimeError("Redis not available — bot cannot start without Redis")
        if engine_url is None:
            raise RuntimeError("Engine URL not configured — bot cannot start without engine HTTP API")

        state = await self._load_state_from_redis(redis, bot_ref, symbol)
        if state is None:
            state = {}
        state = _reconcile_state(state, open_positions, symbol, self.bot_id)

        fsm_state = BotState(state.get("state", BotState.AWAITING_ENTRY_TRIGGER.value))
        self.ctx = StrategyContext(
            state=state,
            fsm_state=fsm_state,
            bot_id=self.bot_id,
            config=self.strategy_config,
        )

        # Set up aggregator
        manifest = self.strategy.manifest
        bar_sub = next((s for s in manifest.subscriptions if s.type == "bars"), None)
        if bar_sub:
            bar_seconds = bar_sub.params.get("bar_seconds", 180)
            lookback = bar_sub.params.get("lookback", 100)

            # Try to restore aggregator state
            agg_state = load_state_from_file(STATE_DIR, self.bot_id,
                                              f"{self.strategy_config['symbol']}-agg")
            if agg_state:
                self.aggregator = BarAggregator.from_state_dict(agg_state)
                logger.info('{"event": "AGGREGATOR_RESTORED", "bars": %d}',
                            self.aggregator.buffered_bars)
            else:
                self.aggregator = BarAggregator(bar_seconds, lookback)

        # Set up middleware pipeline
        risk_config = {**self.strategy_config.get("risk", {}),
                       "max_position_value": self.strategy_config.get("max_position_value", "10000"),
                       "max_shares": self.strategy_config.get("max_shares", 20)}

        # manual_entry_only comes from the BotDefinition (YAML) once the
        # runner flip lands (step 5). For now, pull it from the bot's
        # config dict if present so YAML-defined test bots already get
        # the gate when runner reads them.
        manual_entry_only = bool(self.config.get("manual_entry_only", False))
        manual_entry_mw = ManualEntryMiddleware(self.bot_id, manual_entry_only)

        # BotStateStore gives RiskMiddleware its fail-closed KILL_SWITCH
        # read against Redis. When redis is None (test fixtures without
        # Redis), the store's own fail-closed logic kicks in and BUYs
        # are rejected — which is the safer default for tests anyway.
        from ib_trader.bots.state import BotStateStore
        state_store = BotStateStore(redis)

        risk_mw = RiskMiddleware(
            self.bot_id, risk_config,
            self._trades,
            state_store=state_store,
        )
        logging_mw = LoggingMiddleware(self.bot_id, self._bot_events_repo, redis=redis)
        persistence_mw = PersistenceMiddleware(
            self.bot_id, write_fn=self._write_state,
        )
        execution_mw = ExecutionMiddleware(
            self.bot_id,
            engine_url=engine_url, bot_ref=bot_ref,
        )
        self._execution_mw = execution_mw

        # ManualEntryMiddleware runs FIRST so blocked entries never
        # count against risk limits and the audit log sees the drop.
        self.pipeline = MiddlewarePipeline(
            [manual_entry_mw, risk_mw, logging_mw, persistence_mw, execution_mw],
            rollback_fn=self._write_state,
        )
        self._risk_mw = risk_mw

        # Subscribe to bars via engine HTTP API (retry — engine may not be ready yet)
        symbol = self.strategy_config["symbol"]
        import httpx
        for attempt in range(10):
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.post(
                        f"{engine_url}/engine/subscribe-bars",
                        json={"symbol": symbol},
                    )
                    resp.raise_for_status()
                    logger.info('{"event": "BARS_SUBSCRIBED_HTTP", "symbol": "%s"}', symbol)
                    break
            except (httpx.ConnectError, httpx.ConnectTimeout):
                if attempt < 9:
                    logger.info(
                        '{"event": "ENGINE_NOT_READY", "attempt": %d, "symbol": "%s"}',
                        attempt + 1, symbol,
                    )
                    await asyncio.sleep(2)
                else:
                    logger.warning('{"event": "ENGINE_CONNECT_GAVE_UP", "symbol": "%s"}', symbol)

        # Warmup: prefetch historical 3-min bars to fill the aggregator immediately
        await self._warmup_from_history(symbol)
        self._warmup_complete = True
        self._signal_cooldown_until = time.monotonic() + 15  # no signals for 15s after startup

        # Run strategy startup
        actions = await self.strategy.on_start(self.ctx)
        if actions:
            await self._run_pipeline(actions)

        logger.info('{"event": "STRATEGY_BOT_STARTED", "bot_id": "%s", '
                     '"strategy": "%s", "symbol": "%s", "fsm_state": "%s"}',
                     self.bot_id, self.strategy.manifest.name, symbol,
                     self.ctx.fsm_state.value)

    async def run_event_loop(self) -> None:
        """Drive the bot purely from Redis stream events.

        Multiplexes XREAD BLOCK across:
          - quote:{symbol}        → QuoteUpdate (every IB tick)
          - bar:{symbol}:5s       → bar aggregation → BarCompleted
          - fill:{bot_ref}        → OrderFilled / OrderRejected
          - position:changes      → external close detection

        The IB quote stream is the bot's clock: when no quotes arrive
        (market closed), the bot does nothing. Supervisory tasks
        (heartbeat, entry timeout, stale quote watchdog) run as
        separate asyncio tasks managed by the runner.
        """
        if not self.strategy or not self.ctx:
            raise RuntimeError("Bot not initialized — call on_startup() first")

        symbol = self.strategy_config["symbol"]
        bot_ref = self.strategy_config.get("ref_id", self.bot_id)
        redis = self.config.get("_redis")
        if redis is None:
            raise RuntimeError("Redis required for event-driven bot")

        from ib_trader.redis.streams import StreamNames
        quote_stream = StreamNames.quote(symbol)
        bar_stream = StreamNames.bar(symbol, "5s")
        order_stream = StreamNames.order_updates()
        pos_stream = StreamNames.position_changes()

        # orderRef prefix for filtering — only events matching our bot
        _order_ref_prefix = f"IBT:{bot_ref}:"

        # Resume bar stream from the warmup cursor so no bars are dropped in
        # the gap between warmup completion and the XREAD below. If warmup
        # didn't run (e.g. no aggregator), start at "$" as before.
        bar_start = getattr(self, '_last_bar_stream_id', None) or "$"
        streams = {
            quote_stream: "$",
            bar_stream: bar_start,
            order_stream: "$",
            pos_stream: "$",
        }

        logger.info(
            '{"event": "BOT_EVENT_LOOP_STARTED", "bot_id": "%s", "symbol": "%s", '
            '"streams": ["%s", "%s", "%s", "%s"]}',
            self.bot_id, symbol, quote_stream, bar_stream, order_stream, pos_stream,
        )

        while True:
            try:
                # 5s timeout = liveness floor. If Redis returns nothing for 5s,
                # we still loop (no work to do, just wait for next event).
                results = await redis.xread(streams, block=5000)
            except asyncio.CancelledError:
                raise
            except (ConnectionError, OSError, _RedisConnectionError) as e:
                raise asyncio.CancelledError() from e
            except Exception:
                logger.exception('{"event": "BOT_XREAD_ERROR", "bot_id": "%s"}', self.bot_id)
                await asyncio.sleep(1)
                continue

            if not results:
                continue

            for stream_name, entries in results:
                for entry_id, raw_data in entries:
                    streams[stream_name] = entry_id
                    try:
                        await self._dispatch_event(stream_name, raw_data,
                                                    quote_stream, bar_stream,
                                                    order_stream, pos_stream,
                                                    symbol, bot_ref,
                                                    _order_ref_prefix)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            '{"event": "BOT_DISPATCH_ERROR", "bot_id": "%s", "stream": "%s"}',
                            self.bot_id, stream_name,
                        )

    async def _check_stoic_mode_timeout(self) -> None:
        """Release stoic mode if the terminal event never arrived.

        Normally stoic mode clears when the order-stream handler sees
        ``terminal=True`` for ``self._awaiting_terminal_ib_order_id``.
        If IB goes silent or a stream publisher drops the terminal
        event, the flag would pin the bot mute forever. Bound the
        hang at ``stoic_mode_max_seconds`` (default 300) and surface a
        WARNING alert so the operator investigates.
        """
        if not self._order_submit_in_flight:
            return
        if self._stoic_mode_set_at == 0.0:
            return
        max_seconds = float(
            self.config.get("stoic_mode_max_seconds")
            or self.config.get("_settings", {}).get("stoic_mode_max_seconds", 300)
        )
        if time.monotonic() - self._stoic_mode_set_at < max_seconds:
            return

        awaited = self._awaiting_terminal_ib_order_id or "<unknown>"
        logger.error(
            '{"event": "BOT_STOIC_MODE_TIMEOUT", "bot_id": "%s", '
            '"ib_order_id": "%s", "timeout_s": %.0f}',
            self.bot_id, awaited, max_seconds,
        )
        # Publish a WARNING alert through the same pager-style writer
        # used by fatal bot events — reuse the existing helper.
        try:
            await self._handle_pager_alert({
                "trigger": "BOT_STOIC_MODE_TIMEOUT",
                "severity": "WARNING",
                "symbol": self.strategy_config.get("symbol"),
                "message": (
                    f"Bot {self.bot_id} was waiting on terminal event for "
                    f"ib_order_id={awaited} but it never arrived within "
                    f"{max_seconds:.0f}s. Stoic mode released; investigate."
                ),
                "ib_order_id": awaited,
            })
        except Exception:
            logger.exception(
                '{"event": "STOIC_TIMEOUT_ALERT_FAILED", "bot_id": "%s"}',
                self.bot_id,
            )
        self._order_submit_in_flight = False
        self._awaiting_terminal_ib_order_id = None
        self._stoic_mode_set_at = 0.0

    async def _dispatch_event(self, stream_name: str, raw_data: dict,
                               quote_stream: str, bar_stream: str,
                               order_stream: str, pos_stream: str,
                               symbol: str, bot_ref: str,
                               order_ref_prefix: str = "") -> None:
        """Route a single Redis stream entry to the strategy."""
        import json as _json

        # Safety timeout on stoic mode. If for some reason we never
        # received the terminal order-stream event for our order, the
        # flag would pin the bot silent forever. Break out after N
        # seconds, log, and surface a WARNING alert so the operator
        # notices.
        await self._check_stoic_mode_timeout()

        # Refresh ctx.state from Redis at the top of every event —
        # single source of truth, no stale reads.
        await self._refresh_state()

        # Deserialize JSON-encoded values
        data = {}
        for k, v in raw_data.items():
            try:
                data[k] = _json.loads(v)
            except (ValueError, TypeError):
                data[k] = v

        # ── Quote tick ─────────────────────────────────────────────────
        if stream_name == quote_stream:
            if self.ctx.fsm_state != BotState.AWAITING_EXIT_TRIGGER:
                return  # Quotes only matter for exit monitoring
            if self._order_submit_in_flight:
                # An order is being submitted. The FSM hasn't transitioned
                # yet (that happens AFTER the HTTP response), so the state
                # check above passes — but the strategy would emit a
                # duplicate exit if we ran it here. Skip this tick; the
                # order-stream handler will wake the bot up when the order
                # terminalises.
                return

            bid_str = data.get("bid")
            ask_str = data.get("ask")
            last_str = data.get("last")
            if not (bid_str or ask_str or last_str):
                return

            bid = Decimal(str(bid_str)) if bid_str else Decimal("0")
            ask = Decimal(str(ask_str)) if ask_str else Decimal("0")
            last = Decimal(str(last_str)) if last_str else Decimal("0")

            ts_str = data.get("ts")
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            quote = QuoteUpdate(
                symbol=symbol,
                bid=bid if bid > 0 else last,
                ask=ask if ask > 0 else last,
                last=last,
                timestamp=ts,
            )
            self._last_quote_time = time.monotonic()
            self._quote_stale_logged = False
            actions = await self.strategy.on_event(quote, self.ctx)
            if actions:
                await self._run_pipeline(actions)
            return

        # ── Bar completion (5s raw bar from IB) ────────────────────────
        if stream_name == bar_stream:
            if not self.aggregator:
                return
            if self._order_submit_in_flight:
                # Mirror of the quote-tick guard. A strategy eval here
                # could re-emit a duplicate entry while the first is in
                # flight. Skip; next bar after the order terminalises
                # will carry the up-to-date state.
                return

            bar = {
                "timestamp_utc": data.get("ts", ""),
                "open": float(data.get("o", 0)),
                "high": float(data.get("h", 0)),
                "low": float(data.get("l", 0)),
                "close": float(data.get("c", 0)),
                "volume": int(data.get("v", 0)),
            }
            completed = self.aggregator.add_bars([bar])
            if not completed:
                return

            # Persist aggregator state so the next start can warm-start
            # from the on-disk snapshot instead of re-fetching history.
            try:
                flush_state_to_file(
                    STATE_DIR, self.bot_id,
                    f"{symbol}-agg",
                    self.aggregator.to_state_dict(),
                )
            except OSError:
                logger.warning(
                    '{"event": "AGG_STATE_FLUSH_FAILED", "bot_id": "%s", '
                    '"symbol": "%s"}',
                    self.bot_id, symbol,
                )

            # Skip during post-startup cooldown
            cooldown = getattr(self, "_signal_cooldown_until", 0)
            if time.monotonic() < cooldown:
                return

            window = self.aggregator.get_bar_window()
            if not window:
                return

            # Only evaluate the LAST completed bar (catch-up batches → stale signals)
            last_bar = completed[-1]
            event = BarCompleted(
                symbol=symbol,
                bar=last_bar,
                window=window,
                bar_count=self.aggregator.bar_count,
            )
            actions = await self.strategy.on_event(event, self.ctx)
            if actions:
                await self._run_pipeline(actions)
            return

        # ── Order updates from engine (unified stream) ─────────────────
        if stream_name == order_stream:
            # Filter: only process events tagged with our orderRef prefix
            order_ref = data.get("orderRef") or ""
            if not order_ref.startswith(order_ref_prefix):
                return  # not ours — different bot or manual order

            status = data.get("status", "")
            terminal = data.get("terminal", False)
            side = data.get("side", "")
            filled_qty_str = data.get("filled_qty", "0")
            avg_price_str = data.get("avg_price")
            last_fill_qty_str = data.get("last_fill_qty")
            last_fill_price_str = data.get("last_fill_price")
            event_ib_order_id = str(data.get("ib_order_id") or "")

            # Release stoic mode as soon as the terminal event for the
            # order we were waiting on arrives. This is the only
            # mechanism (other than the safety timeout) that clears the
            # flag once a PlaceOrder has actually been submitted.
            if (
                terminal
                and self._awaiting_terminal_ib_order_id is not None
                and event_ib_order_id == self._awaiting_terminal_ib_order_id
            ):
                logger.info(
                    '{"event": "BOT_STOIC_MODE_RELEASED", "bot_id": "%s", '
                    '"ib_order_id": "%s", "status": "%s"}',
                    self.bot_id, event_ib_order_id, status,
                )
                self._order_submit_in_flight = False
                self._awaiting_terminal_ib_order_id = None
                self._stoic_mode_set_at = 0.0

            # Always dispatch to FSM for doc update (both progress + terminal)
            from ib_trader.bots.fsm import FSM, BotEvent, EventType
            fsm = FSM(self.bot_id, self.config.get("_redis"))

            if status in ("Filled", "PartiallyFilled", "PartialFillCancelled"):
                # Fill event — progress or terminal
                fsm_event = EventType.ENTRY_FILLED if side == "BUY" else EventType.EXIT_FILLED
                fill_result = await fsm.dispatch(BotEvent(fsm_event, payload={
                    "qty": filled_qty_str,
                    "price": avg_price_str or "0",
                    "commission": data.get("total_commission", "0"),
                    "terminal": terminal,
                    "last_fill_qty": last_fill_qty_str,
                    "last_fill_price": last_fill_price_str,
                }))
                # Execute side effects emitted by the fill transition —
                # in particular ``record_trade_closed`` from the terminal
                # exit path, which is now the sole producer of P&L.
                await self._execute_side_effects(fill_result)

                # On terminal fills, also update strat:* key + dispatch to
                # the strategy for trail init bookkeeping. P&L is handled
                # by the FSM-driven side effect above.
                if terminal:
                    await self._apply_fill(
                        bot_ref=bot_ref,
                        symbol=symbol,
                        side=side[0] if side else "",  # "B" or "S"
                        qty=Decimal(filled_qty_str),
                        price=Decimal(avg_price_str or "0"),
                        commission=Decimal(data.get("total_commission", "0")),
                        ib_order_id=data.get("ib_order_id", ""),
                    )

            elif terminal and status in ("Cancelled", "Rejected"):
                # Terminal cancel/reject — revert state via FSM.
                pos = self.ctx.fsm_state
                fsm_event = (
                    EventType.ENTRY_CANCELLED
                    if pos == BotState.ENTRY_ORDER_PLACED
                    else EventType.EXIT_CANCELLED
                )
                cancel_result = await fsm.dispatch(BotEvent(fsm_event, payload={
                    "reason": status,
                    "filled_qty": filled_qty_str,
                }))
                await self._execute_side_effects(cancel_result)
                await self._apply_cancel(bot_ref=bot_ref, symbol=symbol)
                rejected = OrderRejected(
                    trade_serial=None,
                    symbol=symbol,
                    reason=f"Order {status}",
                    command_id="",
                )
                actions = await self.strategy.on_event(rejected, self.ctx)
                if actions:
                    await self._run_pipeline(actions)

            # Non-terminal, non-fill statuses (Submitted, PreSubmitted) — log only
            return

        # ── Position change (external manipulation) ────────────────────
        if stream_name == pos_stream:
            if self._order_submit_in_flight:
                # A manual-close detector running mid-submit could emit a
                # second, conflicting order. Skip; the position-change
                # event will be re-evaluated from the Redis positionEvent
                # cache next time around.
                return
            if data.get("symbol") != symbol:
                return
            # Only react to STK position events — option contracts share
            # the same symbol and would cause false MANUAL_CLOSE triggers.
            # Once we re-key by con_id this filter goes away.
            evt_sec_type = str(data.get("sec_type", "STK")).upper()
            bot_sec_type = self.strategy_config.get("sec_type", "STK").upper()
            if evt_sec_type != bot_sec_type:
                return
            await self._apply_position_event(
                bot_ref=bot_ref,
                symbol=symbol,
                ib_qty=Decimal(str(data.get("qty", "0"))),
                ib_avg_price=Decimal(str(data.get("avg_price", "0"))),
            )
            return

    async def on_tick(self) -> None:
        """DEPRECATED — bot is event-driven via run_event_loop().

        Kept as a no-op for backward compatibility. Old runners that
        call this method will see a warning logged once and nothing
        else.
        """
        if not getattr(self, '_on_tick_warned', False):
            logger.warning(
                '{"event": "ON_TICK_DEPRECATED", "bot_id": "%s", '
                '"msg": "on_tick() is deprecated — use run_event_loop()"}',
                self.bot_id,
            )
            self._on_tick_warned = True

    async def check_entry_timeout(self) -> None:
        """Supervisory check: cancel entry if ENTRY_ORDER_PLACED has sat
        longer than entry_timeout_seconds.

        Called periodically by the runner's supervisory task — not driven
        by market events because timeout is purely time-based.
        """
        if not self.strategy or not self.ctx:
            return
        await self._refresh_state()
        if self.ctx.fsm_state != BotState.ENTRY_ORDER_PLACED:
            return

        timeout = self.strategy_config.get("exit", {}).get("entry_timeout_seconds", 30)
        entry_time_str = self.ctx.state.get("entry_time")
        if not entry_time_str:
            return

        entry_time = _parse_aware_dt(entry_time_str)
        elapsed = (datetime.now(timezone.utc) - entry_time).total_seconds()
        if elapsed > timeout:
            from ib_trader.bots.fsm import FSM, BotEvent, EventType
            fsm = FSM(self.bot_id, self.config.get("_redis"))
            timeout_result = await fsm.dispatch(
                BotEvent(EventType.ENTRY_TIMEOUT, payload={
                    "elapsed_seconds": elapsed,
                })
            )
            await self._execute_side_effects(timeout_result)
            actions = [
                LogSignal(
                    event_type=LogEventType.ORDER,
                    message=f"Entry timeout after {elapsed:.0f}s — cancelled",
                ),
                UpdateState({
                    "trade_serial": None,
                    "entry_time": None,
                    "entry_command_id": None,
                }),
            ]
            await self._run_pipeline(actions)

    _stop_requested: bool = False

    def request_stop(self) -> None:
        """Signal the event loop to exit cleanly on next iteration."""
        self._stop_requested = True

    async def force_buy(self) -> dict:
        """Execute a force-buy immediately. Called directly by the runner
        HTTP API — no polling, no Redis key, no control stream.

        Returns a result dict with order details.
        """
        if not self.strategy or not self.ctx:
            raise RuntimeError("Bot not initialized")
        await self._refresh_state()
        symbol = self.strategy_config["symbol"]
        await self._execute_force_buy(symbol)
        return {"symbol": symbol, "action": "FORCE_BUY"}

    async def check_force_buy(self) -> None:
        """DEPRECATED — use force_buy() via the runner HTTP API instead.

        Kept for backward compatibility with old control-stream path.
        """
        if not self.strategy or not self.ctx:
            return
        last_action = await self.read_last_action()
        if last_action != "FORCE_BUY":
            return

        await self.clear_last_action()
        symbol = self.strategy_config["symbol"]
        await self._refresh_state()

        if self.ctx.fsm_state == BotState.AWAITING_ENTRY_TRIGGER:
            await self._execute_force_buy(symbol)
        else:
            actions = [LogSignal(
                event_type=LogEventType.RISK,
                message=f"FORCE_BUY ignored — fsm_state is {self.ctx.fsm_state.value}, "
                        "not AWAITING_ENTRY_TRIGGER",
            )]
            await self._run_pipeline(actions)

    async def check_stale_quote(self) -> None:
        """Supervisory check: warn / halt if no quote arrives for too long."""
        if not self.strategy or not self.ctx:
            return
        await self._refresh_state()
        if self.ctx.fsm_state != BotState.AWAITING_EXIT_TRIGGER:
            return  # Stale quotes only matter when monitoring exits

        elapsed = time.monotonic() - self._last_quote_time
        if elapsed > _STALE_QUOTE_HALT_SECONDS and not self._quote_stale_logged:
            self._quote_stale_logged = True
            actions = [LogSignal(
                event_type=LogEventType.ERROR,
                message=f"No quote data for {elapsed:.0f}s — halting bot",
                payload={"no_fresh_data_s": elapsed},
            )]
            await self._run_pipeline(actions)
            from ib_trader.bots.fsm import FSM, BotEvent, EventType
            try:
                crash_result = await FSM(
                    self.bot_id, self.config.get("_redis"),
                ).dispatch(
                    BotEvent(EventType.CRASH, payload={"message": "STALE_QUOTES"})
                )
            except Exception:
                logger.exception(
                    '{"event": "STALE_QUOTE_CRASH_DISPATCH_FAILED", "bot_id": "%s"}',
                    self.bot_id,
                )
            else:
                await self._execute_side_effects(crash_result)
        elif elapsed > _STALE_QUOTE_WARN_SECONDS and not self._quote_stale_logged:
            logger.warning(
                '{"event": "STALE_QUOTES", "bot_id": "%s", "no_fresh_s": %.1f}',
                self.bot_id, elapsed,
            )

    async def on_stop(self) -> None:
        """Cleanup on bot stop."""
        if self.strategy and self.ctx:
            actions = await self.strategy.on_stop(self.ctx)
            if actions and self.pipeline:
                await self._run_pipeline(actions)

        # Unsubscribe bars via engine HTTP API
        symbol = self.strategy_config.get("symbol", "")
        engine_url = self.config.get("_engine_url")
        if engine_url and symbol:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    await client.post(
                        f"{engine_url}/engine/unsubscribe-bars",
                        json={"symbol": symbol},
                    )
            except Exception:
                from ib_trader.logging_.alerts import log_and_alert
                await log_and_alert(
                    redis=self.config.get("_redis"),
                    trigger="UNSUBSCRIBE_HTTP_FAILED",
                    message=f"Failed to unsubscribe bars for {symbol} via engine HTTP.",
                    severity="WARNING",
                    bot_id=self.bot_id, symbol=symbol,
                )

    async def _execute_force_buy(self, symbol: str) -> None:
        """Execute a forced buy, bypassing all entry conditions."""
        config = self.strategy_config
        close_price = Decimal("0")

        # Get latest price from Redis quote key
        redis = self.config.get("_redis")
        if redis:
            from ib_trader.redis.state import StateStore, StateKeys
            store = StateStore(redis)
            quote = await store.get(StateKeys.quote_latest(symbol))
            if quote:
                last = quote.get("last")
                if last:
                    close_price = Decimal(str(last))

        # Calculate quantity
        max_value = Decimal(str(config.get("max_position_value", "10000")))
        max_shares = config.get("max_shares", 20)
        if close_price > 0:
            qty = min(int(max_value / close_price), max_shares)
            qty = max(qty, 1)
        else:
            qty = 1

        order_strategy = config.get("order_strategy", "mid")

        actions = [
            LogSignal(
                event_type=LogEventType.SIGNAL,
                message=f"FORCE BUY (manual override) — {symbol} qty={qty} @ {order_strategy}",
                payload={"type": "FORCE_BUY", "symbol": symbol,
                         "qty": qty, "price": str(close_price)},
            ),
            PlaceOrder(
                symbol=symbol,
                side="BUY",
                qty=Decimal(str(qty)),
                order_type=order_strategy,
            ),
            UpdateState({
                "entry_time": datetime.now(timezone.utc).isoformat(),
            }),
        ]
        await self._run_pipeline(actions)

    async def _warmup_from_history(self, symbol: str) -> None:
        """Prefetch historical bars via the engine, then read them from Redis.

        Critical: snapshot the bar stream's latest entry ID BEFORE asking
        the engine to publish warmup bars. Without that, stale entries left
        over from prior runs are consumed instead of the freshly published
        historical bars. The captured cursor also seeds run_event_loop so
        live bars arriving between warmup and the event loop are not lost.
        """
        if not self.aggregator:
            return

        lookback = self.strategy_config.get("lookback_bars", 20)
        bar_seconds = self.strategy_config.get("bar_size_seconds", 180)
        total_5sec_bars = lookback * (bar_seconds // 5)
        duration_seconds = total_5sec_bars * 5 + 60

        redis = self.config.get("_redis")
        if redis is not None:
            from ib_trader.redis.streams import StreamNames
            stream_name = StreamNames.bar(symbol, "5s")
            try:
                latest = await redis.xrevrange(stream_name, count=1)
                self._last_bar_stream_id = latest[0][0] if latest else "0"
            except Exception:
                self._last_bar_stream_id = "0"

        engine_url = self.config.get("_engine_url")
        if engine_url:
            import httpx
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    await client.post(
                        f"{engine_url}/engine/warmup-bars",
                        json={"symbol": symbol, "duration_seconds": duration_seconds},
                    )
            except Exception:
                from ib_trader.logging_.alerts import log_and_alert
                await log_and_alert(
                    redis=self.config.get("_redis"),
                    trigger="WARMUP_HTTP_FAILED",
                    message=f"Failed to warm up bars for {symbol} via engine HTTP.",
                    severity="WARNING",
                    bot_id=self.bot_id, symbol=symbol,
                )

        # Read only the warmup bars (and any live bars that landed while we
        # were waiting). Oversize the count so a typical 20x3-min lookback
        # (~720 raw bars) is covered with comfortable headroom.
        bars = await self._read_new_bars(symbol, count=max(total_5sec_bars * 2, 2000))
        if bars and self.aggregator:
            completed = self.aggregator.add_bars(bars)
            if self.pipeline and self.ctx:
                actions = [LogSignal(
                    event_type=LogEventType.STATE,
                    message=(f"Warmup complete: {len(bars)} raw bars loaded, "
                             f"{len(completed)} target bars, "
                             f"buffered={self.aggregator.buffered_bars}/{lookback}"),
                    payload={"raw_bars": len(bars), "completed_bars": len(completed),
                             "buffered": self.aggregator.buffered_bars},
                )]
                await self._run_pipeline(actions)
        else:
            if self.pipeline and self.ctx:
                actions = [LogSignal(
                    event_type=LogEventType.STATE,
                    message="Warmup: no historical bars available, starting cold",
                )]
                await self._run_pipeline(actions)

    async def _read_new_bars(self, symbol: str, count: int = 500) -> list[dict]:
        """Read new bars from the Redis bar stream.

        The engine publishes 5-second bars to bar:{symbol}:5s via
        reqRealTimeBars push callbacks (live) and /engine/warmup-bars
        (historical prefetch).
        """
        redis = self.config.get("_redis")
        if not redis:
            return []

        from ib_trader.redis.streams import StreamNames

        stream = StreamNames.bar(symbol, "5s")
        last_id = getattr(self, '_last_bar_stream_id', "0")

        try:
            results = await redis.xread({stream: last_id}, count=count)
            if not results:
                return []

            bars = []
            for _stream_name, entries in results:
                for entry_id, raw_data in entries:
                    self._last_bar_stream_id = entry_id
                    # Deserialize JSON-encoded values
                    import json as _json
                    data = {}
                    for k, v in raw_data.items():
                        try:
                            data[k] = _json.loads(v)
                        except (ValueError, TypeError):
                            data[k] = v

                    bars.append({
                        "timestamp_utc": data.get("ts", ""),
                        "open": float(data.get("o", 0)),
                        "high": float(data.get("h", 0)),
                        "low": float(data.get("l", 0)),
                        "close": float(data.get("c", 0)),
                        "volume": int(data.get("v", 0)),
                    })
            return bars

        except Exception as exc:
            logger.debug('{"event": "REDIS_BARS_READ_ERROR", "error": "%s"}', exc)
            return []

    async def _get_latest_quote(self, symbol: str):
        """Read the latest quote from Redis key.

        The engine's tick publisher writes to quote:{symbol}:latest
        on every streaming tick from IB.
        """
        redis = self.config.get("_redis")
        if not redis:
            return None

        from ib_trader.redis.state import StateStore, StateKeys

        try:
            store = StateStore(redis)
            quote = await store.get(StateKeys.quote_latest(symbol))
            if not quote:
                return None

            bid_str = quote.get("bid")
            ask_str = quote.get("ask")
            last_str = quote.get("last")

            if not bid_str and not ask_str and not last_str:
                return None

            bid = Decimal(str(bid_str)) if bid_str else Decimal("0")
            ask = Decimal(str(ask_str)) if ask_str else Decimal("0")
            last = Decimal(str(last_str)) if last_str else Decimal("0")

            ts_str = quote.get("ts")
            ts = datetime.fromisoformat(ts_str) if ts_str else datetime.now(timezone.utc)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            return QuoteUpdate(
                symbol=symbol,
                bid=bid if bid > 0 else last,
                ask=ask if ask > 0 else last,
                last=last,
                timestamp=ts,
            )
        except Exception:
            from ib_trader.logging_.alerts import log_and_alert
            await log_and_alert(
                redis=self.config.get("_redis"),
                trigger="REDIS_QUOTE_READ_ERROR",
                message=f"Failed to read latest quote for {symbol} from Redis.",
                severity="WARNING",
                bot_id=self.bot_id, symbol=symbol,
            )
            return None

def _reconcile_state(state: dict | None, open_positions: list,
                     symbol: str, bot_id: str) -> dict:
    """Reconcile persisted bot state against actual IB positions on startup.

    Cases:
    1. No state / no IB position → return empty dict (FSM defaults to
       AWAITING_ENTRY_TRIGGER at context construction).
    2. FSM says AWAITING_EXIT_TRIGGER / EXIT_ORDER_PLACED + IB has
       position → resume (normal restart).
    3. FSM says we hold a position + IB has none → stale, drop to empty.
    4. No state + IB has position → orphan, warn, don't auto-adopt.
    5. FSM says ENTRY_ORDER_PLACED → mid-entry when we crashed; drop to
       empty so the FSM starts clean.

    The FSM state itself is not rewritten here — the strip guard in
    _write_state refuses it. Instead we either keep the state dict intact
    (case 2) or return empty (all others) and let the FSM default kick in.
    """
    ib_has_position = any(
        getattr(t, "symbol", None) == symbol for t in open_positions
    )

    if not state:
        if ib_has_position:
            logger.warning(
                '{"event": "ORPHANED_POSITION", "bot_id": "%s", "symbol": "%s", '
                '"message": "IB has position but no bot state — will not auto-adopt"}',
                bot_id, symbol,
            )
        return {}

    fsm_state = state.get("state", BotState.AWAITING_ENTRY_TRIGGER.value)
    in_position_states = (
        BotState.AWAITING_EXIT_TRIGGER.value,
        BotState.EXIT_ORDER_PLACED.value,
    )

    if fsm_state in in_position_states:
        if not ib_has_position:
            logger.warning(
                '{"event": "STALE_STATE_CLEARED", "bot_id": "%s", "symbol": "%s", '
                '"old_state": "%s", "message": "IB has no position, clearing state"}',
                bot_id, symbol, fsm_state,
            )
            return {}
        logger.info(
            '{"event": "STATE_RECONCILED", "bot_id": "%s", "symbol": "%s", '
            '"state": "%s", "entry_price": "%s"}',
            bot_id, symbol, fsm_state, state.get("entry_price"),
        )
        return state

    if fsm_state == BotState.ENTRY_ORDER_PLACED.value:
        logger.info(
            '{"event": "ENTRY_ORDER_CLEARED_ON_RESTART", '
            '"bot_id": "%s", "symbol": "%s"}',
            bot_id, symbol,
        )
        return {}

    return state


def _parse_fill_from_output(output: str, expected_symbol: str) -> dict | None:
    """Parse serial, fill price, qty, commission from engine command output.

    Expected output format:
        Order #3 — BUY 16 QQQ @ mid
        [08:38:43] Placed @ $611.80 (bid: $611.8 ask: $611.81)
        ✓ FILLED: 16.0 shares QQQ @ $611.7975 avg
          Commission: $0.332657
          Serial: #3
    """
    import re

    result: dict[str, int | Decimal] = {}

    # Parse Serial: #N
    serial_match = re.search(r'Serial:\s*#(\d+)', output)
    if serial_match:
        result["serial"] = int(serial_match.group(1))
    else:
        return None  # No serial = can't identify the trade

    # Parse FILLED or CLOSED: N shares SYMBOL @ $PRICE
    fill_match = re.search(
        r'(?:FILLED|CLOSED):\s*([\d.]+)\s*shares\s*(\w+)\s*@\s*\$([\d.]+)', output
    )
    if fill_match:
        fill_symbol = fill_match.group(2)
        if fill_symbol != expected_symbol:
            return None  # Symbol mismatch
        result["qty"] = Decimal(fill_match.group(1))
        result["fill_price"] = Decimal(fill_match.group(3))
    else:
        result["qty"] = Decimal("0")
        result["fill_price"] = Decimal("0")

    # Parse Commission: $N
    comm_match = re.search(r'Commission:\s*\$([\d.]+)', output)
    result["commission"] = Decimal(comm_match.group(1)) if comm_match else Decimal("0")

    return result


def _load_persisted_state(bot_id: str, symbol: str) -> dict | None:
    """Load bot state from JSON file."""
    path = STATE_DIR / f"{bot_id}-{symbol}.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning('{"event": "STATE_LOAD_FAILED", "path": "%s", "error": "%s"}',
                        path, exc)
        return None


def _create_strategy(name: str, config: dict) -> Strategy | None:
    """Instantiate a strategy by name."""
    if name == "sawtooth_rsi":
        from ib_trader.bots.strategies.sawtooth_rsi import SawtoothRsiStrategy
        return SawtoothRsiStrategy(config)
    if name == "close_trend_rsi":
        from ib_trader.bots.strategies.close_trend_rsi import CloseTrendRsiStrategy
        return CloseTrendRsiStrategy(config)
    return None


# Register with the bot runner
from ib_trader.bots.registry import register_strategy
register_strategy("strategy_bot", StrategyBotRunner)
