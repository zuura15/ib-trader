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

    def _run_pipeline(self, actions: list, ctx=None) -> None:
        """Run actions through pipeline and capture any submitted command ID."""
        self.pipeline.process(actions, ctx or self.ctx)
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

        # Restore or initialize state, with reconciliation against open positions
        symbol = self.strategy_config["symbol"]
        state = _load_persisted_state(self.bot_id, symbol)
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
        )
        execution_mw = ExecutionMiddleware(self.bot_id, self._pending_commands)
        self._execution_mw = execution_mw

        self.pipeline = MiddlewarePipeline([
            risk_mw, logging_mw, persistence_mw, execution_mw,
        ])
        self._risk_mw = risk_mw

        # Submit subscribe_bars command to engine
        symbol = self.strategy_config["symbol"]
        cmd = PendingCommand(
            source=f"bot:{self.bot_id}",
            broker="ib",
            command_text=f"subscribe_bars {symbol}",
            submitted_at=datetime.now(timezone.utc),
        )
        self._pending_commands.insert(cmd)

        # Warmup: prefetch historical 3-min bars to fill the aggregator immediately
        await self._warmup_from_history(symbol)
        self._warmup_complete = True
        self._signal_cooldown_until = time.monotonic() + 15  # no signals for 15s after startup

        # Run strategy startup
        actions = await self.strategy.on_start(self.ctx)
        if actions:
            self._run_pipeline(actions)

        logger.info('{"event": "STRATEGY_BOT_STARTED", "bot_id": "%s", '
                     '"strategy": "%s", "symbol": "%s", "position": "%s"}',
                     self.bot_id, self.strategy.manifest.name, symbol,
                     self.ctx.state.get("position_state", "FLAT"))

    async def on_tick(self) -> None:
        """Called every tick_interval_seconds by the bot runner.

        Reads new bars from market_bars table, aggregates them,
        and delivers events to the strategy. Also checks streaming
        quotes for exit monitoring.
        """
        if not self.strategy or not self.ctx:
            return

        symbol = self.strategy_config["symbol"]
        pos = PositionState(self.ctx.state.get("position_state", "FLAT"))

        # 0. Check for force-buy override
        last_action = self.read_last_action()
        if last_action == "FORCE_BUY":
            self.clear_last_action()
            if pos == PositionState.FLAT:
                await self._execute_force_buy(symbol)
                return
            else:
                actions = [LogSignal(
                    event_type="RISK",
                    message=f"FORCE_BUY ignored — position state is {pos.value}, not FLAT",
                )]
                self._run_pipeline(actions)

        # 0b. Check entry timeout every tick (not just on bar completion)
        if pos == PositionState.ENTERING:
            timeout = self.strategy_config.get("exit", {}).get("entry_timeout_seconds", 30)
            entry_time_str = self.ctx.state.get("entry_time")
            if entry_time_str:
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
                    self._run_pipeline(actions)
                    return

        # 0c. Check for failed SELL in EXITING — return to OPEN for continued monitoring
        if pos == PositionState.EXITING:
            bot_source = f"bot:{self.bot_id}"
            recent_cmds = self._pending_commands.get_by_source(bot_source, limit=5)
            for cmd in recent_cmds:
                cmd_text = (cmd.command_text or "").lower()
                if cmd.status == PendingCommandStatus.FAILURE and ("sell" in cmd_text or "close" in cmd_text):
                    actions = [
                        LogSignal(
                            event_type="ERROR",
                            message=f"Exit order FAILED: {cmd.error} — returning to OPEN for continued monitoring",
                            payload={"command": cmd.command_text, "error": cmd.error},
                        ),
                        UpdateState({"position_state": PositionState.OPEN.value}),
                    ]
                    self._run_pipeline(actions)
                    break

        # 1. Read new 5-sec bars from market_bars table
        new_bars = self._read_new_bars(symbol)

        # Log tick status periodically (every 12 ticks = ~60 seconds)
        self._tick_count = getattr(self, "_tick_count", 0) + 1
        if self._tick_count % 12 == 1:
            agg_bars = self.aggregator.buffered_bars if self.aggregator else 0
            agg_partial = self.aggregator.has_partial if self.aggregator else False
            lookback = self.aggregator.lookback_bars if self.aggregator else 0
            actions = [LogSignal(
                event_type="HEARTBEAT",
                message=(f"tick #{self._tick_count} | state={pos.value} | "
                         f"raw_bars={len(new_bars)} | "
                         f"buffered={agg_bars}/{lookback} | "
                         f"partial={'yes' if agg_partial else 'no'}"),
                payload={"tick": self._tick_count, "position_state": pos.value,
                         "new_raw_bars": len(new_bars),
                         "buffered_bars": agg_bars,
                         "lookback_needed": lookback},
            )]
            self._run_pipeline(actions)

        if new_bars and self.aggregator:
            completed = self.aggregator.add_bars(new_bars)

            if completed:
                actions = [LogSignal(
                    event_type="EVAL",
                    message=(f"Aggregated {len(completed)} bar(s) | "
                             f"total={self.aggregator.bar_count} | "
                             f"buffered={self.aggregator.buffered_bars}/"
                             f"{self.aggregator.lookback_bars}"),
                )]
                self._run_pipeline(actions)

            # Only evaluate the LAST completed bar in a batch.
            # When warmup or catch-up produces multiple bars at once,
            # evaluating each one would fire stale signals.
            # Also skip during post-startup cooldown.
            if completed:
                bar = completed[-1]
                cooldown = getattr(self, "_signal_cooldown_until", 0)
                if time.monotonic() < cooldown:
                    actions = [LogSignal(
                        event_type="EVAL",
                        message=f"Signal cooldown active ({cooldown - time.monotonic():.0f}s remaining) — skipping entry evaluation",
                    )]
                    self._run_pipeline(actions)
                elif self.aggregator.get_bar_window():
                    window = self.aggregator.get_bar_window()
                    event = BarCompleted(
                        symbol=symbol,
                        bar=bar,
                        window=window,
                        bar_count=self.aggregator.bar_count,
                    )
                    actions = await self.strategy.on_event(event, self.ctx)
                    if actions:
                        self._run_pipeline(actions)
                else:
                    needed = self.aggregator.lookback_bars
                    have = self.aggregator.buffered_bars
                    actions = [LogSignal(
                        event_type="EVAL",
                        message=f"Bar completed but window not ready ({have}/{needed} bars buffered)",
                    )]
                    self._run_pipeline(actions)

        # 2. Check for fills on pending commands
        await self._check_pending_fills()

        # 3. Exit monitoring via quotes (if in OPEN state)
        pos = PositionState(self.ctx.state.get("position_state", "FLAT"))
        if pos == PositionState.OPEN:
            quote = self._get_latest_quote(symbol)
            if quote:
                # Check if quote data is actually fresh (not just that a row exists)
                quote_age = (datetime.now(timezone.utc) - quote.timestamp).total_seconds()
                if quote_age < _STALE_QUOTE_WARN_SECONDS:
                    self._last_quote_time = time.monotonic()
                    self._quote_stale_logged = False
                    actions = await self.strategy.on_event(quote, self.ctx)
                    if actions:
                        self._run_pipeline(actions)
                else:
                    # Data exists but is stale — still run exit check with
                    # stale data (better to exit on old data than not at all)
                    actions = await self.strategy.on_event(quote, self.ctx)
                    if actions:
                        self._run_pipeline(actions)

                    elapsed = time.monotonic() - self._last_quote_time
                    if elapsed > _STALE_QUOTE_HALT_SECONDS and not self._quote_stale_logged:
                        self._quote_stale_logged = True
                        actions = [LogSignal(
                            event_type="ERROR",
                            message=f"Quote data stale ({quote_age:.0f}s old, no fresh data for {elapsed:.0f}s) — halting bot",
                            payload={"quote_age_s": quote_age, "no_fresh_data_s": elapsed},
                        )]
                        self._run_pipeline(actions)
                        self._bots.update_status(self.bot_id, "ERROR",
                                                  error_message="STALE_QUOTES")
                    elif elapsed > _STALE_QUOTE_WARN_SECONDS and not self._quote_stale_logged:
                        logger.warning('{"event": "STALE_QUOTES", "bot_id": "%s", '
                                        '"quote_age_s": %.1f, "no_fresh_s": %.1f}',
                                        self.bot_id, quote_age, elapsed)
            else:
                elapsed = time.monotonic() - self._last_quote_time
                if elapsed > _STALE_QUOTE_HALT_SECONDS and not self._quote_stale_logged:
                    self._quote_stale_logged = True
                    actions = [LogSignal(
                        event_type="ERROR",
                        message=f"No quote data for {elapsed:.0f}s — halting bot",
                    )]
                    self._run_pipeline(actions)
                    self._bots.update_status(self.bot_id, "ERROR",
                                              error_message="STALE_QUOTES")

    async def on_stop(self) -> None:
        """Cleanup on bot stop."""
        if self.strategy and self.ctx:
            actions = await self.strategy.on_stop(self.ctx)
            if actions and self.pipeline:
                self._run_pipeline(actions)

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

        # Get latest price from market_bars
        try:
            session = self._session_factory()
            result = session.execute(
                __import__("sqlalchemy").text(
                    "SELECT close FROM market_bars WHERE symbol = :s "
                    "ORDER BY timestamp_utc DESC LIMIT 1"
                ),
                {"s": symbol},
            )
            row = result.fetchone()
            if row:
                close_price = Decimal(str(row[0]))
        except Exception:
            pass

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
        self._run_pipeline(actions)

    async def _warmup_from_history(self, symbol: str) -> None:
        """Prefetch historical 3-min bars from the engine to fill the aggregator.

        Submits a warmup command that the engine processes by fetching
        historical bars and writing them to market_bars. Then reads them
        into the aggregator so the strategy can evaluate immediately.
        """
        if not self.aggregator:
            return

        lookback = self.strategy_config.get("lookback_bars", 20)
        bar_seconds = self.strategy_config.get("bar_size_seconds", 180)
        # Request enough 5-sec bars to fill lookback target bars
        # Each target bar = bar_seconds/5 five-sec bars
        total_5sec_bars = lookback * (bar_seconds // 5)
        duration_seconds = total_5sec_bars * 5 + 60  # small buffer

        # Submit warmup command to engine
        cmd = PendingCommand(
            source=f"bot:{self.bot_id}",
            broker="ib",
            command_text=f"warmup_bars {symbol} {duration_seconds}",
            submitted_at=datetime.now(timezone.utc),
        )
        self._pending_commands.insert(cmd)

        # Wait for the command to complete (engine writes bars to market_bars)
        deadline = time.monotonic() + 30  # 30 second timeout
        while time.monotonic() < deadline:
            refreshed = self._pending_commands.get(cmd.id)
            if refreshed and refreshed.status.value in ("SUCCESS", "FAILURE"):
                break
            await asyncio.sleep(0.5)

        # Now read the historical bars from market_bars
        bars = self._read_new_bars(symbol)
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
                self._run_pipeline(actions)
        else:
            if self.pipeline and self.ctx:
                actions = [LogSignal(
                    event_type="STATE",
                    message="Warmup: no historical bars available, starting cold",
                )]
                self._run_pipeline(actions)

    def _read_new_bars(self, symbol: str) -> list[dict]:
        """Read new 5-second bars from market_bars table since last read.

        For v1, reads from the market_bars table that the engine populates.
        Falls back to empty list if table doesn't exist yet.
        """
        try:
            session = self._session_factory()
            query = (
                "SELECT timestamp_utc, open, high, low, close, volume "
                "FROM market_bars WHERE symbol = :symbol"
            )
            params = {"symbol": symbol}

            if self._last_bar_ts:
                query += " AND timestamp_utc > :since"
                # Use space separator to match SQLite's text format
                params["since"] = str(self._last_bar_ts).replace("T", " ")

            query += " ORDER BY timestamp_utc ASC LIMIT 500"

            result = session.execute(
                __import__("sqlalchemy").text(query), params,
            )
            rows = result.fetchall()

            if not rows:
                return []

            bars = []
            for row in rows:
                bars.append({
                    "timestamp_utc": row[0],
                    "open": float(row[1]),
                    "high": float(row[2]),
                    "low": float(row[3]),
                    "close": float(row[4]),
                    "volume": int(row[5]),
                })
                raw_ts = datetime.fromisoformat(str(row[0])) if isinstance(row[0], str) else row[0]
                if raw_ts and raw_ts.tzinfo is None:
                    raw_ts = raw_ts.replace(tzinfo=timezone.utc)
                self._last_bar_ts = raw_ts

            return bars

        except Exception as exc:
            # Table may not exist yet if engine hasn't created it
            logger.debug('{"event": "MARKET_BARS_READ_ERROR", "error": "%s"}', exc)
            try:
                self._session_factory().rollback()
            except Exception:
                pass
            return []

    def _get_latest_quote(self, symbol: str) -> QuoteUpdate | None:
        """Read the latest streaming quote from market_quotes table.

        The engine writes streaming ticker data every ~2 seconds.
        Falls back to market_bars if market_quotes is not available.
        """
        try:
            session = self._session_factory()

            # Try streaming quotes first (updated every ~2s)
            result = session.execute(
                __import__("sqlalchemy").text(
                    "SELECT bid, ask, last, updated_at "
                    "FROM market_quotes WHERE symbol = :symbol"
                ),
                {"symbol": symbol},
            )
            row = result.fetchone()
            if row and (row[0] or row[1] or row[2]):
                bid = Decimal(str(row[0])) if row[0] else Decimal("0")
                ask = Decimal(str(row[1])) if row[1] else Decimal("0")
                last = Decimal(str(row[2])) if row[2] else Decimal("0")

                ts = row[3]
                if isinstance(ts, str):
                    ts = datetime.fromisoformat(ts)
                if ts and ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                return QuoteUpdate(
                    symbol=symbol,
                    bid=bid if bid > 0 else last,
                    ask=ask if ask > 0 else last,
                    last=last,
                    timestamp=ts or datetime.now(timezone.utc),
                )

            # Fallback to market_bars
            result = session.execute(
                __import__("sqlalchemy").text(
                    "SELECT timestamp_utc, close "
                    "FROM market_bars WHERE symbol = :symbol "
                    "ORDER BY timestamp_utc DESC LIMIT 1"
                ),
                {"symbol": symbol},
            )
            row = result.fetchone()
            if not row:
                return None

            close = Decimal(str(row[1]))
            spread = close * Decimal("0.0002")
            ts = row[0]
            if isinstance(ts, str):
                ts = datetime.fromisoformat(ts)
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            return QuoteUpdate(
                symbol=symbol,
                bid=close - spread / 2,
                ask=close + spread / 2,
                last=close,
                timestamp=ts or datetime.now(timezone.utc),
            )
        except Exception:
            return None

    async def _check_pending_fills(self) -> None:
        """Check for fills on the specific command we submitted.

        Tracks _pending_cmd_id so we only check our own command,
        not stale commands from previous bot/engine instances.
        """
        if not self._pending_cmd_id:
            return

        cmd = self._pending_commands.get(self._pending_cmd_id)
        if not cmd:
            return

        pos = PositionState(self.ctx.state.get("position_state", "FLAT"))
        symbol = self.strategy_config["symbol"]

        # Still running — wait
        if cmd.status == PendingCommandStatus.PENDING or cmd.status == PendingCommandStatus.RUNNING:
            return

        if pos == PositionState.ENTERING:
            if cmd.status == PendingCommandStatus.SUCCESS:
                output = cmd.output or ""
                parsed = _parse_fill_from_output(output, symbol)
                if parsed:
                    event = OrderFilled(
                        trade_serial=parsed["serial"],
                        symbol=symbol,
                        side="BUY",
                        fill_price=parsed["fill_price"],
                        qty=parsed["qty"],
                        commission=parsed["commission"],
                        ib_order_id="",
                    )
                    actions = await self.strategy.on_event(event, self.ctx)
                    if actions:
                        self._run_pipeline(actions)
                self._pending_cmd_id = None

            elif cmd.status == PendingCommandStatus.FAILURE:
                event = OrderRejected(
                    trade_serial=None,
                    symbol=symbol,
                    reason=cmd.error or "Unknown failure",
                    command_id=cmd.id,
                )
                actions = await self.strategy.on_event(event, self.ctx)
                if actions:
                    self._run_pipeline(actions)
                self._pending_cmd_id = None

        elif pos == PositionState.EXITING:
            if cmd.status == PendingCommandStatus.SUCCESS:
                output = cmd.output or ""
                entry_price_str = self.ctx.state.get("entry_price")
                entry_price = Decimal(str(entry_price_str)) if entry_price_str else Decimal("0")
                parsed = _parse_fill_from_output(output, symbol)

                fill_price = parsed["fill_price"] if parsed else Decimal("0")
                fill_qty = parsed["qty"] if parsed else Decimal("0")
                fill_commission = parsed["commission"] if parsed else Decimal("0")

                event = OrderFilled(
                    trade_serial=self.ctx.state.get("trade_serial") or 0,
                    symbol=symbol,
                    side="SELL",
                    fill_price=fill_price,
                    qty=fill_qty,
                    commission=fill_commission,
                    ib_order_id="",
                )
                actions = await self.strategy.on_event(event, self.ctx)
                if actions:
                    self._run_pipeline(actions)
                    if entry_price > 0 and fill_price > 0:
                        pnl = (fill_price - entry_price) * fill_qty
                        self._risk_mw.record_pnl(pnl)
                    self._risk_mw.record_trade()
                self._pending_cmd_id = None

            elif cmd.status == PendingCommandStatus.FAILURE:
                # Failed exit — handled by the EXITING recovery in on_tick
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
