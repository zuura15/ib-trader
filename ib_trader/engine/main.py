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

        # --- Publish current IB positions to Redis ---
        await _publish_ib_positions_to_redis(ctx)

        # --- Start background tasks ---
        bg_tasks = [
            asyncio.create_task(_heartbeat_loop(ctx, pid)),

            # Event relay: IB callbacks → Redis streams + keys
            asyncio.create_task(_event_relay_loop(ctx)),

            # Tick publisher: streaming ticks → Redis
            asyncio.create_task(_tick_publisher_loop(ctx)),
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

        # Engine loop — handles commands from REPL/API via pending_commands.
        # Bot commands go through the HTTP API directly.
        from ib_trader.engine.service import engine_loop
        max_concurrent = ctx.settings.get("engine_max_concurrent", 5)
        poll_interval = ctx.settings.get("engine_poll_interval", 0.1)
        await engine_loop(ctx, max_concurrent=max_concurrent,
                          poll_interval=poll_interval)

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
    except Exception:
        logger.exception('{"event": "IB_DISCONNECT_ALERT_WRITE_FAILED"}')


async def _publish_ib_positions_to_redis(ctx: AppContext) -> None:
    """Fetch current IB positions and publish to Redis for the API.

    Writes each position as a Redis key so the positions endpoint can
    serve them. Also subscribes to market data for position symbols
    so the tick publisher sends live quotes.
    """
    from decimal import Decimal
    from datetime import datetime, timezone
    from ib_trader.redis.state import StateStore

    redis = ctx.redis
    if redis is None or not hasattr(ctx.ib, '_ib'):
        return

    state = StateStore(redis)
    ib_obj = ctx.ib._ib

    try:
        await asyncio.wait_for(ib_obj.reqPositionsAsync(), timeout=10)
    except Exception:
        logger.exception('{"event": "PUBLISH_POSITIONS_FAILED"}')
        return

    count = 0
    for p in ib_obj.positions():
        sym = p.contract.symbol
        qty = Decimal(str(p.position))
        avg_cost = Decimal(str(p.avgCost))
        con_id = p.contract.conId
        sec_type = p.contract.secType

        # Write to Redis — use "ib" as bot_ref for manual/untagged positions
        pos_key = f"ibpos:{sym}:{sec_type}:{con_id}"
        await state.set(pos_key, {
            "symbol": sym,
            "sec_type": sec_type,
            "quantity": str(qty),
            "avg_cost": str(avg_cost),
            "con_id": con_id,
            "account": p.account,
            "broker": "ib",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        })

        # Subscribe to market data for equities only — option/futures quotes
        # are different instruments and would overwrite equity quote keys
        if sec_type == "STK":
            try:
                await ctx.ib.subscribe_market_data(con_id, sym)
            except Exception:
                pass

        count += 1

    logger.info('{"event": "IB_POSITIONS_PUBLISHED", "count": %d}', count)
    print(f"[ENGINE] Published {count} IB positions to Redis.")


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
    from ib_trader.engine.order_ref import decode as decode_ref

    redis = ctx.redis
    if redis is None:
        return

    state = StateStore(redis)

    # --- Global fill callback ---
    async def on_fill(ib_order_id: str, qty_filled: Decimal,
                      avg_price: Decimal, commission: Decimal) -> None:
        """Handle fill from IB — publish to Redis stream + update state key."""
        try:
            # Get orderRef from the active trade
            order_ref_str = ""
            if hasattr(ctx.ib, '_active_trades'):
                trade = ctx.ib._active_trades.get(ib_order_id)
                if trade and hasattr(trade, 'order') and hasattr(trade.order, 'orderRef'):
                    order_ref_str = trade.order.orderRef or ""

            ref = decode_ref(order_ref_str)
            bot_ref = ref.bot_ref if ref else "unknown"
            symbol = ref.symbol if ref else ""
            serial = ref.serial if ref else 0
            side = ref.side if ref else ""

            # Publish to fill stream
            writer = StreamWriter(redis, StreamNames.fill(bot_ref), maxlen=500)
            await writer.add({
                "type": "FILL",
                "ib_order_id": ib_order_id,
                "orderRef": order_ref_str,
                "symbol": symbol,
                "side": side,
                "qty": str(qty_filled),
                "price": str(avg_price),
                "commission": str(commission),
                "serial": serial,
                "ts": datetime.now(timezone.utc).isoformat(),
            })

            # Update position state key — handle partial fills correctly
            if ref:
                pos_key = StateKeys.position(bot_ref, symbol)
                existing = await state.get(pos_key) or {}
                existing_qty = Decimal(existing.get("qty", "0"))

                if side == "B":
                    # BUY fill: accumulate quantity (handles partial fills)
                    new_qty = existing_qty + qty_filled
                    pos_data = {
                        "state": "OPEN",
                        "qty": str(new_qty),
                        "avg_price": str(avg_price),  # IB avg_price is already cumulative
                        "serial": serial,
                        "entry_price": str(avg_price),
                        "entry_time": existing.get("entry_time") or datetime.now(timezone.utc).isoformat(),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                else:
                    # SELL fill: subtract from position, only FLAT when qty reaches 0
                    new_qty = existing_qty - qty_filled
                    if new_qty <= 0:
                        new_state = "FLAT"
                        new_qty = Decimal("0")
                    else:
                        new_state = "EXITING"  # Partial exit — still have shares
                    pos_data = {
                        "state": new_state,
                        "qty": str(new_qty),
                        "avg_price": str(avg_price),
                        "serial": serial,
                        "entry_price": existing.get("entry_price"),
                        "entry_time": existing.get("entry_time"),
                        "updated_at": datetime.now(timezone.utc).isoformat(),
                    }
                await state.set(pos_key, pos_data)

            logger.info(
                '{"event": "FILL_RELAYED", "ib_order_id": "%s", "bot_ref": "%s", '
                '"symbol": "%s", "qty": "%s", "price": "%s"}',
                ib_order_id, bot_ref, symbol, qty_filled, avg_price,
            )
        except Exception:
            logger.exception('{"event": "FILL_RELAY_ERROR", "ib_order_id": "%s"}', ib_order_id)

    # --- Global status callback ---
    async def on_status(ib_order_id: str, status: str) -> None:
        """Handle order status change from IB — publish to Redis stream."""
        try:
            order_ref_str = ""
            if hasattr(ctx.ib, '_active_trades'):
                trade = ctx.ib._active_trades.get(ib_order_id)
                if trade and hasattr(trade, 'order') and hasattr(trade.order, 'orderRef'):
                    order_ref_str = trade.order.orderRef or ""

            ref = decode_ref(order_ref_str)
            bot_ref = ref.bot_ref if ref else "unknown"

            writer = StreamWriter(redis, StreamNames.fill(bot_ref), maxlen=500)
            await writer.add({
                "type": "STATUS",
                "ib_order_id": ib_order_id,
                "orderRef": order_ref_str,
                "status": status,
                "ts": datetime.now(timezone.utc).isoformat(),
            })

            # On cancellation, update position state
            if ref and status in ("Cancelled", "Inactive", "ApiCancelled"):
                pos_key = StateKeys.position(ref.bot_ref, ref.symbol)
                existing = await state.get(pos_key)
                if existing and existing.get("state") in ("ENTERING", "EXITING"):
                    # Revert: ENTERING → FLAT, EXITING → OPEN
                    new_state = "FLAT" if existing["state"] == "ENTERING" else "OPEN"
                    existing["state"] = new_state
                    existing["updated_at"] = datetime.now(timezone.utc).isoformat()
                    await state.set(pos_key, existing)

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
    """Process a positionEvent from IB and publish to Redis."""
    from decimal import Decimal
    from datetime import datetime, timezone
    from ib_trader.redis.streams import StreamWriter, StreamNames
    from ib_trader.redis.state import StateStore, StateKeys

    redis = ctx.redis
    if redis is None:
        return

    if sem:
        await sem.acquire()
    try:
        symbol = position.contract.symbol
        qty = Decimal(str(position.position))
        avg_price = Decimal(str(position.avgCost))
        con_id = position.contract.conId
        sec_type = position.contract.secType

        # Update the ibpos key for the API positions endpoint
        state = StateStore(redis)
        ibpos_key = f"ibpos:{symbol}:{sec_type}:{con_id}"
        if qty == 0:
            await state.delete(ibpos_key)
        else:
            await state.set(ibpos_key, {
                "symbol": symbol,
                "sec_type": sec_type,
                "quantity": str(qty),
                "avg_cost": str(avg_price),
                "con_id": con_id,
                "account": position.account,
                "broker": "ib",
                "updated_at": datetime.now(timezone.utc).isoformat(),
            })

        # Publish position change to stream
        writer = StreamWriter(redis, StreamNames.position_changes(), maxlen=1000)
        await writer.add({
            "symbol": symbol,
            "qty": str(qty),
            "avg_price": str(avg_price),
            "con_id": position.contract.conId,
            "account": position.account,
            "ts": datetime.now(timezone.utc).isoformat(),
        })

        # If position went to zero, find the specific bot that owned it.
        # Only flatten the FIRST matching key to avoid multi-bot collision
        # when two bots trade the same symbol.
        if qty == 0:
            state = StateStore(redis)
            async for key in redis.scan_iter(match=f"pos:*:{symbol}"):
                current = await state.get(key)
                if current and current.get("state") in ("OPEN", "EXITING"):
                    bot_ref = key.split(":")[1]
                    prev_state = current["state"]
                    current["state"] = "FLAT"
                    current["qty"] = "0"
                    current["updated_at"] = datetime.now(timezone.utc).isoformat()
                    await state.set(key, current)

                    # Notify the bot
                    fill_writer = StreamWriter(redis, StreamNames.fill(bot_ref), maxlen=500)
                    await fill_writer.add({
                        "type": "POSITION_CLOSED_EXTERNALLY",
                        "symbol": symbol,
                        "prev_state": prev_state,
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                    logger.info(
                        '{"event": "EXTERNAL_CLOSE_DETECTED", "symbol": "%s", "bot_ref": "%s"}',
                        symbol, bot_ref,
                    )
                    break  # Only flatten ONE bot's key per position close

    except Exception:
        logger.exception('{"event": "POSITION_EVENT_ERROR"}')
    finally:
        if sem:
            sem.release()


async def _tick_publisher_loop(ctx: AppContext) -> None:
    """Publish streaming tick data from IB to Redis.

    Reads from the IB client's in-memory ticker cache and publishes to
    Redis streams + keys. Runs every 200ms to balance freshness and load.

    This is a transitional approach — ideally we'd hook directly into
    ib_async's ticker update callback, but the current abstraction layer
    exposes get_ticker() as the read interface.
    """
    from datetime import datetime, timezone
    from ib_trader.redis.streams import StreamWriter, StreamNames
    from ib_trader.redis.state import StateStore, StateKeys

    redis = ctx.redis
    if redis is None:
        return

    state = StateStore(redis)

    # Track which symbols we've created writers for
    writers: dict[str, StreamWriter] = {}
    last_values: dict[str, tuple] = {}  # symbol → (bid, ask, last) to avoid duplicate publishes

    while True:
        try:
            if not hasattr(ctx.ib, '_streaming'):
                await asyncio.sleep(1)
                continue

            for con_id, entry in list(ctx.ib._streaming.items()):
                ticker = ctx.ib.get_ticker(con_id)
                if ticker is None:
                    continue

                symbol = entry.get("contract", None)
                if symbol and hasattr(symbol, "symbol"):
                    symbol = symbol.symbol
                elif isinstance(entry.get("symbol"), str):
                    symbol = entry["symbol"]
                else:
                    continue

                bid = ticker.get("bid")
                ask = ticker.get("ask")
                last = ticker.get("last")

                # Skip if no price data at all
                if bid is None and ask is None and last is None:
                    continue

                # Skip if nothing has changed
                current = (bid, ask, last)
                if current == last_values.get(symbol):
                    continue
                last_values[symbol] = current

                if symbol not in writers:
                    writers[symbol] = StreamWriter(redis, StreamNames.quote(symbol), maxlen=5000)

                now = datetime.now(timezone.utc).isoformat()

                # Publish to stream
                await writers[symbol].add({
                    "bid": str(bid) if bid else None,
                    "ask": str(ask) if ask else None,
                    "last": str(last) if last else None,
                    "volume": ticker.get("volume"),
                    "ts": now,
                })

                # Update latest key with TTL
                await state.set(
                    StateKeys.quote_latest(symbol),
                    {
                        "bid": str(bid) if bid else None,
                        "ask": str(ask) if ask else None,
                        "last": str(last) if last else None,
                        "volume": ticker.get("volume"),
                        "ts": now,
                    },
                    ttl=StateKeys.QUOTE_TTL,
                )

        except Exception:
            logger.exception('{"event": "TICK_PUBLISHER_ERROR"}')

        await asyncio.sleep(0.2)  # 200ms — 5 publishes/second per symbol max


async def _heartbeat_loop(ctx: AppContext, pid: int) -> None:
    """Write ENGINE heartbeat to SQLite periodically."""
    interval = ctx.settings.get("heartbeat_interval_seconds", 30)
    while True:
        try:
            ctx.heartbeats.upsert("ENGINE", pid)
        except Exception:
            logger.exception('{"event": "HEARTBEAT_WRITE_FAILED"}')
        await asyncio.sleep(interval)


if __name__ == "__main__":
    main()
