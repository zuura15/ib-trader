# ADR-004: Shared SQLite as IPC — No Sockets or Pipes

**Date:** 2026-03-08
**Status:** Accepted

## Decision

The CLI REPL and daemon communicate exclusively through SQLite. No Unix sockets, no named pipes, no shared memory, no message queues.

## Reasoning

Any IPC mechanism that requires both processes to be running simultaneously creates coupling. If the REPL crashes and the daemon is listening on a socket, the daemon must handle the broken connection. If the daemon crashes and the REPL is writing to a pipe, the REPL must handle SIGPIPE. SQLite as the communication medium means each process reads and writes independently — neither process cares whether the other is running. The daemon detects REPL absence by reading a stale heartbeat timestamp. The REPL detects daemon absence by reading a stale heartbeat timestamp. Both actions are reads against a database table, not connection-state management.

## Consequences

- `system_heartbeats` table is the mutual watchdog mechanism.
- `system_alerts` table is how the daemon communicates alert state to any future UI or monitoring tool.
- Adding a third process (e.g., a bot) requires only that it reads/writes to the same SQLite file — no IPC wiring needed.
- SQLite WAL mode (ADR-003) is required to make this concurrent access safe.

## Future Considerations

High-frequency bots will eventually need sub-millisecond coordination that SQLite cannot provide. At that point, a lightweight pub/sub layer (Redis, ZeroMQ) may be introduced for bot-to-engine coordination. The REPL/daemon watchdog mechanism stays as SQLite regardless.
