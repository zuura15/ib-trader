"""Unit tests for command queue behaviour in the TUI.

Tests focus on the queue mechanics (full/empty/drain) in isolation using a
simple coroutine harness — no Textual App instantiation required.
"""
import asyncio
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_queue(maxsize: int = 10) -> "asyncio.Queue[str | None]":
    return asyncio.Queue(maxsize=maxsize)


# ---------------------------------------------------------------------------
# Basic queue operations
# ---------------------------------------------------------------------------

class TestCommandQueue:
    @pytest.mark.asyncio
    async def test_put_and_get(self):
        q = make_queue()
        q.put_nowait("buy AAPL 1 mid")
        item = await q.get()
        assert item == "buy AAPL 1 mid"

    @pytest.mark.asyncio
    async def test_queue_full_raises(self):
        q = make_queue(maxsize=2)
        q.put_nowait("cmd1")
        q.put_nowait("cmd2")
        with pytest.raises(asyncio.QueueFull):
            q.put_nowait("cmd3")

    @pytest.mark.asyncio
    async def test_empty_queue_does_not_raise(self):
        q = make_queue()
        assert q.empty()

    @pytest.mark.asyncio
    async def test_sentinel_none_drains_processor(self):
        """None is the stop sentinel — processor should exit when it receives it."""
        q = make_queue()
        results = []

        async def processor():
            while True:
                item = await q.get()
                if item is None:
                    break
                results.append(item)

        q.put_nowait("cmd1")
        q.put_nowait("cmd2")
        q.put_nowait(None)

        await asyncio.wait_for(processor(), timeout=2.0)
        assert results == ["cmd1", "cmd2"]

    @pytest.mark.asyncio
    async def test_commands_processed_in_order(self):
        q = make_queue()
        for i in range(5):
            q.put_nowait(f"cmd{i}")
        q.put_nowait(None)
        order = []

        async def processor():
            while True:
                item = await q.get()
                if item is None:
                    break
                order.append(item)

        await processor()
        assert order == [f"cmd{i}" for i in range(5)]

    @pytest.mark.asyncio
    async def test_processor_handles_error_and_continues(self):
        """Command processor must continue after an exception on one command."""
        q = make_queue()
        q.put_nowait("bad")
        q.put_nowait("good")
        q.put_nowait(None)
        processed = []

        async def processor():
            while True:
                item = await q.get()
                if item is None:
                    break
                try:
                    if item == "bad":
                        raise ValueError("simulated error")
                    processed.append(item)
                except Exception:
                    pass  # Error handled — continue

        await processor()
        assert processed == ["good"]

    @pytest.mark.asyncio
    async def test_exit_command_terminates_processor(self):
        """'exit' causes the processor loop to stop."""
        q = make_queue()
        q.put_nowait("cmd1")
        q.put_nowait("exit")
        q.put_nowait("cmd2")  # Should never be processed
        processed = []

        async def processor():
            while True:
                item = await q.get()
                if item is None:
                    break
                if item == "exit":
                    break
                processed.append(item)

        await processor()
        assert processed == ["cmd1"]
        assert not q.empty()  # cmd2 was never consumed
