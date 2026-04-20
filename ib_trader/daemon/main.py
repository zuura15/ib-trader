"""Daemon entry point — persistent background process.

Owns: monitoring, reconciliation, SQLite integrity, system health alerts.
Watches the CLI REPL process via SQLite heartbeats.
Runs a Textual TUI for live dashboard and command input.

The daemon does NOT use util.startLoop(). It runs its own asyncio event loop.
Daemon IB clientId = REPL clientId + 1.
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import click

from ib_trader.config.loader import load_env, load_settings, load_symbols, check_file_permissions
from ib_trader.config.context import AppContext
from ib_trader.data.repository import (
    TradeRepository, RepriceEventRepository,
    ContractRepository, HeartbeatRepository, AlertRepository,
    create_db_engine, create_session_factory, init_db,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.daemon.reconciler import run_reconciliation, run_transaction_reconciliation
from ib_trader.daemon.monitor import check_repl_heartbeat, check_ib_connectivity
from ib_trader.daemon.integrity import run_integrity_check
from ib_trader.engine.exceptions import ConfigurationError
from ib_trader.engine.tracker import OrderTracker
from ib_trader.ib.insync_client import InsyncClient
from ib_trader.logging_.logger import setup_logging

logger = logging.getLogger(__name__)

VERSION = "1.0.0"


async def run_daemon(ctx: AppContext, session_factory) -> None:
    """Main daemon coroutine. Runs all background loops.

    Args:
        ctx: Application dependency injection container.
        session_factory: SQLAlchemy scoped session factory (for integrity checks).
    """
    # Write daemon heartbeat
    pid = os.getpid()
    ctx.heartbeats.upsert("DAEMON", pid)
    logger.info('{"event": "APP_STARTED", "process": "DAEMON", "pid": %d}', pid)
    print(f"[DAEMON] Started (pid={pid}). Connecting to IB Gateway...")

    # Run integrity check on startup
    run_integrity_check(session_factory, ctx)

    # Connect IB for passive health checks (different client ID)
    # Retry with clear output so operator knows what's happening
    host = ctx.settings.get("ib_host", "127.0.0.1")
    port = ctx.settings.get("ib_port", 4001)
    retry_interval = ctx.settings.get("ib_connect_retry_seconds", 10)
    ib_connected = False
    attempt = 0
    while not ib_connected:
        attempt += 1
        try:
            await ctx.ib.connect()
            ib_connected = True
            print("[DAEMON] Connected to IB Gateway.")
        except Exception as e:
            print(
                f"[DAEMON] IB Gateway not reachable at {host}:{port} "
                f"(attempt {attempt}). Retrying in {retry_interval}s... ({e})"
            )
            logger.warning(
                '{"event": "IB_CONNECT_RETRY", "process": "DAEMON", '
                '"attempt": %d, "error": "%s"}', attempt, str(e),
            )
            await asyncio.sleep(retry_interval)

    # Run transaction reconciliation once on startup
    print("[DAEMON] Running startup reconciliation...")
    try:
        await run_transaction_reconciliation(ctx)
        print("[DAEMON] Startup reconciliation complete.")
        logger.info('{"event": "STARTUP_TRANSACTION_RECONCILIATION_COMPLETE"}')
    except Exception as e:
        print(f"[DAEMON] Startup reconciliation failed: {e}")
        logger.error('{"event": "STARTUP_TRANSACTION_RECONCILIATION_FAILED", "error": "%s"}',
                     str(e), exc_info=True)

    print("[DAEMON] Ready. Monitoring heartbeats, reconciliation, integrity...")

    consecutive_ib_failures: list = []
    last_recon_time = None
    last_recon_changes = 0

    async def get_status() -> dict:
        """Build current status dict for the TUI dashboard."""
        repl_hb = ctx.heartbeats.get("REPL")
        repl_alive = False
        repl_pid = None
        if repl_hb:
            last = repl_hb.last_seen_at
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - last).total_seconds()
            repl_alive = age < ctx.settings["heartbeat_stale_threshold_seconds"]
            repl_pid = repl_hb.pid

        alerts = ctx.alerts.get_open()

        # Build basic stats from transactions and trade groups
        all_open_orders = ctx.transactions.get_open_orders()
        all_trades = ctx.trades.get_all()
        open_trades = [t for t in all_trades if t.status.value == "OPEN"]
        closed_trades = [t for t in all_trades if t.status.value == "CLOSED"]

        recon_display = "never"
        if last_recon_time:
            age = (datetime.now(timezone.utc) - last_recon_time).total_seconds()
            if age < 60:
                recon_display = "just now"
            elif age < 3600:
                recon_display = f"{int(age/60)} min ago"
            else:
                recon_display = f"{int(age/3600)} hr ago"

        return {
            "ib_connected": len(consecutive_ib_failures) < 3,
            "repl_alive": repl_alive,
            "repl_pid": repl_pid,
            "last_recon": recon_display,
            "recon_changes": last_recon_changes,
            "alerts": alerts,
            "stats": {
                "open_orders": len(all_open_orders),
                "open_trades": len(open_trades),
                "closed_trades": len(closed_trades),
                "total_trades": len(all_trades),
                "pnl": Decimal("0"),      # TODO: aggregate from metrics
                "commission": Decimal("0"),
            },
        }

    async def handle_command(command: str) -> None:
        """Handle a daemon command from the TUI input."""
        nonlocal last_recon_time, last_recon_changes
        cmd = command.strip().lower()

        if cmd == "refresh":
            result = await run_reconciliation(ctx)
            last_recon_time = datetime.now(timezone.utc)
            last_recon_changes = result["changes"]

        elif cmd == "orders":
            open_orders = ctx.transactions.get_open_orders()
            logger.info('{"event": "DAEMON_ORDERS_CMD", "count": %d}', len(open_orders))

        elif cmd == "stats":
            logger.info('{"event": "DAEMON_STATS_CMD"}')

        elif cmd == "status":
            logger.info('{"event": "DAEMON_STATUS_CMD"}')

        elif cmd == "cleanup":
            logger.info('{"event": "MODIFY_STUB_RECEIVED", "cmd": "cleanup"}')

        else:
            logger.warning('{"event": "DAEMON_UNKNOWN_CMD", "cmd": "%s"}', cmd)

    # Import and run TUI
    from ib_trader.daemon.tui import DaemonTUI

    refresh_seconds = float(ctx.settings.get("daemon_tui_refresh_seconds", 5))

    app = DaemonTUI(
        get_status=get_status,
        handle_command=handle_command,
        refresh_seconds=refresh_seconds,
    )

    # Run background loops alongside the TUI
    async def background_loops():
        nonlocal last_recon_time, last_recon_changes

        recon_interval = ctx.settings["reconciliation_interval_seconds"]
        integrity_interval = ctx.settings["db_integrity_check_interval_seconds"]
        heartbeat_interval = ctx.settings["heartbeat_interval_seconds"]
        ib_check_interval = 1800  # 30 minutes passive IB check

        recon_counter = 0
        integrity_counter = 0
        hb_counter = 0
        ib_counter = 0

        while True:
            await asyncio.sleep(30)  # Check every 30 seconds

            # Write daemon heartbeat
            hb_counter += 30
            if hb_counter >= heartbeat_interval:
                ctx.heartbeats.upsert("DAEMON", pid)
                logger.debug('{"event": "HEARTBEAT_WRITTEN", "process": "DAEMON"}')
                hb_counter = 0

            # Skip loops if TUI is in CATASTROPHIC pause
            if app.loops_paused:
                continue

            # REPL heartbeat check
            repl_hb = ctx.heartbeats.get("REPL")
            engine_hb = ctx.heartbeats.get("ENGINE")
            if repl_hb:
                repl_age = (datetime.now(timezone.utc) - (repl_hb.last_seen_at.replace(tzinfo=timezone.utc) if repl_hb.last_seen_at.tzinfo is None else repl_hb.last_seen_at)).total_seconds()
                if repl_age > ctx.settings["heartbeat_stale_threshold_seconds"]:
                    print(f"[DAEMON] WARNING  REPL heartbeat stale ({repl_age:.0f}s ago, pid={repl_hb.pid})")
            if engine_hb:
                engine_age = (datetime.now(timezone.utc) - (engine_hb.last_seen_at.replace(tzinfo=timezone.utc) if engine_hb.last_seen_at.tzinfo is None else engine_hb.last_seen_at)).total_seconds()
                if engine_age > ctx.settings["heartbeat_stale_threshold_seconds"]:
                    print(f"[DAEMON] WARNING  ENGINE heartbeat stale ({engine_age:.0f}s ago, pid={engine_hb.pid})")

            await check_repl_heartbeat(ctx)

            # IB connectivity check (every 30 min)
            ib_counter += 30
            if ib_counter >= ib_check_interval:
                try:
                    await check_ib_connectivity(ctx, consecutive_ib_failures)
                except Exception as e:
                    print(f"[DAEMON] WARNING  IB connectivity check failed: {e}")
                ib_counter = 0

            # Reconciliation (hourly per reconciliation_interval_seconds)
            recon_counter += 30
            if recon_counter >= recon_interval:
                print("[DAEMON] Running reconciliation...")
                result = await run_reconciliation(ctx)
                await run_transaction_reconciliation(ctx)
                last_recon_time = datetime.now(timezone.utc)
                last_recon_changes = result["changes"]
                if last_recon_changes > 0:
                    print(f"[DAEMON] Reconciliation found {last_recon_changes} change(s)")
                else:
                    print("[DAEMON] Reconciliation complete — no discrepancies")
                recon_counter = 0

            # Integrity check (every 6 hours)
            integrity_counter += 30
            if integrity_counter >= integrity_interval:
                print("[DAEMON] Running integrity check...")
                run_integrity_check(session_factory, ctx)
                print("[DAEMON] Integrity check complete.")
                integrity_counter = 0

    # Run TUI and background loops concurrently
    async def run_all():
        loop_task = asyncio.create_task(background_loops())
        try:
            await app.run_async()
        finally:
            loop_task.cancel()
            try:
                await loop_task
            except asyncio.CancelledError:
                pass
            ctx.heartbeats.delete("DAEMON")
            logger.info('{"event": "APP_STOPPED", "process": "DAEMON"}')

    await run_all()


@click.command()
@click.option("--db", default="trader.db", help="SQLite database path")
@click.option("--env", default=".env", help=".env file path")
@click.option("--settings", "settings_path", default="config/settings.yaml", help="Settings YAML path")
@click.option("--symbols", "symbols_path", default="config/symbols.yaml", help="Symbols YAML path")
@click.option("--smoke", is_flag=True, default=False, help="STUB: run smoke tests before starting")
@click.option(
    "--paper/--live", "paper",
    default=True,
    help="Paper trading (default). Pass --live to target the live Gateway.",
)
def main(db: str, env: str, settings_path: str, symbols_path: str, smoke: bool, paper: bool) -> None:
    """IB Trader Daemon — background monitoring and reconciliation TUI.

    Runs persistently in a dedicated terminal window.
    Defaults to paper trading. Pass --live to target the live Gateway.
    """
    if smoke:
        logger.info('{"event": "MODIFY_STUB_RECEIVED", "cmd": "--smoke", "note": "not implemented"}')
        print("--smoke flag received (stub — smoke suite not yet wired to daemon startup, proceeding normally)")

    setup_logging()

    try:
        env_vars = load_env(env)
        settings = load_settings(settings_path)
        _symbols = load_symbols(symbols_path)
    except ConfigurationError as e:
        print(f"\u2717 Configuration error: {e}")
        sys.exit(1)

    settings["ib_host"] = env_vars.get("IB_HOST", settings.get("ib_host", "127.0.0.1"))
    if paper:
        settings["ib_port"] = int(env_vars.get("IB_PORT_PAPER", settings.get("ib_port", 4002)))
        settings["ib_market_data_type"] = int(env_vars.get("IB_MARKET_DATA_TYPE_PAPER", settings.get("ib_market_data_type", 3)))
        account_id = env_vars.get("IB_ACCOUNT_ID_PAPER") or env_vars["IB_ACCOUNT_ID"]
    else:
        settings["ib_port"] = int(env_vars.get("IB_PORT", 4001))
        settings["ib_market_data_type"] = int(env_vars.get("IB_MARKET_DATA_TYPE", 1))
        account_id = env_vars["IB_ACCOUNT_ID"]
    # Daemon uses client_id + 1 to avoid conflict with REPL
    repl_client_id = int(env_vars.get("IB_CLIENT_ID", settings.get("ib_client_id", 1)))
    daemon_client_id = repl_client_id + 1
    settings["ib_client_id"] = daemon_client_id

    if Path(db).exists():
        try:
            check_file_permissions(db, 0o600, "SQLite database")
        except ConfigurationError as e:
            print(f"\u26a0 Warning: {e}")

    db_url = f"sqlite:///{db}"
    engine = create_db_engine(db_url)
    init_db(engine)
    session_factory = create_session_factory(engine)

    ib_client = InsyncClient(
        host=settings["ib_host"],
        port=settings["ib_port"],
        client_id=daemon_client_id,
        account_id=account_id,
        min_call_interval_ms=settings["ib_min_call_interval_ms"],
        market_data_type=settings["ib_market_data_type"],
    )

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
    )

    asyncio.run(run_daemon(ctx, session_factory))
