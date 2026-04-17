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
    _RedisConnectionError = type(None)  # no-op if redis not installed
from decimal import Decimal
from pathlib import Path

import yaml
from sqlalchemy.orm import scoped_session

from ib_trader.bots.base import BotBase
from ib_trader.bots.strategy import (
    Strategy, StrategyContext, PositionState,
    BarCompleted, QuoteUpdate, OrderFilled, OrderRejected,
    Action, PlaceOrder, LogSignal, UpdateState,
)
from ib_trader.bots.bar_aggregator import BarAggregator, load_state_from_file
from ib_trader.bots.middleware import (
    MiddlewarePipeline, RiskMiddleware, LoggingMiddleware,
    PersistenceMiddleware, ExecutionMiddleware, ManualEntryMiddleware,
)
from ib_trader.data.models import (
    BotEvent, TransactionAction, LegType,
)
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.data.repositories.bot_repository import BotRepository, BotEventRepository
from ib_trader.data.repository import TradeRepository
from ib_trader.data.repositories.transaction_repository import TransactionRepository

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

        # Market data state
        self._last_bar_ts: datetime | None = None
        self._last_quote_time: float = time.monotonic()  # init to now, not 0
        self._quote_stale_logged: bool = False

        # Repos for middleware
        self._bot_events_repo = BotEventRepository(session_factory)
        self._session_factory = session_factory

    async def _apply_fill(self, *, bot_ref: str, symbol: str, side: str,
                          qty: Decimal, price: Decimal, commission: Decimal,
                          serial: int, ib_order_id: str) -> None:
        """Accumulate a tagged fill into the bot's unified state key and
        dispatch OrderFilled to the strategy.

        Engine publishes each partial fill to the fill stream. Bot is the
        sole writer of strat:<bot_ref>:<symbol>. Qty accumulates within the
        same trade serial; a new serial resets.
        """
        redis = self.config.get("_redis")
        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        key = f"bot:{self.bot_id}"
        existing = await store.get(key) or {}

        existing_qty = Decimal(existing.get("qty", "0"))
        same_trade = (existing.get("serial") == serial)
        now_iso = datetime.now(timezone.utc).isoformat()

        if side == "B":
            base_qty = existing_qty if same_trade else Decimal("0")
            new_qty = base_qty + qty
            new_state = "OPEN"
            entry_time = existing.get("entry_time") if same_trade else now_iso
            fill_event = OrderFilled(
                trade_serial=serial, symbol=symbol, side="BUY",
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
            engine_fields = {
                "state": new_state,
                "position_state": new_state,
                "qty": str(new_qty),
                "avg_price": str(price),
                "serial": serial,
                "entry_price": str(price),
                "entry_time": entry_time,
                "symbol": self.strategy_config.get("symbol", ""),
                "high_water_mark": str(price),
                "current_stop": str(hard_stop.quantize(Decimal("0.01"))),
                "hard_stop": str(hard_stop.quantize(Decimal("0.01"))),
                "trail_activation_price": str(trail_activation_price.quantize(Decimal("0.01"))),
                "trail_width_pct": str(trail_width),
                "trail_activated": False,
            }
        else:
            base_qty = existing_qty if same_trade else qty
            new_qty = base_qty - qty if same_trade else Decimal("0")
            if new_qty <= 0:
                new_state = "FLAT"
                new_qty = Decimal("0")
            else:
                new_state = "EXITING"
            fill_event = OrderFilled(
                trade_serial=serial, symbol=symbol, side="SELL",
                fill_price=price, qty=qty, commission=commission,
                ib_order_id=ib_order_id,
            )
            engine_fields = {
                "state": new_state,
                "position_state": new_state,
                "qty": str(new_qty),
                "avg_price": str(price),
                "serial": serial,
                "entry_price": existing.get("entry_price"),
                "entry_time": existing.get("entry_time"),
            }
        engine_fields["updated_at"] = now_iso
        await store.set(key, {**existing, **engine_fields})

        # Strategy tick — trail/exit bookkeeping runs inside on_event.
        actions = await self.strategy.on_event(fill_event, self.ctx)
        if actions:
            await self._run_pipeline(actions)

        # Record P&L + trade count on full close.
        if side != "B" and new_state == "FLAT":
            entry_price_str = existing.get("entry_price")
            if entry_price_str:
                entry_price = Decimal(str(entry_price_str))
                if entry_price > 0 and price > 0:
                    pnl = (price - entry_price) * (base_qty if same_trade else qty)
                    self._risk_mw.record_pnl(pnl)
            self._risk_mw.record_trade()

        # NOTE: FSM dispatch for fills is done by the caller in
        # _dispatch_event (the order:updates stream handler). Do NOT
        # dispatch here — that would double-count the qty.

    async def _apply_cancel(self, *, bot_ref: str, symbol: str) -> None:
        """Revert ENTERING → FLAT when an order is cancelled/rejected."""
        redis = self.config.get("_redis")
        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        key = f"bot:{self.bot_id}"
        existing = await store.get(key) or {}
        cur = existing.get("state") or existing.get("position_state")
        if cur not in ("ENTERING", "EXITING"):
            return
        new_state = "FLAT" if cur == "ENTERING" else "OPEN"
        existing["state"] = new_state
        existing["position_state"] = new_state
        existing["updated_at"] = datetime.now(timezone.utc).isoformat()
        await store.set(key, existing)

        # NOTE: FSM dispatch for cancels is done by the caller in
        # _dispatch_event. Do NOT dispatch here.

    async def _apply_reconciled(self, *, bot_ref: str, symbol: str,
                                 new_state: str, reason: str,
                                 qty: Decimal | None, avg_price: Decimal | None) -> None:
        """Apply a hint from the reconciler — rare path, observability only."""
        redis = self.config.get("_redis")
        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        key = f"bot:{self.bot_id}"
        existing = await store.get(key) or {}
        existing["state"] = new_state
        existing["position_state"] = new_state
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
        cur_state = existing.get("state") or existing.get("position_state", "FLAT")
        if cur_state == "FLAT":
            # Nothing to reconcile — bot has no tracked position.
            return

        expected = Decimal(existing.get("qty", "0"))
        actual = abs(ib_qty)   # bot tracks absolute qty; long/short is implicit

        if actual >= expected:
            # IB has at least as much as we expect — no manual reduction.
            # (actual > expected means manual add; bot doesn't claim those.)
            return

        reduction = expected - actual
        now_iso = datetime.now(timezone.utc).isoformat()
        if actual == 0:
            new_state = "FLAT"
        else:
            new_state = cur_state  # partial close — stay in OPEN/EXITING
        existing["state"] = new_state
        existing["position_state"] = new_state
        existing["qty"] = str(actual)
        existing["updated_at"] = now_iso
        await store.set(key, existing)

        # Bolt-on FSM dispatch — if actual==0, MANUAL_CLOSE takes us
        # back to AWAITING_ENTRY_TRIGGER in the FSM.
        if actual == 0:
            from ib_trader.bots.fsm import FSM, BotEvent, EventType
            try:
                await FSM(self.bot_id, redis).dispatch(BotEvent(
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

        self.log_event(
            "MANUAL_CLOSE",
            message=f"User manually reduced {symbol} by {reduction} "
                    f"(bot had {expected}, IB has {actual})",
            payload={
                "expected_qty": str(expected),
                "actual_qty": str(actual),
                "reduction": str(reduction),
                "new_state": new_state,
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
            await self._dispatch_place_order_fsm(place_orders)
            logger.info(
                '{"event": "FSM_PLACE_ORDER_DISPATCHED", "bot_id": "%s", '
                '"cmd_id": "%s", "side": "%s"}',
                self.bot_id, cmd_id,
                place_orders[0].side if place_orders else "?",
            )

    async def _dispatch_place_order_fsm(self, place_orders) -> None:
        """Emit PlaceEntryOrder / PlaceExitOrder FSM events for orders
        that committed through the pipeline."""
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
            }
            try:
                await fsm.dispatch(BotEvent(event_type, payload=payload))
            except Exception:
                logger.exception(
                    '{"event": "FSM_DISPATCH_PLACE_ORDER_FAILED", "bot_id": "%s"}',
                    self.bot_id,
                )

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
            state = {"position_state": "FLAT"}
        state = _reconcile_state(state, open_positions, symbol, self.bot_id)

        self.ctx = StrategyContext(
            state=state,
            position_state=PositionState(state.get("position_state", "FLAT")),
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
            self._bots, self._trades,
            state_store=state_store,
        )
        logging_mw = LoggingMiddleware(self.bot_id, self._bot_events_repo, redis=redis)
        persistence_mw = PersistenceMiddleware(
            self.bot_id, self.strategy_config["symbol"], STATE_DIR,
            redis=redis, bot_ref=bot_ref,
        )
        execution_mw = ExecutionMiddleware(
            self.bot_id, self._pending_commands,
            engine_url=engine_url, bot_ref=bot_ref,
        )
        self._execution_mw = execution_mw

        # ManualEntryMiddleware runs FIRST so blocked entries never
        # count against risk limits and the audit log sees the drop.
        self.pipeline = MiddlewarePipeline([
            manual_entry_mw, risk_mw, logging_mw, persistence_mw, execution_mw,
        ])
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
                     '"strategy": "%s", "symbol": "%s", "position": "%s"}',
                     self.bot_id, self.strategy.manifest.name, symbol,
                     self.ctx.state.get("position_state", "FLAT"))

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
            except (ConnectionError, OSError, _RedisConnectionError):
                raise asyncio.CancelledError()
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

    async def _dispatch_event(self, stream_name: str, raw_data: dict,
                               quote_stream: str, bar_stream: str,
                               order_stream: str, pos_stream: str,
                               symbol: str, bot_ref: str,
                               order_ref_prefix: str = "") -> None:
        """Route a single Redis stream entry to the strategy."""
        import json as _json

        # Deserialize JSON-encoded values
        data = {}
        for k, v in raw_data.items():
            try:
                data[k] = _json.loads(v)
            except (ValueError, TypeError):
                data[k] = v

        # ── Quote tick ─────────────────────────────────────────────────
        if stream_name == quote_stream:
            pos = PositionState(self.ctx.state.get("position_state", "FLAT"))
            if pos != PositionState.OPEN:
                return  # Quotes only matter for exit monitoring

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

            # Always dispatch to FSM for doc update (both progress + terminal)
            from ib_trader.bots.fsm import FSM, BotEvent, EventType
            fsm = FSM(self.bot_id, self.config.get("_redis"))

            if status in ("Filled", "PartiallyFilled", "PartialFillCancelled"):
                # Fill event — progress or terminal
                fsm_event = EventType.ENTRY_FILLED if side == "BUY" else EventType.EXIT_FILLED
                await fsm.dispatch(BotEvent(fsm_event, payload={
                    "qty": filled_qty_str,
                    "price": avg_price_str or "0",
                    "commission": data.get("total_commission", "0"),
                    "terminal": terminal,
                    "last_fill_qty": last_fill_qty_str,
                    "last_fill_price": last_fill_price_str,
                }))

                # On terminal fills, also update strat:* key + dispatch to
                # the strategy for trail init / P&L bookkeeping
                if terminal:
                    await self._apply_fill(
                        bot_ref=bot_ref,
                        symbol=symbol,
                        side=side[0] if side else "",  # "B" or "S"
                        qty=Decimal(filled_qty_str),
                        price=Decimal(avg_price_str or "0"),
                        commission=Decimal(data.get("total_commission", "0")),
                        serial=0,
                        ib_order_id=data.get("ib_order_id", ""),
                    )

            elif terminal and status in ("Cancelled", "Rejected"):
                # Terminal cancel/reject — revert state
                pos = PositionState(self.ctx.state.get("position_state", "FLAT"))
                fsm_event = EventType.ENTRY_CANCELLED if pos == PositionState.ENTERING else EventType.EXIT_CANCELLED
                await fsm.dispatch(BotEvent(fsm_event, payload={
                    "reason": status,
                    "filled_qty": filled_qty_str,
                }))
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
        """Supervisory check: cancel entry if ENTERING > entry_timeout_seconds.

        Called periodically by the runner's supervisory task — not driven
        by market events because timeout is purely time-based.
        """
        if not self.strategy or not self.ctx:
            return
        pos = PositionState(self.ctx.state.get("position_state", "FLAT"))
        if pos != PositionState.ENTERING:
            return

        timeout = self.strategy_config.get("exit", {}).get("entry_timeout_seconds", 30)
        entry_time_str = self.ctx.state.get("entry_time")
        if not entry_time_str:
            return

        entry_time = _parse_aware_dt(entry_time_str)
        elapsed = (datetime.now(timezone.utc) - entry_time).total_seconds()
        if elapsed > timeout:
            actions = [
                LogSignal(
                    event_type="ORDER",
                    message=f"Entry timeout after {elapsed:.0f}s — cancelling, returning to FLAT",
                ),
                UpdateState({
                    "position_state": PositionState.FLAT.value,
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
        pos = PositionState(self.ctx.state.get("position_state", "FLAT"))

        if pos == PositionState.FLAT:
            await self._execute_force_buy(symbol)
        else:
            actions = [LogSignal(
                event_type="RISK",
                message=f"FORCE_BUY ignored — position state is {pos.value}, not FLAT",
            )]
            await self._run_pipeline(actions)

    async def check_stale_quote(self) -> None:
        """Supervisory check: warn / halt if no quote arrives for too long."""
        if not self.strategy or not self.ctx:
            return
        pos = PositionState(self.ctx.state.get("position_state", "FLAT"))
        if pos != PositionState.OPEN:
            return  # Stale quotes only matter when monitoring exits

        elapsed = time.monotonic() - self._last_quote_time
        if elapsed > _STALE_QUOTE_HALT_SECONDS and not self._quote_stale_logged:
            self._quote_stale_logged = True
            actions = [LogSignal(
                event_type="ERROR",
                message=f"No quote data for {elapsed:.0f}s — halting bot",
                payload={"no_fresh_data_s": elapsed},
            )]
            await self._run_pipeline(actions)
            from ib_trader.bots.fsm import FSM, BotEvent, EventType
            try:
                await FSM(self.bot_id, self.config.get("_redis")).dispatch(
                    BotEvent(EventType.CRASH, payload={"message": "STALE_QUOTES"})
                )
            except Exception:
                pass
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
                logger.debug(
                    '{"event": "UNSUBSCRIBE_HTTP_FAILED", "symbol": "%s"}', symbol,
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
                event_type="SIGNAL",
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
                "position_state": PositionState.ENTERING.value,
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
                logger.debug('{"event": "WARMUP_HTTP_FAILED", "symbol": "%s"}', symbol)

        # Read only the warmup bars (and any live bars that landed while we
        # were waiting). Oversize the count so a typical 20x3-min lookback
        # (~720 raw bars) is covered with comfortable headroom.
        bars = await self._read_new_bars(symbol, count=max(total_5sec_bars * 2, 2000))
        if bars and self.aggregator:
            completed = self.aggregator.add_bars(bars)
            if self.pipeline and self.ctx:
                actions = [LogSignal(
                    event_type="STATE",
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
                    event_type="STATE",
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
            for stream_name, entries in results:
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
            logger.debug('{"event": "REDIS_QUOTE_READ_ERROR", "symbol": "%s"}', symbol)
            return None

    async def _check_pending_fills(self) -> None:
        """Check for fills by reading the Redis position state key.

        The engine's fill relay updates pos:{bot_ref}:{symbol} on every
        IB fill callback. If the position transitioned to a different state
        than what the bot expects, we process the fill/cancellation.
        """
        pos = PositionState(self.ctx.state.get("position_state", "FLAT"))
        symbol = self.strategy_config["symbol"]
        redis = self.config.get("_redis")
        bot_ref = self.strategy_config.get("ref_id", self.bot_id)

        if not redis:
            return

        from ib_trader.redis.state import StateStore
        store = StateStore(redis)
        redis_pos = await store.get(f"bot:{self.bot_id}")

        if not redis_pos:
            return

        redis_state = redis_pos.get("state") or redis_pos.get("position_state", "FLAT")

        # ENTERING → check if IB filled (OPEN) or cancelled (FLAT)
        if pos == PositionState.ENTERING:
            if redis_state == "OPEN":
                fill_price = Decimal(redis_pos.get("entry_price") or "0")
                fill_qty = Decimal(redis_pos.get("qty") or "0")
                serial = redis_pos.get("serial", 0)

                event = OrderFilled(
                    trade_serial=serial,
                    symbol=symbol,
                    side="BUY",
                    fill_price=fill_price,
                    qty=fill_qty,
                    commission=Decimal("0"),
                    ib_order_id="",
                )
                actions = await self.strategy.on_event(event, self.ctx)
                if actions:
                    await self._run_pipeline(actions)
                self._pending_cmd_id = None
                logger.info(
                    '{"event": "FILL_DETECTED_REDIS", "bot_ref": "%s", "symbol": "%s", '
                    '"qty": "%s", "price": "%s"}',
                    bot_ref, symbol, fill_qty, fill_price,
                )

            elif redis_state == "FLAT":
                # Order was cancelled/rejected
                event = OrderRejected(
                    trade_serial=None,
                    symbol=symbol,
                    reason="Order cancelled or rejected (detected via Redis)",
                    command_id="",
                )
                actions = await self.strategy.on_event(event, self.ctx)
                if actions:
                    await self._run_pipeline(actions)
                self._pending_cmd_id = None

        # EXITING → check if IB completed the exit (FLAT) or cancelled (OPEN)
        elif pos == PositionState.EXITING:
            if redis_state == "FLAT":
                entry_price_str = self.ctx.state.get("entry_price")
                entry_price = Decimal(str(entry_price_str)) if entry_price_str else Decimal("0")
                fill_price = Decimal(redis_pos.get("avg_price") or "0")
                fill_qty = Decimal(redis_pos.get("qty") or "0")

                event = OrderFilled(
                    trade_serial=self.ctx.state.get("trade_serial") or 0,
                    symbol=symbol,
                    side="SELL",
                    fill_price=fill_price,
                    qty=fill_qty,
                    commission=Decimal("0"),
                    ib_order_id="",
                )
                actions = await self.strategy.on_event(event, self.ctx)
                if actions:
                    await self._run_pipeline(actions)
                    if entry_price > 0 and fill_price > 0:
                        pnl = (fill_price - entry_price) * fill_qty
                        self._risk_mw.record_pnl(pnl)
                    self._risk_mw.record_trade()
                self._pending_cmd_id = None
                logger.info(
                    '{"event": "EXIT_FILL_DETECTED_REDIS", "bot_ref": "%s", "symbol": "%s"}',
                    bot_ref, symbol,
                )

            elif redis_state == "OPEN":
                # Exit was cancelled — back to OPEN
                self._pending_cmd_id = None


def _reconcile_state(state: dict | None, open_positions: list,
                     symbol: str, bot_id: str) -> dict:
    """Reconcile persisted bot state against actual IB positions.

    Cases:
    1. No state file, no IB position → FLAT (normal cold start)
    2. State says OPEN, IB has position → resume (normal restart)
    3. State says OPEN, IB has NO position for this symbol → stale state, go FLAT
    4. No state file, IB has position → orphaned position, log warning, stay FLAT
       (don't auto-adopt positions we didn't create)
    5. State says FLAT, IB has position → same as #4, stay FLAT
    """
    # Check if IB has a position for this symbol
    ib_has_position = any(
        getattr(t, "symbol", None) == symbol for t in open_positions
    )

    if state is None:
        if ib_has_position:
            logger.warning(
                '{"event": "ORPHANED_POSITION", "bot_id": "%s", "symbol": "%s", '
                '"message": "IB has position but no bot state — will not auto-adopt"}',
                bot_id, symbol,
            )
        return {"position_state": PositionState.FLAT.value}

    pos = state.get("position_state", "FLAT")

    if pos in ("OPEN", "EXITING"):
        if not ib_has_position:
            # State thinks we're in a trade but IB disagrees — trust IB
            logger.warning(
                '{"event": "STALE_STATE_CLEARED", "bot_id": "%s", "symbol": "%s", '
                '"old_state": "%s", "message": "IB has no position, clearing state"}',
                bot_id, symbol, pos,
            )
            return {"position_state": PositionState.FLAT.value}
        # Both agree — resume
        logger.info(
            '{"event": "STATE_RECONCILED", "bot_id": "%s", "symbol": "%s", '
            '"state": "%s", "entry_price": "%s"}',
            bot_id, symbol, pos, state.get("entry_price"),
        )
        return state

    # State is FLAT or ENTERING
    if pos == "ENTERING":
        # Was mid-entry when we crashed — go back to FLAT
        logger.info(
            '{"event": "ENTERING_STATE_CLEARED", "bot_id": "%s", "symbol": "%s"}',
            bot_id, symbol,
        )
        return {"position_state": PositionState.FLAT.value}

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

    result = {}

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
from ib_trader.bots.registry import register_strategy  # noqa: E402
register_strategy("strategy_bot", StrategyBotRunner)
