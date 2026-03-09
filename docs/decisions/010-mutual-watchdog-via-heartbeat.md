# ADR-010: Mutual Watchdog via SQLite Heartbeats

**Date:** 2026-03-08
**Status:** Accepted

## Decision

The REPL and daemon watch each other by writing heartbeat timestamps to the `system_heartbeats` SQLite table every 30 seconds. Neither process sends signals or messages to the other — they read timestamps from the database.

## Reasoning

Any watchdog mechanism that requires both processes to be running (socket ping, signal handler) creates coupling and failure modes. A process that fails to respond to a ping might be heavily loaded, not crashed. A SQLite timestamp read is a passive, non-intrusive check with no false positives from transient load. If the REPL's heartbeat is stale beyond the configured threshold, it has either crashed or been killed — the daemon escalates to CATASTROPHIC. The daemon's heartbeat staleness is a WARNING to the REPL (reconciliation offline) but not a blocker.

## Consequences

- `system_heartbeats` table has one row per process (`REPL` and `DAEMON`).
- Heartbeat writes happen every `heartbeat_interval_seconds` (default 30s) in a background asyncio task.
- Stale threshold for REPL heartbeat detection by daemon: `heartbeat_stale_threshold_seconds` (default 300s = 5 min).
- On REPL clean exit: heartbeat row is deleted. A missing row is treated as "not running" (WARNING), not "crashed" (CATASTROPHIC). Only a stale (present but old) row triggers CATASTROPHIC.
- PID is written alongside the heartbeat for diagnostic purposes (not used programmatically).

## Future Considerations

If the system runs across multiple machines, the heartbeat mechanism extends naturally: each process writes to a shared database (Postgres) with a hostname column. The stale detection logic is unchanged.
