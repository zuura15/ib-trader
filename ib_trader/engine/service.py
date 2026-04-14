"""Engine service — command execution helpers for the internal HTTP API.

The engine runs the internal HTTP API (see engine/internal_api.py); this
module provides the execute_single_command coroutine that the API invokes
to run buy/sell/close commands and the historical-bar warmup helper.
"""
import dataclasses
import json
import logging

from ib_trader.config.context import AppContext
from ib_trader.data.models import PendingCommandStatus
from ib_trader.repl.commands import (
    BuyCommand, SellCommand, CloseCommand, parse_command,
)
from ib_trader.repl.output_router import OutputRouter


logger = logging.getLogger(__name__)


def recover_stale_commands(ctx: AppContext) -> int:
    """Mark any RUNNING command-audit rows from a previous crash as FAILURE.

    execute_single_command writes an audit row in RUNNING state at the start
    of every HTTP-API command. If the engine crashes mid-command, that row
    would otherwise stay RUNNING forever and make /api/commands/{id} stuck.
    Called once on engine startup before the internal API begins serving.
    """
    if ctx.pending_commands is None:
        return 0
    stale = ctx.pending_commands.get_by_status(PendingCommandStatus.RUNNING)
    for cmd_row in stale:
        ctx.pending_commands.complete(
            cmd_row.id, PendingCommandStatus.FAILURE,
            error="Engine crashed during execution. Command was interrupted.",
        )
        logger.warning(json.dumps({
            "event": "STALE_COMMAND_RECOVERED",
            "cmd_id": cmd_row.id,
            "command": cmd_row.command_text,
            "source": cmd_row.source,
        }))
    return len(stale)


class _ListRenderer:
    """OutputRouter renderer that collects messages into a list.

    Used by the engine service to capture command output for writing
    back to the pending_commands table. Also captures structured metadata
    (trade serial, order_ref) for the internal HTTP API.
    """

    def __init__(self) -> None:
        self.messages: list[str] = []
        self.metadata: dict = {}  # Structured data: serial, order_ref, etc.

    def write_log(self, message: str, severity=None) -> None:
        self.messages.append(message)

    def write_command_output(self, message: str, severity=None) -> None:
        self.messages.append(message)

    def update_order_row(self, serial, data) -> None:
        # Capture the serial when the order row is updated
        if serial is not None:
            self.metadata["serial"] = serial

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
        open_trades = trades.get_open()
        all_trades = trades.get_all()
        open_orders = ctx.transactions.get_open_orders()

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
        open_orders = ctx.transactions.get_open_orders()
        if not open_orders:
            return "No open orders."
        lines = []
        for txn in open_orders:
            price = txn.limit_price or "MKT"
            lines.append(
                f"  #{txn.trade_serial or '-':>3} {txn.symbol:5} {txn.side:4} "
                f"{txn.quantity} @ {price} [{txn.action.value}] ib_id={txn.ib_order_id}"
            )
        return f"{len(open_orders)} open orders:\n" + "\n".join(lines)

    if verb == "refresh":
        return "Refresh triggered."

    return verb


async def _handle_warmup_bars(symbol: str, duration_seconds: int, ctx: AppContext) -> str:
    """Fetch historical 5-sec bars and publish to the Redis bar stream.

    Bots read warmup bars via XREAD on bar:{symbol}:5s (from "0") — same
    stream the live reqRealTimeBars callback publishes to. No SQLite write.
    """
    from ib_trader.redis.streams import StreamWriter, StreamNames

    contract_info = await ctx.ib.qualify_contract(symbol)
    con_id = contract_info["con_id"]
    contract = ctx.ib._contract_cache.get(con_id)

    if contract is None:
        return f"No cached contract for {symbol}"

    await ctx.ib._throttle()
    bars = await ctx.ib._ib.reqHistoricalDataAsync(
        contract,
        endDateTime="",
        durationStr=f"{duration_seconds} S",
        barSizeSetting="5 secs",
        whatToShow="TRADES",
        useRTH=False,
        formatDate=2,
    )

    if not bars:
        return f"No historical bars returned for {symbol}"

    if ctx.redis is None:
        logger.warning('{"event": "WARMUP_BARS_NO_REDIS", "symbol": "%s"}', symbol)
        return f"Redis unavailable — skipped warmup for {symbol}"

    writer = StreamWriter(ctx.redis, StreamNames.bar(symbol, "5s"), maxlen=5000)
    count = 0
    for bar in bars:
        ts = bar.date
        ts_str = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
        await writer.add({
            "ts": ts_str,
            "o": float(bar.open),
            "h": float(bar.high),
            "l": float(bar.low),
            "c": float(bar.close),
            "v": int(bar.volume),
        })
        count += 1

    logger.info(json.dumps({
        "event": "WARMUP_BARS_PUBLISHED", "symbol": symbol,
        "count": count, "duration_s": duration_seconds,
    }))
    return f"Published {count} warmup bars for {symbol} ({duration_seconds}s of history)"


async def execute_single_command(
    ctx: AppContext,
    command_text: str,
    source: str = "api",
    bot_ref: str | None = None,
) -> dict:
    """Execute a buy/sell/close/built-in command for the internal HTTP API.

    Bar subscription lifecycle (subscribe/warmup/unsubscribe) is handled by
    dedicated endpoints in engine/internal_api.py, not via this path.

    Args:
        ctx: AppContext with broker connection.
        command_text: Raw command string (e.g., "buy QQQ 20 mid").
        source: Command source identifier (e.g., "bot:saw-rsi", "api").
        bot_ref: Optional bot reference ID for orderRef tagging. The engine
                 encodes the full orderRef after allocating the trade serial.

    Returns:
        Dict with keys: status, output, serial, order_ref.
    """
    renderer = _ListRenderer()
    cmd_router = OutputRouter()
    cmd_router.set_renderer(renderer)
    cmd_ctx = dataclasses.replace(ctx, router=cmd_router)

    async def _notify_commands_changed() -> None:
        if getattr(ctx, "redis", None) is not None:
            from ib_trader.redis.streams import publish_activity
            await publish_activity(ctx.redis, "commands")

    # Write audit log to pending_commands (write-only, never read on hot path)
    audit_id = None
    if ctx.pending_commands:
        from ib_trader.data.models import PendingCommand
        import uuid
        from datetime import datetime as _dt, timezone as _tz
        audit_id = str(uuid.uuid4())
        cmd = PendingCommand(
            id=audit_id,
            source=source,
            command_text=command_text,
            status=PendingCommandStatus.RUNNING,
            submitted_at=_dt.now(_tz.utc),
        )
        ctx.pending_commands._session().add(cmd)
        ctx.pending_commands._session().commit()
        await _notify_commands_changed()

    try:
        parsed = parse_command(command_text, router=cmd_router)

        if parsed is None:
            error = f"Unknown command: {command_text}"
            if audit_id:
                ctx.pending_commands.complete(audit_id, PendingCommandStatus.FAILURE, error=error)
                await _notify_commands_changed()
            return {"status": "FAILURE", "output": error}

        if isinstance(parsed, str):
            output = _handle_builtin(parsed, cmd_ctx)
            if audit_id:
                ctx.pending_commands.complete(audit_id, PendingCommandStatus.SUCCESS, output=output)
                await _notify_commands_changed()
            return {"status": "SUCCESS", "output": output}

        if isinstance(parsed, (BuyCommand, SellCommand)):
            from ib_trader.engine.order import execute_order
            # Inject bot_ref so execute_order can encode orderRef after serial allocation
            if bot_ref:
                parsed = dataclasses.replace(parsed, bot_ref=bot_ref)
            await execute_order(parsed, cmd_ctx)
        elif isinstance(parsed, CloseCommand):
            from ib_trader.engine.order import execute_close
            if bot_ref:
                parsed = dataclasses.replace(parsed, bot_ref=bot_ref)
            await execute_close(parsed, cmd_ctx)

        output = "\n".join(renderer.messages) if renderer.messages else ""
        if audit_id:
            ctx.pending_commands.complete(audit_id, PendingCommandStatus.SUCCESS, output=output)
            await _notify_commands_changed()

        # Use structured metadata from the renderer (set by update_order_row
        # and execute_order) instead of parsing output text
        result = {"status": "SUCCESS", "output": output}
        result.update(renderer.metadata)

        # Build order_ref from bot_ref + the engine-allocated serial
        if bot_ref and "serial" in result:
            from ib_trader.engine.order_ref import encode as encode_ref
            side_code = "B" if isinstance(parsed, BuyCommand) else "S"
            try:
                result["order_ref"] = encode_ref(
                    bot_ref, parsed.symbol, side_code, result["serial"],
                )
            except (ValueError, AttributeError):
                pass

        return result

    except Exception as e:
        error = str(e)
        if audit_id:
            ctx.pending_commands.complete(audit_id, PendingCommandStatus.FAILURE, error=error)
            await _notify_commands_changed()
        raise
