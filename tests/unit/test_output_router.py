"""Unit tests for OutputRouter, OutputPane, OutputSeverity, and RendererProtocol."""
import pytest
from unittest.mock import MagicMock

from ib_trader.repl.output_router import (
    OutputPane, OutputSeverity, OutputRouter, RendererProtocol
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_router() -> OutputRouter:
    return OutputRouter()


def make_renderer() -> MagicMock:
    """Mock implementing RendererProtocol."""
    renderer = MagicMock(spec=RendererProtocol)
    return renderer


# ---------------------------------------------------------------------------
# Buffering (pre-renderer)
# ---------------------------------------------------------------------------

class TestBuffering:
    def test_emit_before_renderer_buffers_message(self):
        router = make_router()
        router.emit("hello")
        assert len(router._buffer) == 1

    def test_debug_never_buffered(self):
        router = make_router()
        router.emit("debug msg", severity=OutputSeverity.DEBUG)
        assert len(router._buffer) == 0

    def test_buffer_overflow_drops_and_counts(self):
        from ib_trader.repl.output_router import _MAX_BUFFER
        router = make_router()
        for i in range(_MAX_BUFFER + 10):
            router.emit(f"msg {i}")
        assert len(router._buffer) == _MAX_BUFFER
        assert router._overflow_dropped == 10

    def test_flush_on_set_renderer_clears_buffer(self):
        router = make_router()
        router.emit("before renderer")
        renderer = make_renderer()
        router.set_renderer(renderer)
        assert len(router._buffer) == 0
        renderer.write_command_output.assert_called_once()

    def test_flush_delivers_in_order(self):
        router = make_router()
        router.emit("first", pane=OutputPane.COMMAND)
        router.emit("second", pane=OutputPane.COMMAND)
        renderer = make_renderer()
        calls = []
        renderer.write_command_output.side_effect = lambda msg, sev: calls.append(msg)
        router.set_renderer(renderer)
        assert calls == ["first", "second"]

    def test_overflow_count_logged_and_reset_on_set_renderer(self, caplog):
        from ib_trader.repl.output_router import _MAX_BUFFER
        import logging
        router = make_router()
        for i in range(_MAX_BUFFER + 5):
            router.emit(f"msg {i}")
        renderer = make_renderer()
        with caplog.at_level(logging.WARNING, logger="ib_trader.repl.output_router"):
            router.set_renderer(renderer)
        assert "OUTPUT_BUFFER_OVERFLOW" in caplog.text
        assert router._overflow_dropped == 0


# ---------------------------------------------------------------------------
# Routing rules
# ---------------------------------------------------------------------------

class TestRoutingRules:
    def setup_method(self):
        self.router = make_router()
        self.renderer = make_renderer()
        self.router.set_renderer(self.renderer)

    def test_command_pane_writes_command_output(self):
        self.router.emit("cmd msg", pane=OutputPane.COMMAND)
        self.renderer.write_command_output.assert_called_once_with("cmd msg", OutputSeverity.INFO)
        self.renderer.write_log.assert_not_called()

    def test_log_pane_writes_log_only(self):
        self.router.emit("log msg", pane=OutputPane.LOG)
        self.renderer.write_log.assert_called_once_with("log msg", OutputSeverity.INFO)
        self.renderer.write_command_output.assert_not_called()

    def test_both_pane_writes_to_both(self):
        self.router.emit("both msg", pane=OutputPane.BOTH)
        self.renderer.write_log.assert_called_once()
        self.renderer.write_command_output.assert_called_once()

    def test_error_severity_overrides_pane_to_both(self):
        self.router.emit("err", pane=OutputPane.LOG, severity=OutputSeverity.ERROR)
        self.renderer.write_log.assert_called_once()
        self.renderer.write_command_output.assert_called_once()

    def test_warning_severity_overrides_pane_to_both(self):
        self.router.emit("warn", pane=OutputPane.COMMAND, severity=OutputSeverity.WARNING)
        self.renderer.write_log.assert_called_once()
        self.renderer.write_command_output.assert_called_once()

    def test_success_severity_respects_pane(self):
        self.router.emit("ok", pane=OutputPane.COMMAND, severity=OutputSeverity.SUCCESS)
        self.renderer.write_command_output.assert_called_once_with("ok", OutputSeverity.SUCCESS)
        self.renderer.write_log.assert_not_called()

    def test_debug_severity_never_reaches_renderer(self):
        self.router.emit("debug", severity=OutputSeverity.DEBUG)
        self.renderer.write_log.assert_not_called()
        self.renderer.write_command_output.assert_not_called()

    def test_default_pane_is_command(self):
        self.router.emit("default")
        self.renderer.write_command_output.assert_called_once()
        self.renderer.write_log.assert_not_called()

    def test_default_severity_is_info(self):
        self.router.emit("msg")
        self.renderer.write_command_output.assert_called_once_with("msg", OutputSeverity.INFO)


# ---------------------------------------------------------------------------
# Renderer error handling
# ---------------------------------------------------------------------------

class TestRendererErrors:
    def test_renderer_exception_does_not_propagate(self):
        router = make_router()
        renderer = make_renderer()
        renderer.write_command_output.side_effect = RuntimeError("widget gone")
        router.set_renderer(renderer)
        # Should not raise
        router.emit("msg")

    def test_renderer_error_logged(self, caplog):
        import logging
        router = make_router()
        renderer = make_renderer()
        renderer.write_command_output.side_effect = RuntimeError("boom")
        router.set_renderer(renderer)
        with caplog.at_level(logging.ERROR, logger="ib_trader.repl.output_router"):
            router.emit("msg")
        assert "RENDERER_ERROR" in caplog.text


# ---------------------------------------------------------------------------
# RendererProtocol structural check
# ---------------------------------------------------------------------------

class TestRendererProtocol:
    def test_mock_satisfies_protocol(self):
        renderer = make_renderer()
        assert isinstance(renderer, RendererProtocol)

    def test_minimal_class_satisfies_protocol(self):
        from typing import Any

        class MinimalRenderer:
            def write_log(self, message: str, severity: OutputSeverity) -> None:
                pass
            def write_command_output(self, message: str, severity: OutputSeverity) -> None:
                pass
            def update_order_row(self, serial: int, data: dict[str, Any]) -> None:
                pass
            def update_header(self, ib_connected: bool, account_id: str, symbol_count: int) -> None:
                pass

        assert isinstance(MinimalRenderer(), RendererProtocol)
