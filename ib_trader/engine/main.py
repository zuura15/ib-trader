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

        # --- Redis setup ---
        redis_url = ctx.settings.get("redis_url", "redis://localhost:6379/0")
        try:
            from ib_trader.redis.client import get_redis
            redis = await get_redis(redis_url)
            ctx.redis = redis
            print("[ENGINE] Connected to Redis.")
        except Exception as e:
            logger.warning(
                '{"event": "REDIS_CONNECT_FAILED", "error": "%s"}', str(e),
            )
            print(f"[ENGINE] WARNING: Redis not available ({e}). Running without Redis.")
            redis = None

        # --- Start background tasks ---
        bg_tasks = [
            asyncio.create_task(_heartbeat_loop(ctx, pid)),
            # Legacy position/watchlist loops kept as dual-write safety net
            asyncio.create_task(_position_cache_loop(ctx)),
            asyncio.create_task(_watchlist_cache_loop(ctx)),
        ]

        # Redis-dependent tasks
        if redis:
            # Event relay: IB callbacks → Redis streams + keys
            bg_tasks.append(asyncio.create_task(_event_relay_loop(ctx)))

            # Tick publisher: streaming ticks → Redis
            bg_tasks.append(asyncio.create_task(_tick_publisher_loop(ctx)))

            # Reconciler: startup recovery + sanity checks
            from ib_trader.engine.reconciler import Reconciler
            reconciler = Reconciler(
                ctx.ib, redis,
                sanity_interval=ctx.settings.get("reconciler_sanity_interval", 60),
            )
            await reconciler.startup_reconcile()
            bg_tasks.append(asyncio.create_task(reconciler.run_sanity_loop()))
            print("[ENGINE] Reconciler started.")

            # Internal HTTP API
            from ib_trader.engine.internal_api import start_internal_api
            internal_port = ctx.settings.get("engine_internal_port", 8081)
            api_task = await start_internal_api(ctx, port=internal_port)
            bg_tasks.append(api_task)
            print(f"[ENGINE] Internal API on 127.0.0.1:{internal_port}")

        # Legacy engine loop (polls pending_commands) — kept for REPL/API
        # commands during dual-write transition. Will be removed in Phase 5.
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

            # Update position state key
            if ref:
                pos_key = StateKeys.position(bot_ref, symbol)
                new_state = "OPEN" if side == "B" else "FLAT"
                pos_data = {
                    "state": new_state,
                    "qty": str(qty_filled) if new_state == "OPEN" else "0",
                    "avg_price": str(avg_price),
                    "serial": serial,
                    "entry_price": str(avg_price) if side == "B" else None,
                    "entry_time": datetime.now(timezone.utc).isoformat() if side == "B" else None,
                    "updated_at": datetime.now(timezone.utc).isoformat(),
                }
                # Merge with existing data (keep fields like entry_price on sell)
                existing = await state.get(pos_key)
                if existing and side == "S":
                    pos_data["entry_price"] = existing.get("entry_price")
                    pos_data["entry_time"] = existing.get("entry_time")
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
    # Wire up positionEvent if the IB client supports it
    if hasattr(ctx.ib, '_ib'):
        ib_obj = ctx.ib._ib

        def on_position_event(position) -> None:
            """Handle position change from IB (any source, including manual TWS closes)."""
            asyncio.create_task(_handle_position_event(ctx, position))

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


async def _handle_position_event(ctx, position) -> None:
    """Process a positionEvent from IB and publish to Redis."""
    from decimal import Decimal
    from datetime import datetime, timezone
    from ib_trader.redis.streams import StreamWriter, StreamNames
    from ib_trader.redis.state import StateStore, StateKeys

    redis = ctx.redis
    if redis is None:
        return

    try:
        symbol = position.contract.symbol
        qty = Decimal(str(position.position))
        avg_price = Decimal(str(position.avgCost))

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

        # If position went to zero, check if any bot had this symbol open
        if qty == 0:
            state = StateStore(redis)
            async for key in redis.scan_iter(match=f"pos:*:{symbol}"):
                current = await state.get(key)
                if current and current.get("state") in ("OPEN", "EXITING"):
                    bot_ref = key.split(":")[1]
                    current["state"] = "FLAT"
                    current["qty"] = "0"
                    current["updated_at"] = datetime.now(timezone.utc).isoformat()
                    await state.set(key, current)

                    # Notify the bot
                    fill_writer = StreamWriter(redis, StreamNames.fill(bot_ref), maxlen=500)
                    await fill_writer.add({
                        "type": "POSITION_CLOSED_EXTERNALLY",
                        "symbol": symbol,
                        "prev_state": "OPEN",
                        "ts": datetime.now(timezone.utc).isoformat(),
                    })
                    logger.info(
                        '{"event": "EXTERNAL_CLOSE_DETECTED", "symbol": "%s", "bot_ref": "%s"}',
                        symbol, bot_ref,
                    )

    except Exception:
        logger.exception('{"event": "POSITION_EVENT_ERROR"}')


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


async def _position_cache_loop(ctx: AppContext) -> None:
    """Periodically fetch positions from IB, read live streaming prices via
    the abstraction layer, and write to run/positions.json.

    Uses ctx.ib.subscribe_market_data (ref-counted) so subscriptions are
    shared with the watchlist loop without collision.
    """
    import json as _json
    from decimal import Decimal

    positions_path = Path("run/positions.json")
    positions_path.parent.mkdir(parents=True, exist_ok=True)

    class _Enc(_json.JSONEncoder):
        def default(self, o):
            if isinstance(o, Decimal):
                return str(o)
            return super().default(o)

    # Get the raw ib_async IB object for positions() only (no market data calls)
    ib_obj = None
    if hasattr(ctx.ib, '_ib'):
        ib_obj = ctx.ib._ib

    if ib_obj is None:
        logger.warning('{"event": "POSITION_CACHE_NO_IB_OBJ"}')
        return

    # Track which con_ids we've subscribed for positions
    subscribed_con_ids: set[int] = set()

    await asyncio.sleep(5)

    while True:
        try:
            # Detect a dead Gateway eagerly. is_connected() flips false the
            # moment ib_async sees the socket close, BEFORE the next IB call
            # would hang or raise. Surface it as a CATASTROPHIC alert and
            # skip this iteration so we don't write stale positions.
            if hasattr(ctx.ib, "is_connected") and not ctx.ib.is_connected():
                _raise_ib_disconnect_alert(ctx)
                logger.error('{"event": "POSITION_CACHE_IB_DEAD"}')
                await asyncio.sleep(2)
                continue
            try:
                # Wrap in a timeout so a zombie Gateway (process dead but
                # socket still open) cannot hang the loop forever.
                await asyncio.wait_for(ib_obj.reqPositionsAsync(), timeout=10)
            except asyncio.TimeoutError:
                logger.error('{"event": "REQ_POSITIONS_TIMEOUT"}')
                _raise_ib_disconnect_alert(ctx)
                await asyncio.sleep(2)
                continue
            except (ConnectionError, OSError, BrokenPipeError) as e:
                logger.error(
                    '{"event": "REQ_POSITIONS_CONN_ERROR", "error": "%s"}',
                    str(e),
                )
                _raise_ib_disconnect_alert(ctx)
                await asyncio.sleep(2)
                continue
            except Exception:
                # Anything else: log loud and keep going. Promoted from
                # DEBUG so we don't lose visibility on real failures.
                logger.warning('{"event": "REQ_POSITIONS_FAILED"}', exc_info=True)
            raw_positions = ib_obj.positions()

            current_con_ids = set()
            for p in raw_positions:
                con_id = p.contract.conId
                current_con_ids.add(con_id)
                if con_id not in subscribed_con_ids:
                    try:
                        await ctx.ib.subscribe_market_data(con_id, p.contract.symbol)
                        subscribed_con_ids.add(con_id)
                    except Exception:
                        logger.debug(
                            '{"event": "POSITION_SUB_FAILED", "con_id": %d}', con_id,
                        )

            # Unsubscribe positions we no longer hold
            for gone_id in list(subscribed_con_ids - current_con_ids):
                await ctx.ib.unsubscribe_market_data(gone_id)
                subscribed_con_ids.discard(gone_id)

            # Build output using the abstraction layer's get_ticker
            positions = []
            for idx, p in enumerate(raw_positions):
                sym = p.contract.symbol
                sec = p.contract.secType
                con_id = p.contract.conId

                mkt_price = None
                ticker = ctx.ib.get_ticker(con_id)
                if ticker is not None:
                    bid = ticker.get("bid")
                    ask = ticker.get("ask")
                    last = ticker.get("last")
                    if bid and ask:
                        mkt_price = (bid + ask) / 2
                    elif last:
                        mkt_price = last

                positions.append({
                    "id": f"{sym}_{sec}_{idx}",
                    "account_id": p.account,
                    "symbol": sym,
                    "sec_type": sec,
                    "quantity": str(p.position),
                    "avg_cost": str(p.avgCost),
                    "market_price": f"{mkt_price:.4f}" if mkt_price is not None else None,
                    "broker": "ib",
                })

            tmp_path = positions_path.with_suffix(".tmp")
            tmp_path.write_text(_json.dumps(positions, cls=_Enc), encoding="utf-8")
            tmp_path.rename(positions_path)
            logger.debug('{"event": "POSITION_FILE_WRITTEN", "count": %d}', len(positions))

        except Exception:
            logger.exception('{"event": "POSITION_CACHE_ERROR"}')

        try:
            await asyncio.wait_for(position_refresh_event.wait(), timeout=2)
            position_refresh_event.clear()
        except asyncio.TimeoutError:
            pass


async def _watchlist_cache_loop(ctx: AppContext) -> None:
    """Stream market data for watchlist symbols and write to run/watchlist.json.

    Re-reads config/watchlist.yaml each cycle to pick up changes from the API.
    Uses ref-counted streaming subscriptions shared with the position cache loop.
    Paces new subscriptions (max 5 per cycle) to avoid IB rate limits.
    """
    import json as _json
    from datetime import datetime, timezone
    from decimal import Decimal

    from ib_trader.config.loader import load_watchlist

    watchlist_path = Path("run/watchlist.json")
    watchlist_path.parent.mkdir(parents=True, exist_ok=True)

    # symbol → con_id mapping for active subscriptions
    active: dict[str, int] = {}
    # symbol → {next_retry: float, attempts: int} for failed qualifications
    failed: dict[str, dict] = {}
    # symbol → float for cached previous close (fetched once via historical data)
    cached_close: dict[str, float] = {}
    # symbol → int count of consecutive cycles with no last price (stale detection)
    stale_cycles: dict[str, int] = {}
    # symbol → int count of consecutive cycles where ticker_time hasn't advanced
    time_stale_cycles: dict[str, int] = {}
    # symbol → last seen ticker_time for time-based stale detection
    last_ticker_time: dict[str, object] = {}

    _MAX_NEW_PER_CYCLE = 5
    _WATCHLIST_YAML = "config/watchlist.yaml"
    _CYCLE_SECONDS = 5
    _TIME_STALE_THRESHOLD = 12  # 12 cycles × 5s = 60s without a tick update

    prev_symbols: list[str] = []

    await asyncio.sleep(8)  # let positions loop start first
    logger.info('{"event": "WATCHLIST_LOOP_STARTED"}')

    while True:
        try:
            # Read current watchlist (fault-tolerant)
            symbols = load_watchlist(_WATCHLIST_YAML)
            if not symbols:
                symbols = prev_symbols  # retain previous on read failure
            else:
                if symbols != prev_symbols:
                    logger.info(
                        '{"event": "WATCHLIST_CONFIG_RELOADED", "count": %d}',
                        len(symbols),
                    )
                prev_symbols = symbols

            wanted = set(symbols)
            current = set(active.keys())

            # Unsubscribe removed symbols
            for sym in current - wanted:
                con_id = active.pop(sym)
                await ctx.ib.unsubscribe_market_data(con_id)
                stale_cycles.pop(sym, None)
                time_stale_cycles.pop(sym, None)
                last_ticker_time.pop(sym, None)
                cached_close.pop(sym, None)
                logger.info(
                    '{"event": "WATCHLIST_SUB_CANCELLED", "symbol": "%s"}', sym,
                )

            # Subscribe new symbols (paced)
            added = 0
            for sym in wanted - current:
                if added >= _MAX_NEW_PER_CYCLE:
                    break

                # Check retry backoff for previously failed symbols
                fail_info = failed.get(sym)
                if fail_info and asyncio.get_event_loop().time() < fail_info["next_retry"]:
                    continue

                try:
                    info = await ctx.ib.qualify_contract(sym)
                    con_id = info["con_id"]
                    await ctx.ib.subscribe_market_data(con_id, sym)
                    active[sym] = con_id
                    failed.pop(sym, None)
                    added += 1
                except Exception as e:
                    attempts = (fail_info["attempts"] + 1) if fail_info else 1
                    delay = min(30 * (2 ** (attempts - 1)), 300)  # 30s → 300s cap
                    failed[sym] = {
                        "next_retry": asyncio.get_event_loop().time() + delay,
                        "attempts": attempts,
                    }
                    logger.warning(
                        '{"event": "WATCHLIST_QUALIFY_FAILED", "symbol": "%s", '
                        '"attempt": %d, "next_retry_s": %d, "error": "%s"}',
                        sym, attempts, delay, str(e),
                    )

            # Build watchlist JSON
            now = datetime.now(timezone.utc).isoformat()
            items = []
            for sym in symbols:
                con_id = active.get(sym)
                if con_id is None:
                    # Not yet subscribed or failed
                    fail_info = failed.get(sym)
                    items.append({
                        "symbol": sym,
                        "last": None, "change": None, "change_pct": None,
                        "volume": None, "avg_volume": None,
                        "high": None, "low": None,
                        "high_52w": None, "low_52w": None,
                        "error": "qualification_failed" if fail_info and fail_info["attempts"] >= 5 else None,
                    })
                    continue

                ticker = ctx.ib.get_ticker(con_id)

                # Detect stale tickers via two strategies:
                # 1. last is None for 3+ consecutive cycles (original check)
                # 2. ticker_time hasn't advanced for _TIME_STALE_THRESHOLD
                #    cycles — catches tickers that return cached non-None
                #    values after IB stops pushing updates (GLD, ETFs).
                t_last = ticker.get("last") if ticker else None
                needs_resub = False

                if t_last is None:
                    stale_cycles[sym] = stale_cycles.get(sym, 0) + 1
                    if stale_cycles[sym] >= 3:
                        needs_resub = True
                else:
                    stale_cycles.pop(sym, None)

                # Time-based staleness: if ticker_time stops advancing,
                # the Ticker object is returning cached data.
                t_time = ticker.get("ticker_time") if ticker else None
                if t_time is not None:
                    prev_time = last_ticker_time.get(sym)
                    if prev_time is not None and t_time == prev_time:
                        time_stale_cycles[sym] = time_stale_cycles.get(sym, 0) + 1
                        if time_stale_cycles[sym] >= _TIME_STALE_THRESHOLD:
                            needs_resub = True
                    else:
                        time_stale_cycles.pop(sym, None)
                    last_ticker_time[sym] = t_time

                if needs_resub and con_id in active:
                    reason = "no_last" if t_last is None else "time_frozen"
                    logger.warning(
                        '{"event": "WATCHLIST_STALE_RESUB", "symbol": "%s", '
                        '"reason": "%s", "null_cycles": %d, "time_cycles": %d}',
                        sym, reason,
                        stale_cycles.get(sym, 0),
                        time_stale_cycles.get(sym, 0),
                    )
                    await ctx.ib.unsubscribe_market_data(con_id)
                    await ctx.ib.subscribe_market_data(con_id, sym)
                    stale_cycles.pop(sym, None)
                    time_stale_cycles.pop(sym, None)
                    last_ticker_time.pop(sym, None)

                if ticker is None:
                    items.append({
                        "symbol": sym,
                        "last": None, "change": None, "change_pct": None,
                        "volume": None, "avg_volume": None,
                        "high": None, "low": None,
                        "high_52w": None, "low_52w": None,
                        "error": None,
                    })
                    continue

                last = ticker.get("last")
                close = ticker.get("close")

                # IB often doesn't provide previous close for ETFs outside
                # regular hours. Fall back to a one-time historical lookup.
                if close is None and last is not None and sym not in cached_close:
                    try:
                        snap = await ctx.ib.get_market_snapshot(con_id)
                        ref = float(snap.get("last", 0) or 0)
                        if ref > 0:
                            cached_close[sym] = ref
                            logger.debug(
                                '{"event": "WATCHLIST_CLOSE_FETCHED", "symbol": "%s", "close": %s}',
                                sym, ref,
                            )
                    except Exception:
                        pass
                if close is None:
                    close = cached_close.get(sym)

                change = None
                change_pct = None
                if last is not None and close is not None and close > 0:
                    change = round(last - close, 4)
                    change_pct = round((change / close) * 100, 2)

                def _fmt(v):
                    return str(v) if v is not None else None

                def _fmt_int(v):
                    return str(int(v)) if v is not None else None

                items.append({
                    "symbol": sym,
                    "last": _fmt(last),
                    "change": _fmt(change),
                    "change_pct": _fmt(change_pct),
                    "volume": _fmt_int(ticker.get("volume")),
                    "avg_volume": _fmt_int(ticker.get("avg_volume")),
                    "high": _fmt(ticker.get("high")),
                    "low": _fmt(ticker.get("low")),
                    "high_52w": _fmt(ticker.get("high_52w")),
                    "low_52w": _fmt(ticker.get("low_52w")),
                    "error": None,
                })

            output = {"generated_at": now, "items": items}
            tmp = watchlist_path.with_suffix(".tmp")
            tmp.write_text(_json.dumps(output), encoding="utf-8")
            tmp.rename(watchlist_path)
            logger.debug('{"event": "WATCHLIST_FILE_WRITTEN", "count": %d}', len(items))

        except Exception:
            logger.exception('{"event": "WATCHLIST_CACHE_ERROR"}')

        await asyncio.sleep(_CYCLE_SECONDS)


if __name__ == "__main__":
    main()
