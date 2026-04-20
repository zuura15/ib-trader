"""REPL entry point — interactive trading session.

Start once with 'ib-trader' and trade from the prompt.
Owns the IB connection, startup health checks, and all order execution.

Startup sequence:
1. Health check (.env, SQLite, settings, symbols, IB connection, account)
2. Scan for ABANDONED orders — warn if any exist
3. Check daemon heartbeat — warn if daemon is not running
4. Write REPL_STARTED event and PID to SQLite
5. Warm contract cache for whitelisted symbols
6. Show prompt and enter REPL loop
"""
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

_HISTORY_FILE = Path.home() / ".ib_trader_history"

try:
    import readline
    readline.set_history_length(1000)
    try:
        readline.read_history_file(_HISTORY_FILE)
    except FileNotFoundError:
        pass  # First run — no history file yet
    except OSError as e:
        # Unreadable file is non-fatal; log at WARNING level after logger init
        print(f"\u26a0 Could not load command history: {e}", file=sys.stderr)
except ImportError:
    # readline is unavailable on Windows without pyreadline; degrade silently.
    readline = None  # type: ignore[assignment]

import click

from ib_trader.config.loader import load_env, load_settings, load_symbols, check_file_permissions
from ib_trader.config.context import AppContext
from ib_trader.data.repository import (
    TradeRepository, RepriceEventRepository,
    ContractRepository, HeartbeatRepository, AlertRepository,
    create_db_engine, create_session_factory, init_db,
)
from ib_trader.data.repositories.transaction_repository import TransactionRepository
from ib_trader.engine.tracker import OrderTracker
from ib_trader.engine.exceptions import ConfigurationError, SafetyLimitError
from ib_trader.engine.recovery import recover_in_flight_orders, format_recovery_warnings
from ib_trader.engine.order import execute_order, execute_close
from ib_trader.ib.insync_client import InsyncClient
from ib_trader.logging_.logger import setup_logging
from ib_trader.repl.commands import (
    parse_command, BuyCommand, SellCommand, CloseCommand, ModifyCommand
)

logger = logging.getLogger(__name__)

VERSION = "1.0.0"


async def run_repl(ctx: AppContext, symbols: list[str]) -> None:
    """Main REPL session loop.

    Args:
        ctx: Application dependency injection container.
        symbols: Validated symbol whitelist.
    """
    # Connect to IB
    try:
        await ctx.ib.connect()
    except Exception as e:
        print(f"\u2717 IB connection failed: {e}")
        print("Please check that TWS or IB Gateway is running, then press Enter to retry...")
        await asyncio.to_thread(input)
        try:
            await ctx.ib.connect()
        except Exception as e2:
            logger.error('{"event": "IB_DISCONNECTED", "error": "%s"}', str(e2), exc_info=True)
            print(f"\u2717 Reconnect failed: {e2}")
            sys.exit(1)

    # Startup checks
    abandoned = recover_in_flight_orders(ctx.transactions, ctx.trades)
    warnings = format_recovery_warnings(abandoned)

    # Check daemon heartbeat
    daemon_hb = ctx.heartbeats.get("DAEMON")
    daemon_warning = None
    if not daemon_hb:
        daemon_warning = "\u26a0 Daemon is not running \u2014 reconciliation and monitoring are offline"
    else:
        age = (datetime.now(timezone.utc) - daemon_hb.last_seen_at.replace(tzinfo=timezone.utc)).total_seconds()
        if age > ctx.settings["heartbeat_stale_threshold_seconds"]:
            daemon_warning = "\u26a0 Daemon is not running \u2014 reconciliation and monitoring are offline"

    # Write REPL_STARTED event
    pid = os.getpid()
    ctx.heartbeats.upsert("REPL", pid)
    logger.info(
        '{"event": "REPL_STARTED", "pid": %d, "version": "%s"}', pid, VERSION
    )

    # Warm contract cache
    for symbol in symbols:
        try:
            from ib_trader.engine.order import _get_contract
            await _get_contract(symbol, ctx)
        except Exception as e:
            logger.warning('{"event": "CONTRACT_CACHE_MISS", "symbol": "%s", "error": "%s"}',
                           symbol, str(e))

    # Print startup banner
    settings = ctx.settings
    print(f"IB Trader v{VERSION} \u2014 connected to Gateway @ {settings['ib_host']}:{settings['ib_port']}")
    print(f"Account: {ctx.account_id} | {len(symbols)} symbols loaded")

    for w in warnings:
        print(w)
    if daemon_warning:
        print(daemon_warning)

    # Start heartbeat background task
    heartbeat_task = asyncio.create_task(_heartbeat_loop(ctx, pid))

    # REPL loop
    try:
        while True:
            try:
                line = await asyncio.get_event_loop().run_in_executor(None, lambda: input("\n> "))
            except (EOFError, KeyboardInterrupt):
                break

            cmd = parse_command(line)

            if cmd is None:
                continue

            if cmd == "exit" or cmd == "quit":
                break

            if cmd == "help":
                _print_help()
                continue

            if cmd == "orders":
                await _cmd_orders(ctx)
                continue

            if cmd == "stats":
                await _cmd_stats(ctx)
                continue

            if cmd == "status":
                await _cmd_status(ctx, settings)
                continue

            if cmd == "refresh":
                print("Reconciliation is a daemon feature. Start ib-daemon for background reconciliation.")
                continue

            if isinstance(cmd, ModifyCommand):
                logger.info(
                    '{"event": "MODIFY_STUB_RECEIVED", "serial": %d}', cmd.serial
                )
                print(f"modify #{cmd.serial}: accepted (stub \u2014 no action taken)")
                continue

            # Execute command
            try:
                if isinstance(cmd, (BuyCommand, SellCommand)):
                    await execute_order(cmd, ctx)
                elif isinstance(cmd, CloseCommand):
                    await execute_close(cmd, ctx)
            except SafetyLimitError as e:
                print(f"\u2717 Error: {e}")
            except Exception as e:
                logger.error('{"event": "COMMAND_ERROR", "error": "%s"}', str(e), exc_info=True)
                print(f"\u2717 Error: {e}")

    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except asyncio.CancelledError:
            pass

        ctx.heartbeats.delete("REPL")
        logger.info('{"event": "REPL_EXIT_CLEAN", "pid": %d}', pid)
        await ctx.ib.disconnect()
        if readline is not None:
            try:
                readline.write_history_file(_HISTORY_FILE)
            except OSError as e:
                logger.warning(
                    '{"event": "HISTORY_WRITE_FAILED", "path": "%s", "error": "%s"}',
                    _HISTORY_FILE, e,
                )
        print("Goodbye. Session logged.")


async def _heartbeat_loop(ctx: AppContext, pid: int) -> None:
    """Write REPL heartbeat to SQLite every heartbeat_interval_seconds."""
    interval = ctx.settings["heartbeat_interval_seconds"]
    while True:
        await asyncio.sleep(interval)
        ctx.heartbeats.upsert("REPL", pid)
        logger.debug('{"event": "HEARTBEAT_WRITTEN", "process": "REPL"}')


async def _cmd_orders(ctx: AppContext) -> None:
    """Display all open orders."""
    open_orders = ctx.transactions.get_open_orders()
    if not open_orders:
        print("No open orders.")
        return
    print(f"{'Serial':<8} {'Symbol':<8} {'Side':<5} {'Qty':<8} {'Status':<12} {'IB ID'}")
    print("-" * 60)
    for txn in open_orders:
        print(
            f"#{txn.trade_serial or '-':<7} {txn.symbol:<8} {txn.side:<5} "
            f"{txn.quantity:<8} {txn.action.value:<12} {txn.ib_order_id or '-'}"
        )


async def _cmd_stats(ctx: AppContext) -> None:
    """Display a summary of all trades — open and closed."""
    from ib_trader.data.models import LegType, TransactionAction

    all_trades = ctx.trades.get_all()
    if not all_trades:
        print("No trades recorded yet.")
        return

    open_count = sum(1 for t in all_trades if t.status.value == "OPEN")
    closed_count = len(all_trades) - open_count

    print(f"\nTrades: {len(all_trades)} total  |  {open_count} open  |  {closed_count} closed\n")
    print(f"{'#':<5} {'Symbol':<8} {'Dir':<6} {'Qty':<5} {'Entry @':<10} {'Profit Taker':<22} {'Comm':<8} {'Status'}")
    print("-" * 78)

    total_commission = Decimal("0")

    for trade in all_trades:
        txns = ctx.transactions.get_for_trade(trade.id)

        # Find ENTRY fill for entry price
        entry = next(
            (t for t in txns if t.leg_type == LegType.ENTRY and t.action == TransactionAction.FILLED),
            None,
        )
        # Find ENTRY placement for pending entry price
        entry_placed = next(
            (t for t in txns if t.leg_type == LegType.ENTRY and t.action == TransactionAction.PLACE_ACCEPTED),
            None,
        )
        pt = next(
            (t for t in txns if t.leg_type == LegType.PROFIT_TAKER and t.action == TransactionAction.FILLED),
            None,
        )
        pt_placed = next(
            (t for t in txns if t.leg_type == LegType.PROFIT_TAKER and t.action == TransactionAction.PLACE_ACCEPTED),
            None,
        )
        close = next(
            (t for t in txns if t.leg_type == LegType.CLOSE and t.action == TransactionAction.FILLED),
            None,
        )

        # Entry details
        if entry and entry.ib_avg_fill_price:
            entry_str = f"${entry.ib_avg_fill_price}"
            qty_str = str(entry.ib_filled_qty)
        elif entry_placed and entry_placed.price_placed:
            entry_str = f"~${entry_placed.price_placed}"
            qty_str = str(entry_placed.quantity)
        else:
            entry_str = "\u2014"
            qty_str = "\u2014"

        # Profit taker / close status
        if close and close.ib_avg_fill_price:
            pt_str = f"closed @ ${close.ib_avg_fill_price}"
        elif pt and pt.ib_avg_fill_price:
            pt_str = f"PT filled @ ${pt.ib_avg_fill_price}"
        elif pt_placed and pt_placed.price_placed:
            pt_str = f"PT open @ ${pt_placed.price_placed}"
        elif pt_placed:
            pt_str = "PT open"
        else:
            pt_str = "\u2014"

        # Commission: sum all transactions
        trade_comm = sum(
            (t.commission or Decimal("0")) for t in txns
        )
        total_commission += trade_comm
        comm_str = f"${trade_comm}" if trade_comm else "\u2014"

        trade_status = "OPEN" if trade.status.value == "OPEN" else "closed"
        direction = trade.direction[:1]  # L / S

        print(
            f"#{trade.serial_number:<4} {trade.symbol:<8} {direction:<6} "
            f"{qty_str:<5} {entry_str:<10} {pt_str:<22} {comm_str:<8} {trade_status}"
        )

    print("-" * 78)
    print(f"Total commission: ${total_commission}")


async def _cmd_status(ctx: AppContext, settings: dict) -> None:
    """Display gateway and system status."""
    daemon_hb = ctx.heartbeats.get("DAEMON")
    if daemon_hb:
        age = (datetime.now(timezone.utc) - daemon_hb.last_seen_at.replace(tzinfo=timezone.utc)).total_seconds()
        print(f"Daemon: running (last seen {int(age)}s ago, PID {daemon_hb.pid})")
    else:
        print("Daemon: not running")
    print(f"Gateway: {settings['ib_host']}:{settings['ib_port']}")
    print(f"Account: {ctx.account_id}")


def _print_help() -> None:
    """Print available REPL commands with full parameter documentation."""
    print("""
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  IB Trader — command reference
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

ORDER ENTRY
  buy  SYMBOL QTY STRATEGY [PROFIT] [OPTIONS]
  sell SYMBOL QTY STRATEGY [PROFIT] [OPTIONS]

    SYMBOL      Ticker (e.g. MSFT, AAPL) — must be in the symbols whitelist
    QTY         Number of shares (positive integer or decimal)
                  Ignored when --dollars is given; calculated automatically
    STRATEGY    mid     — GTC limit order starting at mid, repriced toward ask
                          over the reprice window (default 10s, 10 steps)
                market  — immediate market order, no repricing
    PROFIT      Optional. Total dollar profit target for a profit taker order.
                  e.g. "500" places a GTC limit sell at avg_fill + $500/qty

    Options:
      --dollars N           Size the order by notional instead of share count.
                              QTY is ignored; shares = floor(N / mid_price)
                              capped at max_order_size_shares (default 10)
      --take-profit-price N Place a profit taker at this exact price instead of
                              calculating from PROFIT amount
      --stop-loss N         Record a stop-loss price (stub — logged only,
                              no IB stop order placed yet)

    Examples:
      buy MSFT 1 mid
      buy MSFT 1 mid 500                    ← profit taker at +$500 total
      buy MSFT 1 mid --take-profit-price 420
      buy MSFT 5 market
      buy MSFT 0 mid --dollars 2000         ← ~4 shares at current mid

POSITION MANAGEMENT
  close SERIAL [STRATEGY] [PROFIT] [OPTIONS]

    SERIAL      Trade serial number (shown in # column of 'orders' / 'stats')
    STRATEGY    mid | market  (default: mid)
    PROFIT      Optional profit taker for the closing leg

    Options:
      --take-profit-price N  Explicit close profit taker price

    Examples:
      close 3
      close 3 market
      close 3 mid 200      ← close and place a profit taker at +$200

  modify SERIAL             (stub — accepted but no action taken)

INFORMATION
  orders    List all currently open orders (placed and not yet terminal)
  stats     Show all trades — entry price, profit taker status, commission
  status    Show gateway connection and daemon heartbeat
  help      This help

SESSION
  exit | quit   Clean shutdown (saves command history)

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
  Arrow keys: ↑/↓ scroll command history   Ctrl-C or Ctrl-D: clean exit
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
""")


@click.command()
@click.option("--db", default="trader.db", help="SQLite database path")
@click.option("--env", default=".env", help=".env file path")
@click.option("--settings", "settings_path", default="config/settings.yaml", help="Settings YAML path")
@click.option("--symbols", "symbols_path", default="config/symbols.yaml", help="Symbols YAML path")
@click.option(
    "--paper/--live", "paper",
    default=True,
    help="Paper trading (default). Pass --live to connect to the live Gateway.",
)
def main(db: str, env: str, settings_path: str, symbols_path: str, paper: bool) -> None:
    """IB Trader — interactive trading session for Interactive Brokers.

    Start once and trade from the prompt. Type 'help' for commands.
    Defaults to paper trading. Pass --live to connect to the live Gateway.
    """
    setup_logging()

    try:
        env_vars = load_env(env)
        settings = load_settings(settings_path)
        symbols = load_symbols(symbols_path)
    except ConfigurationError as e:
        print(f"\u2717 Configuration error: {e}")
        sys.exit(1)

    # settings.yaml defaults to paper (port 4002, market data type 3). The
    # --paper / --live flags + IB_PORT_PAPER / IB_PORT env vars are opt-in
    # overrides.
    settings["ib_host"] = env_vars.get("IB_HOST", settings.get("ib_host", "127.0.0.1"))
    if paper:
        settings["ib_port"] = int(env_vars.get("IB_PORT_PAPER", settings.get("ib_port", 4002)))
        settings["ib_market_data_type"] = int(env_vars.get("IB_MARKET_DATA_TYPE_PAPER", settings.get("ib_market_data_type", 3)))
        account_id = env_vars.get("IB_ACCOUNT_ID_PAPER") or env_vars["IB_ACCOUNT_ID"]
    else:
        settings["ib_port"] = int(env_vars.get("IB_PORT", 4001))
        settings["ib_market_data_type"] = int(env_vars.get("IB_MARKET_DATA_TYPE", 1))
        account_id = env_vars["IB_ACCOUNT_ID"]
    settings["ib_client_id"] = int(env_vars.get("IB_CLIENT_ID", settings.get("ib_client_id", 1)))

    # Check DB file permissions if it exists
    if Path(db).exists():
        try:
            check_file_permissions(db, 0o600, "SQLite database")
        except ConfigurationError as e:
            print(f"\u26a0 Warning: {e}")

    # Set up database
    db_url = f"sqlite:///{db}"
    engine = create_db_engine(db_url)
    init_db(engine)  # Creates tables if not present (Alembic handles migrations in prod)
    session_factory = create_session_factory(engine)

    # Build IB client
    ib_client = InsyncClient(
        host=settings["ib_host"],
        port=settings["ib_port"],
        client_id=settings["ib_client_id"],
        account_id=account_id,
        min_call_interval_ms=settings["ib_min_call_interval_ms"],
        market_data_type=settings["ib_market_data_type"],
    )

    # Build repositories
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

    logger.info('{"event": "APP_STARTED", "process": "REPL", "db": "%s"}', db)

    # Launch Textual TUI — it owns the asyncio event loop.
    # ib_insync async coroutines run as tasks within Textual's loop.
    from ib_trader.repl.tui import IBTraderApp
    app = IBTraderApp(ctx=ctx, symbols=symbols)
    app.run()
