"""Textual TUI for the IB Trader daemon.

Live auto-refreshing dashboard with interactive command input.
Two zones:
- Top: live dashboard (auto-refreshes every daemon_tui_refresh_seconds)
- Bottom: interactive command input at > prompt

CATASTROPHIC alerts: full red TUI, halts background loops, waits for Enter.
WARNING alerts: amber indicator, loops continue.
"""
import asyncio
from datetime import datetime, timezone
from typing import Callable

from textual.app import App, ComposeResult
from textual.widgets import Header, Footer, Static, Input
from textual.reactive import reactive
from textual import work


class DashboardWidget(Static):
    """Live dashboard showing system status, order counts, and P&L."""

    DEFAULT_CSS = """
    DashboardWidget {
        height: auto;
        border: solid $primary;
        padding: 1 2;
    }
    """

    def __init__(self, get_status: Callable, **kwargs):
        """Initialize with a status callback.

        Args:
            get_status: Callable that returns a dict with dashboard data.
        """
        super().__init__(**kwargs)
        self._get_status = get_status

    def update_content(self, data: dict) -> None:
        """Refresh the dashboard with new data."""
        gateway_status = "\u2713 Connected" if data.get("ib_connected") else "\u2717 Disconnected"
        cli_status = "\u2713 Running" if data.get("repl_alive") else "\u2717 Not running"
        repl_pid = data.get("repl_pid", "-")
        last_recon = data.get("last_recon", "never")
        recon_changes = data.get("recon_changes", 0)

        stats = data.get("stats", {})
        orders_placed = stats.get("placed", 0)
        filled = stats.get("filled", 0)
        canceled = stats.get("canceled", 0)
        open_now = stats.get("open_now", 0)
        abandoned = stats.get("abandoned", 0)
        ext_closed = stats.get("ext_closed", 0)
        pnl = stats.get("pnl", Decimal("0"))
        commission = stats.get("commission", Decimal("0"))

        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
        alerts = data.get("alerts", [])
        alert_line = ""
        if alerts:
            for a in alerts:
                if a.severity.value == "CATASTROPHIC":
                    alert_line = f"\n[bold red]\u26a0 CATASTROPHIC: {a.message}[/bold red]"
                else:
                    alert_line = f"\n[yellow]\u26a0 WARNING: {a.message}[/yellow]"

        self.update(
            f"[bold]IB TRADER DAEMON[/bold]          [green]\u25cf ACTIVE[/green]\n"
            f"{'Gateway':<14} {gateway_status:<20} {timestamp}\n"
            f"{'Last Recon':<14} {last_recon:<20} {recon_changes} changes\n"
            f"{'CLI Status':<14} {cli_status:<20} PID {repl_pid}\n"
            f"\n[bold]TODAY[/bold]\n"
            f"{'Orders Placed':<18} {orders_placed:<6} {'Open Now':<14} {open_now}\n"
            f"{'Filled':<18} {filled:<6} {'Abandoned':<14} {abandoned}\n"
            f"{'Canceled':<18} {canceled:<6} {'Ext. Closed':<14} {ext_closed}\n"
            f"{'Realized P&L':<18} ${pnl}\n"
            f"{'Commissions':<18} -${commission}"
            f"{alert_line}"
        )


class DaemonTUI(App):
    """Main Textual TUI application for the IB Trader daemon.

    Top zone: auto-refreshing dashboard.
    Bottom zone: command input.

    On CATASTROPHIC alert: TUI goes red, all loops pause, waits for Enter.
    On WARNING: amber indicator shown, loops continue.
    """

    CSS = """
    Screen {
        layout: vertical;
    }
    #dashboard {
        height: 1fr;
        border: solid $primary;
        padding: 1 2;
    }
    #alert-banner {
        height: auto;
        background: $error;
        color: $text;
        padding: 1 2;
        display: none;
    }
    #alert-banner.catastrophic {
        display: block;
        background: $error;
    }
    #alert-banner.warning {
        display: block;
        background: $warning;
    }
    #command-input {
        height: 3;
        border: solid $primary;
    }
    """

    BINDINGS = [
        ("ctrl+c", "quit", "Quit"),
    ]

    catastrophic_mode: reactive[bool] = reactive(False)

    def __init__(
        self,
        get_status: Callable,
        handle_command: Callable,
        refresh_seconds: float = 5.0,
        **kwargs,
    ):
        """Initialize the TUI.

        Args:
            get_status: Async callable returning dashboard data dict.
            handle_command: Async callable that handles daemon commands.
            refresh_seconds: Dashboard refresh interval.
        """
        super().__init__(**kwargs)
        self._get_status = get_status
        self._handle_command = handle_command
        self._refresh_seconds = refresh_seconds
        self._loops_paused = False
        self._resume_event = asyncio.Event()

    def compose(self) -> ComposeResult:
        """Build the TUI layout."""
        yield Header(show_clock=True)
        yield Static(id="dashboard")
        yield Static(id="alert-banner")
        yield Input(placeholder="> ", id="command-input")
        yield Footer()

    def on_mount(self) -> None:
        """Start the dashboard refresh loop after mount."""
        self.refresh_dashboard()

    @work(exclusive=True)
    async def refresh_dashboard(self) -> None:
        """Background worker that refreshes the dashboard on interval."""
        while True:
            try:
                data = await self._get_status()
                dashboard = self.query_one("#dashboard", Static)
                self._update_dashboard(dashboard, data)
                self._handle_alerts(data.get("alerts", []))
            except Exception:
                pass
            await asyncio.sleep(self._refresh_seconds)

    def _update_dashboard(self, widget: Static, data: dict) -> None:
        """Update the dashboard widget with fresh data."""
        gateway_status = "\u2713 Connected" if data.get("ib_connected") else "\u2717 Disconnected"
        cli_status = "\u2713 Running" if data.get("repl_alive") else "\u2717 Not running"
        repl_pid = data.get("repl_pid", "-")
        last_recon = data.get("last_recon", "never")
        recon_changes = data.get("recon_changes", 0)
        stats = data.get("stats", {})
        timestamp = datetime.now(timezone.utc).strftime("%H:%M:%S")

        widget.update(
            f"[bold]IB TRADER DAEMON[/bold]          [green]\u25cf CONNECTED[/green]\n"
            f"{'Gateway':<14} {gateway_status:<20} {timestamp}\n"
            f"{'Last Recon':<14} {last_recon:<20} {recon_changes} changes\n"
            f"{'CLI Status':<14} {cli_status:<20} PID {repl_pid}\n"
            f"\n[bold]TODAY[/bold]\n"
            f"{'Orders Placed':<18} {stats.get('placed', 0):<6} "
            f"{'Open Now':<14} {stats.get('open_now', 0)}\n"
            f"{'Filled':<18} {stats.get('filled', 0):<6} "
            f"{'Abandoned':<14} {stats.get('abandoned', 0)}\n"
            f"{'Canceled':<18} {stats.get('canceled', 0):<6} "
            f"{'Ext. Closed':<14} {stats.get('ext_closed', 0)}\n"
            f"{'Realized P&L':<18} +${stats.get('pnl', 0)}\n"
            f"{'Commissions':<18} -${stats.get('commission', 0)}"
        )

    def _handle_alerts(self, alerts: list) -> None:
        """Update alert banner based on current open alerts."""
        banner = self.query_one("#alert-banner", Static)
        if not alerts:
            banner.remove_class("catastrophic", "warning")
            banner.update("")
            self._loops_paused = False
            self._resume_event.set()
            return

        catastrophic = [a for a in alerts if a.severity.value == "CATASTROPHIC"]
        warnings = [a for a in alerts if a.severity.value == "WARNING"]

        if catastrophic:
            a = catastrophic[0]
            banner.remove_class("warning")
            banner.add_class("catastrophic")
            banner.update(
                f"\u26a0 CATASTROPHIC: {a.message}\n"
                f"Fix the issue then press Enter to resume..."
            )
            self._loops_paused = True
            self._resume_event.clear()
        elif warnings:
            a = warnings[0]
            banner.remove_class("catastrophic")
            banner.add_class("warning")
            banner.update(f"\u26a0 WARNING: {a.message}")
            self._loops_paused = False
            self._resume_event.set()

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        """Handle command input from the > prompt."""
        command = event.value.strip()
        event.input.clear()

        if not command:
            # If in CATASTROPHIC mode, Enter resumes
            if self._loops_paused:
                await self._resume_catastrophic()
            return

        await self._handle_command(command)

    async def _resume_catastrophic(self) -> None:
        """Resume from CATASTROPHIC state after user presses Enter."""
        # Resolve open CATASTROPHIC alerts
        # (handled by the daemon main loop watching _resume_event)
        self._loops_paused = False
        self._resume_event.set()

    @property
    def loops_paused(self) -> bool:
        """True if background loops should be paused (CATASTROPHIC state)."""
        return self._loops_paused

    @property
    def resume_event(self) -> asyncio.Event:
        """Event set when CATASTROPHIC state is resolved."""
        return self._resume_event


# Import Decimal here to avoid circular import issues in update_content
from decimal import Decimal  # noqa: E402
