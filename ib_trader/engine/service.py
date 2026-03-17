"""Engine service — central command execution loop.

The engine service is the sole process with broker connections. All other
processes (REPL, API server, bot runner) submit commands by inserting rows
into the pending_commands table. The engine polls for PENDING commands,
executes them via the trading engine, and writes results back.

Commands execute concurrently via asyncio.create_task() with a semaphore
to limit concurrent broker calls.
"""
import asyncio
import dataclasses
import json
import logging
from datetime import datetime, timezone

from ib_trader.config.context import AppContext
from ib_trader.data.models import PendingCommandStatus
from ib_trader.engine.recovery import recover_in_flight_orders
from ib_trader.repl.commands import (
    BuyCommand, SellCommand, CloseCommand, parse_command,
)
from ib_trader.repl.output_router import OutputRouter, OutputSeverity

logger = logging.getLogger(__name__)

_DEFAULT_MAX_CONCURRENT = 5
_DEFAULT_POLL_INTERVAL_S = 0.1


class _ListRenderer:
    """OutputRouter renderer that collects messages into a list.

    Used by the engine service to capture command output for writing
    back to the pending_commands table.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []

    def write_log(self, message: str, severity=None) -> None:
        self.messages.append(message)

    def write_command_output(self, message: str, severity=None) -> None:
        self.messages.append(message)

    def update_order_row(self, serial, data) -> None:
        pass

    def update_header(self, **kwargs) -> None:
        pass


def _handle_builtin(verb: str, ctx: AppContext) -> str:
    """Handle built-in read-only commands and return output text."""
    if verb == "help":
        return (
            "Available commands:\n"
            "  buy SYMBOL QTY STRATEGY [--profit N] [--stop-loss N]\n"
            "  sell SYMBOL QTY STRATEGY [--profit N] [--stop-loss N]\n"
            "  close SERIAL [STRATEGY]\n"
            "  status    — show system status\n"
            "  stats     — show trading statistics\n"
            "  orders    — list open orders\n"
            "  help      — show this message\n"
            "\n"
            "Strategies: mid, market, bid, ask, limit PRICE"
        )

    if verb in ("status", "stats"):
        trades = ctx.trades
        orders = ctx.orders
        open_trades = trades.get_open()
        all_trades = trades.get_all()
        open_orders = orders.get_all_open()

        # P&L
        closed = [t for t in all_trades if t.status.value == "CLOSED" and t.realized_pnl is not None]
        total_pnl = sum(float(t.realized_pnl) for t in closed)
        total_commission = sum(float(t.total_commission or 0) for t in closed)

        lines = [
            f"Positions:  {len(open_trades)} open",
            f"Orders:     {len(open_orders)} open",
            f"Trades:     {len(all_trades)} total ({len(closed)} closed)",
            f"Realized:   ${total_pnl:+.2f}",
            f"Commission: ${total_commission:.2f}",
        ]

        # Heartbeats
        for proc in ("ENGINE", "DAEMON", "API", "BOT_RUNNER"):
            hb = ctx.heartbeats.get(proc)
            if hb:
                lines.append(f"{proc:11} pid={hb.pid} last={hb.last_seen_at.strftime('%H:%M:%S')}")
            else:
                lines.append(f"{proc:11} not running")

        return "\n".join(lines)

    if verb == "orders":
        open_orders = ctx.orders.get_all_open()
        if not open_orders:
            return "No open orders."
        lines = []
        for o in open_orders:
            price = o.price_placed or "MKT"
            lines.append(
                f"  #{o.serial_number or '-':>3} {o.symbol:5} {o.side:4} "
                f"{o.qty_requested} @ {price} [{o.status.value}] ib_id={o.ib_order_id}"
            )
        return f"{len(open_orders)} open orders:\n" + "\n".join(lines)

    if verb == "refresh":
        return "Refresh triggered."

    return verb


async def _execute_single_command(cmd_row, ctx: AppContext,
                                   sem: asyncio.Semaphore) -> None:
    """Execute a single command from the pending_commands queue.

    Runs under a semaphore to limit concurrent broker calls.
    Captures all output via a _ListRenderer and writes it back
    to the pending_commands row on completion.
    """
    async with sem:
        # Create a per-command OutputRouter with a list-collecting renderer
        # so output doesn't leak into the main router.
        # CRITICAL: Use dataclasses.replace() to create an isolated context
        # copy — NEVER mutate the shared ctx.router, as concurrent tasks
        # would corrupt each other's output.
        renderer = _ListRenderer()
        cmd_router = OutputRouter()
        cmd_router.set_renderer(renderer)

        # Resolve broker for this command (supports multi-broker)
        try:
            broker = ctx.get_broker(cmd_row.broker)
        except KeyError:
            msg = f"Broker '{cmd_row.broker}' not configured"
            print(f"[ENGINE] FAIL  {cmd_row.command_text!r} — {msg}")
            ctx.pending_commands.complete(
                cmd_row.id, PendingCommandStatus.FAILURE, error=msg,
            )
            return

        # Create isolated context copy with the right broker and router
        cmd_ctx = dataclasses.replace(ctx, ib=broker, router=cmd_router)

        print(f"[ENGINE] EXEC  {cmd_row.command_text!r} (source={cmd_row.source}, broker={cmd_row.broker})")

        try:
            parsed = parse_command(cmd_row.command_text, router=cmd_router)

            if parsed is None:
                print(f"[ENGINE] FAIL  {cmd_row.command_text!r} — unknown command")
                cmd_ctx.pending_commands.complete(
                    cmd_row.id, PendingCommandStatus.FAILURE,
                    error=f"Unknown command: {cmd_row.command_text}",
                )
                return

            if isinstance(parsed, str):
                # Built-in read-only commands
                output = _handle_builtin(parsed, cmd_ctx)
                print(f"[ENGINE] OK    {cmd_row.command_text!r}")
                cmd_ctx.pending_commands.complete(
                    cmd_row.id, PendingCommandStatus.SUCCESS,
                    output=output,
                )
                return

            if isinstance(parsed, (BuyCommand, SellCommand)):
                from ib_trader.engine.order import execute_order
                await execute_order(parsed, cmd_ctx)
            elif isinstance(parsed, CloseCommand):
                from ib_trader.engine.order import execute_close
                await execute_close(parsed, cmd_ctx)
            else:
                print(f"[ENGINE] FAIL  {cmd_row.command_text!r} — unsupported type")
                cmd_ctx.pending_commands.complete(
                    cmd_row.id, PendingCommandStatus.FAILURE,
                    error=f"Unsupported command type: {type(parsed).__name__}",
                )
                return

            output = "\n".join(renderer.messages) if renderer.messages else None
            print(f"[ENGINE] OK    {cmd_row.command_text!r}")
            cmd_ctx.pending_commands.complete(
                cmd_row.id, PendingCommandStatus.SUCCESS,
                output=output,
            )

        except Exception as e:
            print(f"[ENGINE] ERROR {cmd_row.command_text!r} — {e}")
            logger.exception(json.dumps({
                "event": "COMMAND_FAILED",
                "cmd_id": cmd_row.id,
                "error": str(e),
            }))

            # Clean up any orphaned PENDING orders created by the failed command.
            # When execute_order throws mid-way, it may have created Order + TradeGroup
            # records in SQLite that are stuck in PENDING with no ib_order_id.
            try:
                from ib_trader.data.models import OrderStatus, TradeStatus
                from datetime import datetime, timezone
                now = datetime.now(timezone.utc)
                orphans = cmd_ctx.orders.get_in_states([OrderStatus.PENDING])
                for orphan in orphans:
                    if orphan.ib_order_id is None:
                        cmd_ctx.orders.update_status(orphan.id, OrderStatus.ABANDONED)
                        # Close the parent trade group too
                        trade = cmd_ctx.trades.get_by_serial(orphan.serial_number) if orphan.serial_number is not None else None
                        if trade and trade.status == TradeStatus.OPEN:
                            cmd_ctx.trades.update_status(trade.id, TradeStatus.CLOSED)
                        logger.info(json.dumps({
                            "event": "ORPHAN_ORDER_CLEANED",
                            "order_id": orphan.id,
                            "symbol": orphan.symbol,
                        }))
            except Exception:
                logger.exception(json.dumps({"event": "ORPHAN_CLEANUP_FAILED"}))

            output = "\n".join(renderer.messages) if renderer.messages else None
            error_msg = f"{str(e)}\n\n{output}" if output else str(e)
            cmd_ctx.pending_commands.complete(
                cmd_row.id, PendingCommandStatus.FAILURE,
                error=error_msg,
            )


async def recover_stale_commands(ctx: AppContext) -> int:
    """Mark any RUNNING commands from a previous crash as FAILURE.

    Called on engine startup. Returns the number of stale commands found.
    """
    stale = ctx.pending_commands.get_by_status(PendingCommandStatus.RUNNING)
    for cmd_row in stale:
        ctx.pending_commands.complete(
            cmd_row.id, PendingCommandStatus.FAILURE,
            error="Engine crashed during execution. Command was interrupted.",
        )
        print(f"[ENGINE] RECOVERED stale command: {cmd_row.command_text!r} (source={cmd_row.source})")
        logger.warning(json.dumps({
            "event": "STALE_COMMAND_RECOVERED",
            "cmd_id": cmd_row.id,
            "command": cmd_row.command_text,
            "source": cmd_row.source,
        }))
    return len(stale)


async def engine_loop(ctx: AppContext,
                       max_concurrent: int = _DEFAULT_MAX_CONCURRENT,
                       poll_interval: float = _DEFAULT_POLL_INTERVAL_S,
                       ) -> None:
    """Main engine service loop.

    Polls pending_commands for PENDING rows, executes them concurrently
    via asyncio tasks, and writes results back.

    Args:
        ctx: AppContext with all repositories and broker connection.
        max_concurrent: Maximum number of commands executing simultaneously.
        poll_interval: Seconds between polls of the pending_commands table.
    """
    sem = asyncio.Semaphore(max_concurrent)

    # Startup recovery
    stale_count = await recover_stale_commands(ctx)
    if stale_count:
        print(f"[ENGINE] Recovered {stale_count} stale command(s) from previous crash.")
        logger.warning(json.dumps({
            "event": "STALE_COMMANDS_FOUND", "count": stale_count,
        }))
    else:
        print("[ENGINE] No stale commands found.")

    # Recover any abandoned orders from a previous crash
    abandoned = recover_in_flight_orders(ctx.orders)
    if abandoned:
        print(f"[ENGINE] Found {len(abandoned)} abandoned order(s) from previous crash.")
        logger.warning(json.dumps({
            "event": "ABANDONED_ORDERS_FOUND", "count": len(abandoned),
        }))

    print(f"[ENGINE] Ready. Polling pending_commands (max_concurrent={max_concurrent})...")
    logger.info(json.dumps({
        "event": "ENGINE_LOOP_STARTED", "max_concurrent": max_concurrent,
    }))

    while True:
        try:
            pending = ctx.pending_commands.get_pending()
            for cmd_row in pending:
                ctx.pending_commands.update_status(
                    cmd_row.id, PendingCommandStatus.RUNNING,
                )
                asyncio.create_task(
                    _execute_single_command(cmd_row, ctx, sem),
                )
        except Exception:
            logger.exception('{"event": "ENGINE_POLL_ERROR"}')

        await asyncio.sleep(poll_interval)
