# ADR-002: Zero In-Memory State

**Date:** 2026-03-08
**Status:** Accepted

## Decision

No order state, trade state, or position state lives in Python process memory. Every event is written to SQLite before the next operation proceeds. On startup, the application reconstructs all context from SQLite alone.

## Reasoning

Trading systems that keep state in memory lose it on crash. A crash mid-reprice-loop at 10:32 AM would silently orphan an open order at an unknown price step with no record. By writing to SQLite before each action, a crashed process leaves a complete audit trail. The restarting process can determine exactly what was in flight, warn the user, and let them handle it manually — rather than making potentially uninformed automated decisions in a changed market.

## Consequences

- Every engine function writes to SQLite before calling IB and after receiving the IB response.
- The sequence is always: write intent to DB → call IB → write IB response to DB → proceed.
- Cache is allowed only for static contract data (refreshed daily). Never for order or trade state.
- On crash recovery, orders in `REPRICING` or `AMENDING` state are marked `ABANDONED` with a warning to the user.
- The `OrderTracker` in `engine/tracker.py` maps IB order IDs to asyncio Events — this is coordination state only, not trade state. It is rebuilt from SQLite on startup.

## Future Considerations

This pattern naturally supports future migration to a distributed architecture where multiple REPL instances write to a shared database (Postgres). The SQLite WAL mode choice (ADR-003) is the current concurrency strategy; Postgres would replace it without changing the state model.
