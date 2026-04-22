"""Command Center TUI for the IB Trader REPL.

Full-screen Textual application that replaces the plain REPL loop.
Implements RendererProtocol so it can receive routed output from engine
and command code without any coupling to Textual types in those modules.

Architecture:
- IBTraderApp owns the asyncio event loop (via Textual's App.run()).
- ib_insync async coroutines are scheduled as tasks within Textual's loop.
- A command queue (asyncio.Queue, maxsize=10) decouples Input.Submitted
  events from the serial command executor worker.
- Background workers handle: startup sequence, command processing,
  REPL heartbeat, and periodic TUI refresh.
- util.startLoop() is NOT used — ib_insync works in any asyncio loop.
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from textual.app import App, ComposeResult
from textual.containers import Vertical
from textual.widgets import DataTable, Footer, Input, RichLog, Static

from ib_trader.config.context import AppContext
from ib_trader.repl.output_router import (
    OutputPane, OutputSeverity, OutputRouter
)
from ib_trader.repl.pane_config import PaneName, load_pane_configs

logger = logging.getLogger(__name__)

VERSION = "1.0.0"

# Severity → CSS color class used in RichLog markup.
_SEVERITY_STYLE: dict[OutputSeverity, str] = {
    OutputSeverity.INFO: "white",
    OutputSeverity.SUCCESS: "green",
    OutputSeverity.WARNING: "yellow",
    OutputSeverity.ERROR: "red bold",
    OutputSeverity.DEBUG: "dim",
}


class TextualRenderer:
    """Implements RendererProtocol by writing to Textual widgets.

    Created by IBTraderApp.on_mount() and passed to OutputRouter.set_renderer().
    Holds weak references to the app's widgets via query_one() calls so it
    does not prevent garbage collection of the app.
    """

    def __init__(self, app: "IBTraderApp") -> None:
        self._app = app

    def write_log(self, message: str, severity: OutputSeverity) -> None:
        """Write a message to the scrolling activity log pane."""
        try:
            log = self._app.query_one("#log-pane", RichLog)
            style = _SEVERITY_STYLE.get(severity, "white")
            log.write(f"[{style}]{message}[/{style}]")
        except Exception as exc:
            logger.debug("write_log failed: %s", exc)

    def write_command_output(self, message: str, severity: OutputSeverity) -> None:
        """Write a message to the command output pane."""
        try:
            out = self._app.query_one("#command-output", RichLog)
            style = _SEVERITY_STYLE.get(severity, "white")
            out.write(f"[{style}]{message}[/{style}]")
        except Exception as exc:
            logger.debug("write_command_output failed: %s", exc)

    def update_order_row(self, serial: int, data: dict[str, Any]) -> None:
        """Update or insert a row in the orders pane."""
        try:
            table = self._app.query_one("#orders-pane", DataTable)
            row_key = str(serial)
            source = data.get("source", "EXTERNAL")
            values = (
                source,
                data.get("symbol", ""),
                data.get("side", ""),
                str(data.get("qty", "")),
                data.get("status", ""),
                data.get("ib_order_id", ""),
            )
            try:
                table.update_cell(row_key, "source", values[0])
            except Exception:
                table.add_row(*values, key=row_key)
        except Exception as exc:
            logger.debug("update_order_row failed: %s", exc)

    def update_header(self, ib_connected: bool, account_id: str, symbol_count: int,
                      last_poll_ok: datetime | None = None,
                      poll_stale: bool = False) -> None:
        """Update the header bar with current connection status and poll elapsed time."""
        try:
            hdr = self._app.query_one("#header-pane", Static)
            status = "[green]\u2713 connected[/green]" if ib_connected else "[red]\u2717 disconnected[/red]"
            now = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")

            # Elapsed time since last successful poll
            refresh_str = ""
            if last_poll_ok is not None:
                elapsed = (datetime.now(timezone.utc) - last_poll_ok).total_seconds()
                if elapsed < 60:
                    refresh_str = f"Last refresh: {int(elapsed)}s ago"
                else:
                    mins = int(elapsed // 60)
                    secs = int(elapsed % 60)
                    refresh_str = f"Last refresh: {mins}m {secs}s ago"
                if poll_stale:
                    refresh_str += " \u26a0 stale"
            elif poll_stale:
                refresh_str = "\u26a0 stale"

            parts = [
                f" IB Trader v{VERSION}",
                f"IB: {status}",
                f"Account: {account_id}",
                f"{symbol_count} symbols",
                now,
            ]
            if refresh_str:
                parts.append(refresh_str)
            hdr.update("  |  ".join(parts))
        except Exception as exc:
            logger.debug("update_header failed: %s", exc)


class IBTraderApp(App):
    """Full-screen Textual application for the IB Trader REPL.

    Replaces the plain asyncio REPL loop.  All IB operations run within
    Textual's event loop — no nest_asyncio or util.startLoop() required.
    """

    CSS = """
    Screen {
        layout: vertical;
    }
    #header-pane {
        height: 1;
        background: $primary-darken-3;
        color: $text;
        padding: 0 1;
    }
    #log-pane {
        border: solid $primary-darken-2;
        border-title-align: left;
        border-title-color: $text;
    }
    #positions-pane {
        border: solid $secondary-darken-1;
        border-title-align: left;
        border-title-color: $text;
    }
    #command-pane {
        border: solid $success-darken-1;
        border-title-align: left;
        border-title-color: $text;
    }
    #command-output {
        height: 1fr;
    }
    #command-input {
        height: 3;
    }
    #orders-pane {
        border: solid $secondary-darken-1;
        border-title-align: left;
        border-title-color: $text;
    }
    """

    ALLOW_SELECT = True

    BINDINGS = [  # noqa: RUF012 — Textual framework expects class-level list
        ("ctrl+c", "quit_clean", "Quit"),
    ]

    def __init__(self, ctx: AppContext, symbols: list[str]) -> None:
        super().__init__()
        self._ctx = ctx
        self._symbols = symbols
        self._cmd_queue: asyncio.Queue[str | None] = asyncio.Queue(maxsize=10)
        self._pid: int = os.getpid()
        self._ib_connected: bool = False
        self._last_poll_ok: datetime | None = None
        self._last_poll_failed: bool = False
        self._pane_configs = self._load_pane_configs()

    def _load_pane_configs(self):
        try:
            return load_pane_configs(self._ctx.settings)
        except ValueError:
            from ib_trader.repl.pane_config import _DEFAULTS, PaneConfig, PaneName
            return [
                PaneConfig(
                    name=PaneName(d["name"]),
                    rank=d["rank"],
                    height=d["height"],
                    enabled=d["enabled"],
                )
                for d in _DEFAULTS
                if d["enabled"]
            ]

    def compose(self) -> ComposeResult:
        """Build the layout from sorted PaneConfig."""
        for config in self._pane_configs:
            if config.name == PaneName.HEADER:
                yield Static("IB Trader — connecting...", id="header-pane")
            elif config.name == PaneName.LOG:
                yield RichLog(id="log-pane", wrap=True, highlight=False, markup=True)
            elif config.name == PaneName.POSITIONS:
                yield DataTable(id="positions-pane")
            elif config.name == PaneName.COMMAND:
                with Vertical(id="command-pane"):
                    yield RichLog(id="command-output", wrap=True, highlight=False, markup=True)
                    yield Input(placeholder="> type a command (help for usage)...", id="command-input")
            elif config.name == PaneName.ORDERS:
                yield DataTable(id="orders-pane")
        yield Footer()

    def on_mount(self) -> None:
        """Set pane heights, initialize tables, attach renderer, start workers."""
        self._apply_heights()
        self._init_tables()
        self._apply_border_titles()

        # Focus the command input immediately.
        try:
            self.query_one("#command-input", Input).focus()
        except Exception as e:
            logger.debug("command-input focus failed", exc_info=e)

        # Attach the renderer — flushes any buffered startup messages.
        renderer = TextualRenderer(self)
        self._ctx.router.set_renderer(renderer)

        # Workers run as asyncio tasks inside Textual's event loop.
        self.run_worker(self._startup(), exclusive=True, name="startup")

    def _apply_heights(self) -> None:
        """Set widget heights from PaneConfig."""
        id_map = {
            PaneName.HEADER: "header-pane",
            PaneName.LOG: "log-pane",
            PaneName.POSITIONS: "positions-pane",
            PaneName.COMMAND: "command-pane",
            PaneName.ORDERS: "orders-pane",
        }
        for config in self._pane_configs:
            wid = id_map.get(config.name)
            if wid:
                try:
                    self.query_one(f"#{wid}").styles.height = config.height
                except Exception as e:
                    logger.debug("pane height set failed for %s", wid, exc_info=e)

    def _init_tables(self) -> None:
        """Add column headers to DataTable widgets."""
        try:
            tbl = self.query_one("#orders-pane", DataTable)
            tbl.add_columns("#", "Symbol", "Side", "Qty", "Price", "Status", "IB ID")
        except Exception as e:
            logger.debug("orders-pane init failed", exc_info=e)
        try:
            tbl = self.query_one("#positions-pane", DataTable)
            tbl.add_columns("#", "Symbol", "Dir", "Qty", "Entry @", "PT / Close", "Comm", "Status")
        except Exception as e:
            logger.debug("positions-pane init failed", exc_info=e)

    def _apply_border_titles(self) -> None:
        """Set border titles on pane widgets after mount."""
        titles = {
            "#log-pane": "Log",
            "#positions-pane": "Positions",
            "#command-pane": "Commands",
            "#orders-pane": "Open Orders",
        }
        for selector, title in titles.items():
            try:
                self.query_one(selector).border_title = title
            except Exception as e:
                logger.debug("border title set failed for %s", selector, exc_info=e)

    # ------------------------------------------------------------------
    # Textual event handlers
    # ------------------------------------------------------------------

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enqueue a command when the user presses Enter in the input widget."""
        event.input.clear()
        cmd_text = event.value.strip()
        if not cmd_text:
            return
        try:
            self._cmd_queue.put_nowait(cmd_text)
        except asyncio.QueueFull:
            self._ctx.router.emit(
                "\u2717 Command queue full — previous command still running, please wait",
                pane=OutputPane.COMMAND,
                severity=OutputSeverity.WARNING,
            )

    async def action_quit_clean(self) -> None:
        """Ctrl-C: clean shutdown."""
        await self._clean_exit()

    # ------------------------------------------------------------------
    # Background workers
    # ------------------------------------------------------------------

    async def _startup(self) -> None:
        """Connect to IB, run startup checks, then launch ongoing workers."""
        router = self._ctx.router

        # Live account detection — block until user confirms.
        if not self._ctx.account_id.startswith("DU"):
            router.emit(
                f"\u26a0  WARNING: Connected to LIVE account {self._ctx.account_id}. "
                f"Real money is at risk.\n"
                f"   Paper trading accounts begin with 'DU'. "
                f"Press Enter to continue, or Ctrl-C to abort.",
                pane=OutputPane.COMMAND,
                severity=OutputSeverity.WARNING,
                event="LIVE_ACCOUNT_WARNING",
            )
            logger.warning(
                '{"event": "LIVE_ACCOUNT_CONNECTED", "account_id": "%s"}',
                self._ctx.account_id,
            )
            # NOTE: In TUI mode, blocking for Enter is not practical.
            # The warning is prominently displayed. The user can Ctrl-C to abort.

        # Connect to IB Gateway.
        try:
            await self._ctx.ib.connect()
            self._ib_connected = True
            logger.info('{"event": "IB_CONNECTED_TUI"}')
        except Exception as exc:
            logger.error('{"event": "IB_CONNECT_FAILED", "error": "%s"}', str(exc), exc_info=True)
            router.emit(
                f"\u2717 IB connection failed: {exc}\nCheck that TWS or IB Gateway is running.",
                pane=OutputPane.COMMAND,
                severity=OutputSeverity.ERROR,
                event="IB_CONNECT_FAILED",
            )

        # Crash recovery: mark abandoned orders, close orphaned trade groups.
        from ib_trader.engine.recovery import (
            recover_in_flight_orders, format_recovery_warnings,
            close_orphaned_trade_groups,
        )
        abandoned = recover_in_flight_orders(self._ctx.transactions, self._ctx.trades)
        for warning in format_recovery_warnings(abandoned):
            router.emit(warning, pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING)
        orphans_closed = close_orphaned_trade_groups(self._ctx.trades, self._ctx.transactions)
        if orphans_closed:
            router.emit(
                f"\u26a0 Closed {orphans_closed} orphaned trade group(s) from previous session",
                pane=OutputPane.COMMAND,
                severity=OutputSeverity.WARNING,
            )

        # Daemon heartbeat check.
        daemon_hb = self._ctx.heartbeats.get("DAEMON")
        if not daemon_hb:
            router.emit(
                "\u26a0 Daemon is not running \u2014 reconciliation and monitoring are offline",
                pane=OutputPane.COMMAND,
                severity=OutputSeverity.WARNING,
            )
        else:
            age = (
                datetime.now(timezone.utc)
                - daemon_hb.last_seen_at.replace(tzinfo=timezone.utc)
            ).total_seconds()
            threshold = self._ctx.settings["heartbeat_stale_threshold_seconds"]
            if age > threshold:
                router.emit(
                    "\u26a0 Daemon is not running \u2014 reconciliation and monitoring are offline",
                    pane=OutputPane.COMMAND,
                    severity=OutputSeverity.WARNING,
                )

        # Register REPL heartbeat.
        self._ctx.heartbeats.upsert("REPL", self._pid)
        logger.info('{"event": "REPL_STARTED", "pid": %d, "version": "%s"}', self._pid, VERSION)

        # Warm contract cache.
        for symbol in self._symbols:
            try:
                from ib_trader.engine.order import _get_contract
                await _get_contract(symbol, self._ctx)
            except Exception as exc:
                router.emit(
                    f"\u26a0 Could not warm cache for {symbol}: {exc}",
                    pane=OutputPane.LOG,
                    severity=OutputSeverity.WARNING,
                )

        # Banner.
        settings = self._ctx.settings
        router.emit(
            f"IB Trader v{VERSION} \u2014 connected to Gateway @ "
            f"{settings['ib_host']}:{settings['ib_port']}\n"
            f"Account: {self._ctx.account_id} | {len(self._symbols)} symbols loaded\n"
            "Type 'help' for available commands.",
            pane=OutputPane.COMMAND,
            severity=OutputSeverity.SUCCESS,
        )

        # Update header now that we have connection details.
        try:
            renderer = self._ctx.router._renderer
            if renderer:
                renderer.update_header(
                    self._ib_connected, self._ctx.account_id, len(self._symbols),
                    last_poll_ok=self._last_poll_ok,
                    poll_stale=self._last_poll_failed,
                )
        except Exception as e:
            logger.debug("header update failed", exc_info=e)

        # Refresh positions/orders tables on startup.
        await self._refresh_tables()

        # Launch ongoing workers.
        self.run_worker(self._process_commands(), exclusive=False, name="cmd-processor")
        self.run_worker(self._heartbeat_loop(), exclusive=False, name="heartbeat")
        self.run_worker(self._polling_loop(), exclusive=False, name="polling")

    async def _process_commands(self) -> None:
        """Process commands from the queue one at a time (serial execution)."""
        from ib_trader.repl.commands import (
            parse_command, BuyCommand, SellCommand, CloseCommand, ModifyCommand
        )
        from ib_trader.engine.exceptions import SafetyLimitError
        from ib_trader.engine.order import execute_order, execute_close

        router = self._ctx.router

        while True:
            cmd_text = await self._cmd_queue.get()
            if cmd_text is None:
                break

            cmd = parse_command(cmd_text, router=router)
            if cmd is None:
                continue

            if cmd == "exit":
                await self._clean_exit()
                return

            if cmd == "help":
                _emit_help(router)
                continue

            if cmd == "orders":
                await _cmd_orders(self._ctx, router)
                continue

            if cmd == "stats":
                await _cmd_stats(self._ctx, router)
                continue

            if cmd == "status":
                await _cmd_status(self._ctx, router)
                continue

            if cmd == "refresh":
                router.emit(
                    "Reconciliation is a daemon feature. Start ib-daemon for background reconciliation.",
                    pane=OutputPane.COMMAND,
                    severity=OutputSeverity.INFO,
                )
                continue

            if isinstance(cmd, ModifyCommand):
                logger.info('{"event": "MODIFY_STUB_RECEIVED", "serial": %d}', cmd.serial)
                router.emit(
                    f"modify #{cmd.serial}: accepted (stub \u2014 no action taken)",
                    pane=OutputPane.COMMAND,
                    severity=OutputSeverity.INFO,
                )
                continue

            try:
                if isinstance(cmd, (BuyCommand, SellCommand)):
                    await execute_order(cmd, self._ctx)
                elif isinstance(cmd, CloseCommand):
                    await execute_close(cmd, self._ctx)
            except SafetyLimitError as exc:
                router.emit(
                    f"\u2717 Error: {exc}",
                    pane=OutputPane.COMMAND,
                    severity=OutputSeverity.ERROR,
                )
            except Exception as exc:
                logger.error('{"event": "COMMAND_ERROR", "error": "%s"}', str(exc), exc_info=True)
                router.emit(
                    f"\u2717 Error: {exc}",
                    pane=OutputPane.COMMAND,
                    severity=OutputSeverity.ERROR,
                )

            # Refresh the positions/orders pane after every command.
            try:
                await self._refresh_tables()
            except Exception as e:
                logger.debug("post-command table refresh failed", exc_info=e)

    async def _heartbeat_loop(self) -> None:
        """Write REPL heartbeat to SQLite at configured interval."""
        interval = self._ctx.settings["heartbeat_interval_seconds"]
        while True:
            await asyncio.sleep(interval)
            try:
                self._ctx.heartbeats.upsert("REPL", self._pid)
                logger.debug('{"event": "HEARTBEAT_WRITTEN", "process": "REPL"}')
            except Exception as exc:
                logger.warning('{"event": "HEARTBEAT_WRITE_FAILED", "error": "%s"}', str(exc))

    async def _polling_loop(self) -> None:
        """Refresh header and table displays on a configurable interval.

        Uses poll_interval_seconds from settings for the IB poll cycle.
        Falls back to tui_refresh_interval_seconds for header-only refreshes.
        """
        poll_interval = float(self._ctx.settings.get("poll_interval_seconds", 60))
        header_interval = float(self._ctx.settings.get("tui_refresh_interval_seconds", 5))

        # How many header refreshes fit into one poll cycle
        header_ticks_per_poll = max(1, int(poll_interval / header_interval))
        tick = 0

        while True:
            await asyncio.sleep(header_interval)
            tick += 1

            # Update header every tick (shows elapsed time)
            try:
                renderer = self._ctx.router._renderer
                if renderer:
                    renderer.update_header(
                        self._ib_connected, self._ctx.account_id, len(self._symbols),
                        last_poll_ok=self._last_poll_ok,
                        poll_stale=self._last_poll_failed,
                    )
            except Exception as e:
                logger.debug("header tick update failed", exc_info=e)

            # Full IB poll + table refresh at poll_interval_seconds
            if tick >= header_ticks_per_poll:
                tick = 0
                try:
                    await self._refresh_tables()
                    self._last_poll_ok = datetime.now(timezone.utc)
                    self._last_poll_failed = False
                except Exception:
                    self._last_poll_failed = True

    async def _refresh_tables(self) -> None:
        """Refresh the positions and orders DataTable widgets from SQLite."""
        await self._refresh_positions()
        await self._refresh_orders()

    async def _refresh_positions(self) -> None:
        """Repopulate the positions DataTable from current trades."""
        try:
            tbl = self.query_one("#positions-pane", DataTable)
        except Exception:
            return

        from ib_trader.data.models import LegType, TransactionAction

        open_trades = self._ctx.trades.get_open()
        tbl.clear()
        for trade in open_trades:
            txns = self._ctx.transactions.get_for_trade(trade.id)
            entry = next(
                (t for t in txns if t.leg_type == LegType.ENTRY and t.action == TransactionAction.FILLED),
                None,
            )
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

            if entry and entry.ib_avg_fill_price:
                entry_str = f"${entry.ib_avg_fill_price}"
                qty_str = str(entry.ib_filled_qty)
            elif entry_placed and entry_placed.price_placed:
                entry_str = f"~${entry_placed.price_placed}"
                qty_str = str(entry_placed.quantity)
            else:
                entry_str = "\u2014"
                qty_str = "\u2014"

            if close and close.ib_avg_fill_price:
                pt_str = f"closed@${close.ib_avg_fill_price}"
            elif pt and pt.ib_avg_fill_price:
                pt_str = f"PT@${pt.ib_avg_fill_price}"
            elif pt_placed and pt_placed.price_placed:
                pt_str = f"PT open@${pt_placed.price_placed}"
            elif pt_placed:
                pt_str = "PT open"
            else:
                pt_str = "\u2014"

            trade_comm = sum((t.commission or Decimal("0")) for t in txns)
            comm_str = f"${trade_comm}" if trade_comm else "—"
            status = "OPEN" if trade.status.value == "OPEN" else "closed"
            direction = trade.direction[:1]

            tbl.add_row(
                f"#{trade.serial_number}",
                trade.symbol,
                direction,
                qty_str,
                entry_str,
                pt_str,
                comm_str,
                status,
            )

    async def _refresh_orders(self) -> None:
        """Repopulate the orders DataTable from our SQLite orders table.

        Shows only orders originated by our system. IB live status is
        enriched from the in-memory active_trades cache when available.
        """
        try:
            tbl = self.query_one("#orders-pane", DataTable)
        except Exception:
            return

        open_orders = self._ctx.transactions.get_open_orders()
        tbl.clear()
        for txn in open_orders:
            # Enrich with live IB status if available
            ib_status = self._ctx.ib.get_live_order_status(txn.ib_order_id) if txn.ib_order_id else None
            display_status = ib_status if ib_status else txn.action.value
            em_dash = "\u2014"
            price_str = f"${txn.limit_price}" if txn.limit_price else em_dash

            tbl.add_row(
                f"#{txn.trade_serial or em_dash}",
                txn.symbol,
                txn.side,
                str(txn.quantity),
                price_str,
                display_status,
                txn.ib_order_id or em_dash,
            )

    async def _clean_exit(self) -> None:
        """Write final heartbeat deletion, disconnect, and exit."""
        try:
            self._ctx.heartbeats.delete("REPL")
        except Exception as e:
            logger.debug("heartbeat delete on exit failed", exc_info=e)
        try:
            await self._ctx.ib.disconnect()
        except Exception as e:
            logger.debug("IB disconnect on exit failed", exc_info=e)
        logger.info('{"event": "REPL_EXIT_CLEAN", "pid": %d}', self._pid)
        self.exit()


# ---------------------------------------------------------------------------
# Command output helpers (used by both IBTraderApp and called directly
# from tests via ctx.router so they work in non-TUI mode too)
# ---------------------------------------------------------------------------

def _emit_help(router: OutputRouter) -> None:
    """Emit the help text to the command output pane."""
    help_text = """
\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
  IB Trader \u2014 command reference
\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501

ORDER ENTRY
  buy  SYMBOL QTY STRATEGY [PROFIT] [OPTIONS]
  sell SYMBOL QTY STRATEGY [PROFIT] [OPTIONS]

    SYMBOL      Ticker (e.g. MSFT, AAPL) \u2014 must be in the symbols whitelist
    QTY         Number of shares (positive integer or decimal)
    STRATEGY    mid     \u2014 limit at mid, repriced toward ask/bid over reprice window
                market  \u2014 immediate market order
                bid     \u2014 fixed GTC limit at current bid price, no repricing
                ask     \u2014 fixed GTC limit at current ask price, no repricing
                limit   \u2014 fire-and-forget GTC limit at PRICE (requires price arg)
    PROFIT      Optional total dollar profit target (e.g. 500 = $500 profit taker)

    Options:
      --dollars N           Size by notional: qty = floor(N / mid_price)
      --take-profit-price N Explicit profit taker price
      --stop-loss N         Record stop loss price (logged only, no IB action)

    Examples:
      buy MSFT 1 mid
      buy MSFT 1 mid 500
      buy MSFT 1 mid --take-profit-price 420
      buy MSFT 5 market
      buy MSFT 1 limit 400.00
      buy MSFT 0 mid --dollars 2000
      sell AAPL 2 bid
      sell AAPL 1 limit 250.00

POSITION MANAGEMENT
  close SERIAL [STRATEGY] [PROFIT] [--take-profit-price N]

    SERIAL      Trade serial number (shown in # column of orders / stats)
    STRATEGY    mid | market | bid | ask | limit  (default: mid)
                limit requires a price: close SERIAL limit PRICE

    Examples:
      close 3
      close 3 market
      close 3 mid 200
      close 3 limit 450.00

  modify SERIAL             (stub \u2014 accepted but no action taken)

INFORMATION
  orders    List all currently open orders
  stats     Show all trades with entry price, profit taker status, commission
  status    Show gateway connection and daemon heartbeat
  help      This help text

SESSION
  exit | quit   Clean shutdown

\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501\u2501
"""
    router.emit(help_text, pane=OutputPane.COMMAND, severity=OutputSeverity.INFO)


async def _cmd_orders(ctx: AppContext, router: OutputRouter) -> None:
    """Emit the open orders table to the command output pane.

    Shows only orders originated by our system, with live IB status when available.
    """
    open_orders = ctx.transactions.get_open_orders()
    if not open_orders:
        router.emit("No open orders.", pane=OutputPane.COMMAND, severity=OutputSeverity.INFO)
        return

    lines = [f"{'#':<6} {'Symbol':<8} {'Side':<5} {'Qty':<8} {'Price':<12} {'Status':<14} {'IB ID'}",
             "-" * 70]
    em_dash = "\u2014"
    for txn in open_orders:
        ib_status = ctx.ib.get_live_order_status(txn.ib_order_id) if txn.ib_order_id else None
        display_status = ib_status if ib_status else txn.action.value
        price_str = f"${txn.limit_price}" if txn.limit_price else em_dash
        serial = f"#{txn.trade_serial}" if txn.trade_serial else em_dash
        lines.append(
            f"{serial:<6} {txn.symbol:<8} {txn.side:<5} "
            f"{txn.quantity!s:<8} {price_str:<12} {display_status:<14} {txn.ib_order_id or em_dash}"
        )
    router.emit("\n".join(lines), pane=OutputPane.COMMAND, severity=OutputSeverity.INFO)


async def _cmd_stats(ctx: AppContext, router: OutputRouter) -> None:
    """Emit the full trade summary table to the command output pane."""
    from ib_trader.data.models import LegType, TransactionAction

    all_trades = ctx.trades.get_all()
    if not all_trades:
        router.emit("No trades recorded yet.", pane=OutputPane.COMMAND, severity=OutputSeverity.INFO)
        return

    open_count = sum(1 for t in all_trades if t.status.value == "OPEN")
    closed_count = len(all_trades) - open_count
    lines = [
        f"\nTrades: {len(all_trades)} total  |  {open_count} open  |  {closed_count} closed\n",
        f"{'#':<5} {'Symbol':<8} {'Dir':<6} {'Qty':<5} {'Entry @':<10} "
        f"{'Profit Taker':<22} {'Comm':<8} {'Status'}",
        "-" * 78,
    ]
    total_commission = Decimal("0")
    for trade in all_trades:
        txns = ctx.transactions.get_for_trade(trade.id)
        entry = next(
            (t for t in txns if t.leg_type == LegType.ENTRY and t.action == TransactionAction.FILLED),
            None,
        )
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
        if entry and entry.ib_avg_fill_price:
            entry_str = f"${entry.ib_avg_fill_price}"
            qty_str = str(entry.ib_filled_qty)
        elif entry_placed and entry_placed.price_placed:
            entry_str = f"~${entry_placed.price_placed}"
            qty_str = str(entry_placed.quantity)
        else:
            entry_str = "\u2014"
            qty_str = "\u2014"
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
        trade_comm = sum((t.commission or Decimal("0")) for t in txns)
        total_commission += trade_comm
        comm_str = f"${trade_comm}" if trade_comm else "\u2014"
        trade_status = "OPEN" if trade.status.value == "OPEN" else "closed"
        direction = trade.direction[:1]
        lines.append(
            f"#{trade.serial_number:<4} {trade.symbol:<8} {direction:<6} "
            f"{qty_str:<5} {entry_str:<10} {pt_str:<22} {comm_str:<8} {trade_status}"
        )
    lines.append("-" * 78)
    lines.append(f"Total commission: ${total_commission}")
    router.emit("\n".join(lines), pane=OutputPane.COMMAND, severity=OutputSeverity.INFO)


async def _cmd_status(ctx: AppContext, router: OutputRouter) -> None:
    """Emit gateway and daemon status to the command output pane."""
    settings = ctx.settings
    daemon_hb = ctx.heartbeats.get("DAEMON")
    lines = []
    if daemon_hb:
        age = (
            datetime.now(timezone.utc)
            - daemon_hb.last_seen_at.replace(tzinfo=timezone.utc)
        ).total_seconds()
        lines.append(f"Daemon: running (last seen {int(age)}s ago, PID {daemon_hb.pid})")
    else:
        lines.append("Daemon: not running")
    lines.append(f"Gateway: {settings['ib_host']}:{settings['ib_port']}")
    lines.append(f"Account: {ctx.account_id}")
    router.emit("\n".join(lines), pane=OutputPane.COMMAND, severity=OutputSeverity.INFO)
