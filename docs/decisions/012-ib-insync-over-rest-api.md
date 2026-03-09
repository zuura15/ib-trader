# ADR-012: ib_insync Over IB REST API

**Date:** 2026-03-08
**Status:** Accepted

## Decision

Use `ib_insync` (a Python wrapper around IB's TWS API) rather than IB's Client Portal REST API.

## Reasoning

IB's Client Portal REST API requires a separate gateway process, has session management complexity, does not support streaming push events, and has limited order management capabilities. `ib_insync` connects directly to TWS or IB Gateway via TCP, receives push events for fills and order status changes in real time, supports the full order management API (amendment, GTC orders, extended hours flags), and has an async-native design compatible with Python asyncio. The push-event model is essential — the reprice loop needs fill notifications immediately, not via polling.

## Consequences

- `ib_insync` is the sole broker communication library. It is isolated to `ib/insync_client.py`.
- TWS or IB Gateway must be running locally (or on a reachable network address).
- Connection credentials are host/port only — TWS handles authentication. No API keys or passwords in the app.
- `util.startLoop()` is called once in the REPL process. The daemon uses its own independent event loop without `util.startLoop()`.
- Client IDs must be unique: REPL uses `IB_CLIENT_ID`, daemon uses `IB_CLIENT_ID + 1`.

## Future Considerations

If IB deprecates the TWS API in favor of REST-only, the `IBClientBase` abstraction (ADR-007) allows swapping implementations. The REST API's polling model would require rearchitecting the fill notification system, but engine code would remain unchanged.
