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

    # subscribe_bars and unsubscribe_bars are handled in _execute_single_command
    # since they need async context.

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
            # Handle bar subscription commands before normal parsing
            cmd_text = cmd_row.command_text.strip()
            if cmd_text.startswith("subscribe_bars "):
                symbol = cmd_text.split(maxsplit=1)[1].strip()
                output = await _handle_subscribe_bars(symbol, cmd_ctx)
                cmd_ctx.pending_commands.complete(
                    cmd_row.id, PendingCommandStatus.SUCCESS, output=output,
                )
                print(f"[ENGINE] OK    {cmd_row.command_text!r}")
                return
            if cmd_text.startswith("warmup_bars "):
                parts = cmd_text.split()
                symbol = parts[1]
                duration = int(parts[2]) if len(parts) > 2 else 7200
                output = await _handle_warmup_bars(symbol, duration, cmd_ctx)
                cmd_ctx.pending_commands.complete(
                    cmd_row.id, PendingCommandStatus.SUCCESS, output=output,
                )
                print(f"[ENGINE] OK    {cmd_row.command_text!r}")
                return
            if cmd_text.startswith("unsubscribe_bars "):
                symbol = cmd_text.split(maxsplit=1)[1].strip()
                output = await _handle_unsubscribe_bars(symbol, cmd_ctx)
                cmd_ctx.pending_commands.complete(
                    cmd_row.id, PendingCommandStatus.SUCCESS, output=output,
                )
                print(f"[ENGINE] OK    {cmd_row.command_text!r}")
                return

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

            # Trigger immediate position cache refresh after order execution
            if isinstance(parsed, (BuyCommand, SellCommand, CloseCommand)):
                await asyncio.sleep(2)  # Give IB time to update positions
                from ib_trader.engine.main import position_refresh_event
                position_refresh_event.set()

        except Exception as e:
            print(f"[ENGINE] ERROR {cmd_row.command_text!r} — {e}")
            logger.exception(json.dumps({
                "event": "COMMAND_FAILED",
                "cmd_id": cmd_row.id,
                "error": str(e),
            }))

            # Clean up any orphaned trade groups created by the failed command.
            # When execute_order throws mid-way, it may have created TradeGroup
            # records in SQLite that are stuck OPEN with no confirmed placement.
            try:
                from ib_trader.data.models import TradeStatus
                open_trades = cmd_ctx.trades.get_open()
                for trade in open_trades:
                    if cmd_ctx.transactions.has_unconfirmed_placements(trade.id):
                        cmd_ctx.trades.update_status(trade.id, TradeStatus.CLOSED)
                        logger.info(json.dumps({
                            "event": "ORPHAN_TRADE_CLEANED",
                            "trade_id": trade.id,
                            "symbol": trade.symbol,
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


# Active bar subscriptions: {symbol: con_id}
_bar_subscriptions: dict[str, int] = {}


async def _handle_subscribe_bars(symbol: str, ctx: AppContext) -> str:
    """Subscribe to bar data and streaming quotes for a symbol.

    Two background tasks:
    1. Bar poller: fetches 5-sec bars via reqHistoricalData every 30 seconds
    2. Quote writer: reads streaming ticker every 2 seconds, writes to market_quotes
    """
    from ib_trader.data.models import MarketBar, MarketQuote
    from datetime import datetime, timezone

    if symbol in _bar_subscriptions:
        return f"Already subscribed to bars for {symbol}"

    # Ensure tables exist
    db_engine = ctx.pending_commands._session().get_bind()
    MarketBar.__table__.create(bind=db_engine, checkfirst=True)
    MarketQuote.__table__.create(bind=db_engine, checkfirst=True)

    # Qualify the contract
    contract_info = await ctx.ib.qualify_contract(symbol)
    con_id = contract_info["con_id"]

    session_factory = ctx.pending_commands._session_factory
    _bar_subscriptions[symbol] = con_id

    # Start a background polling task
    async def _poll_bars():
        """Poll IB for recent 5-sec bars every 30 seconds."""
        last_bar_time = None
        while symbol in _bar_subscriptions:
            try:
                await ctx.ib._throttle()
                contract = ctx.ib._contract_cache.get(con_id)
                if contract is None:
                    logger.warning('{"event": "BAR_POLL_NO_CONTRACT", "symbol": "%s"}', symbol)
                    await asyncio.sleep(30)
                    continue

                bars = await ctx.ib._ib.reqHistoricalDataAsync(
                    contract,
                    endDateTime="",
                    durationStr="120 S",
                    barSizeSetting="5 secs",
                    whatToShow="TRADES",
                    useRTH=False,
                    formatDate=2,
                )

                if bars:
                    session = session_factory()
                    new_count = 0
                    for bar in bars:
                        bar_time = bar.date
                        if last_bar_time and bar_time <= last_bar_time:
                            continue
                        bar_row = MarketBar(
                            symbol=symbol,
                            bar_seconds=5,
                            timestamp_utc=bar_time,
                            open=bar.open,
                            high=bar.high,
                            low=bar.low,
                            close=bar.close,
                            volume=int(bar.volume),
                            created_at=datetime.now(timezone.utc),
                        )
                        session.add(bar_row)
                        new_count += 1
                    if new_count > 0:
                        session.commit()
                        last_bar_time = bars[-1].date
                        logger.debug(
                            '{"event": "BARS_POLLED", "symbol": "%s", "new": %d, "total_in_batch": %d}',
                            symbol, new_count, len(bars),
                        )
                    else:
                        session.rollback()

                    # Purge old bars (keep last 24h)
                    try:
                        from datetime import timedelta
                        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
                        session.execute(
                            __import__("sqlalchemy").text(
                                "DELETE FROM market_bars WHERE symbol = :s AND timestamp_utc < :c"
                            ),
                            {"s": symbol, "c": cutoff},
                        )
                        session.commit()
                    except Exception:
                        pass

            except Exception as exc:
                logger.warning('{"event": "BAR_POLL_ERROR", "symbol": "%s", "error": "%s"}',
                               symbol, exc)
                try:
                    session_factory().rollback()
                except Exception:
                    pass

            await asyncio.sleep(30)

    asyncio.create_task(_poll_bars())

    # Also subscribe to streaming market data for real-time quotes
    await ctx.ib.subscribe_market_data(con_id, symbol)

    async def _poll_quotes():
        """Write latest streaming ticker to market_quotes every 2 seconds."""
        while symbol in _bar_subscriptions:
            try:
                ticker = ctx.ib.get_ticker(con_id)
                if ticker:
                    bid = ticker.get("bid")
                    ask = ticker.get("ask")
                    last = ticker.get("last")
                    vol = ticker.get("volume")

                    # Only write if we have at least one valid price
                    if bid or ask or last:
                        session = session_factory()
                        from ib_trader.data.models import MarketQuote
                        # Upsert: update if exists, insert if not
                        existing = session.query(MarketQuote).filter(
                            MarketQuote.symbol == symbol
                        ).first()
                        now = datetime.now(timezone.utc)
                        if existing:
                            if bid: existing.bid = bid
                            if ask: existing.ask = ask
                            if last: existing.last = last
                            if vol: existing.volume = int(vol)
                            existing.updated_at = now
                        else:
                            session.add(MarketQuote(
                                symbol=symbol,
                                bid=bid, ask=ask, last=last,
                                volume=int(vol) if vol else None,
                                updated_at=now,
                            ))
                        session.commit()
            except Exception as exc:
                logger.debug('{"event": "QUOTE_WRITE_ERROR", "symbol": "%s", "error": "%s"}',
                             symbol, exc)
                try:
                    session_factory().rollback()
                except Exception:
                    pass
            await asyncio.sleep(2)

    asyncio.create_task(_poll_quotes())

    logger.info(json.dumps({
        "event": "BARS_SUBSCRIBED", "symbol": symbol, "con_id": con_id,
        "method": "historical_polling + streaming_quotes",
    }))
    return f"Subscribed to bars + quotes for {symbol} (con_id={con_id})"


async def _handle_warmup_bars(symbol: str, duration_seconds: int, ctx: AppContext) -> str:
    """Fetch historical 5-sec bars and write to market_bars for bot warmup."""
    from ib_trader.data.models import MarketBar
    from datetime import datetime, timezone

    db_engine = ctx.pending_commands._session().get_bind()
    MarketBar.__table__.create(bind=db_engine, checkfirst=True)

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

    session_factory = ctx.pending_commands._session_factory
    session = session_factory()
    count = 0
    for bar in bars:
        bar_row = MarketBar(
            symbol=symbol,
            bar_seconds=5,
            timestamp_utc=bar.date,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=int(bar.volume),
            created_at=datetime.now(timezone.utc),
        )
        session.add(bar_row)
        count += 1
    session.commit()

    logger.info(json.dumps({
        "event": "WARMUP_BARS_LOADED", "symbol": symbol,
        "count": count, "duration_s": duration_seconds,
    }))
    return f"Loaded {count} warmup bars for {symbol} ({duration_seconds}s of history)"


async def _handle_unsubscribe_bars(symbol: str, ctx: AppContext) -> str:
    """Unsubscribe from bars and streaming quotes for a symbol."""
    con_id = _bar_subscriptions.pop(symbol, None)
    if con_id is None:
        return f"No active bar subscription for {symbol}"

    await ctx.ib.unsubscribe_realtime_bars(con_id)
    await ctx.ib.unsubscribe_market_data(con_id)
    logger.info(json.dumps({
        "event": "BARS_UNSUBSCRIBED", "symbol": symbol, "con_id": con_id,
    }))
    return f"Unsubscribed from bars + quotes for {symbol}"


async def execute_single_command(
    ctx: AppContext,
    command_text: str,
    source: str = "api",
    order_ref: str | None = None,
) -> dict:
    """Execute a command directly (for the internal HTTP API).

    Unlike the polling-based _execute_single_command which takes a DB row,
    this function takes raw command text and returns the result as a dict.
    Used by the internal API to bypass the pending_commands table.

    Args:
        ctx: AppContext with broker connection.
        command_text: Raw command string (e.g., "buy QQQ 20 mid").
        source: Command source identifier (e.g., "bot:saw-rsi", "api").
        order_ref: Optional orderRef to tag on the IB order.

    Returns:
        Dict with keys: status, output, ib_order_id, serial.
    """
    renderer = _ListRenderer()
    cmd_router = OutputRouter()
    cmd_router.set_renderer(renderer)
    cmd_ctx = dataclasses.replace(ctx, router=cmd_router)

    # Write audit log to pending_commands (write-only, never read on hot path)
    audit_id = None
    if ctx.pending_commands:
        from ib_trader.data.models import PendingCommand
        import uuid
        audit_id = str(uuid.uuid4())
        cmd = PendingCommand(
            id=audit_id,
            source=source,
            command_text=command_text,
            status=PendingCommandStatus.RUNNING,
        )
        ctx.pending_commands._session().add(cmd)
        ctx.pending_commands._session().commit()

    try:
        cmd_text = command_text.strip()

        # Handle bar subscription commands
        if cmd_text.startswith("subscribe_bars "):
            symbol = cmd_text.split(maxsplit=1)[1].strip()
            output = await _handle_subscribe_bars(symbol, cmd_ctx)
            if audit_id:
                ctx.pending_commands.complete(audit_id, PendingCommandStatus.SUCCESS, output=output)
            return {"status": "SUCCESS", "output": output}

        if cmd_text.startswith("warmup_bars "):
            parts = cmd_text.split()
            symbol = parts[1]
            duration = int(parts[2]) if len(parts) > 2 else 7200
            output = await _handle_warmup_bars(symbol, duration, cmd_ctx)
            if audit_id:
                ctx.pending_commands.complete(audit_id, PendingCommandStatus.SUCCESS, output=output)
            return {"status": "SUCCESS", "output": output}

        if cmd_text.startswith("unsubscribe_bars "):
            symbol = cmd_text.split(maxsplit=1)[1].strip()
            output = await _handle_unsubscribe_bars(symbol, cmd_ctx)
            if audit_id:
                ctx.pending_commands.complete(audit_id, PendingCommandStatus.SUCCESS, output=output)
            return {"status": "SUCCESS", "output": output}

        parsed = parse_command(command_text, router=cmd_router)

        if parsed is None:
            error = f"Unknown command: {command_text}"
            if audit_id:
                ctx.pending_commands.complete(audit_id, PendingCommandStatus.FAILURE, error=error)
            return {"status": "FAILURE", "output": error}

        if isinstance(parsed, str):
            output = _handle_builtin(parsed, cmd_ctx)
            if audit_id:
                ctx.pending_commands.complete(audit_id, PendingCommandStatus.SUCCESS, output=output)
            return {"status": "SUCCESS", "output": output}

        if isinstance(parsed, (BuyCommand, SellCommand)):
            from ib_trader.engine.order import execute_order
            # Pass order_ref through the command if provided
            if order_ref and hasattr(parsed, '__dict__'):
                parsed = dataclasses.replace(parsed, order_ref=order_ref) if hasattr(parsed, 'order_ref') else parsed
            await execute_order(parsed, cmd_ctx)
        elif isinstance(parsed, CloseCommand):
            from ib_trader.engine.order import execute_close
            await execute_close(parsed, cmd_ctx)

        output = "\n".join(renderer.messages) if renderer.messages else ""
        if audit_id:
            ctx.pending_commands.complete(audit_id, PendingCommandStatus.SUCCESS, output=output)

        # Extract serial and ib_order_id from output
        result = {"status": "SUCCESS", "output": output}
        for msg in renderer.messages:
            if "Serial:" in msg:
                try:
                    serial_str = msg.split("Serial:")[1].split()[0].strip("#").strip()
                    result["serial"] = int(serial_str)
                except (ValueError, IndexError):
                    pass
            if "ib_order_id=" in msg or "FILLED:" in msg:
                result["ib_order_id"] = ""  # Filled orders have been tracked

        return result

    except Exception as e:
        error = str(e)
        if audit_id:
            ctx.pending_commands.complete(audit_id, PendingCommandStatus.FAILURE, error=error)
        raise


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
    abandoned = recover_in_flight_orders(ctx.transactions, ctx.trades)
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
            # Rollback any poisoned session state so the next poll cycle works
            try:
                ctx.pending_commands._session().rollback()
            except Exception:
                pass

        await asyncio.sleep(poll_interval)
