# ADR-013: Textual App as Event Loop Owner — ib_insync Integration

**Status**: Accepted
**Date**: 2026-03-10

---

## Context

The REPL originally ran inside `asyncio.run(run_repl(...))` with
`ib_insync.util.startLoop()` called beforehand. `util.startLoop()` applies
`nest_asyncio` to allow `asyncio.run()` to be called from within an already-
running event loop — a pattern designed for Jupyter notebooks where a loop is
already running. In a plain Python process this was redundant; `asyncio.run()`
was the loop owner.

The Command Center TUI feature replaces the plain REPL loop with a full-screen
Textual application. Textual's `App.run()` creates and owns the asyncio event
loop for the lifetime of the session. This requires a decision on how to
integrate ib_insync within Textual's loop.

---

## Decision

1. **Remove `util.startLoop()`.**  It is a Jupyter compatibility shim and
   provides no benefit when Textual owns the loop.

2. **Replace `asyncio.run(run_repl(...))` with `IBTraderApp(...).run()`.**
   Textual's `App.run()` creates the event loop and drives it.

3. **Run ib_insync operations as `asyncio.create_task()` / `run_worker()`
   calls inside Textual's loop.**  ib_insync's `IB` class is built on pure
   asyncio coroutines and awaitable methods; it has no requirement for a
   specific event loop implementation.  Any running asyncio event loop
   suffices.

4. **IB connection state is detected via the polling loop**, not via a direct
   event hook from the `ib/` layer.  The TUI polls every
   `tui_refresh_interval_seconds` (default 5 s) to check connectivity and
   refresh the header.  This trades some latency (up to 5 s to show
   a disconnect) for keeping the `ib/` abstraction layer clean.

---

## Consequences

- **Positive**: No `nest_asyncio` dependency in production code paths.  The
  Textual app and ib_insync share a single event loop cleanly.

- **Positive**: ib_insync `errorEvent`, `execDetailsEvent`, and
  `orderStatusEvent` callbacks fire as coroutines within the same loop as the
  Textual UI, so no cross-thread synchronisation is required.

- **Negative**: IB disconnect detection has up to `tui_refresh_interval_seconds`
  latency before the header updates.  A real-time solution would require
  exposing a connectivity event from `insync_client.py` to the TUI layer,
  coupling the two layers.  This trade-off is acceptable.

- **Negative**: `run_repl()` in `repl/main.py` is now dead code (no longer
  called by `main()`).  It remains for reference and could be reinstated for
  a non-TUI mode if required in the future.

---

## Alternatives Considered

### A — Keep `util.startLoop()`, run Textual inside the existing loop

Rejected. Textual's `App.run()` starts its own asyncio event loop.  Nesting
it inside one already started by `startLoop()` would require `nest_asyncio`,
add complexity, and is unsupported by Textual.

### B — Run ib_insync in a separate thread

Rejected. ib_insync is asyncio-native; running it in a thread while the TUI
runs in another introduces cross-thread queue complexity and removes the ability
to `await` IB responses directly in engine coroutines.

### C — Use Textual `on_worker_state_changed` to gate UI refresh on fill events

Deferred. The current polling model is sufficient.  Fill events already update
SQLite synchronously, so the TUI will reflect fills within one poll interval
regardless.
