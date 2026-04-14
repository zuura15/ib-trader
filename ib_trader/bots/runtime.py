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
    PersistenceMiddleware, ExecutionMiddleware,
)
from ib_trader.data.models import (
    PendingCommand, PendingCommandStatus, BotEvent,
    TransactionAction, LegType,
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

    async def _load_state_from_redis(self, redis, bot_ref: str, symbol: str) -> dict | None:
        """Load strategy state from Redis key.

        Returns the state dict if found, None otherwise.
        """
        from ib_trader.redis.state import StateStore, StateKeys
        try:
            store = StateStore(redis)
            # Try strategy state key first
            strat_state = await store.get(StateKeys.strategy(bot_ref, symbol))
            if strat_state:
                logger.info(
                    '{"event": "STATE_LOADED_REDIS", "bot_ref": "%s", "symbol": "%s"}',
                    bot_ref, symbol,
                )
                return strat_state

            # Try position state key
            pos_state = await store.get(StateKeys.position(bot_ref, symbol))
            if pos_state:
                logger.info(
                    '{"event": "POSITION_LOADED_REDIS", "bot_ref": "%s", "symbol": "%s", "state": "%s"}',
                    bot_ref, symbol, pos_state.get("state"),
                )
                return {
                    "position_state": pos_state.get("state", "FLAT"),
                    "entry_price": pos_state.get("entry_price"),
                    "entry_time": pos_state.get("entry_time"),
                    "trade_serial": pos_state.get("serial", 0),
                }
        except Exception:
            logger.exception('{"event": "REDIS_STATE_LOAD_ERROR", "bot_ref": "%s"}', bot_ref)
        return None

    async def _run_pipeline(self, actions: list, ctx=None) -> None:
        """Run actions through pipeline and capture any submitted command ID."""
        await self.pipeline.process(actions, ctx or self.ctx)
        if self.pipeline.last_cmd_id:
            self._pending_cmd_id = self.pipeline.last_cmd_id
            self.pipeline.last_cmd_id = None

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

        risk_mw = RiskMiddleware(
            self.bot_id, risk_config,
            self._bots, self._trades,
        )
        logging_mw = LoggingMiddleware(self.bot_id, self._bot_events_repo)
        persistence_mw = PersistenceMiddleware(
            self.bot_id, self.strategy_config["symbol"], STATE_DIR,
            redis=redis, bot_ref=bot_ref,
        )
        execution_mw = ExecutionMiddleware(
            self.bot_id, self._pending_commands,
            engine_url=engine_url, bot_ref=bot_ref,
        )
        self._execution_mw = execution_mw

        self.pipeline = MiddlewarePipeline([
            risk_mw, logging_mw, persistence_mw, execution_mw,
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
        fill_stream = StreamNames.fill(bot_ref)
        pos_stream = StreamNames.position_changes()

        streams = {
            quote_stream: "$",
            bar_stream: "$",
            fill_stream: "$",
            pos_stream: "$",
        }

        logger.info(
            '{"event": "BOT_EVENT_LOOP_STARTED", "bot_id": "%s", "symbol": "%s", '
            '"streams": ["%s", "%s", "%s", "%s"]}',
            self.bot_id, symbol, quote_stream, bar_stream, fill_stream, pos_stream,
        )

        while True:
            try:
                # 5s timeout = liveness floor. If Redis returns nothing for 5s,
                # we still loop (no work to do, just wait for next event).
                results = await redis.xread(streams, block=5000)
            except asyncio.CancelledError:
                raise
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
                                                    fill_stream, pos_stream,
                                                    symbol, bot_ref)
                    except asyncio.CancelledError:
                        raise
                    except Exception:
                        logger.exception(
                            '{"event": "BOT_DISPATCH_ERROR", "bot_id": "%s", "stream": "%s"}',
                            self.bot_id, stream_name,
                        )

    async def _dispatch_event(self, stream_name: str, raw_data: dict,
                               quote_stream: str, bar_stream: str,
                               fill_stream: str, pos_stream: str,
                               symbol: str, bot_ref: str) -> None:
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

        # ── Fill / status from engine ─────────────────────────────────
        if stream_name == fill_stream:
            event_type = data.get("type", "")
            pos = PositionState(self.ctx.state.get("position_state", "FLAT"))

            # Only react to FILL events that change position state
            if event_type == "FILL":
                redis = self.config.get("_redis")
                from ib_trader.redis.state import StateStore, StateKeys
                store = StateStore(redis)
                redis_pos = await store.get(StateKeys.position(bot_ref, symbol))
                if not redis_pos:
                    return

                redis_state = redis_pos.get("state", "FLAT")
                fill_qty = Decimal(redis_pos.get("qty") or "0")
                fill_price = Decimal(redis_pos.get("avg_price") or "0")
                serial = redis_pos.get("serial", 0)

                # ENTERING → OPEN: BUY filled
                if pos == PositionState.ENTERING and redis_state == "OPEN":
                    fill_event = OrderFilled(
                        trade_serial=serial,
                        symbol=symbol,
                        side="BUY",
                        fill_price=fill_price,
                        qty=fill_qty,
                        commission=Decimal("0"),
                        ib_order_id="",
                    )
                    actions = await self.strategy.on_event(fill_event, self.ctx)
                    if actions:
                        await self._run_pipeline(actions)
                    return

                # EXITING → FLAT: SELL filled (full close)
                if pos == PositionState.EXITING and redis_state == "FLAT":
                    entry_price_str = self.ctx.state.get("entry_price")
                    entry_price = Decimal(str(entry_price_str)) if entry_price_str else Decimal("0")
                    sell_qty = Decimal(redis_pos.get("qty") or "0")
                    fill_event = OrderFilled(
                        trade_serial=self.ctx.state.get("trade_serial") or 0,
                        symbol=symbol,
                        side="SELL",
                        fill_price=fill_price,
                        qty=sell_qty,
                        commission=Decimal("0"),
                        ib_order_id="",
                    )
                    actions = await self.strategy.on_event(fill_event, self.ctx)
                    if actions:
                        await self._run_pipeline(actions)
                        if entry_price > 0 and fill_price > 0:
                            pnl = (fill_price - entry_price) * sell_qty
                            self._risk_mw.record_pnl(pnl)
                        self._risk_mw.record_trade()
                    return

            # Cancellations / rejections
            if event_type == "STATUS" and data.get("status") in ("Cancelled", "Inactive", "ApiCancelled"):
                if pos == PositionState.ENTERING:
                    rejected = OrderRejected(
                        trade_serial=None,
                        symbol=symbol,
                        reason=f"Order {data.get('status', 'cancelled')}",
                        command_id="",
                    )
                    actions = await self.strategy.on_event(rejected, self.ctx)
                    if actions:
                        await self._run_pipeline(actions)
            return

        # ── Position change (external manipulation) ────────────────────
        if stream_name == pos_stream:
            if data.get("symbol") != symbol:
                return
            qty = Decimal(str(data.get("qty", "0")))
            if qty == 0:
                # Could be a full close from elsewhere — log it. Bot's own
                # position is tracked by orderRef so it's not auto-flattened
                # (see TODO.md for "external close while bot OPEN").
                logger.info(
                    '{"event": "EXTERNAL_POSITION_ZERO", "bot_id": "%s", "symbol": "%s"}',
                    self.bot_id, symbol,
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

    async def check_force_buy(self) -> None:
        """Supervisory check: handle FORCE_BUY action set on the bot row.

        Called by the supervisory task when it detects a control event
        (the runner already wakes immediately on bot:control:* stream
        events; this method just executes the action).
        """
        if not self.strategy or not self.ctx:
            return
        last_action = self.read_last_action()
        if last_action != "FORCE_BUY":
            return

        self.clear_last_action()
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
            self._bots.update_status(self.bot_id, "ERROR",
                                      error_message="STALE_QUOTES")
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

        # Unsubscribe bars
        symbol = self.strategy_config.get("symbol", "")
        cmd = PendingCommand(
            source=f"bot:{self.bot_id}",
            broker="ib",
            command_text=f"unsubscribe_bars {symbol}",
            submitted_at=datetime.now(timezone.utc),
        )
        self._pending_commands.insert(cmd)

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
        """Prefetch historical 3-min bars from the engine to fill the aggregator.

        Submits a warmup command that the engine processes by fetching
        historical bars via the engine's HTTP API. Bars are written to the
        Redis bar stream, which the bot then reads for warmup.
        """
        if not self.aggregator:
            return

        lookback = self.strategy_config.get("lookback_bars", 20)
        bar_seconds = self.strategy_config.get("bar_size_seconds", 180)
        total_5sec_bars = lookback * (bar_seconds // 5)
        duration_seconds = total_5sec_bars * 5 + 60

        engine_url = self.config.get("_engine_url")
        if engine_url:
            # Request warmup via engine HTTP API (synchronous)
            import httpx
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{engine_url}/engine/orders",
                        json={"symbol": symbol, "side": "BUY", "qty": "0",
                              "order_type": f"warmup_bars {symbol} {duration_seconds}"},
                    )
                    # Even if this fails, we continue — bars will arrive via stream
            except Exception:
                logger.debug('{"event": "WARMUP_HTTP_FAILED", "symbol": "%s"}', symbol)

        # Read whatever bars are in the Redis stream
        bars = await self._read_new_bars(symbol)
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

    async def _read_new_bars(self, symbol: str) -> list[dict]:
        """Read new bars from the Redis bar stream.

        The engine publishes 5-second bars to bar:{symbol}:5s via
        reqRealTimeBars push callbacks.
        """
        redis = self.config.get("_redis")
        if not redis:
            return []

        from ib_trader.redis.streams import StreamReader, StreamNames

        stream = StreamNames.bar(symbol, "5s")
        last_id = getattr(self, '_last_bar_stream_id', "0")

        try:
            results = await redis.xread({stream: last_id}, count=500)
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

        from ib_trader.redis.state import StateStore, StateKeys
        store = StateStore(redis)
        redis_pos = await store.get(StateKeys.position(bot_ref, symbol))

        if not redis_pos:
            return

        redis_state = redis_pos.get("state", "FLAT")

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
