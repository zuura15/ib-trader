"""Output routing layer for the REPL TUI.

Decouples all engine and command code from the Textual widget layer.
No Textual imports — this module is importable without Textual installed.

OutputRouter is the single wiring point for all REPL output.  Engine code,
command handlers, and the REPL startup sequence all call router.emit() instead
of print().  The router delivers messages to whichever renderer is currently
attached, buffering pre-TUI messages in a deque with a fixed capacity.
"""
from __future__ import annotations

import logging
from collections import deque
from enum import Enum
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Maximum messages buffered while no renderer is attached.
_MAX_BUFFER = 500


class OutputPane(Enum):
    """Target pane for a routed message."""

    LOG = "log"          # Activity log pane (scrolling event stream)
    COMMAND = "command"  # Command output pane (responses to user commands)
    BOTH = "both"        # Both log and command output panes simultaneously


class OutputSeverity(Enum):
    """Display severity of a routed message."""

    DEBUG = "debug"      # Internal debug — log file only, never shown in TUI
    INFO = "info"        # Normal operation information
    SUCCESS = "success"  # Positive outcome (fill, order placed, etc.)
    WARNING = "warning"  # Non-fatal issue
    ERROR = "error"      # Error or failure


@runtime_checkable
class RendererProtocol(Protocol):
    """Protocol that the Textual TUI renderer must implement.

    The OutputRouter calls these methods to update the UI.
    No Textual types appear in this interface — the router has no Textual
    dependency, so it can be imported and unit-tested without Textual installed.
    """

    def write_log(self, message: str, severity: OutputSeverity) -> None:
        """Write a message to the scrolling activity log pane."""
        ...

    def write_command_output(self, message: str, severity: OutputSeverity) -> None:
        """Write a message to the command output pane."""
        ...

    def update_order_row(self, serial: int, data: dict[str, Any]) -> None:
        """Update or insert a row in the orders pane.

        Args:
            serial: Trade serial number (row key).
            data: Dict with keys: symbol, side, qty, status, ib_order_id.
        """
        ...

    def update_header(self, ib_connected: bool, account_id: str, symbol_count: int,
                      last_poll_ok: "Any | None" = None,
                      poll_stale: bool = False) -> None:
        """Update the header pane with connection, account, and poll status.

        Args:
            ib_connected: Whether the IB Gateway connection is live.
            account_id: Account identifier (shown to user).
            symbol_count: Number of symbols in the whitelist.
            last_poll_ok: Datetime of last successful IB poll (optional).
            poll_stale: True if the last poll failed (optional).
        """
        ...


class OutputRouter:
    """Routes output from engine and command code to the TUI renderer.

    Before a renderer is attached (pre-TUI bootstrap), messages are buffered in
    a deque.  Once set_renderer() is called, the buffer is flushed in order.
    Messages that arrive when the buffer is full are dropped and counted in
    _overflow_dropped; the count is logged as a WARNING when the renderer
    attaches.

    Routing rules applied by emit():
    - DEBUG severity: logged to file only — never delivered to any TUI pane.
    - ERROR / WARNING severity: always routed to BOTH panes, regardless of the
      ``pane`` argument passed by the caller.
    - All other messages: routed to the pane specified by the caller.

    Renderer errors are caught and logged — a failing renderer never crashes the
    engine or the REPL loop.

    Thread safety: not thread-safe.  All calls must originate from the asyncio
    event loop thread.
    """

    def __init__(self) -> None:
        self._renderer: RendererProtocol | None = None
        self._buffer: deque[tuple[str, OutputPane, OutputSeverity]] = deque()
        self._overflow_dropped: int = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def set_renderer(self, renderer: RendererProtocol) -> None:
        """Attach the TUI renderer and flush any buffered messages.

        Args:
            renderer: Object implementing RendererProtocol.
        """
        self._renderer = renderer
        if self._overflow_dropped:
            logger.warning(
                '{"event": "OUTPUT_BUFFER_OVERFLOW", "dropped": %d}',
                self._overflow_dropped,
            )
            self._overflow_dropped = 0
        while self._buffer:
            msg, pane, severity = self._buffer.popleft()
            self._render(msg, pane, severity)

    def emit(
        self,
        message: str,
        pane: OutputPane = OutputPane.COMMAND,
        severity: OutputSeverity = OutputSeverity.INFO,
        event: str | None = None,
        **log_kwargs: Any,
    ) -> None:
        """Emit a message to the TUI and/or structured log file.

        Args:
            message: Human-readable message text.
            pane: Target TUI pane.  Ignored for DEBUG severity; overridden to
                BOTH for ERROR and WARNING severity.
            severity: Display severity level.
            event: Structured log event name.  When provided, the log entry is
                written as a JSON object ``{"event": "...", "message": "...", ...}``.
            **log_kwargs: Additional key-value pairs appended to the JSON log entry
                when ``event`` is specified.
        """
        self._log(message, severity, event, log_kwargs)

        # DEBUG messages are file-only.
        if severity == OutputSeverity.DEBUG:
            return

        # ERROR / WARNING always reach both panes regardless of caller intent.
        effective_pane = pane
        if severity in (OutputSeverity.ERROR, OutputSeverity.WARNING):
            effective_pane = OutputPane.BOTH

        if self._renderer is None:
            self._buffer_message(message, effective_pane, severity)
        else:
            self._render(message, effective_pane, severity)

    def update_order_row(self, serial: int, data: dict) -> None:
        """Delegate to renderer if attached, otherwise no-op.

        Also used by the engine's _ListRenderer to capture structured metadata
        (serial number) for internal API responses.
        """
        if self._renderer is not None:
            try:
                self._renderer.update_order_row(serial, data)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _log(
        self,
        message: str,
        severity: OutputSeverity,
        event: str | None,
        extra: dict[str, Any],
    ) -> None:
        """Write the message to the Python logging system."""
        level_map = {
            OutputSeverity.DEBUG: logging.DEBUG,
            OutputSeverity.INFO: logging.INFO,
            OutputSeverity.SUCCESS: logging.INFO,
            OutputSeverity.WARNING: logging.WARNING,
            OutputSeverity.ERROR: logging.ERROR,
        }
        level = level_map[severity]
        if event:
            pairs = ", ".join(f'"{k}": "{v}"' for k, v in extra.items())
            log_msg = f'{{"event": "{event}", "message": "{message}"'
            if pairs:
                log_msg += f", {pairs}"
            log_msg += "}"
            logger.log(level, "%s", log_msg)
        else:
            logger.log(level, "%s", message)

    def _buffer_message(
        self, message: str, pane: OutputPane, severity: OutputSeverity
    ) -> None:
        """Add a message to the pre-renderer buffer, tracking overflow."""
        if len(self._buffer) >= _MAX_BUFFER:
            self._overflow_dropped += 1
            return
        self._buffer.append((message, pane, severity))

    def _render(
        self, message: str, pane: OutputPane, severity: OutputSeverity
    ) -> None:
        """Deliver a message to the attached renderer, catching any errors."""
        if self._renderer is None:
            return
        try:
            if pane in (OutputPane.LOG, OutputPane.BOTH):
                self._renderer.write_log(message, severity)
            if pane in (OutputPane.COMMAND, OutputPane.BOTH):
                self._renderer.write_command_output(message, severity)
        except Exception as exc:
            logger.error(
                '{"event": "RENDERER_ERROR", "error": "%s"}',
                str(exc),
                exc_info=True,
            )
