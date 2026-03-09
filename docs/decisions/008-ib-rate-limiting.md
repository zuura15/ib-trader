# ADR-008: IB Rate Limiting via Throttle Layer

**Date:** 2026-03-08
**Status:** Accepted

## Decision

A throttle layer inside `ib/base.py` enforces a minimum interval between IB API calls (default 100ms). All methods on `IBClientBase` pass through this layer automatically. Pacing violations trigger exponential backoff.

## Reasoning

IB enforces pacing limits and will reject requests that arrive too fast. Handling pacing at the call site (in engine code) would scatter timing logic across the codebase and make it easy to introduce a new IB call that bypasses the limit. A single throttle layer in the abstraction ensures every call is paced, regardless of where it originates. The reprice loop, reconciliation loop, and contract cache all call IB through the same throttle.

## Consequences

- `IBClientBase` includes a throttle mechanism tracking the timestamp of the last API call.
- Calls arriving faster than `ib_min_call_interval_ms` (default 100ms) are delayed via `asyncio.sleep`.
- `IB_THROTTLED` events are logged at DEBUG level whenever a call is delayed.
- IB pacing violation errors (error code 100 or similar) trigger exponential backoff: `retry_delay_seconds * (retry_backoff_multiplier ** attempt)`.
- The throttle is async-safe — concurrent coroutines share the same throttle state via asyncio locks.

## Future Considerations

If the system moves to a multi-process architecture where multiple REPL instances share an IB connection, the throttle must be promoted to a shared resource (e.g., a Redis-backed rate limiter). For now, single-process throttling is sufficient.
