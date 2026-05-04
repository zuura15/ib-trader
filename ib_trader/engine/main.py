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
    ib-engine                      # auto-detect paper vs live from Gateway
    ib-engine --force-mode live    # assert the Gateway must be live, else exit
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
from ib_trader.data.repositories.bot_trade_repository import BotTradeRepository
from ib_trader.data.repositories.template_repository import OrderTemplateRepository
from ib_trader.engine.tracker import OrderTracker
from ib_trader.logging_.logger import setup_logging

logger = logging.getLogger(__name__)

# Retain references to fire-and-forget asyncio tasks so the loop's weakref
# collection doesn't cancel them mid-flight. See Python docs on create_task.
_background_tasks: set[asyncio.Task] = set()


def _spawn_background(coro) -> asyncio.Task:
    """Create an asyncio task, track it, and auto-discard on completion."""
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


@click.command()
@click.option("--db", default="trader.db", help="SQLite database path")
@click.option("--env", default=".env", help="Environment file path")
@click.option("--settings", "settings_path", default="config/settings.yaml",
              help="Settings YAML path")
@click.option("--symbols", "symbols_path", default="config/symbols.yaml",
              help="Symbols whitelist path")
@click.option(
    "--force-mode",
    type=click.Choice(["paper", "live"]),
    default=None,
    help=(
        "Assert the detected Gateway mode must match this value. "
        "Without this flag the engine auto-detects from the Gateway's "
        "managedAccounts (DU*=paper, else live)."
    ),
)
def main(db: str, env: str, settings_path: str, symbols_path: str,
         force_mode: str | None):
    """IB Trader Engine Service — central command execution loop."""
    setup_logging()

    # Load configuration (same pattern as REPL and daemon)
    env_vars = load_env(env)
    settings = load_settings(settings_path)
    symbols = load_symbols(symbols_path)

    settings["ib_host"] = env_vars.get("IB_HOST", settings.get("ib_host", "127.0.0.1"))
    settings["ib_client_id"] = int(env_vars.get("IB_CLIENT_ID", 1))

    # Probe the Gateway and detect paper/live from managedAccounts before
    # the engine enters its main loop. This sets settings["ib_port"],
    # settings["ib_market_data_type"], settings["account_mode"], and
    # returns the account_id to trade under.
    from ib_trader.ib.gateway_probe import (
        load_candidates, probe_gateway, pick_account, pick_market_data_type,
    )
    candidates = load_candidates(settings)
    probe_timeout = float(settings.get("ib_probe_timeout", 2.0))
    try:
        result = asyncio.run(probe_gateway(
            settings["ib_host"], candidates, settings["ib_client_id"],
            timeout=probe_timeout,
        ))
    except RuntimeError as e:
        raise SystemExit(str(e)) from e

    if force_mode and result.mode != force_mode:
        raise SystemExit(
            f"--force-mode={force_mode!r} but Gateway reports mode={result.mode!r} "
            f"(accounts={result.accounts}). Refusing to start."
        )

    account_id = pick_account(result.mode, env_vars, result.accounts)
    settings["ib_port"] = result.port
    settings["ib_market_data_type"] = pick_market_data_type(result.mode, env_vars, settings)
    settings["account_mode"] = result.mode
    logger.info(
        '{"event": "ENGINE_MODE_DETECTED", "mode": "%s", "port": %d, '
        '"label": "%s", "account_id": "%s"}',
        result.mode, result.port, result.label, account_id,
    )
    print(
        f"[ENGINE] Detected {result.mode} mode on {result.label} "
        f"(port {result.port}), account {account_id}."
    )

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
        bot_trades=BotTradeRepository(session_factory),
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
        # The connect callback auto-resolves the alert on reconnect.
        if hasattr(ctx.ib, "set_disconnect_callback"):
            ctx.ib.set_disconnect_callback(lambda: _raise_ib_disconnect_alert(ctx))
        if hasattr(ctx.ib, "set_connect_callback"):
            ctx.ib.set_connect_callback(lambda: _resolve_ib_disconnect_alert(ctx))

        # Fail fast if the configured account_id isn't one the Gateway
        # can actually trade. IB authenticates at the session level, so
        # a connect() success alone doesn't confirm we can place orders
        # on the account we think we're using.
        _validate_account_id(ctx)

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

        # --- Publish engine session metadata ---
        # /api/status reads this so the UI header reports what the engine
        # is actually connected to (paper vs live) rather than parsing
        # .env. The account_mode flag was set from the CLI --paper/--live
        # choice; account_id was picked from the matching env key.
        from datetime import datetime as _dt, timezone as _tz
        from ib_trader.redis.state import StateStore, StateKeys
        _store = StateStore(redis)
        await _store.set(StateKeys.engine_session(), {
            "account_id": ctx.account_id,
            "account_mode": ctx.settings.get("account_mode", "unknown"),
            "port": ctx.settings.get("ib_port"),
            "host": ctx.settings.get("ib_host"),
            "connected_at": _dt.now(_tz.utc).isoformat(),
        })
        logger.info(
            '{"event": "ENGINE_SESSION_PUBLISHED", "account_mode": "%s", '
            '"port": %s, "account_id": "%s"}',
            ctx.settings.get("account_mode"), ctx.settings.get("ib_port"),
            ctx.account_id,
        )

        # --- Subscribe to watchlist symbols for tick publishing ---
        from ib_trader.config.loader import load_watchlist
        from ib_trader.repl.commands import _is_futures_local_symbol
        watchlist_symbols = load_watchlist("config/watchlist.yaml")
        watchlist_subscribed = 0
        for sym in watchlist_symbols:
            try:
                # Detect IB-paste FUT form (e.g. ``MESM6``) so MES futures
                # in the watchlist qualify as FUT instead of failing the
                # default STK qualify path.
                sec_type = "FUT" if _is_futures_local_symbol(sym) else "STK"
                info = await ctx.ib.qualify_contract(sym, sec_type=sec_type)
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
        except Exception as e:
            logger.debug("IB disconnect failed during cleanup", exc_info=e)
        try:
            from ib_trader.redis.client import close_redis
            await close_redis()
        except Exception as e:
            logger.debug("redis close failed during cleanup", exc_info=e)
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


def _validate_account_id(ctx: AppContext) -> None:
    """Fail fast if the configured account_id isn't in the Gateway's
    managed-accounts list.

    IB Gateway authenticates per-session (the user logs in through the
    Gateway UI). A successful connect() proves the Gateway is reachable
    but says nothing about whether our configured account_id matches.
    If it doesn't, orders get rejected on submission — sometimes with
    a useful error, sometimes silently routed to the logged-in account.
    Either way it's a footgun. Catch it here instead.
    """
    try:
        managed = list(ctx.ib.managed_accounts())
    except Exception as e:
        logger.error(
            '{"event": "MANAGED_ACCOUNTS_READ_FAILED", "error": "%s"}', str(e),
        )
        raise SystemExit(
            f"Could not read managedAccounts from IB: {e}. Refusing to start."
        ) from e
    if not managed:
        raise SystemExit(
            "Gateway reported no managed accounts. Check that Gateway is "
            "logged in and the API client has account permissions."
        )
    if ctx.account_id not in managed:
        logger.error(
            '{"event": "ACCOUNT_ID_MISMATCH", "configured": "%s", '
            '"gateway_managed": %s}',
            ctx.account_id, json.dumps(managed),
        )
        raise SystemExit(
            f"Configured account_id {ctx.account_id!r} is NOT in the "
            f"Gateway's managed accounts {managed}. Fix IB_ACCOUNT_ID / "
            f"IB_ACCOUNT_ID_PAPER in .env or switch Gateway accounts. "
            f"Refusing to start — orders would otherwise be silently "
            f"rejected or mis-routed."
        )
    logger.info(
        '{"event": "ACCOUNT_ID_VALIDATED", "account_id": "%s", '
        '"managed_accounts": %s}',
        ctx.account_id, json.dumps(managed),
    )


def _raise_ib_disconnect_alert(ctx: AppContext) -> None:
    """Publish a CATASTROPHIC alert to Redis when the IB Gateway drops.

    Called from the ib_async event-loop callback in InsyncClient. Must be
    fast and non-blocking. Uses ``fire_and_forget_alert`` so the alert
    lands in ``alerts:active`` (where ``/api/alerts`` reads from and
    where the UI CatastrophicOverlay triggers off). The prior version
    wrote to SQLite via ``ctx.alerts.create()``, which the UI never saw
    — that's why the Gateway dropping silently on 2026-04-22 never
    surfaced in the header.

    Dedupe: skip if a live IB_GATEWAY_DISCONNECTED is already present in
    Redis. A flapping connection → one active alert, not a pile.
    """
    from ib_trader.logging_.alerts import fire_and_forget_alert
    redis = ctx.redis

    async def _dedupe_and_fire():
        import json as _json
        try:
            from ib_trader.redis.state import StateKeys as _SK
            active = await redis.hgetall(_SK.alerts_active()) if redis else {}
        except Exception:
            logger.exception('{"event": "IB_DISCONNECT_ALERT_DEDUPE_FAILED"}')
            active = {}
        for _aid, raw in (active or {}).items():
            try:
                if _json.loads(raw).get("trigger") == "IB_GATEWAY_DISCONNECTED":
                    return
            except (TypeError, ValueError):
                continue
        fire_and_forget_alert(
            redis=redis,
            trigger="IB_GATEWAY_DISCONNECTED",
            severity="CATASTROPHIC",
            message=(
                "IB Gateway connection lost. The engine cannot place or "
                "track orders. Restart IB Gateway / TWS to reconnect."
            ),
        )
        logger.error(
            '{"event": "SYSTEM_ALERT_RAISED", "severity": "CATASTROPHIC", '
            '"trigger": "IB_GATEWAY_DISCONNECTED"}'
        )

    if redis is None:
        # Early-boot path (no Redis yet). Log at ERROR only — the engine
        # hasn't connected to Redis so the UI can't render anything anyway.
        logger.error(
            '{"event": "SYSTEM_ALERT_RAISED", "severity": "CATASTROPHIC", '
            '"trigger": "IB_GATEWAY_DISCONNECTED", '
            '"note": "redis unavailable; UI signal skipped"}'
        )
        return
    _spawn_background(_dedupe_and_fire())


def _resolve_ib_disconnect_alert(ctx: AppContext) -> None:
    """Auto-resolve any live IB_GATEWAY_DISCONNECTED alerts on reconnect.

    Called from the ib_async connectedEvent via InsyncClient. Removes the
    alert from ``alerts:active`` and nudges WS consumers so the UI clears
    the CATASTROPHIC banner immediately. Stale SQLite rows are left as-is
    (archival only; the UI never reads them)."""
    redis = ctx.redis
    if redis is None:
        return

    async def _resolve():
        import json as _json
        from ib_trader.redis.state import StateKeys as _SK
        try:
            active = await redis.hgetall(_SK.alerts_active())
        except Exception:
            logger.exception('{"event": "IB_CONNECT_ALERT_RESOLVE_FAILED"}')
            return
        to_remove: list[str] = []
        for aid, raw in (active or {}).items():
            try:
                if _json.loads(raw).get("trigger") == "IB_GATEWAY_DISCONNECTED":
                    to_remove.append(aid)
            except (TypeError, ValueError):
                continue
        if not to_remove:
            return
        try:
            await redis.hdel(_SK.alerts_active(), *to_remove)
            from ib_trader.redis.streams import publish_activity
            await publish_activity(redis, "alerts")
            logger.info(
                '{"event": "SYSTEM_ALERT_RESOLVED", '
                '"trigger": "IB_GATEWAY_DISCONNECTED", "count": %d}',
                len(to_remove),
            )
        except Exception:
            logger.exception('{"event": "IB_CONNECT_ALERT_RESOLVE_FAILED"}')

    _spawn_background(_resolve())


async def _refresh_positions_cache(ctx: AppContext, *, subscribe_mktdata: bool = False) -> int:
    """Fetch current IB positions into the in-memory cache.

    Returns the count of non-zero positions. On first call (startup) set
    ``subscribe_mktdata=True`` to subscribe to quotes for position symbols.
    """
    from decimal import Decimal
    from datetime import datetime, timezone
    import asyncio as _asyncio

    try:
        # Bumped to 30s — after FUT requalifies and FUT market-data
        # subscribes started piling up on startup, IB sometimes takes
        # longer than 10s to send the reqPositions END marker. The
        # cache stays fresh from positionEvent in the meantime, so a
        # late END is cosmetic.
        await ctx.ib.req_positions_async(timeout=30)
    except _asyncio.TimeoutError:
        # positionEvent keeps the cache live; the next 30s refresh
        # tick will retry. Demote to a warning so the operator sees
        # it without the full stack trace from the raised exception.
        logger.warning(
            '{"event": "POSITION_REFRESH_TIMEOUT", "note": '
            '"reqPositions END not received in 30s; positionEvent stream '
            'continues to keep the cache live"}',
        )
        return len(ctx.positions_cache)
    except Exception:
        logger.exception('{"event": "POSITION_REFRESH_FAILED"}')
        return len(ctx.positions_cache)

    positions = []
    for p in ctx.ib.get_raw_positions():
        sym = p.contract.symbol
        qty = Decimal(str(p.position))
        if qty == 0:
            continue
        avg_cost_raw = Decimal(str(p.avgCost))
        con_id = p.contract.conId
        sec_type = p.contract.secType

        # Seed the wrapper's contract cache. For FUT, re-qualify via
        # localSymbol so the cached contract has a populated exchange
        # field — IB's reqPositions hands back FUT contracts with empty
        # exchange (positions are account-wide, not exchange-specific),
        # which then makes reqMktData reject with code 321 ("Please
        # enter exchange"). STK position contracts arrive with
        # exchange=SMART already, so they go straight into the cache.
        try:
            if con_id not in ctx.ib._contract_cache:
                if sec_type == "FUT":
                    local_sym = getattr(p.contract, "localSymbol", "") or ""
                    if local_sym:
                        try:
                            await ctx.ib.qualify_contract(local_sym, sec_type="FUT")
                        except Exception as e:
                            logger.debug(
                                "FUT requalify-by-localSymbol failed for %s",
                                local_sym, exc_info=e,
                            )
                if con_id not in ctx.ib._contract_cache:
                    ctx.ib._contract_cache[con_id] = p.contract
        except Exception as e:
            logger.debug("seed contract cache failed", exc_info=e)
        expiry = getattr(p.contract, "lastTradeDateOrContractMonth", None) or None
        trading_class = getattr(p.contract, "tradingClass", None) or None
        multiplier = getattr(p.contract, "multiplier", None) or None

        # IB's `Position.avgCost` for FUT is the total cost including
        # the contract multiplier (e.g. MES filled at 6000 reports
        # avgCost=30000 because multiplier=5). Normalize to the per-unit
        # price so the UI value lines up directly with chart prices and
        # IB's order-ticket display. STK keeps avgCost as-is (multiplier
        # = 1 there).
        if sec_type == "FUT" and multiplier:
            try:
                avg_cost = avg_cost_raw / Decimal(str(multiplier))
            except Exception:
                avg_cost = avg_cost_raw
        else:
            avg_cost = avg_cost_raw

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
        except Exception as e:
            logger.debug("ticker price enrichment failed", exc_info=e)

        display_symbol = sym
        if sec_type == "FUT" and expiry:
            try:
                from ib_trader.utils.symbol import format_display_symbol
                display_symbol = format_display_symbol(sym, "FUT", expiry)
            except Exception:
                display_symbol = f"{sym} {expiry}"

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
            # Epic 1 additions
            "expiry": expiry,
            "trading_class": trading_class,
            "multiplier": multiplier,
            "display_symbol": display_symbol,
        })

        if subscribe_mktdata and sec_type in ("STK", "FUT"):
            try:
                await ctx.ib.subscribe_market_data(con_id, sym)
            except Exception as e:
                logger.debug("market data subscribe failed for %s", sym, exc_info=e)

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
    timeout = float(ctx.settings.get("order_terminal_timeout_seconds", 300))
    while True:
        await asyncio.sleep(interval)
        try:
            count = await _refresh_positions_cache(ctx)
            # Watchdog — surface orders that IB has not terminalized
            # within the timeout as a user-acknowledgeable panic alert.
            # The ledger itself never self-derives a terminal; that's
            # IB's job. This loop is the escalation path when IB stalls.
            if hasattr(ctx, '_order_ledger'):
                stuck = ctx._order_ledger.check_stuck(timeout_seconds=timeout)
                for entry in stuck:
                    _alert_stuck_order(ctx, entry, timeout)
            logger.debug('{"event": "POSITION_POLL", "count": %d}', count)
        except Exception:
            logger.exception('{"event": "POSITION_POLL_ERROR"}')


def _alert_stuck_order(ctx: AppContext, entry, timeout_seconds: float) -> None:
    """Fire a WARNING alert for an order IB hasn't terminalized in time."""
    import time
    from ib_trader.logging_.alerts import fire_and_forget_alert

    age_s = int(time.monotonic() - entry.created_at)
    msg = (
        f"IB has not sent a terminal status for order {entry.ib_order_id} "
        f"({entry.symbol} {entry.side}, filled {entry.filled_qty}/"
        f"{entry.target_qty}) after {age_s}s. Reconcile manually — the "
        f"ledger will not self-terminate."
    )
    fire_and_forget_alert(
        redis=ctx.redis,
        trigger="ORDER_TERMINAL_TIMEOUT",
        message=msg,
        severity="WARNING",
        symbol=entry.symbol,
        ib_order_id=entry.ib_order_id,
        extra={
            "order_ref": entry.order_ref,
            "side": entry.side,
            "target_qty": str(entry.target_qty),
            "filled_qty": str(entry.filled_qty),
            "last_status": entry.last_status,
            "age_seconds": age_s,
            "timeout_seconds": int(timeout_seconds),
        },
    )


async def _event_relay_loop(ctx: AppContext) -> None:
    """Relay IB fill/status/position events to Redis streams and keys.

    Registers global callbacks on the IB client. When IB fires an event,
    the callback publishes to the appropriate Redis stream and updates
    the Redis position state key.

    This is the PRIMARY update path — the reconciler is just a safety net.
    """
    from decimal import Decimal
    from ib_trader.redis.streams import StreamWriter, StreamNames
    from ib_trader.redis.state import StateKeys
    from ib_trader.engine.order_ledger import OrderLedger

    redis = ctx.redis
    if redis is None:
        return

    def _live_position_qty(symbol: str, sec_type: str = "STK") -> Decimal:
        """Look up the broker's current net position for ``symbol`` from
        ib_async's in-memory state. Used by the order ledger at terminal
        time to reconcile against the pre-place snapshot when our tracked
        fills fall short of the order target."""
        for p in ctx.ib.get_raw_positions():
            if (
                getattr(p.contract, 'symbol', None) == symbol
                and getattr(p.contract, 'secType', 'STK') == sec_type
            ):
                return Decimal(str(p.position))
        return Decimal("0")

    ledger = OrderLedger(position_getter=_live_position_qty)
    # Expose on ctx so the position poll loop can sweep stale entries
    ctx._order_ledger = ledger

    def on_order_placed(
        ib_order_id: str, symbol: str, sec_type: str, con_id: int,
        side: str, qty: Decimal, order_ref: str,
    ) -> None:
        """Fired synchronously by insync_client right after every place_*
        returns. Snapshots the broker's net position *before* any fill
        events can run (asyncio is single-threaded; no fill callback can
        slip in between placeOrder() and this dispatch), then registers
        the order with the ledger including the snapshot. The ledger uses
        the snapshot at terminal-emit time to reconcile any fills IB
        dropped during venue re-routes against the broker-truth diff."""
        try:
            pre_qty = _live_position_qty(symbol, sec_type)
            ledger.register(
                ib_order_id=ib_order_id,
                order_ref=order_ref,
                symbol=symbol,
                sec_type=sec_type,
                con_id=con_id,
                side=side,
                target_qty=Decimal(str(qty)),
                pre_position=pre_qty,
            )
        except Exception:
            logger.exception(
                '{"event": "LEDGER_REGISTER_FAILED", "ib_order_id": "%s"}',
                ib_order_id,
            )

    if hasattr(ctx.ib, "register_order_placed_callback"):
        ctx.ib.register_order_placed_callback(on_order_placed)
    order_writer = StreamWriter(redis, StreamNames.order_updates(), maxlen=5000)
    orders_open_key = StateKeys.orders_open()

    async def _update_orders_open(events: list[dict]) -> None:
        """Maintain the orders:open Redis hash from emitted events.

        Non-terminal events upsert; terminal events remove. Epic 1:
        non-terminal events are enriched with expiry/trading_class/
        multiplier/display_symbol pulled from the active IB Trade so the
        Orders panel can render futures natively.
        """
        for evt in events:
            oid = evt.get("ib_order_id")
            if not oid:
                continue
            if evt.get("terminal"):
                await redis.hdel(orders_open_key, oid)
            else:
                import json as _json
                enriched = dict(evt)
                meta = ctx.ib.get_trade_meta(str(oid))
                if meta is not None and meta.get("contract") is not None:
                    c = meta["contract"]
                    expiry = getattr(c, "lastTradeDateOrContractMonth", None) or None
                    trading_class = getattr(c, "tradingClass", None) or None
                    multiplier = getattr(c, "multiplier", None) or None
                    sym = meta["symbol"]
                    sec_type_c = meta["sec_type"]
                    enriched.setdefault("expiry", expiry)
                    enriched.setdefault("trading_class", trading_class)
                    enriched.setdefault("multiplier", multiplier)
                    display = sym
                    if sec_type_c == "FUT" and expiry:
                        try:
                            from ib_trader.utils.symbol import format_display_symbol
                            display = format_display_symbol(sym, "FUT", expiry)
                        except Exception:
                            display = f"{sym} {expiry}"
                    enriched.setdefault("display_symbol", display)
                await redis.hset(orders_open_key, oid, _json.dumps(enriched))

    def _get_trade_meta(ib_order_id: str) -> tuple[str, str, str, int, str]:
        """Extract (orderRef, symbol, sec_type, con_id, side) from the active trade."""
        meta = ctx.ib.get_trade_meta(ib_order_id)
        if meta is None:
            return "", "", "STK", 0, ""
        return (
            meta["order_ref"],
            meta["symbol"],
            meta["sec_type"],
            meta["con_id"],
            meta["side"],
        )

    # --- Global fill callback ---
    async def on_fill(ib_order_id: str, qty_filled: Decimal,
                      avg_price: Decimal, commission: Decimal) -> None:
        """Handle fill from IB — record in ledger, publish to order:updates."""
        try:
            order_ref, symbol, sec_type, con_id, side = _get_trade_meta(ib_order_id)

            # Read the ORIGINAL order total qty (not the live-updated
            # remaining) so the ledger can compute target_qty correctly
            # even when multiple execDetailsEvents fire in quick
            # succession. Using `qty + orderStatus.remaining` races
            # because IB updates orderStatus.remaining to 0 for ALL
            # fills of a SMART-split order before any on_fill task gets
            # to read it — which caused the ledger to evict the entry
            # mid-order and emit a second terminal event with only the
            # second fill's per-fill qty. totalQuantity is static once
            # the order is placed, so it's always correct.
            meta = ctx.ib.get_trade_meta(ib_order_id)
            total_qty = meta["total_qty"] if meta else Decimal("-1")
            remaining = meta["remaining"] if meta else Decimal("-1")

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
                total_qty=total_qty,
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

    # --- Global commission callback ---
    # IB delivers CommissionReport on a separate event that fires
    # shortly after execDetails but often BEFORE the engine's own
    # _handle_fill coroutine has written the FILLED TransactionEvent
    # row (observed ~14 ms skew in live PSQ trades). Naively calling
    # add_commission at that moment finds 0 rows and loses the value.
    # Retry with small backoff — the row almost always shows up
    # within a few hundred ms. Non-gating: this runs as its own
    # background task; bot FSM flow never waits for it.
    async def on_commission(
        ib_order_id: str, exec_id: str, commission: Decimal,
        realized_pnl: Decimal | None = None,
    ) -> None:
        if (commission is None or commission == 0) and (
            realized_pnl is None or realized_pnl == 0
        ):
            return
        try:
            order_id_int = int(ib_order_id)
        except (TypeError, ValueError):
            return
        try:
            txn_rows = 0
            # Backoff: 50, 100, 200, 400, 800, 1600 ms (~3.15 s total).
            # Covers the normal 10–50 ms race and tolerates a stalled
            # DB write without exhausting the event loop.
            delays = [0.05, 0.1, 0.2, 0.4, 0.8, 1.6]
            if commission and commission != 0:
                for delay in [0.0, *delays]:
                    if delay:
                        await asyncio.sleep(delay)
                    txn_rows = ctx.transactions.add_commission(order_id_int, commission)
                    if txn_rows > 0:
                        break
                if txn_rows == 0:
                    logger.warning(
                        '{"event": "COMMISSION_UNMATCHED", "ib_order_id": "%s", '
                        '"exec_id": "%s", "commission": "%s", "reason": '
                        '"no_FILLED_transaction_row_after_retry"}',
                        ib_order_id, exec_id, commission,
                    )
                    return
            # Resolve trade_serial + trade_id via any transaction row for
            # this order. Used both for bot-trade commission and for
            # writing IB-authoritative realized P&L on the trade group.
            trade_serial = None
            trade_id = None
            try:
                from ib_trader.data.models import (
                    TransactionEvent, TransactionAction,
                )
                s = ctx.transactions._session_factory()
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
                if ev is not None:
                    trade_serial = ev.trade_serial
                    trade_id = ev.trade_id
            except Exception:
                logger.debug("commission serial lookup failed", exc_info=True)
            bt_rows = 0
            if trade_serial is not None and ctx.bot_trades is not None and commission:
                bt_rows = ctx.bot_trades.add_commission_by_serial(
                    trade_serial, commission,
                )
            # Write IB's authoritative realized P&L (additive across
            # multi-execution closes; opening fills filtered upstream).
            ib_pnl_written = False
            if (
                realized_pnl is not None and realized_pnl != 0
                and trade_id is not None
            ):
                try:
                    ctx.trades.add_ib_realized_pnl(trade_id, realized_pnl)
                    ib_pnl_written = True
                except Exception:
                    logger.exception(
                        '{"event": "IB_REALIZED_PNL_WRITE_FAILED", '
                        '"ib_order_id": "%s", "trade_id": "%s"}',
                        ib_order_id, trade_id,
                    )
            logger.info(
                '{"event": "COMMISSION_APPLIED", "ib_order_id": "%s", '
                '"exec_id": "%s", "commission": "%s", '
                '"txn_rows": %d, "bot_trade_rows": %d, '
                '"ib_realized_pnl": "%s", "ib_pnl_written": %s}',
                ib_order_id, exec_id, commission, txn_rows, bt_rows,
                realized_pnl if realized_pnl is not None else "",
                "true" if ib_pnl_written else "false",
            )
        except Exception:
            logger.exception(
                '{"event": "COMMISSION_APPLY_FAILED", "ib_order_id": "%s"}',
                ib_order_id,
            )

    # Register global callbacks (fire for ALL orders)
    ctx.ib.register_fill_callback(on_fill)
    ctx.ib.register_status_callback(on_status)
    if hasattr(ctx.ib, "register_commission_callback"):
        ctx.ib.register_commission_callback(on_commission)

    # --- Position event callback ---
    # Wire up positionEvent if the IB client supports it.
    # Use a semaphore to prevent connection pool exhaustion when IB fires
    # positionEvent for all positions simultaneously on startup.
    _position_sem = asyncio.Semaphore(5)

    def on_position_event(position) -> None:
        """Handle position change from IB (any source, including manual TWS closes)."""
        _spawn_background(_handle_position_event(ctx, position, _position_sem))

    ctx.ib.register_position_event_callback(on_position_event)
    # Subscribe to position updates
    try:
        await ctx.ib.req_positions_async()
    except Exception as e:
        logger.debug("reqPositionsAsync failed during wire-up", exc_info=e)
    logger.info('{"event": "POSITION_EVENT_WIRED"}')

    # Keep the task alive — this is a daemonic loop, cancelled at shutdown.
    while True:  # noqa: ASYNC110 — not waiting on an event; it's a sleep-forever keepalive
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
        avg_price_raw = Decimal(str(position.avgCost))
        con_id = position.contract.conId
        sec_type = position.contract.secType
        expiry = getattr(position.contract, "lastTradeDateOrContractMonth", None) or None
        trading_class = getattr(position.contract, "tradingClass", None) or None
        multiplier = getattr(position.contract, "multiplier", None) or None

        # Same avgCost normalization as _refresh_positions_cache: IB
        # reports total cost (price × multiplier) for FUT; we want the
        # per-unit price so the UI matches chart prices. STK is a
        # multiplier-of-1 no-op.
        if sec_type == "FUT" and multiplier:
            try:
                avg_price = avg_price_raw / Decimal(str(multiplier))
            except Exception:
                avg_price = avg_price_raw
        else:
            avg_price = avg_price_raw

        # Seed the wrapper's contract cache. For FUT, re-qualify via
        # localSymbol so the cached contract has a populated exchange
        # field (see _refresh_positions_cache for the same dance and
        # the IB-321 reasoning).
        try:
            if con_id not in ctx.ib._contract_cache:
                if sec_type == "FUT":
                    local_sym = getattr(position.contract, "localSymbol", "") or ""
                    if local_sym:
                        try:
                            await ctx.ib.qualify_contract(local_sym, sec_type="FUT")
                        except Exception as e:
                            logger.debug(
                                "FUT requalify-by-localSymbol failed (positionEvent) for %s",
                                local_sym, exc_info=e,
                            )
                if con_id not in ctx.ib._contract_cache:
                    ctx.ib._contract_cache[con_id] = position.contract
        except Exception as e:
            logger.debug("seed contract cache (positionEvent) failed", exc_info=e)

        # Subscribe to live ticks for STK and FUT so subsequent ticker
        # reads here and chart updates have a price to show. Idempotent
        # — wrapper deduplicates by con_id.
        if qty != 0 and sec_type in ("STK", "FUT"):
            try:
                await ctx.ib.subscribe_market_data(con_id, symbol)
            except Exception as e:
                logger.debug(
                    "market data subscribe (positionEvent) failed for %s",
                    symbol, exc_info=e,
                )

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
        except Exception as e:
            logger.debug("ticker price enrichment failed", exc_info=e)

        display_symbol = symbol
        if sec_type == "FUT" and expiry:
            try:
                from ib_trader.utils.symbol import format_display_symbol
                display_symbol = format_display_symbol(symbol, "FUT", expiry)
            except Exception:
                display_symbol = f"{symbol} {expiry}"

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
            "expiry": expiry,
            "trading_class": trading_class,
            "multiplier": multiplier,
            "display_symbol": display_symbol,
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

    # Wrapper exposes register_pending_tickers_callback as the public
    # surface for ib-async's pendingTickersEvent. The old guard checked
    # for the private ``_ib`` attribute, which was name-mangled when the
    # wrapper was sealed — silently disabling the entire quote pipeline.
    if not hasattr(ctx.ib, 'register_pending_tickers_callback'):
        logger.warning('{"event": "TICK_PUBLISHER_NO_REGISTER_API"}')
        return

    state = StateStore(redis)
    writers: dict[str, StreamWriter] = {}

    async def _publish_one(ticker) -> None:
        try:
            contract = getattr(ticker, "contract", None)
            if contract is None:
                return
            sec_type = getattr(contract, "secType", None)
            # Skip OPT — option tickers share the underlying's `symbol`
            # (e.g. AAPL puts/calls all set ``contract.symbol = "AAPL"``)
            # and would overwrite the equity quote on `quote:AAPL`. STK
            # publishes by symbol; FUT publishes by localSymbol (e.g.
            # `quote:MESM6`) so chart subscribers get live updates.
            if sec_type not in ("STK", "FUT"):
                return
            con_id = getattr(contract, "conId", None)
            # FUT keys on localSymbol so the chart's `quote:MESM6`
            # subscription matches what we publish. STK keeps `symbol`
            # (= ticker, e.g. "AAPL") which is what the watchlist uses.
            if sec_type == "FUT":
                symbol = getattr(contract, "localSymbol", None)
            else:
                symbol = getattr(contract, "symbol", None)
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

            # No price-tuple dedup. Identical prices across ticks are
            # perfectly valid market data — a repeated trade at the same
            # last, or a book that held firm while size changed on the
            # opposite side. Deduping on (bid, ask, last) silently dropped
            # ticks on narrow-band symbols (PSQ et al.), leaving the
            # quote stream and the per-symbol :latest key stale for
            # minutes even though IB was pushing ticks continuously.
            # ib_async only surfaces a ticker via pendingTickersEvent
            # when it has new data, so there's no real duplicate to
            # dedup against here.

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
            # Quote-stream liveness heartbeat. Any IB tick for any
            # tracked symbol keeps this key fresh — purely about "are
            # quotes flowing at all", so bots can halt when the whole
            # stream stalls instead of false-alarming on one quiet
            # symbol. This is NOT the engine process heartbeat (that
            # lives under the SQLite `heartbeats` ENGINE row and is
            # driven independently); the two concerns are decoupled.
            await state.set(
                StateKeys.quotes_heartbeat(),
                {"ts": quote_data["ts"], "symbol": symbol},
                ttl=StateKeys.QUOTES_HEARTBEAT_TTL,
            )
        except Exception:
            logger.exception('{"event": "TICK_PUBLISHER_ERROR"}')

    # Per-symbol timestamp of the last pendingTickersEvent we saw. Used by
    # the heartbeat loop below to report which tickers have gone silent
    # even though ib_async still has them in its tickers() set.
    last_pending_event_ts: dict[str, float] = {}

    def on_pending_tickers(tickers) -> None:
        # ib_async fires this synchronously; schedule per-ticker async work.
        import time as _t
        now_mono = _t.monotonic()
        for t in tickers:
            contract = getattr(t, "contract", None)
            sym = getattr(contract, "symbol", None) if contract is not None else None
            if sym:
                last_pending_event_ts[sym] = now_mono
            _spawn_background(_publish_one(t))

    ctx.ib.register_pending_tickers_callback(on_pending_tickers)
    logger.info('{"event": "TICK_PUBLISHER_WIRED"}')

    async def _heartbeat() -> None:
        """Periodic snapshot of every ticker ib_async is holding.

        Written so the next STALE_QUOTES stall can be diagnosed from the
        log alone: on each tick we record the ticker's con_id, last
        bid/ask/last, and seconds since our last `pendingTickersEvent`
        for that symbol. If a symbol appears in this list with a large
        ``silent_s`` value, ib_async still has the ticker but is no
        longer firing events for it — distinct from losing the ticker
        entirely (which would cause the row to disappear).
        """
        import time as _t
        interval = float(ctx.settings.get("tick_heartbeat_interval_seconds", 30))
        while True:
            try:
                await asyncio.sleep(interval)
                if not hasattr(ctx.ib, "get_tickers"):
                    continue
                try:
                    tickers = ctx.ib.get_tickers()
                except Exception as e:
                    logger.debug("get_tickers() failed", exc_info=e)
                    continue
                now_mono = _t.monotonic()
                rows: list[dict] = []
                for t in tickers:
                    contract = getattr(t, "contract", None)
                    if contract is None:
                        continue
                    sym = getattr(contract, "symbol", None)
                    if not sym:
                        continue
                    last_seen = last_pending_event_ts.get(sym)
                    silent_s = round(now_mono - last_seen, 1) if last_seen else None

                    def _f(attr, _t=t):
                        v = getattr(_t, attr, None)
                        if v is None:
                            return None
                        try:
                            fv = float(v)
                            # ib_async uses NaN for missing fields
                            if fv != fv:  # NaN check
                                return None
                            return fv
                        except (TypeError, ValueError):
                            return None
                    rows.append({
                        "symbol": sym,
                        "con_id": getattr(contract, "conId", None),
                        "sec_type": getattr(contract, "secType", None),
                        "bid": _f("bid"),
                        "ask": _f("ask"),
                        "last": _f("last"),
                        "silent_s": silent_s,
                    })
                logger.info(
                    '{"event": "TICK_PUBLISHER_HEARTBEAT", "ticker_count": %d, '
                    '"tickers": %s}',
                    len(rows), json.dumps(rows),
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception('{"event": "TICK_PUBLISHER_HEARTBEAT_ERROR"}')

    hb_task = asyncio.create_task(_heartbeat())

    try:
        await asyncio.Event().wait()
    finally:
        hb_task.cancel()
        try:
            await hb_task
        except (asyncio.CancelledError, Exception) as e:
            logger.debug("tick heartbeat cancel", exc_info=e)
        ctx.ib.unregister_pending_tickers_callback(on_pending_tickers)


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
