"""Engine service CLI entry point.

The engine is the sole process with an IB broker connection. All other
processes (bot runner, API, REPL) communicate via Redis streams/keys
and the engine's internal HTTP API.

The engine:
  1. Receives IB push callbacks (ticks, fills, status, positions)
  2. Publishes to Redis streams + sets Redis keys (immediate)
  3. Writes to SQLite for audit (observational, not gating)
  4. Runs the reconciler for startup recovery and sanity checks
  5. Hosts the internal HTTP API for order placement

Usage:
    ib-engine                # defaults, IB broker
    ib-engine --paper        # paper trading
    ib-engine --db trader.db
"""
import asyncio
import json
import logging
import os
import signal
from pathlib import Path

# Module-level event signaling that a command completed and positions should refresh.
position_refresh_event = asyncio.Event()

import click

from ib_trader.config.loader import load_env, load_settings, load_symbols, check_file_permissions
from ib_trader.config.context import AppContext
from ib_trader.data.repository import (
    TradeRepository, RepriceEventRepository,
    ContractRepository, HeartbeatRepository, AlertRepository,
    create_db_engine, create_session_factory, init_db,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.data.repositories.pending_command_repository import PendingCommandRepository
from ib_trader.data.repositories.bot_repository import BotRepository, BotEventRepository
from ib_trader.data.repositories.template_repository import OrderTemplateRepository
from ib_trader.engine.tracker import OrderTracker
from ib_trader.logging_.logger import setup_logging

logger = logging.getLogger(__name__)


@click.command()
@click.option("--db", default="trader.db", help="SQLite database path")
@click.option("--env", default=".env", help="Environment file path")
@click.option("--settings", "settings_path", default="config/settings.yaml",
              help="Settings YAML path")
@click.option("--symbols", "symbols_path", default="config/symbols.yaml",
              help="Symbols whitelist path")
@click.option("--paper", is_flag=True, default=False, help="Use paper trading account")
def main(db: str, env: str, settings_path: str, symbols_path: str, paper: bool):
    """IB Trader Engine Service — central command execution loop."""
    setup_logging()

    # Load configuration (same pattern as REPL and daemon)
    env_vars = load_env(env)
    settings = load_settings(settings_path)
    symbols = load_symbols(symbols_path)

    # Override with env values
    settings["ib_host"] = env_vars.get("IB_HOST", settings.get("ib_host", "127.0.0.1"))
    if paper:
        settings["ib_port"] = int(env_vars.get("IB_PORT_PAPER", 4002))
        settings["ib_market_data_type"] = int(env_vars.get("IB_MARKET_DATA_TYPE_PAPER", 3))
        account_id = env_vars.get("IB_ACCOUNT_ID_PAPER") or env_vars["IB_ACCOUNT_ID"]
    else:
        settings["ib_port"] = int(env_vars.get("IB_PORT", 4001))
        settings["ib_market_data_type"] = int(env_vars.get("IB_MARKET_DATA_TYPE", 1))
        account_id = env_vars["IB_ACCOUNT_ID"]
    settings["ib_client_id"] = int(env_vars.get("IB_CLIENT_ID", 1))

    # Check DB permissions
    if Path(db).exists():
        try:
            check_file_permissions(db, 0o600, "SQLite database")
        except Exception as e:
            logger.warning('{"event": "DB_PERMISSION_WARNING", "error": "%s"}', str(e))

    # Create engine and session factory
    db_url = f"sqlite:///{db}"
    engine = create_db_engine(db_url)
    init_db(engine)
    session_factory = create_session_factory(engine)

    # Create IB client
    from ib_trader.ib.insync_client import InsyncClient
    ib_client = InsyncClient(
        host=settings["ib_host"],
        port=settings["ib_port"],
        client_id=settings["ib_client_id"],
        account_id=account_id,
        min_call_interval_ms=settings["ib_min_call_interval_ms"],
        market_data_type=settings["ib_market_data_type"],
    )

    # Assemble AppContext
    ctx = AppContext(
        ib=ib_client,
        trades=TradeRepository(session_factory),
        reprice_events=RepriceEventRepository(session_factory),
        contracts=ContractRepository(session_factory),
        heartbeats=HeartbeatRepository(session_factory),
        alerts=AlertRepository(session_factory),
        tracker=OrderTracker(),
        settings=settings,
        account_id=account_id,
        transactions=TransactionRepository(session_factory),
        pending_commands=PendingCommandRepository(session_factory),
        bots=BotRepository(session_factory),
        bot_events=BotEventRepository(session_factory),
        templates=OrderTemplateRepository(session_factory),
    )

    asyncio.run(run_engine(ctx, symbols))


async def run_engine(ctx: AppContext, symbols: list[str]) -> None:
    """Main engine service coroutine."""
    pid = os.getpid()
    retry_interval = ctx.settings.get("ib_connect_retry_seconds", 10)

    # Write heartbeat
    ctx.heartbeats.upsert("ENGINE", pid)
    logger.info('{"event": "ENGINE_STARTED", "pid": %d}', pid)
    print(f"[ENGINE] Started (pid={pid}). Connecting to IB Gateway...")

    # Graceful shutdown: cancel only the main task (not all tasks) so
    # CancelledError propagates through whatever await is active and the
    # finally block runs. Idempotent guard prevents duplicate signals
    # from re-cancelling during cleanup.
    loop = asyncio.get_running_loop()
    main_task = asyncio.current_task()
    shutting_down = False

    def request_shutdown():
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        print("[ENGINE] Shutting down gracefully...")
        logger.info('{"event": "SHUTDOWN_REQUESTED"}')
        main_task.cancel()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        loop.add_signal_handler(sig, request_shutdown)

    bg_tasks: list[asyncio.Task] = []
    try:
        # Connect to IB with retry loop
        await _connect_with_retry(ctx, retry_interval)

        # Wire the IB-disconnect callback so a dead Gateway is surfaced
        # as a CATASTROPHIC alert (visible in the frontend Alerts pane and
        # console pane via the existing WebSocket alerts channel).
        if hasattr(ctx.ib, "set_disconnect_callback"):
            ctx.ib.set_disconnect_callback(lambda: _raise_ib_disconnect_alert(ctx))

        print("[ENGINE] Connected to IB Gateway.")

        # Warm contract cache
        for symbol in symbols:
            try:
                await ctx.ib.qualify_contract(symbol)
            except Exception:
                logger.warning('{"event": "CONTRACT_WARM_FAILED", "symbol": "%s"}', symbol)

        print(f"[ENGINE] Warmed {len(symbols)} contracts. Processing commands...")

        # --- Crash recovery for pending_commands audit rows ---
        # execute_single_command writes RUNNING audit rows that would otherwise
        # stay RUNNING forever if the engine died mid-command. Clean them up
        # before the internal HTTP API starts accepting new commands.
        from ib_trader.engine.service import recover_stale_commands
        stale_count = recover_stale_commands(ctx)
        if stale_count:
            print(f"[ENGINE] Recovered {stale_count} stale command(s) from previous crash.")
            logger.warning(json.dumps({
                "event": "STALE_COMMANDS_FOUND", "count": stale_count,
            }))

        # --- Redis setup (required) ---
        redis_url = ctx.settings.get("redis_url", "redis://localhost:6379/0")
        from ib_trader.redis.client import get_redis
        redis = await get_redis(redis_url)
        ctx.redis = redis
        print("[ENGINE] Connected to Redis.")

        # --- Subscribe to watchlist symbols for tick publishing ---
        from ib_trader.config.loader import load_watchlist
        watchlist_symbols = load_watchlist("config/watchlist.yaml")
        watchlist_subscribed = 0
        for sym in watchlist_symbols:
            try:
                info = await ctx.ib.qualify_contract(sym)
                await ctx.ib.subscribe_market_data(info["con_id"], sym)
                watchlist_subscribed += 1
            except Exception:
                logger.warning('{"event": "WATCHLIST_SUB_FAILED", "symbol": "%s"}', sym)
        print(f"[ENGINE] Subscribed to {watchlist_subscribed} watchlist symbols.")

        # --- Load current IB positions into in-memory cache ---
        count = await _refresh_positions_cache(ctx, subscribe_mktdata=True)
        print(f"[ENGINE] Published {count} IB positions to Redis.")

        # --- Start background tasks ---
        bg_tasks = [
            asyncio.create_task(_heartbeat_loop(ctx, pid)),

            # Event relay: IB callbacks → Redis streams + keys
            asyncio.create_task(_event_relay_loop(ctx)),

            # Tick publisher: streaming ticks → Redis
            asyncio.create_task(_tick_publisher_loop(ctx)),

            # Position poll: 30s fallback for when positionEvent stops
            asyncio.create_task(_position_poll_loop(ctx)),
        ]

        # Reconciler: startup recovery + sanity checks
        from ib_trader.engine.reconciler import Reconciler
        reconciler = Reconciler(
            ctx.ib, redis,
            sanity_interval=ctx.settings.get("reconciler_sanity_interval", 60),
        )
        await reconciler.startup_reconcile()
        bg_tasks.append(asyncio.create_task(reconciler.run_sanity_loop()))
        print("[ENGINE] Reconciler started.")

        # Internal HTTP API — wait for the socket to bind before proceeding
        from ib_trader.engine.internal_api import start_internal_api
        internal_port = ctx.settings.get("engine_internal_port", 8081)
        api_task = await start_internal_api(ctx, port=internal_port)
        bg_tasks.append(api_task)
        # Give uvicorn time to bind the socket before bots try to connect
        await asyncio.sleep(1)
        print(f"[ENGINE] Internal API on 127.0.0.1:{internal_port}")

        # All command producers (bots, API, REPL) use the HTTP API.
        # No more polling loop. Keep the engine alive.
        print("[ENGINE] Ready. All commands via HTTP API.")
        await asyncio.Event().wait()  # Block forever until cancelled

    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        for t in bg_tasks:
            t.cancel()
        await asyncio.gather(*bg_tasks, return_exceptions=True)
        ctx.heartbeats.delete("ENGINE")
        try:
            await asyncio.shield(ctx.ib.disconnect())
        except Exception:
            pass  # IB may already be dead — don't crash during cleanup
        try:
            from ib_trader.redis.client import close_redis
            await close_redis()
        except Exception:
            pass
        print("[ENGINE] Stopped.")
        logger.info('{"event": "ENGINE_STOPPED"}')


async def _connect_with_retry(ctx: AppContext, retry_interval: int = 10) -> None:
    """Keep trying to connect to IB Gateway until successful.

    Prints a clear message every retry so the operator knows what's happening.
    """
    attempt = 0
    host = ctx.settings.get("ib_host", "127.0.0.1")
    port = ctx.settings.get("ib_port", 4001)

    while True:
        attempt += 1
        try:
            await ctx.ib.connect()
            logger.info('{"event": "IB_CONNECTED", "attempt": %d}', attempt)
            return
        except (KeyboardInterrupt, asyncio.CancelledError):
            raise  # Let shutdown propagate — don't retry on deliberate stop
        except Exception as e:
            msg = (
                f"[ENGINE] IB Gateway not reachable at {host}:{port} "
                f"(attempt {attempt}). Retrying in {retry_interval}s... "
                f"({e})"
            )
            print(msg)
            logger.warning(
                '{"event": "IB_CONNECT_RETRY", "attempt": %d, "host": "%s", '
                '"port": %d, "error": "%s"}',
                attempt, host, port, str(e),
            )
            await asyncio.sleep(retry_interval)


def _raise_ib_disconnect_alert(ctx: AppContext) -> None:
    """Write a CATASTROPHIC alert when the IB Gateway connection drops.

    Called from the ib_async event-loop callback in InsyncClient. Must be
    fast and non-blocking — we only do a single SQLite insert. Dedupe by
    checking for an existing open alert with the same trigger so a flapping
    connection doesn't spam the alerts table.
    """
    from datetime import datetime, timezone
    from ib_trader.data.models import AlertSeverity, SystemAlert
    try:
        existing_open = ctx.alerts.get_open()
        if any(a.trigger == "IB_GATEWAY_DISCONNECTED" for a in existing_open):
            return
        alert = SystemAlert(
            severity=AlertSeverity.CATASTROPHIC,
            trigger="IB_GATEWAY_DISCONNECTED",
            message=(
                "IB Gateway connection lost. The engine cannot place or "
                "track orders. Restart IB Gateway / TWS, then restart the "
                "engine to reconnect."
            ),
            created_at=datetime.now(timezone.utc),
        )
        ctx.alerts.create(alert)
        logger.error(
            '{"event": "SYSTEM_ALERT_RAISED", "severity": "CATASTROPHIC", '
            '"trigger": "IB_GATEWAY_DISCONNECTED"}'
        )
        # Nudge WS consumers — without this, the CATASTROPHIC alert only
        # reaches the UI on the 30s fallback refresh.
        if ctx.redis is not None:
            from ib_trader.redis.streams import publish_activity
            asyncio.create_task(publish_activity(ctx.redis, "alerts"))
    except Exception:
        logger.exception('{"event": "IB_DISCONNECT_ALERT_WRITE_FAILED"}')


async def _refresh_positions_cache(ctx: AppContext, *, subscribe_mktdata: bool = False) -> int:
    """Fetch current IB positions into the in-memory cache.

    Returns the count of non-zero positions. On first call (startup) set
    ``subscribe_mktdata=True`` to subscribe to quotes for position symbols.
    """
    from decimal import Decimal
    from datetime import datetime, timezone

    if not hasattr(ctx.ib, '_ib'):
        return 0

    ib_obj = ctx.ib._ib
    try:
        await asyncio.wait_for(ib_obj.reqPositionsAsync(), timeout=10)
    except Exception:
        logger.exception('{"event": "POSITION_REFRESH_FAILED"}')
        return len(ctx.positions_cache)

    positions = []
    for p in ib_obj.positions():
        sym = p.contract.symbol
        qty = Decimal(str(p.position))
        if qty == 0:
            continue
        avg_cost = Decimal(str(p.avgCost))
        con_id = p.contract.conId
        sec_type = p.contract.secType

        # Enrich with live market price from the in-memory ticker
        market_price = None
        try:
            ticker_data = ctx.ib.get_ticker(con_id)
            if ticker_data:
                bid = ticker_data.get("bid")
                ask = ticker_data.get("ask")
                last = ticker_data.get("last")
                if bid and ask:
                    market_price = str(round((float(bid) + float(ask)) / 2, 4))
                elif last:
                    market_price = str(last)
        except Exception:
            pass

        positions.append({
            "id": f"{sym}_{sec_type}_{con_id}",
            "account_id": p.account,
            "symbol": sym,
            "sec_type": sec_type,
            "quantity": str(qty),
            "avg_cost": str(avg_cost),
            "market_price": market_price,
            "con_id": con_id,
            "broker": "ib",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        if subscribe_mktdata and sec_type == "STK":
            try:
                await ctx.ib.subscribe_market_data(con_id, sym)
            except Exception:
                pass

    # Atomic swap — readers see the old list or the new list, never partial.
    ctx.positions_cache = positions
    return len(positions)


async def _position_poll_loop(ctx: AppContext) -> None:
    """Refresh the in-memory positions cache periodically as a fallback.

    positionEvent is the real-time path; this is the safety net for when
    the callback silently stops firing (reconnect, nightly reset, etc.).
    Interval is ``position_poll_interval_seconds`` in settings.yaml (default 60).
    """
    interval = ctx.settings.get("position_poll_interval_seconds", 60)
    while True:
        await asyncio.sleep(interval)
        try:
            count = await _refresh_positions_cache(ctx)
            # Sweep stale held-cancel entries from the order ledger
            if hasattr(ctx, '_order_ledger'):
                ctx._order_ledger.sweep_stale(max_age_seconds=300)
            logger.debug('{"event": "POSITION_POLL", "count": %d}', count)
        except Exception:
            logger.exception('{"event": "POSITION_POLL_ERROR"}')


async def _event_relay_loop(ctx: AppContext) -> None:
    """Relay IB fill/status/position events to Redis streams and keys.

    Registers global callbacks on the IB client. When IB fires an event,
    the callback publishes to the appropriate Redis stream and updates
    the Redis position state key.

    This is the PRIMARY update path — the reconciler is just a safety net.
    """
    from decimal import Decimal
    from datetime import datetime, timezone
    from ib_trader.redis.streams import StreamWriter, StreamNames
    from ib_trader.redis.state import StateStore, StateKeys
    from ib_trader.engine.order_ledger import OrderLedger

    redis = ctx.redis
    if redis is None:
        return

    state = StateStore(redis)
    ledger = OrderLedger()
    # Expose on ctx so the position poll loop can sweep stale entries
    ctx._order_ledger = ledger
    order_writer = StreamWriter(redis, StreamNames.order_updates(), maxlen=5000)
    orders_open_key = StateKeys.orders_open()

    async def _update_orders_open(events: list[dict]) -> None:
        """Maintain the orders:open Redis hash from emitted events.

        Non-terminal events upsert; terminal events remove.
        """
        for evt in events:
            oid = evt.get("ib_order_id")
            if not oid:
                continue
            if evt.get("terminal"):
                await redis.hdel(orders_open_key, oid)
            else:
                import json as _json
                await redis.hset(orders_open_key, oid, _json.dumps(evt))

    def _get_trade_meta(ib_order_id: str) -> tuple[str, str, str, int, str]:
        """Extract (orderRef, symbol, sec_type, con_id, side) from the active trade."""
        order_ref = ""
        symbol = ""
        sec_type = "STK"
        con_id = 0
        side = ""
        if hasattr(ctx.ib, '_active_trades'):
            trade = ctx.ib._active_trades.get(ib_order_id)
            if trade:
                if hasattr(trade, 'order'):
                    order_ref = getattr(trade.order, 'orderRef', "") or ""
                    side = getattr(trade.order, 'action', "") or ""
                if hasattr(trade, 'contract'):
                    symbol = getattr(trade.contract, 'symbol', "") or ""
                    sec_type = getattr(trade.contract, 'secType', "STK") or "STK"
                    con_id = getattr(trade.contract, 'conId', 0) or 0
        return order_ref, symbol, sec_type, con_id, side

    # --- Global fill callback ---
    async def on_fill(ib_order_id: str, qty_filled: Decimal,
                      avg_price: Decimal, commission: Decimal) -> None:
        """Handle fill from IB — record in ledger, publish to order:updates."""
        try:
            order_ref, symbol, sec_type, con_id, side = _get_trade_meta(ib_order_id)

            # Get remaining qty from the active trade if available
            remaining = Decimal("-1")
            if hasattr(ctx.ib, '_active_trades'):
                trade = ctx.ib._active_trades.get(ib_order_id)
                if trade and hasattr(trade, 'orderStatus'):
                    rem = getattr(trade.orderStatus, 'remaining', -1)
                    if rem >= 0:
                        remaining = Decimal(str(rem))

            events = ledger.record_fill(
                ib_order_id,
                qty=qty_filled,
                price=avg_price,
                commission=commission,
                order_ref=order_ref,
                symbol=symbol,
                sec_type=sec_type,
                con_id=con_id,
                side=side,
                remaining=remaining,
            )

            for event in events:
                await order_writer.add(event)
            await _update_orders_open(events)

            logger.info(
                '{"event": "FILL_RELAYED", "ib_order_id": "%s", '
                '"orderRef": "%s", "symbol": "%s", "qty": "%s", "price": "%s"}',
                ib_order_id, order_ref, symbol, qty_filled, avg_price,
            )
            from ib_trader.redis.streams import publish_activity
            await publish_activity(redis, "orders")
            await publish_activity(redis, "trades")
        except Exception:
            logger.exception('{"event": "FILL_RELAY_ERROR", "ib_order_id": "%s"}', ib_order_id)

    # --- Global status callback ---
    async def on_status(ib_order_id: str, status: str) -> None:
        """Handle order status change from IB — record in ledger, publish."""
        try:
            order_ref, symbol, sec_type, con_id, side = _get_trade_meta(ib_order_id)

            events = ledger.record_status(
                ib_order_id,
                status=status,
                order_ref=order_ref,
                symbol=symbol,
                sec_type=sec_type,
                con_id=con_id,
                side=side,
            )

            for event in events:
                await order_writer.add(event)
            await _update_orders_open(events)

            from ib_trader.redis.streams import publish_activity
            await publish_activity(redis, "orders")
        except Exception:
            logger.exception('{"event": "STATUS_RELAY_ERROR", "ib_order_id": "%s"}', ib_order_id)

    # Register global callbacks (fire for ALL orders)
    ctx.ib.register_fill_callback(on_fill)
    ctx.ib.register_status_callback(on_status)

    # --- Position event callback ---
    # Wire up positionEvent if the IB client supports it.
    # Use a semaphore to prevent connection pool exhaustion when IB fires
    # positionEvent for all positions simultaneously on startup.
    _position_sem = asyncio.Semaphore(5)

    if hasattr(ctx.ib, '_ib'):
        ib_obj = ctx.ib._ib

        def on_position_event(position) -> None:
            """Handle position change from IB (any source, including manual TWS closes)."""
            asyncio.create_task(_handle_position_event(ctx, position, _position_sem))

        ib_obj.positionEvent += on_position_event
        # Subscribe to position updates
        try:
            await ib_obj.reqPositionsAsync()
        except Exception:
            pass
        logger.info('{"event": "POSITION_EVENT_WIRED"}')

    # Keep the task alive
    while True:
        await asyncio.sleep(3600)


async def _handle_position_event(ctx, position, sem=None) -> None:
    """Process a positionEvent from IB.

    Updates the in-memory positions cache and publishes a change event
    to the ``position:changes`` Redis stream (for WS diffs). No
    ``ibpos:*`` Redis keys — positions are served via the engine HTTP
    endpoint directly from memory.
    """
    from decimal import Decimal
    from datetime import datetime, timezone
    from ib_trader.redis.streams import StreamWriter, StreamNames

    redis = ctx.redis

    if sem:
        await sem.acquire()
    try:
        symbol = position.contract.symbol
        qty = Decimal(str(position.position))
        avg_price = Decimal(str(position.avgCost))
        con_id = position.contract.conId
        sec_type = position.contract.secType

        # Update in-memory cache (atomic list rebuild)
        now = datetime.now(timezone.utc).isoformat()
        market_price = None
        try:
            ticker_data = ctx.ib.get_ticker(con_id)
            if ticker_data:
                bid = ticker_data.get("bid")
                ask = ticker_data.get("ask")
                last = ticker_data.get("last")
                if bid and ask:
                    market_price = str(round((float(bid) + float(ask)) / 2, 4))
                elif last:
                    market_price = str(last)
        except Exception:
            pass
        entry = {
            "id": f"{symbol}_{sec_type}_{con_id}",
            "account_id": position.account,
            "symbol": symbol,
            "sec_type": sec_type,
            "quantity": str(qty),
            "avg_cost": str(avg_price),
            "market_price": market_price,
            "con_id": con_id,
            "broker": "ib",
            "updated_at": now,
        }
        new_cache = [p for p in ctx.positions_cache
                     if not (p.get("symbol") == symbol
                             and p.get("sec_type") == sec_type
                             and p.get("con_id") == con_id)]
        if qty != 0:
            new_cache.append(entry)
        ctx.positions_cache = new_cache

        # Publish position change to stream (for WS diffs)
        if redis is not None:
            writer = StreamWriter(redis, StreamNames.position_changes(), maxlen=1000)
            await writer.add({
                "symbol": symbol,
                "sec_type": sec_type,
                "qty": str(qty),
                "avg_price": str(avg_price),
                "con_id": con_id,
                "account": position.account,
                "ts": now,
            })

    except Exception:
        logger.exception('{"event": "POSITION_EVENT_ERROR"}')
    finally:
        if sem:
            sem.release()


def _make_bar_publisher(redis, symbol: str):
    """Create an async callback that publishes 5s bars to bar:{symbol}:5s.

    Returned callback matches the signature expected by
    InsyncClient.subscribe_realtime_bars (receives bar_data dict).
    Consumer format (short keys) matches bots.runtime._dispatch_event.
    """
    from ib_trader.redis.streams import StreamWriter, StreamNames

    writer = StreamWriter(redis, StreamNames.bar(symbol, "5s"), maxlen=5000)

    async def publish(bar_data: dict) -> None:
        ts = bar_data.get("time")
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        try:
            await writer.add({
                "ts": ts_str,
                "o": bar_data.get("open", 0.0),
                "h": bar_data.get("high", 0.0),
                "l": bar_data.get("low", 0.0),
                "c": bar_data.get("close", 0.0),
                "v": bar_data.get("volume", 0),
            })
        except Exception:
            logger.exception('{"event": "BAR_PUBLISH_ERROR", "symbol": "%s"}', symbol)

    return publish


async def _tick_publisher_loop(ctx: AppContext) -> None:
    """Publish streaming tick data from IB to Redis — event-driven.

    Wires into ib_async's pendingTickersEvent so publishes fire on the tick
    itself, not on a poll interval. Quotes are the project's fundamental
    clock; this closes the loop end-to-end.

    Stays alive (awaits forever) so caller can manage lifecycle via
    task cancellation — the finally block unregisters the handler.
    """
    from datetime import datetime, timezone
    from ib_trader.redis.streams import StreamWriter, StreamNames
    from ib_trader.redis.state import StateStore, StateKeys

    redis = ctx.redis
    if redis is None:
        return

    if not hasattr(ctx.ib, '_ib'):
        logger.warning('{"event": "TICK_PUBLISHER_NO_IB"}')
        return

    state = StateStore(redis)
    writers: dict[str, StreamWriter] = {}
    last_values: dict[str, tuple] = {}  # symbol → (bid, ask, last) dedup

    async def _publish_one(ticker) -> None:
        try:
            contract = getattr(ticker, "contract", None)
            if contract is None:
                return
            # Only publish equities — option/futures tickers share the
            # underlying's symbol and would overwrite the equity quote.
            if getattr(contract, "secType", None) != "STK":
                return
            symbol = getattr(contract, "symbol", None)
            con_id = getattr(contract, "conId", None)
            if not symbol or con_id is None:
                return

            # Reuse get_ticker()'s NaN/zero cleaning + cached close fallback.
            data = ctx.ib.get_ticker(con_id)
            if data is None:
                return

            bid = data.get("bid")
            ask = data.get("ask")
            last = data.get("last")
            if bid is None and ask is None and last is None:
                return

            current = (bid, ask, last)
            if current == last_values.get(symbol):
                return
            last_values[symbol] = current

            if symbol not in writers:
                writers[symbol] = StreamWriter(
                    redis, StreamNames.quote(symbol), maxlen=5000,
                )

            close = data.get("close")
            change = None
            change_pct = None
            if last is not None and close is not None and close > 0:
                change = round(last - close, 4)
                change_pct = round((change / close) * 100, 2)

            quote_data = {
                "bid": str(bid) if bid else None,
                "ask": str(ask) if ask else None,
                "last": str(last) if last else None,
                "volume": data.get("volume"),
                "avg_volume": data.get("avg_volume"),
                "high": data.get("high"),
                "low": data.get("low"),
                "close": close,
                "change": change,
                "change_pct": change_pct,
                "high_52w": data.get("high_52w"),
                "low_52w": data.get("low_52w"),
                "ts": datetime.now(timezone.utc).isoformat(),
            }

            await writers[symbol].add(quote_data)
            await state.set(
                StateKeys.quote_latest(symbol),
                quote_data,
                ttl=StateKeys.QUOTE_TTL,
            )
        except Exception:
            logger.exception('{"event": "TICK_PUBLISHER_ERROR"}')

    def on_pending_tickers(tickers) -> None:
        # ib_async fires this synchronously; schedule per-ticker async work.
        for t in tickers:
            asyncio.create_task(_publish_one(t))

    ctx.ib._ib.pendingTickersEvent += on_pending_tickers
    logger.info('{"event": "TICK_PUBLISHER_WIRED"}')

    try:
        await asyncio.Event().wait()
    finally:
        try:
            ctx.ib._ib.pendingTickersEvent -= on_pending_tickers
        except Exception:
            pass


async def _heartbeat_loop(ctx: AppContext, pid: int) -> None:
    """Write ENGINE heartbeat to Redis (primary) and SQLite (audit)."""
    from ib_trader.redis.state import StateKeys
    import json as _json

    interval = ctx.settings.get("heartbeat_interval_seconds", 30)
    while True:
        try:
            # Redis — primary, with TTL auto-expiry
            if ctx.redis is not None:
                key = StateKeys.process_heartbeat("ENGINE")
                val = _json.dumps({"pid": pid, "ts": _now_iso()})
                await ctx.redis.setex(key, StateKeys.PROCESS_HEARTBEAT_TTL, val)
            # SQLite — archival
            ctx.heartbeats.upsert("ENGINE", pid)
        except Exception:
            logger.exception('{"event": "HEARTBEAT_WRITE_FAILED"}')
        await asyncio.sleep(interval)


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


if __name__ == "__main__":
    main()
