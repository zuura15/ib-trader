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
from decimal import Decimal, InvalidOperation
from pathlib import Path

import yaml
from sqlalchemy.orm import scoped_session

from ib_trader.bots.base import BotBase
from ib_trader.bots.lifecycle import (
    BotState, MAX_EXIT_RETRIES as _MAX_EXIT_RETRIES,
    bot_doc_key, clear_position_fields, now_iso,
)
from ib_trader.bots.strategy import (
    Strategy, StrategyContext,
    BarCompleted, QuoteUpdate, OrderFilled, OrderRejected,
    PlaceOrder, LogSignal, UpdateState, LogEventType, ExitType,
)
from ib_trader.bots.bar_aggregator import (
    BarAggregator, flush_state_to_file, load_state_from_file,
)
from ib_trader.bots.middleware import (
    MiddlewarePipeline, RiskMiddleware, LoggingMiddleware,
    PersistenceMiddleware, ExecutionMiddleware, ManualEntryMiddleware,
)
from ib_trader.data.repositories.bot_repository import BotEventRepository
from ib_trader.data.repositories.bot_trade_repository import BotTradeRepository

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
# Halt threshold for the quote-stream heartbeat. Overridden per-deployment
# by settings.quotes_heartbeat_stale_halt_seconds. This is NOT a per-symbol
# check — it's "is the quote stream as a whole alive?". Per-symbol silence
# is fine on low-liquidity instruments (PSQ, etc.).
_DEFAULT_QUOTES_HEARTBEAT_HALT_SECONDS = 120

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
        # Rolling ring of submit timestamps (time.monotonic seconds).
        # Circuit breaker: if `bot_order_rate_limit_count` submissions
        # land within `bot_order_rate_limit_window_seconds`, the bot is
        # force-STOPped to OFF and a CATASTROPHIC alert fires.
        self._recent_submit_times: list[float] = []
        # Dedup guard for ORDER bot_events — a single order can generate
        # several Submitted/PreSubmitted updates as IB routes it. We only
        # want one "ORDER submitted" row in the Activity feed per order.
        self._submitted_logged: set[str] = set()
        # ----- State machinery (post-ADR 016 FSM collapse) -----
        # Per-bot lock serialising every state mutation. Replaces the
        # module-level _DISPATCH_LOCKS that FSM.dispatch used. Every
        # on_* method (on_start, on_stop, on_entry_filled, …) and the
        # place-order flow take this lock around their read-modify-write
        # on the Redis doc so concurrent HTTP handlers + stream handlers
        # never lose updates.
        self._state_lock = asyncio.Lock()
        # Serializes positionEvent reconciliation so concurrent
        # positionEvents on the same bot don't issue overlapping pulls.
        # See ``_apply_position_event`` (GH #85).
        self._position_event_lock = asyncio.Lock()
        # Sentinel used in ``awaiting_ib_order_id`` between the moment
        # on_place_order flips the state and the moment the engine HTTP
        # call returns with a real id.
        self._PENDING_ORDER_ID = "__pending__"

        # Market data state
        self._last_bar_ts: datetime | None = None
        self._last_quote_time: float = time.monotonic()  # init to now, not 0
        self._quote_stale_logged: bool = False

        # Repos for middleware
        self._bot_events_repo = BotEventRepository(session_factory)
        self._bot_trades_repo = BotTradeRepository(session_factory)
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

    async def _read_state_doc(self) -> dict | None:
        """Read the current bot:<id> doc from Redis WITHOUT mutating
        self.ctx. Used by _apply_fill to get the post-FSM values
        instead of the stale pre-FSM snapshot in self.ctx.state."""
        redis = self.config.get("_redis")
        if redis is None:
            return None
        from ib_trader.redis.state import StateStore
        return await StateStore(redis).get(f"bot:{self.bot_id}")

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
        # GH #87: a previous version computed ``existing_qty`` here
        # eagerly. The value was unused (the SELL branch reads from
        # ``fresh_doc`` and the BUY branch ignores prior qty) AND
        # crashed with ``decimal.InvalidOperation`` when ``state["qty"]``
        # was the literal string ``"None"`` after a strategy bug
        # cleared qty to Python ``None``. Removed.
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
            # SELL branch. FSM has already written qty (post-decrement)
            # + cleared entry fields on full exit. _apply_fill must NOT
            # re-decrement qty or resurrect cleared entry fields from
            # the stale ctx.state snapshot. We only update avg_price
            # here; everything else is FSM's authority. Plaster fix
            # pending the Part B FSM architectural cleanup that
            # dissolves _apply_fill entirely.
            fill_event = OrderFilled(
                trade_serial=None, symbol=symbol, side="SELL",
                fill_price=price, qty=qty, commission=commission,
                ib_order_id=ib_order_id,
            )
            try:
                fresh_doc = await self._read_state_doc() or {}
            except Exception:
                fresh_doc = {}
            new_qty = Decimal(str(fresh_doc.get("qty") or "0"))
            engine_fields = {
                "avg_price": str(price),
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

    async def _verify_position_via_pull(self, symbol: str) -> Decimal | None:
        """Force-refresh IB positions via reqPositionsAsync and return the
        current snapshot for ``symbol``.

        Used as the tiebreaker when a positionEvent push disagrees with
        the bot's tracked state (GH #85). The push stream is eventually
        consistent — a positionEvent dispatched moments after a multi-
        venue fill can carry an intermediate snapshot. The pull goes
        against IB's authoritative position book, so its response is
        synchronous-on-IB's-side current.

        Returns ``None`` on failure (engine unreachable, IB disconnected,
        timeout). Callers MUST treat ``None`` as "do not reconcile" — the
        next positionEvent will retry.
        """
        engine_url = self.config.get("_engine_url")
        if not engine_url:
            return None
        import httpx
        try:
            async with httpx.AsyncClient(timeout=5) as c:
                resp = await c.get(
                    f"{engine_url}/engine/positions/refresh",
                    params={"symbol": symbol},
                )
                resp.raise_for_status()
                return abs(Decimal(str(resp.json().get("qty", "0"))))
        except Exception:
            logger.exception(
                '{"event": "POSITION_VERIFY_FAILED", "bot_id": "%s", '
                '"symbol": "%s"}',
                self.bot_id, symbol,
            )
            return None

    async def _apply_position_event(self, *, bot_ref: str, symbol: str,
                                     ib_qty: Decimal, ib_avg_price: Decimal) -> None:
        """Apply the manual-close reconciliation rule on a positionEvent.

        Discipline contract: bot has exclusive control of a symbol while
        active. If IB's aggregate qty drops below what the bot tracks,
        EITHER the user manually closed part/all of the position OR the
        positionEvent feed is delivering a stale snapshot from an
        in-flight fill (GH #85: order-stream and position-stream race).

        We resolve the ambiguity by issuing a fresh ``reqPositionsAsync``
        — a *pull* against IB's authoritative position book. If the pull
        confirms the reduction, it's a real manual close. If the pull
        agrees with our tracked state, the push was stale and we leave
        state alone. If the pull reports a higher position than tracked,
        something genuinely unexpected has happened and we park ERRORED
        rather than guessing.

        A per-symbol lock prevents two positionEvents from queuing
        concurrent verifies.
        """
        async with self._position_event_lock:
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
                # IB has at least as much as we expect — no apparent
                # reduction. (actual > expected means manual add; bot
                # doesn't claim those.)
                return

            # Push says position dropped. Don't trust the push — verify
            # against IB's authoritative book via reqPositions.
            verified = await self._verify_position_via_pull(symbol)
            if verified is None:
                # Pull failed (engine unreachable, IB disconnected,
                # timeout). Fail closed: leave state unchanged. The next
                # positionEvent will re-trigger this code path.
                logger.warning(
                    '{"event": "BOT_POSITION_VERIFY_UNAVAILABLE", '
                    '"bot_id": "%s", "symbol": "%s", "push_qty": "%s", '
                    '"expected": "%s"}',
                    self.bot_id, symbol, actual, expected,
                )
                return

            if verified == expected:
                # Push was stale. State is correct. Log it for
                # observability — frequency of this event tells us how
                # often the push/pull race fires in production.
                logger.info(
                    '{"event": "BOT_POSITION_PUSH_STALE_DETECTED", '
                    '"bot_id": "%s", "symbol": "%s", "push_qty": "%s", '
                    '"verified_qty": "%s"}',
                    self.bot_id, symbol, actual, verified,
                )
                return

            if verified < expected:
                # Pull confirms the reduction. Real manual close.
                reduction = expected - verified
                now_iso = datetime.now(timezone.utc).isoformat()
                existing["qty"] = str(verified)
                existing["updated_at"] = now_iso
                await store.set(key, existing)

                if verified == 0:
                    try:
                        await self.on_manual_close(
                            message=f"IB qty dropped to 0 (bot had {expected})",
                            payload={"reduction": str(reduction)},
                        )
                    except Exception:
                        logger.exception(
                            '{"event": "MANUAL_CLOSE_HANDLER_FAILED", '
                            '"bot_id": "%s"}',
                            self.bot_id,
                        )

                self.log_event(
                    "MANUAL_CLOSE",
                    message=f"User manually reduced {symbol} by {reduction} "
                            f"(bot had {expected}, IB has {verified})",
                    payload={
                        "expected_qty": str(expected),
                        "actual_qty": str(verified),
                        "reduction": str(reduction),
                        "full_close": verified == 0,
                    },
                )
                return

            # verified > expected — IB has *more* than we tracked, in
            # the opposite direction of a manual close. Either someone
            # manually bought outside the bot, or our state is missing
            # a fill we should have seen. Don't guess — escalate to
            # ERRORED for human review.
            try:
                from ib_trader.logging_.alerts import log_and_alert
                await log_and_alert(
                    redis=redis,
                    trigger="BOT_POSITION_UNEXPECTED_INCREASE",
                    severity="CATASTROPHIC",
                    bot_id=self.bot_id,
                    symbol=symbol,
                    message=(
                        f"Bot {self.bot_id} {symbol}: tracked={expected} "
                        f"but IB has {verified}. Parking in ERRORED for "
                        f"manual review."
                    ),
                    extra={"expected": str(expected),
                           "verified": str(verified)},
                    exc_info=False,
                )
            except Exception:
                logger.exception(
                    '{"event": "BOT_POSITION_UNEXPECTED_INCREASE_ALERT_FAILED", '
                    '"bot_id": "%s"}', self.bot_id,
                )
            try:
                await self.on_ib_position_mismatch(
                    message=(
                        f"IB qty {verified} > tracked {expected} for "
                        f"{symbol}; manual review required"
                    ),
                )
            except Exception:
                logger.exception(
                    '{"event": "BOT_POSITION_MISMATCH_HANDLER_FAILED", '
                    '"bot_id": "%s"}', self.bot_id,
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

    # ==================================================================
    # FSM collapse (ADR 016) — state transitions live on the bot.
    # ------------------------------------------------------------------
    # Each ``on_*`` method below replaces a handler from the old
    # ``bots/fsm.py`` ``_h_*`` functions. State is persisted to the
    # same ``bot:<id>`` Redis doc; ``_state_lock`` replaces the
    # module-level ``_DISPATCH_LOCKS`` that FSM.dispatch used.
    #
    # All effects that used to be ``SideEffect`` objects in
    # ``TransitionResult.side_effects`` are direct method calls inside
    # the handler body. That's the whole point of the collapse.
    # ==================================================================

    async def _load_doc(self) -> dict:
        """Load the bot's lifecycle doc from Redis.

        Returns ``{"state": "OFF"}`` when the key is missing so callers
        can treat "never started" the same as "stopped cleanly".
        """
        redis = self.config.get("_redis")
        if redis is None:
            return {"state": BotState.OFF.value}
        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        doc = await store.get(bot_doc_key(self.bot_id))
        return doc or {"state": BotState.OFF.value}

    async def _save_doc(self, doc: dict) -> None:
        """Persist the lifecycle doc. Caller must have updated
        ``updated_at``; this is pure write-through to Redis.

        Also nudges the ``bots`` WS channel so the UI refreshes.
        """
        redis = self.config.get("_redis")
        if redis is None:
            return
        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        await store.set(bot_doc_key(self.bot_id), doc)
        try:
            from ib_trader.redis.streams import publish_activity
            await publish_activity(redis, "bots")
        except Exception as e:
            logger.debug("BOT_WS_NUDGE_FAILED", exc_info=e)

    def _log_transition(self, from_state: BotState, to_state: BotState,
                        trigger: str) -> None:
        """Emit the canonical ``FSM_TRANSITION`` audit line. Kept as
        ``FSM_TRANSITION`` for grep-compatibility with operator log
        scripts; the ``trigger`` field now carries a method name
        (``on_entry_filled``) rather than an ``EventType.value``.
        """
        logger.info(
            '{"event": "FSM_TRANSITION", "bot_id": "%s", '
            '"from": "%s", "to": "%s", "trigger": "%s"}',
            self.bot_id, from_state.value, to_state.value, trigger,
        )

    def _log_invalid(self, cur_state: BotState, trigger: str) -> None:
        logger.info(
            '{"event": "FSM_INVALID_TRANSITION", "bot_id": "%s", '
            '"state": "%s", "event_type": "%s"}',
            self.bot_id, cur_state.value, trigger,
        )

    async def _apply_transition(
        self, doc: dict, new_state: BotState, patch: dict, trigger: str,
    ) -> None:
        """Merge ``patch`` into ``doc``, set new state, save, emit audit.

        Caller is responsible for holding ``self._state_lock`` and for
        validating the from-state before calling this.
        """
        cur = BotState(doc.get("state", BotState.OFF.value))
        doc.update(patch)
        doc["state"] = new_state.value
        doc["updated_at"] = now_iso()
        await self._save_doc(doc)
        if cur != new_state:
            self._log_transition(cur, new_state, trigger)
        if self.ctx is not None:
            self.ctx.state = doc
            self.ctx.fsm_state = new_state

    async def current_state(self) -> BotState:
        """Read the current lifecycle state from Redis."""
        doc = await self._load_doc()
        try:
            return BotState(doc.get("state", BotState.OFF.value))
        except ValueError:
            logger.error(
                '{"event": "FSM_CORRUPT_STATE", "bot_id": "%s", "state": %r}',
                self.bot_id, doc.get("state"),
            )
            return BotState.OFF

    # ------------------------------------------------------------------
    # Lifecycle event methods (one per FSM handler)
    # ------------------------------------------------------------------

    async def on_start(self, *, symbol: str | None = None) -> BotState:
        """Transition OFF / ERRORED → AWAITING_ENTRY_TRIGGER.

        Caller (internal_api /bots/<id>/start) has already validated
        ``is_clean_for_start`` against the doc. This method does the
        actual transition and clears any lingering position or error
        fields as a safety net.
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur not in (BotState.OFF, BotState.ERRORED):
                self._log_invalid(cur, "on_start")
                return cur
            patch = {
                "error_reason": None,
                "error_message": None,
                **clear_position_fields(),
            }
            if symbol:
                patch["symbol"] = symbol
            await self._apply_transition(
                doc, BotState.AWAITING_ENTRY_TRIGGER, patch, "on_start",
            )
            return BotState.AWAITING_ENTRY_TRIGGER

    async def on_stop(self) -> BotState:
        """Transition any running state → OFF. Cancels any in-flight
        order via engine before clearing state. Safe to call from OFF
        (no-op).
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur == BotState.OFF:
                return BotState.OFF
            # Cancel in-flight order first — this used to be a FSM
            # SideEffect(cancel_order); now a direct engine HTTP call.
            if cur in (BotState.ENTRY_ORDER_PLACED, BotState.EXIT_ORDER_PLACED):
                symbol = doc.get("symbol")
                if symbol:
                    await self._handle_cancel_order({
                        "symbol": symbol,
                        "serial": doc.get("serial"),
                        "ib_order_id": doc.get("ib_order_id"),
                    })
            patch = {
                "error_reason": None,
                "error_message": None,
                **clear_position_fields(),
            }
            await self._apply_transition(doc, BotState.OFF, patch, "on_stop")
            return BotState.OFF

    async def on_force_stop(self, *, message: str | None = None) -> BotState:
        """Emergency stop — transition to ERRORED without attempting an
        engine cancel. Operator "panic button" semantics.
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur == BotState.OFF:
                # Already off; just note the reason.
                patch = {"error_reason": "force_stop",
                         "error_message": message or "Force-stopped"}
                await self._apply_transition(
                    doc, BotState.ERRORED, patch, "on_force_stop",
                )
                return BotState.ERRORED
            patch = {
                "error_reason": "force_stop",
                "error_message": message or "Force-stopped by operator",
            }
            await self._apply_transition(
                doc, BotState.ERRORED, patch, "on_force_stop",
            )
            return BotState.ERRORED

    async def on_crash(self, *, message: str | None = None) -> BotState:
        """Task or supervisor detected an unrecoverable failure.
        Transition to ERRORED and raise CATASTROPHIC pager alert.
        """
        msg = message or "Unhandled exception"
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur in (BotState.OFF, BotState.ERRORED):
                # Already terminal; don't re-fire alerts.
                self._log_invalid(cur, "on_crash")
                return cur
            patch = {
                "error_reason": "crash",
                "error_message": msg,
            }
            await self._apply_transition(
                doc, BotState.ERRORED, patch, "on_crash",
            )
            symbol = doc.get("symbol")
        # Pager alert runs outside the state lock — it issues its own
        # Redis writes and doesn't need doc consistency.
        try:
            await self._handle_pager_alert({
                "trigger": "BOT_CRASH",
                "severity": "CATASTROPHIC",
                "symbol": symbol,
                "message": f"Bot crashed: {msg}",
            })
        except Exception:
            logger.exception(
                '{"event": "PAGER_ALERT_FAILED", "bot_id": "%s"}', self.bot_id,
            )
        return BotState.ERRORED

    async def on_ib_position_mismatch(self, *, message: str | None = None) -> BotState:
        """IB position drifted from our tracking in a way the reconciler
        can't resolve. Transition to ERRORED.
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur in (BotState.OFF, BotState.ERRORED):
                self._log_invalid(cur, "on_ib_position_mismatch")
                return cur
            patch = {
                "error_reason": "ib_mismatch",
                "error_message": message or "Unresolvable IB position drift",
            }
            await self._apply_transition(
                doc, BotState.ERRORED, patch, "on_ib_position_mismatch",
            )
            return BotState.ERRORED

    async def on_entry_timeout(self) -> BotState:
        """Supervisor detected an entry order sitting in
        ENTRY_ORDER_PLACED past the timeout. Cancel at engine and
        revert to AWAITING_ENTRY_TRIGGER.
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur != BotState.ENTRY_ORDER_PLACED:
                self._log_invalid(cur, "on_entry_timeout")
                return cur
            symbol = doc.get("symbol")
            cancel_args = {
                "symbol": symbol,
                "serial": doc.get("serial"),
                "ib_order_id": doc.get("ib_order_id"),
            }
            patch = clear_position_fields()
            await self._apply_transition(
                doc, BotState.AWAITING_ENTRY_TRIGGER, patch, "on_entry_timeout",
            )
        # Cancel outside the lock (engine HTTP call).
        if symbol:
            try:
                await self._handle_cancel_order(cancel_args)
            except Exception:
                logger.exception(
                    '{"event": "ENTRY_TIMEOUT_CANCEL_FAILED", "bot_id": "%s"}',
                    self.bot_id,
                )
        return BotState.AWAITING_ENTRY_TRIGGER

    async def on_manual_close(self, *, message: str | None = None,
                              payload: dict | None = None) -> BotState:
        """IB positionEvent observed the user closing the position
        outside the bot. Transition to AWAITING_ENTRY_TRIGGER.
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur not in (BotState.AWAITING_EXIT_TRIGGER,
                           BotState.EXIT_ORDER_PLACED):
                self._log_invalid(cur, "on_manual_close")
                return cur
            patch = clear_position_fields()
            await self._apply_transition(
                doc, BotState.AWAITING_ENTRY_TRIGGER, patch, "on_manual_close",
            )
        # Audit record.
        try:
            self.log_event(
                "MANUAL_CLOSE",
                message=message or "IB qty dropped below tracked qty",
                payload=payload,
            )
        except Exception:
            logger.debug("on_manual_close audit log failed", exc_info=True)
        return BotState.AWAITING_ENTRY_TRIGGER

    async def on_entry_cancelled(self, *, reason: str | None = None) -> BotState:
        """Terminal cancel/reject of an entry order. Revert to
        AWAITING_ENTRY_TRIGGER; strategy gets notified of the rejection
        so it can adjust its internal state (cooldowns, etc.).
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur != BotState.ENTRY_ORDER_PLACED:
                self._log_invalid(cur, "on_entry_cancelled")
                return cur
            symbol = doc.get("symbol")
            patch = clear_position_fields()
            await self._apply_transition(
                doc, BotState.AWAITING_ENTRY_TRIGGER, patch, "on_entry_cancelled",
            )
        # Notify strategy of the rejection (was SideEffect emit_strategy_event).
        if self.strategy is not None and symbol:
            try:
                rejected = OrderRejected(
                    trade_serial=None, symbol=symbol,
                    reason=reason or "cancelled", command_id="",
                )
                actions = await self.strategy.on_event(rejected, self.ctx)
                if actions:
                    await self._run_pipeline(actions)
            except Exception:
                logger.exception(
                    '{"event": "ENTRY_CANCEL_STRATEGY_NOTIFY_FAILED", '
                    '"bot_id": "%s"}', self.bot_id,
                )
        return BotState.AWAITING_ENTRY_TRIGGER

    async def on_exit_cancelled(self) -> BotState:
        """Terminal cancel of an exit order. Revert to
        AWAITING_EXIT_TRIGGER — the position is still held, the
        strategy's next tick can try again.
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur != BotState.EXIT_ORDER_PLACED:
                self._log_invalid(cur, "on_exit_cancelled")
                return cur
            patch = {"order_qty": None, "filled_qty": "0"}
            await self._apply_transition(
                doc, BotState.AWAITING_EXIT_TRIGGER, patch, "on_exit_cancelled",
            )
            return BotState.AWAITING_EXIT_TRIGGER

    async def on_place_entry_order(
        self, *, symbol: str, qty, order_type: str = "mid",
        origin: str = "strategy", ib_order_id: str | None = None,
        serial: int | None = None,
    ) -> BotState:
        """Transition AWAITING_ENTRY_TRIGGER → ENTRY_ORDER_PLACED.

        Called by ``_run_pipeline`` synchronously *before* the engine
        HTTP call, so the new ENTRY_ORDER_PLACED state gates subsequent
        quote / bar ticks during the HTTP wait. Replaces the FSM's
        ``_h_place_entry_order``. ``ib_order_id`` is normally not known
        yet (the HTTP call hasn't returned); callers may pass the
        ``_PENDING_ORDER_ID`` sentinel and patch the real id after the
        response.
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur != BotState.AWAITING_ENTRY_TRIGGER:
                self._log_invalid(cur, "on_place_entry_order")
                return cur
            patch = {
                "order_qty": str(qty),
                "filled_qty": "0",
                "serial": serial if serial is not None else doc.get("serial"),
                "order_origin": origin,
                "symbol": symbol or doc.get("symbol"),
            }
            if ib_order_id is not None:
                patch["ib_order_id"] = ib_order_id
            await self._apply_transition(
                doc, BotState.ENTRY_ORDER_PLACED, patch, "on_place_entry_order",
            )
            return BotState.ENTRY_ORDER_PLACED

    async def on_place_exit_order(
        self, *, symbol: str, qty, order_type: str = "mid",
        origin: str = "strategy", ib_order_id: str | None = None,
    ) -> BotState:
        """Transition AWAITING_EXIT_TRIGGER → EXIT_ORDER_PLACED.

        Mirrors ``on_place_entry_order`` for the exit side. Resets
        ``exit_retries`` — a fresh exit cycle starts the retry count
        over; ``on_exit_filled`` bumps it on terminal partials.
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur != BotState.AWAITING_EXIT_TRIGGER:
                self._log_invalid(cur, "on_place_exit_order")
                return cur
            patch = {
                "order_qty": str(qty),
                "filled_qty": "0",
                "order_origin": origin,
                "exit_retries": 0,
            }
            if ib_order_id is not None:
                patch["ib_order_id"] = ib_order_id
            await self._apply_transition(
                doc, BotState.EXIT_ORDER_PLACED, patch, "on_place_exit_order",
            )
            return BotState.EXIT_ORDER_PLACED

    async def on_entry_filled(
        self, *, qty: Decimal, price: Decimal, terminal: bool,
        commission: Decimal = Decimal("0"), serial: int | None = None,
    ) -> BotState:
        """Apply an entry fill event from the engine order ledger.

        - ``terminal=False``: record running cumulative, no state change.
        - ``terminal=True`` with ``qty > 0``: activate position, transition
          to AWAITING_EXIT_TRIGGER, notify strategy.
        - ``terminal=True`` with ``qty == 0``: treat as cancelled (safety
          net — normal zero-fill path routes through ``on_entry_cancelled``).
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur not in (BotState.ENTRY_ORDER_PLACED,
                           BotState.AWAITING_ENTRY_TRIGGER):
                self._log_invalid(cur, "on_entry_filled")
                return cur
            symbol = doc.get("symbol")

            # Non-terminal progress event — update cumulative only.
            if not terminal:
                patch = {
                    "filled_qty": str(qty),
                    "qty": str(qty),
                    "avg_price": str(price),
                }
                doc.update(patch)
                doc["updated_at"] = now_iso()
                await self._save_doc(doc)
                if self.ctx is not None:
                    self.ctx.state = doc
                return cur

            # Terminal zero-fill — treat as a cancellation.
            if qty == 0:
                patch = clear_position_fields()
                await self._apply_transition(
                    doc, BotState.AWAITING_ENTRY_TRIGGER, patch, "on_entry_filled",
                )
                notify_rejected = True
            else:
                patch = {
                    "filled_qty": str(qty),
                    "qty": str(qty),
                    "avg_price": str(price),
                    "entry_price": str(price),
                    "entry_time": now_iso(),
                    "high_water_mark": str(price),
                    "current_stop": None,
                    "trail_activated": False,
                    "trail_reset_count": 0,          # reset for this trade
                    "entry_serial": doc.get("serial"),
                    # Snapshot the entry order's ib_order_id NOW — once the
                    # exit is placed, doc["ib_order_id"] is overwritten with
                    # the exit's id and the entry reference is lost. Needed
                    # at close time to resolve trade_serial from transactions
                    # so commission (which lands keyed on ib_order_id) can
                    # be matched back to the right bot_trade row.
                    "entry_ib_order_id": doc.get("ib_order_id"),
                }
                await self._apply_transition(
                    doc, BotState.AWAITING_EXIT_TRIGGER, patch, "on_entry_filled",
                )
                notify_rejected = False

        # Strategy notification runs outside the lock.
        if self.strategy is not None and symbol:
            try:
                if notify_rejected:
                    event = OrderRejected(
                        trade_serial=serial, symbol=symbol,
                        reason="terminal_zero_fill", command_id="",
                    )
                else:
                    event = OrderFilled(
                        trade_serial=serial, symbol=symbol, side="BUY",
                        fill_price=price, qty=qty, commission=commission,
                        ib_order_id=doc.get("ib_order_id") or "",
                    )
                actions = await self.strategy.on_event(event, self.ctx)
                if actions:
                    await self._run_pipeline(actions)
            except Exception:
                logger.exception(
                    '{"event": "ENTRY_FILL_STRATEGY_NOTIFY_FAILED", '
                    '"bot_id": "%s"}', self.bot_id,
                )
        return (BotState.AWAITING_ENTRY_TRIGGER if qty == 0
                else BotState.AWAITING_EXIT_TRIGGER)

    async def on_exit_filled(
        self, *, qty: Decimal, price: Decimal, terminal: bool,
        commission: Decimal = Decimal("0"), serial: int | None = None,
    ) -> BotState:
        """Apply an exit fill event.

        - ``terminal=False``: record cumulative, no state change.
        - ``terminal=True`` and position now flat: record closed trade,
          transition to AWAITING_ENTRY_TRIGGER.
        - ``terminal=True`` with residual: retry up to ``_MAX_EXIT_RETRIES``,
          escalate to ERRORED afterwards.

        Mirrors ``_h_exit_filled`` from the deleted FSM.
        """
        async with self._state_lock:
            doc = await self._load_doc()
            cur = BotState(doc.get("state", BotState.OFF.value))
            if cur != BotState.EXIT_ORDER_PLACED:
                self._log_invalid(cur, "on_exit_filled")
                return cur
            order_qty = Decimal(str(doc.get("order_qty") or "0"))
            position_qty = Decimal(str(doc.get("qty") or "0"))
            symbol = doc.get("symbol")

            if not terminal:
                doc["filled_qty"] = str(qty)
                doc["updated_at"] = now_iso()
                await self._save_doc(doc)
                if self.ctx is not None:
                    self.ctx.state = doc
                return cur

            new_position_qty = max(position_qty - qty, Decimal("0"))
            residual_on_order = max(order_qty - qty, Decimal("0"))

            # Fully flat — record the closed trade and stop the bot.
            #
            # Stop-on-exit policy (GH #85): after a successful round-
            # trip we transition to OFF and signal the loop to exit. The
            # operator manually restarts the bot via the UI before the
            # next entry. This avoids the "bot fires another 140
            # immediately after a 100-share stop-out" pattern observed
            # when a positionEvent race corrupts qty mid-cycle, and
            # gives an unattended operator a safe pause to review.
            #
            # FUTURE — see GH #86: replace the stop with a 2-minute
            # cooldown timer. The bot will transition to a new
            # COOLDOWN state on exit, set a wakeup timestamp, and auto-
            # re-arm to AWAITING_ENTRY_TRIGGER after ``cooldown_seconds``
            # (default 120). That removes the manual-restart friction
            # once we have confidence in the drift fix.
            if new_position_qty == 0:
                entry_price = Decimal(str(doc.get("entry_price") or "0"))
                realized_pnl = (
                    (price - entry_price) * position_qty
                    if entry_price > 0 else Decimal("0")
                )
                # Snapshot fields needed for the bot-trade record before
                # clear_position_fields() wipes them.
                record_close_args = {
                    "realized_pnl": str(realized_pnl),
                    "serial": doc.get("serial"),
                    "symbol": symbol,
                    "direction": "LONG",
                    "entry_price": str(entry_price),
                    "entry_qty": str(position_qty),
                    "entry_time": doc.get("entry_time"),
                    "exit_price": str(price),
                    "exit_qty": str(qty),
                    "commission": str(commission),
                    "trail_reset_count": int(doc.get("trail_reset_count") or 0),
                    "entry_serial": doc.get("entry_serial") or doc.get("serial"),
                    "exit_serial": doc.get("serial"),
                    "entry_ib_order_id": doc.get("entry_ib_order_id"),
                    "exit_ib_order_id": doc.get("ib_order_id"),
                }
                patch = {
                    **clear_position_fields(),
                    "last_realized_pnl": str(realized_pnl),
                    "exit_retries": 0,
                    "trail_reset_count": 0,  # Reset for next trade
                }
                await self._apply_transition(
                    doc, BotState.OFF, patch, "on_exit_filled_stop_on_exit",
                )
                # Signal run_event_loop to exit on its next iteration so
                # the runner task supervisor clears bot_instances; the
                # operator's next /start spawns a fresh task cleanly.
                self.request_stop()
                notify_kind = "filled"
                retry_args = None
                escalated = False
            else:
                # Residual still to sell.
                prior_retries = int(doc.get("exit_retries") or 0)
                if prior_retries >= _MAX_EXIT_RETRIES:
                    # Escalate.
                    patch = {
                        "qty": str(new_position_qty),
                        "error_reason": "exit_retries_exhausted",
                        "error_message": (
                            f"Exit failed after {_MAX_EXIT_RETRIES} retries — "
                            f"{new_position_qty} shares remain unsold on {symbol}"
                        ),
                    }
                    await self._apply_transition(
                        doc, BotState.ERRORED, patch, "on_exit_filled",
                    )
                    record_close_args = None
                    retry_args = None
                    escalated = True
                    notify_kind = None
                else:
                    patch = {
                        "qty": str(new_position_qty),
                        "order_qty": str(new_position_qty),
                        "filled_qty": "0",
                        "exit_retries": prior_retries + 1,
                    }
                    await self._apply_transition(
                        doc, BotState.EXIT_ORDER_PLACED, patch, "on_exit_filled",
                    )
                    record_close_args = None
                    retry_args = {
                        "symbol": symbol,
                        "qty": str(new_position_qty),
                        "attempt": prior_retries + 1,
                        "reason": (
                            f"terminal residual: {residual_on_order}/{order_qty} "
                            f"unsold on order, {new_position_qty} remain in position"
                        ),
                    }
                    escalated = False
                    notify_kind = "filled"

        # Side effects run outside the lock.
        if record_close_args is not None:
            try:
                await self._handle_record_trade_closed(record_close_args)
            except Exception:
                logger.exception(
                    '{"event": "RECORD_TRADE_CLOSED_FAILED", "bot_id": "%s"}',
                    self.bot_id,
                )
        if escalated:
            try:
                await self._handle_pager_alert({
                    "trigger": "BOT_EXIT_RETRIES_EXHAUSTED",
                    "severity": "CATASTROPHIC",
                    "symbol": symbol,
                    "message": (
                        f"Exit failed after {_MAX_EXIT_RETRIES} retries — "
                        f"{new_position_qty} shares of {symbol} still held. "
                        f"Manual intervention required."
                    ),
                    "residual_qty": str(new_position_qty),
                    "retries": _MAX_EXIT_RETRIES,
                })
            except Exception:
                logger.exception(
                    '{"event": "PAGER_ALERT_FAILED", "bot_id": "%s"}', self.bot_id,
                )
        if retry_args is not None:
            try:
                await self._handle_retry_exit_order(retry_args)
            except Exception:
                logger.exception(
                    '{"event": "RETRY_EXIT_ORDER_FAILED", "bot_id": "%s"}',
                    self.bot_id,
                )
        if notify_kind == "filled" and self.strategy is not None and symbol:
            try:
                event = OrderFilled(
                    trade_serial=serial, symbol=symbol, side="SELL",
                    fill_price=price, qty=qty, commission=commission,
                    ib_order_id=doc.get("ib_order_id") or "",
                )
                actions = await self.strategy.on_event(event, self.ctx)
                if actions:
                    await self._run_pipeline(actions)
            except Exception:
                logger.exception(
                    '{"event": "EXIT_FILL_STRATEGY_NOTIFY_FAILED", '
                    '"bot_id": "%s"}', self.bot_id,
                )

        if escalated:
            return BotState.ERRORED
        return (BotState.AWAITING_ENTRY_TRIGGER if record_close_args is not None
                else BotState.EXIT_ORDER_PLACED)

    async def _run_pipeline(self, actions: list, ctx=None) -> None:
        """Run actions through pipeline.

        Under the post-FSM-collapse architecture (ADR 016):

        1. For each ``PlaceOrder`` action, transition the bot's state
           SYNCHRONOUSLY before the pipeline runs (the pipeline's
           ExecutionMiddleware awaits the engine HTTP — a concurrent
           quote tick arriving during that await would otherwise re-run
           the strategy and emit a duplicate order). ``ENTRY_ORDER_PLACED``
           / ``EXIT_ORDER_PLACED`` IS the gate; no separate stoic flag.

        2. Run the pipeline — risk checks, logging, persistence,
           execution (HTTP to engine).

        3. The engine blocks its response until the order is terminal
           and publishes fill events on ``order:updates`` along the
           way. The bot's event-loop order-updates handler consumes
           those and calls ``on_entry_filled`` / ``on_exit_filled`` to
           drive the next transition.

        4. If the pipeline swallows the order (RiskMiddleware reject,
           ExecutionMiddleware error) the captured ``prior_states`` are
           restored so the bot can recover.
        """
        from ib_trader.bots.strategy import PlaceOrder
        place_orders = [a for a in actions if isinstance(a, PlaceOrder)]
        had_place_orders = bool(place_orders)

        # Circuit breaker: catch runaways from any cause. Rate-limit
        # trips before we add another order to a potential flood.
        if had_place_orders:
            await self._check_order_rate_limit()

        # Pre-flight state transitions — synchronous, one per place
        # action. We capture the state BEFORE calling the transition
        # (one read up front) so the revert path knows where to go
        # back to on failure. ``on_place_*_order`` returns the new
        # state, not the prior one.
        prior_states: list[BotState] = []
        for order in place_orders:
            pre_doc = await self._load_doc()
            try:
                pre_state = BotState(pre_doc.get("state", BotState.OFF.value))
            except ValueError:
                pre_state = BotState.OFF
            prior_states.append(pre_state)
            if order.side == "BUY":
                await self.on_place_entry_order(
                    symbol=order.symbol, qty=order.qty,
                    order_type=order.order_type,
                    origin=getattr(order, "origin", "strategy"),
                    ib_order_id=self._PENDING_ORDER_ID,
                    serial=self.ctx.state.get("trade_serial") if self.ctx else None,
                )
            else:
                await self.on_place_exit_order(
                    symbol=order.symbol, qty=order.qty,
                    order_type=order.order_type,
                    origin=getattr(order, "origin", "strategy"),
                    ib_order_id=self._PENDING_ORDER_ID,
                )
            self._recent_submit_times.append(time.monotonic())

        cmd_id: str | None = None
        try:
            await self.pipeline.process(actions, ctx or self.ctx)
            cmd_id = self.pipeline.last_cmd_id
            if cmd_id is not None:
                self._pending_cmd_id = cmd_id
                self.pipeline.last_cmd_id = None

            # Patch the real ib_order_id into the doc now that the engine
            # returned it. The stream handler needs this to match the
            # terminal event to our order.
            if place_orders and cmd_id is not None:
                async with self._state_lock:
                    doc = await self._load_doc()
                    doc["ib_order_id"] = str(cmd_id)
                    doc["awaiting_ib_order_id"] = str(cmd_id)
                    doc["updated_at"] = now_iso()
                    await self._save_doc(doc)
                logger.info(
                    '{"event": "BOT_ORDER_ID_CAPTURED", "bot_id": "%s", '
                    '"cmd_id": "%s", "side": "%s"}',
                    self.bot_id, cmd_id,
                    place_orders[0].side if place_orders else "?",
                )
        except Exception:
            # Pipeline failed — revert every pre-flight state transition
            # we made so the bot can retry.
            logger.exception(
                '{"event": "BOT_PIPELINE_FAILED", "bot_id": "%s"}', self.bot_id,
            )
            for prior in prior_states:
                if prior in (BotState.AWAITING_ENTRY_TRIGGER,
                             BotState.AWAITING_EXIT_TRIGGER):
                    try:
                        await self._revert_to_state(prior)
                    except Exception:
                        logger.exception(
                            '{"event": "BOT_REVERT_FAILED", "bot_id": "%s"}',
                            self.bot_id,
                        )
            raise
        else:
            # Pipeline succeeded but produced no cmd_id — middleware
            # dropped the order (e.g. RiskMiddleware rejected). Revert
            # state so the bot isn't stuck in ORDER_PLACED.
            if place_orders and cmd_id is None:
                for prior in prior_states:
                    if prior in (BotState.AWAITING_ENTRY_TRIGGER,
                                 BotState.AWAITING_EXIT_TRIGGER):
                        try:
                            await self._revert_to_state(prior)
                        except Exception:
                            logger.exception(
                                '{"event": "BOT_REVERT_FAILED", "bot_id": "%s"}',
                                self.bot_id,
                            )

    async def _revert_to_state(self, prior: BotState) -> None:
        """Roll the bot's state back to the pre-place-order target.
        Used on pipeline failures.
        """
        async with self._state_lock:
            doc = await self._load_doc()
            patch: dict = {
                "order_qty": None,
                "filled_qty": "0",
                "awaiting_ib_order_id": None,
            }
            if prior == BotState.AWAITING_ENTRY_TRIGGER:
                patch.update(clear_position_fields())
            await self._apply_transition(doc, prior, patch, "pipeline_revert")

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
        # this one is in flight. 260s covers the engine's worst case:
        # total_order_wait (120s) + cancel_settle_timeout_seconds (120s)
        # + buffer.
        import httpx
        try:
            async with httpx.AsyncClient(timeout=260) as client:
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

    def _resolve_trade_serial(self, ib_order_id) -> int | None:
        """Return ``trade_serial`` from the FILLED/PARTIAL_FILL
        transaction row for ``ib_order_id``, or None if we can't find
        one. Used at bot_trade creation time to link the row to the
        trade group so late commission reports can match.
        """
        if ib_order_id in (None, "", 0, "0"):
            return None
        try:
            order_id_int = int(ib_order_id)
        except (TypeError, ValueError):
            return None
        try:
            from ib_trader.data.models import (
                TransactionEvent, TransactionAction,
            )
            s = self._session_factory()
            ev = (
                s.query(TransactionEvent)
                .filter(
                    TransactionEvent.ib_order_id == order_id_int,
                    TransactionEvent.action.in_([
                        TransactionAction.FILLED,
                        TransactionAction.PARTIAL_FILL,
                    ]),
                )
                .first()
            )
            return ev.trade_serial if ev is not None else None
        except Exception:
            logger.debug(
                "resolve_trade_serial failed", exc_info=True,
            )
            return None

    async def _handle_record_trade_closed(self, args: dict) -> None:
        """Record realized P&L + trade count + bot-trade row when an exit
        fully fills.

        Driven by ``on_exit_filled`` which already computes
        ``realized_pnl = (price - entry_price) * order_qty`` before
        clearing the position fields.

        Three side effects:
        1. ``_risk_mw.record_pnl`` — daily loss-cap tracker (Redis).
        2. ``_risk_mw.record_trade`` — daily trade-count tracker (Redis).
        3. ``bot_trades`` SQLite row — one per entry-to-exit round-trip,
           read by the Bot Trades panel via ``GET /api/bot-trades``.
        """
        realized_pnl_str = args.get("realized_pnl") or "0"
        try:
            pnl = Decimal(realized_pnl_str)
        except (ValueError, TypeError):
            pnl = Decimal("0")

        if self._risk_mw:
            if pnl != 0:
                await self._risk_mw.record_pnl(pnl)
            await self._risk_mw.record_trade()

        # --- bot_trades row ---
        # Only write when we have the full round-trip context (symbol,
        # entry_price, exit_price). Earlier callers that pre-date the
        # expanded args schema pass just {realized_pnl, serial} — for
        # those we skip the row rather than write a degenerate entry.
        symbol = args.get("symbol")
        entry_price_str = args.get("entry_price")
        exit_price_str = args.get("exit_price")
        if not (symbol and entry_price_str and exit_price_str):
            return
        try:
            entry_time = args.get("entry_time")
            if isinstance(entry_time, str):
                try:
                    entry_time_dt = datetime.fromisoformat(entry_time)
                except ValueError:
                    entry_time_dt = None
            else:
                entry_time_dt = entry_time
            if entry_time_dt is None:
                entry_time_dt = datetime.now(timezone.utc)
            if entry_time_dt.tzinfo is None:
                entry_time_dt = entry_time_dt.replace(tzinfo=timezone.utc)
            exit_time_dt = datetime.now(timezone.utc)
            duration = int(
                (exit_time_dt - entry_time_dt).total_seconds()
            ) if entry_time_dt else None

            # Resolve entry/exit trade_serials by ib_order_id so late-
            # arriving commission callbacks (which key on ib_order_id →
            # trade_serial) can find this row and update it in place.
            # The bot's own "serial" state is effectively never populated
            # today, but transactions.trade_serial IS populated by the
            # engine's _handle_fill path — so look it up from there.
            entry_serial = self._resolve_trade_serial(
                args.get("entry_ib_order_id")
            ) or args.get("entry_serial")
            exit_serial = self._resolve_trade_serial(
                args.get("exit_ib_order_id")
            ) or args.get("exit_serial")

            from ib_trader.data.models import BotTrade
            row = BotTrade(
                bot_id=self.bot_id,
                bot_name=self.name if hasattr(self, "name") else None,
                symbol=symbol,
                direction=args.get("direction", "LONG"),
                entry_price=Decimal(entry_price_str),
                entry_qty=Decimal(args.get("entry_qty") or "0"),
                entry_time=entry_time_dt,
                exit_price=Decimal(exit_price_str),
                exit_qty=Decimal(args.get("exit_qty") or "0"),
                exit_time=exit_time_dt,
                realized_pnl=pnl,
                commission=Decimal(args.get("commission") or "0"),
                trail_reset_count=int(args.get("trail_reset_count") or 0),
                duration_seconds=duration,
                entry_serial=entry_serial,
                exit_serial=exit_serial,
                created_at=exit_time_dt,
            )
            self._bot_trades_repo.create(row)
            logger.info(
                '{"event": "BOT_TRADE_RECORDED", "bot_id": "%s", '
                '"symbol": "%s", "realized_pnl": "%s", "duration_s": %s, '
                '"trail_resets": %d}',
                self.bot_id, symbol, pnl, duration or 0,
                int(args.get("trail_reset_count") or 0),
            )

            # Stamp both trade_groups so the Trades panel (which reads
            # trade_groups.realized_pnl / status) no longer shows the
            # bot's round-trips as perpetually OPEN with null P&L.
            #   - EXIT trade_group gets the round-trip P&L (realized
            #     when the close leg fills — matches the engine's
            #     execute_close semantics).
            #   - ENTRY trade_group is marked CLOSED with P&L=0 so it
            #     doesn't linger as OPEN, and summing realized_pnl
            #     across CLOSED rows gives the round-trip total.
            # Use the already-resolved entry_serial / exit_serial
            # locals computed just above — args["exit_serial"] is
            # always None (bot doc never populates "serial").
            try:
                from ib_trader.data.repository import TradeRepository
                from ib_trader.data.models import TradeStatus
                trade_repo = TradeRepository(self._session_factory)
                if exit_serial is not None:
                    exit_tg = trade_repo.get_by_serial(int(exit_serial))
                    if exit_tg is not None:
                        trade_repo.update_pnl(
                            exit_tg.id, pnl,
                            Decimal(args.get("commission") or "0"),
                        )
                        trade_repo.update_status(exit_tg.id, TradeStatus.CLOSED)
                if entry_serial is not None:
                    entry_tg = trade_repo.get_by_serial(int(entry_serial))
                    if entry_tg is not None:
                        trade_repo.update_pnl(
                            entry_tg.id, Decimal("0"), Decimal("0"),
                        )
                        trade_repo.update_status(entry_tg.id, TradeStatus.CLOSED)
            except Exception:
                logger.exception(
                    '{"event": "BOT_TRADE_GROUP_UPDATE_FAILED", '
                    '"bot_id": "%s", "entry_serial": %s, "exit_serial": %s}',
                    self.bot_id, entry_serial, exit_serial,
                )
        except Exception:
            logger.exception(
                '{"event": "BOT_TRADE_WRITE_FAILED", "bot_id": "%s"}',
                self.bot_id,
            )

    async def on_startup(self, open_positions: list) -> None:
        """Initialize strategy, aggregator, middleware, and restore state."""
        # Create the strategy instance
        strategy_name = self.config.get("strategy_name", "sawtooth_rsi")
        self.strategy = _create_strategy(strategy_name, self.strategy_config)

        if self.strategy is None:
            raise ValueError(f"Unknown strategy: {strategy_name}")

        # Epic 1 D8 — validate the configured sec_type is declared on the
        # strategy's manifest. Legacy strategies (manifest leaves
        # ``supported_sec_types`` unset) accept STK/ETF silently; new
        # strategies that want FUT must opt in explicitly.
        cfg_sec_type = str(self.strategy_config.get("sec_type", "STK")).upper()
        if not self.strategy.manifest.permits_sec_type(cfg_sec_type):
            raise ValueError(
                f"bot {self.bot_id}: strategy {strategy_name!r} does not support "
                f"sec_type={cfg_sec_type}; declare it in the manifest's "
                f"supported_sec_types to enable."
            )

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
            if self._stop_requested:
                # Self-stop signalled (e.g., by stop-on-exit policy in
                # ``on_exit_filled`` per GH #85). Run teardown
                # (strategy.on_stop + unsubscribe bars) so we don't leak
                # bar subscriptions, then exit. The runner's task
                # supervisor (runner.py) clears bot_instances on done()
                # so a subsequent user-initiated /start spawns a fresh
                # task without an "already running" false positive.
                logger.info(
                    '{"event": "BOT_LOOP_EXIT_SELF_REQUEST", '
                    '"bot_id": "%s"}', self.bot_id,
                )
                try:
                    await self.on_teardown()
                except Exception:
                    logger.exception(
                        '{"event": "BOT_TEARDOWN_FAILED", "bot_id": "%s"}',
                        self.bot_id,
                    )
                break
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

    async def _check_order_rate_limit(self) -> None:
        """Hard-stop circuit breaker for runaway order emission.

        Belt-and-braces safety net: regardless of which upstream bug
        causes a bot to re-emit orders in a tight loop (Apr 19 stoic-
        race, later zero-qty-SELL storm, future unknowns), no bot
        should ever place more than ``bot_order_rate_limit_count``
        orders inside ``bot_order_rate_limit_window_seconds``. When
        tripped, this force-STOPs the bot to OFF, raises a
        CATASTROPHIC alert (blocking modal in the UI), and raises an
        exception so the pipeline aborts before submitting the order
        that would push us over.
        """
        cfg = self.config.get("_settings") if isinstance(self.config, dict) else None
        cfg = cfg or self.config or {}
        limit_count = int(cfg.get("bot_order_rate_limit_count", 5) or 5)
        window = float(cfg.get("bot_order_rate_limit_window_seconds", 2.0) or 2.0)

        now = time.monotonic()
        cutoff = now - window
        # Keep only timestamps inside the window.
        self._recent_submit_times = [
            t for t in self._recent_submit_times if t >= cutoff
        ]
        # +1 because we're about to add the current submission.
        if len(self._recent_submit_times) + 1 < limit_count:
            return

        # Trip. Force the bot OFF, alert loudly, raise so the caller
        # aborts the in-progress submission.
        logger.error(
            '{"event": "BOT_ORDER_RATE_LIMIT_EXCEEDED", "bot_id": "%s", '
            '"count": %d, "window_s": %.2f}',
            self.bot_id, len(self._recent_submit_times) + 1, window,
        )
        try:
            await self.on_stop()
        except Exception:
            logger.exception(
                '{"event": "RATE_LIMIT_FORCE_STOP_FAILED", "bot_id": "%s"}',
                self.bot_id,
            )

        try:
            from ib_trader.logging_.alerts import log_and_alert
            await log_and_alert(
                redis=self.config.get("_redis"),
                trigger="BOT_ORDER_RATE_LIMIT_EXCEEDED",
                severity="CATASTROPHIC",
                message=(
                    f"Bot emitted {len(self._recent_submit_times) + 1} orders "
                    f"within {window:.1f}s — force-stopped to OFF. Investigate "
                    f"before re-enabling."
                ),
                bot_id=self.bot_id,
                symbol=self.strategy_config.get("symbol"),
                extra={
                    "count": len(self._recent_submit_times) + 1,
                    "window_s": window,
                    "limit_count": limit_count,
                },
                exc_info=False,
            )
        except Exception:
            logger.exception(
                '{"event": "RATE_LIMIT_ALERT_FAILED", "bot_id": "%s"}',
                self.bot_id,
            )

        # Clear the ring now that the bot is OFF — if anything re-
        # enables it, it starts fresh.
        self._recent_submit_times.clear()
        raise RuntimeError(
            f"bot {self.bot_id} rate-limit exceeded "
            f"({limit_count} orders in {window:.1f}s)"
        )

    async def _dispatch_event(self, stream_name: str, raw_data: dict,
                               quote_stream: str, bar_stream: str,
                               order_stream: str, pos_stream: str,
                               symbol: str, bot_ref: str,
                               order_ref_prefix: str = "") -> None:
        """Route a single Redis stream entry to the strategy."""
        import json as _json

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
            # Stale-quote liveness: update BEFORE the state gate so the
            # timestamp stays fresh during AWAITING_ENTRY_TRIGGER / *_ORDER_PLACED
            # too. Otherwise a long wait for the next entry trigger leaves
            # _last_quote_time frozen, and the moment the bot transitions
            # into AWAITING_EXIT_TRIGGER the watchdog reads an ancient
            # value and false-positives as BOT_CRASH. Liveness is an
            # observation of the quote stream, independent of whether
            # the strategy chooses to act on it.
            self._last_quote_time = time.monotonic()
            self._quote_stale_logged = False

            # State gate: strategy only runs in AWAITING_EXIT_TRIGGER.
            # ENTRY_ORDER_PLACED / EXIT_ORDER_PLACED reach here too while
            # an order is in flight — the state check alone is sufficient
            # to prevent duplicate orders (no stoic-mode flag needed).
            if self.ctx.fsm_state != BotState.AWAITING_EXIT_TRIGGER:
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
            actions = await self.strategy.on_event(quote, self.ctx)
            if actions:
                await self._run_pipeline(actions)
            return

        # ── Bar completion (5s raw bar from IB) ────────────────────────
        if stream_name == bar_stream:
            if not self.aggregator:
                return
            # State gate: strategy only evaluates bars when it's not
            # already holding an order in flight. ENTRY_ORDER_PLACED /
            # EXIT_ORDER_PLACED states mean a bar-driven strategy eval
            # would attempt to emit a duplicate.
            if self.ctx.fsm_state in (BotState.ENTRY_ORDER_PLACED,
                                      BotState.EXIT_ORDER_PLACED):
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
            event_ib_order_id = str(data.get("ib_order_id") or "")

            # Dispatch via the bot's own lifecycle methods (no more FSM).
            # Under ADR 016, state itself is the gate — no stoic flag to
            # release. The state transitions happen inside on_entry_filled
            # / on_exit_filled / on_entry_cancelled / on_exit_cancelled
            # below.
            if status in ("Filled", "PartiallyFilled", "PartialFillCancelled"):
                # Audit row first — universal FILL signal for the UI.
                self.log_event(
                    "FILL",
                    message=(
                        f"{side} {filled_qty_str} {symbol} @ {avg_price_str or '0'}"
                        + (" (partial)" if not terminal else "")
                    ),
                    payload={
                        "ib_order_id": event_ib_order_id,
                        "qty": filled_qty_str,
                        "price": avg_price_str or "0",
                        "commission": data.get("total_commission", "0"),
                        "terminal": terminal,
                    },
                )
                qty_dec = Decimal(filled_qty_str)
                price_dec = Decimal(avg_price_str or "0")
                comm_dec = Decimal(data.get("total_commission", "0"))
                try:
                    if side == "BUY":
                        await self.on_entry_filled(
                            qty=qty_dec, price=price_dec, terminal=terminal,
                            commission=comm_dec,
                        )
                    else:
                        await self.on_exit_filled(
                            qty=qty_dec, price=price_dec, terminal=terminal,
                            commission=comm_dec,
                        )
                except Exception:
                    logger.exception(
                        '{"event": "FILL_HANDLER_FAILED", "bot_id": "%s"}',
                        self.bot_id,
                    )
                # On terminal fills, update strat:* key for UI consumers.
                if terminal:
                    await self._apply_fill(
                        bot_ref=bot_ref, symbol=symbol,
                        side=side[0] if side else "",
                        qty=qty_dec, price=price_dec, commission=comm_dec,
                        ib_order_id=data.get("ib_order_id", ""),
                    )

            elif status in ("Submitted", "PreSubmitted") and not terminal:
                # Dedup ORDER audit rows per ib_order_id — IB fires multiple
                # Submitted updates as it routes the order through venues.
                if event_ib_order_id and event_ib_order_id not in self._submitted_logged:
                    self._submitted_logged.add(event_ib_order_id)
                    self.log_event(
                        "ORDER",
                        message=f"{side or '?'} {symbol} submitted (#{event_ib_order_id})",
                        payload={
                            "ib_order_id": event_ib_order_id,
                            "status": status,
                            "side": side,
                        },
                    )

            elif terminal and status in ("Cancelled", "Rejected"):
                self.log_event(
                    "CANCELLED",
                    message=f"{side or '?'} {symbol} {status.lower()} (#{event_ib_order_id})",
                    payload={
                        "ib_order_id": event_ib_order_id,
                        "status": status,
                        "filled_qty": filled_qty_str,
                    },
                )
                # Dispatch via bot's own cancel handler (replaces FSM
                # ENTRY_CANCELLED / EXIT_CANCELLED dispatch).
                cur_state = await self.current_state()
                try:
                    if cur_state == BotState.ENTRY_ORDER_PLACED:
                        await self.on_entry_cancelled(reason=status)
                    elif cur_state == BotState.EXIT_ORDER_PLACED:
                        await self.on_exit_cancelled()
                except Exception:
                    logger.exception(
                        '{"event": "CANCEL_HANDLER_FAILED", "bot_id": "%s"}',
                        self.bot_id,
                    )
                await self._apply_cancel(bot_ref=bot_ref, symbol=symbol)

            # Non-terminal, non-fill statuses — log only.
            return

        # ── Position change (external manipulation) ────────────────────
        if stream_name == pos_stream:
            # State gate: skip during an in-flight order. A manual-close
            # detector running mid-submit could emit a second,
            # conflicting order. Next position event will be re-evaluated
            # from the Redis positionEvent cache after the order settles.
            if self.ctx.fsm_state in (BotState.ENTRY_ORDER_PLACED,
                                      BotState.EXIT_ORDER_PLACED):
                return
            if data.get("symbol") != symbol:
                return
            # Epic 1 D8 — drop events whose sec_type isn't supported by
            # the strategy's manifest, and log a WARNING (previously the
            # post-filter silently dropped the event, making misconfigured
            # bots invisible).
            evt_sec_type = str(data.get("sec_type", "STK")).upper()
            if not self.strategy.manifest.permits_sec_type(evt_sec_type):
                logger.warning(
                    '{"event": "BOT_EVENT_SECTYPE_DROPPED", "bot_id": "%s", '
                    '"symbol": "%s", "event_sec_type": "%s", "strategy": "%s"}',
                    self.bot_id, symbol, evt_sec_type, self.strategy.manifest.name,
                )
                return
            # Strategy-config sec_type gate — legacy behaviour for strategies
            # that still use strategy_config["sec_type"] to target a single
            # type within a manifest's supported set.
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
            try:
                await self.on_entry_timeout()
            except Exception:
                logger.exception(
                    '{"event": "ENTRY_TIMEOUT_HANDLER_FAILED", "bot_id": "%s"}',
                    self.bot_id,
                )
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

    async def _read_heartbeat_age(self) -> tuple[bool, float | None, dict]:
        """Return (present, age_seconds, raw_payload) for the quote-stream
        heartbeat key. Age is None if the key is absent or the timestamp
        can't be parsed. This is NOT the engine process heartbeat.
        """
        redis = self.config.get("_redis")
        if redis is None:
            return False, None, {"redis": "unavailable"}
        try:
            from ib_trader.redis.state import StateStore, StateKeys
            hb = await StateStore(redis).get(StateKeys.quotes_heartbeat())
        except Exception as e:
            return False, None, {"redis_get_error": str(e)}
        if not hb:
            return False, None, {}
        ts_str = hb.get("ts")
        if not ts_str:
            return True, None, hb
        try:
            from datetime import datetime, timezone
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = round((datetime.now(timezone.utc) - ts).total_seconds(), 1)
            return True, age, hb
        except Exception as e:
            return True, None, {**hb, "ts_parse_error": str(e)}

    async def _probe_quote_source(self, symbol: str) -> dict:
        """Gather diagnostics about the quote source — the bot has no
        direct IB handle, so we check the state store (what the engine's
        tick publisher last wrote) and note both the per-symbol quote
        and the engine-wide heartbeat. Pure read; safe to call from the
        stale-quote path without risk of side effects.
        """
        info: dict = {"symbol": symbol, "redis_quote_key_present": False}
        redis = self.config.get("_redis")
        if redis is None:
            info["redis"] = "unavailable"
            return info
        try:
            from ib_trader.redis.state import StateStore, StateKeys
            q = await StateStore(redis).get(StateKeys.quote_latest(symbol))
        except Exception as e:
            info["redis_get_error"] = str(e)
            return info
        if q:
            info["redis_quote_key_present"] = True
            info["redis_bid"] = q.get("bid")
            info["redis_ask"] = q.get("ask")
            info["redis_last"] = q.get("last")
            info["redis_quote_ts"] = q.get("ts")
            try:
                from datetime import datetime, timezone
                ts_str = q.get("ts")
                if ts_str:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    info["redis_quote_age_s"] = round(
                        (datetime.now(timezone.utc) - ts).total_seconds(), 1,
                    )
            except Exception as e:
                info["redis_ts_parse_error"] = str(e)

        # Engine-wide market-data heartbeat — the authoritative halt signal.
        hb_present, hb_age, hb_raw = await self._read_heartbeat_age()
        info["heartbeat_present"] = hb_present
        if hb_age is not None:
            info["heartbeat_age_s"] = hb_age
        if hb_raw.get("symbol"):
            info["heartbeat_last_symbol"] = hb_raw["symbol"]
        return info

    async def check_stale_quote(self) -> None:
        """Supervisory check: warn on a quiet symbol, halt only when the
        engine-wide market-data heartbeat goes stale.

        Rationale: individual symbols (especially inverse ETFs like PSQ)
        can genuinely go 120 s+ between IB ticks during quiet periods
        without any outage. A per-symbol halt false-positives the bot
        into ERRORED in those windows. Engine-level liveness — "is ANY
        symbol ticking right now?" — is the real "something broke"
        signal.
        """
        if not self.strategy or not self.ctx:
            return
        await self._refresh_state()
        if self.ctx.fsm_state != BotState.AWAITING_EXIT_TRIGGER:
            return  # Stale quotes only matter when monitoring exits

        per_symbol_elapsed = time.monotonic() - self._last_quote_time
        symbol = self.strategy_config.get("symbol", "")
        halt_threshold = float(
            self.config.get(
                "quotes_heartbeat_stale_halt_seconds",
                _DEFAULT_QUOTES_HEARTBEAT_HALT_SECONDS,
            )
        )

        # Halt decision is driven by the engine-wide heartbeat key,
        # not per-symbol freshness.
        hb_present, hb_age, _ = await self._read_heartbeat_age()
        heartbeat_stale = (
            not hb_present or hb_age is None or hb_age > halt_threshold
        )

        if heartbeat_stale and not self._quote_stale_logged:
            self._quote_stale_logged = True
            probe = await self._probe_quote_source(symbol)
            effective_elapsed = (
                hb_age if (hb_present and hb_age is not None) else per_symbol_elapsed
            )
            logger.error(
                '{"event": "STALE_QUOTE_STREAM_HALT_DIAG", "bot_id": "%s", '
                '"symbol": "%s", "elapsed_s": %.1f, '
                '"heartbeat_present": %s, "halt_threshold_s": %.1f, '
                '"probe": %s}',
                self.bot_id, symbol, effective_elapsed,
                "true" if hb_present else "false",
                halt_threshold, json.dumps(probe),
            )
            actions = [LogSignal(
                event_type=LogEventType.ERROR,
                message=(
                    f"Quote-stream heartbeat stale ({effective_elapsed:.0f}s) "
                    f"— halting bot"
                ),
                payload={
                    "heartbeat_age_s": hb_age,
                    "heartbeat_present": hb_present,
                    "probe": probe,
                },
            )]
            await self._run_pipeline(actions)
            try:
                await self.on_crash(message="STALE_QUOTE_STREAM")
            except Exception:
                logger.exception(
                    '{"event": "STALE_QUOTE_CRASH_DISPATCH_FAILED", "bot_id": "%s"}',
                    self.bot_id,
                )
        elif (
            per_symbol_elapsed > _STALE_QUOTE_WARN_SECONDS
            and not self._quote_stale_logged
        ):
            # Warn-level probe for a quiet individual symbol — ONCE per
            # stale episode (guarded by _quote_stale_logged, reset on
            # fresh ticks). Informational, not a halt: QQQ may be
            # ticking fine while PSQ is silent for several minutes on a
            # slow day.
            probe = await self._probe_quote_source(symbol)
            logger.warning(
                '{"event": "STALE_QUOTES", "bot_id": "%s", "symbol": "%s", '
                '"no_fresh_s": %.1f, "probe": %s}',
                self.bot_id, symbol, per_symbol_elapsed, json.dumps(probe),
            )

    async def on_teardown(self) -> None:
        """Cleanup hook — called by the runner after on_stop() and task
        cancellation. Runs strategy.on_stop(ctx) and unsubscribes the
        engine-side bar publisher for this bot's symbol. Separate from
        the state-transition ``on_stop()`` above so the HTTP endpoint
        doesn't have to wait on engine HTTP during the state flip.
        """
        if self.strategy and self.ctx:
            try:
                actions = await self.strategy.on_stop(self.ctx)
                if actions and self.pipeline:
                    await self._run_pipeline(actions)
            except Exception:
                logger.exception(
                    '{"event": "STRATEGY_STOP_FAILED", "bot_id": "%s"}',
                    self.bot_id,
                )

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

    async def force_sell(self) -> dict:
        """Execute a force-sell immediately. Called directly by the runner
        HTTP API — no polling, no Redis key, no control stream.

        Returns a result dict with order details.
        """
        if not self.strategy or not self.ctx:
            raise RuntimeError("Bot not initialized")
        await self._refresh_state()
        symbol = self.strategy_config["symbol"]
        await self._execute_force_sell(symbol)
        return {"symbol": symbol, "action": "FORCE_SELL"}

    async def _execute_force_sell(self, symbol: str) -> None:
        """Execute a forced sell, bypassing all exit conditions.

        Delegates action construction to ``strategy.build_exit_actions`` so
        the order type, qty, and log payload are bit-identical to an organic
        strategy-driven exit — only the ExitType differs.
        """
        qty_raw = str(self.ctx.state.get("qty", "0") or "0")
        try:
            qty = Decimal(qty_raw)
        except (InvalidOperation, ValueError) as e:
            raise RuntimeError(
                f"Cannot force-sell {symbol}: invalid position qty {qty_raw!r}"
            ) from e
        if qty <= 0:
            raise RuntimeError(
                f"Cannot force-sell {symbol}: no open position (qty={qty_raw})"
            )

        actions = self.strategy.build_exit_actions(
            self.ctx, ExitType.FORCE_EXIT, "manual override",
        )
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
