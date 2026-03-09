# ADR-003: SQLite WAL Mode

**Date:** 2026-03-08
**Status:** Accepted

## Decision

Enable SQLite Write-Ahead Logging (WAL) mode on every new connection via `PRAGMA journal_mode=WAL`.

## Reasoning

Two processes — the REPL and the daemon — read and write the same SQLite database concurrently. Default SQLite journal mode uses exclusive locks that would cause one process to block or error while the other is writing. WAL mode allows concurrent readers and one writer simultaneously, which is the exact access pattern of this system: the REPL writes orders and heartbeats while the daemon reads for reconciliation and monitoring.

## Consequences

- `PRAGMA journal_mode=WAL` is executed in the `set_pragmas` SQLAlchemy event listener on every new connection.
- WAL mode creates `-wal` and `-shm` sidecar files alongside the `.db` file. These are not committed to git.
- `PRAGMA foreign_keys=ON` is also set in the same listener to enforce referential integrity.
- The `.gitignore` excludes `*.db`, `*-wal`, and `*-shm` files.

## Future Considerations

If the system migrates to Postgres (for multi-machine or cloud use), WAL mode is replaced by Postgres's native MVCC. The `set_pragmas` listener is the only SQLite-specific code — removing it is the migration step.
