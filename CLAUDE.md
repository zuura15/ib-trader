# Engineering Standards — IB Trader

These rules apply to every change made to this codebase, regardless of size.
No exceptions. Treat these as team-level non-negotiables.

## Data & State
- NEVER use `float` for monetary values. Always use `Decimal` or integer cents.
- NEVER store order state, trade state, or position state in memory.
- ALL state lives in SQLite. If it's not in the DB, it doesn't exist.
- Every operation follows: do the thing in IB → write result to SQLite → proceed.
- Never proceed to the next step if the previous write to SQLite failed.

## IB API
- ALL IB API calls must go through the abstraction layer in `ib/base.py`.
- NEVER call IB directly from engine, CLI, or daemon code.
- ALL IB calls are subject to the global rate limiter (default 100ms minimum between calls).
- NEVER add a new IB call without going through the throttle layer.
- ALL IB API responses (raw JSON) must be stored in SQLite for audit.

## Logging
- EVERY operation must be logged in structured JSON format.
- Every log entry must include: timestamp, level, event name, and relevant IDs (trade_id, serial, symbol).
- Log levels: DEBUG for throttle/cache hits, INFO for normal operations, WARNING for recoverable issues, ERROR for failures.
- NEVER swallow an exception silently. Always log with full stack trace and re-raise or surface to user.
- New operations must add new named event types — never log freeform strings for structured events.

## Error Handling
- ALL IB API calls must be wrapped in retry logic (default 3 retries, configurable).
- On final retry failure: log full context, print clear human-readable error, exit non-zero.
- NEVER catch a generic Exception without logging and re-raising.
- Order rejections from IB must surface the rejection reason clearly — never hide it.

## Testing
- Every new function in `engine/`, `ib/`, or `data/` must have a corresponding unit test.
- Every new CLI command must have an integration test.
- Tests must run with NO live TWS or IB Gateway connection (use mock IB layer).
- NEVER add a feature without running the full test suite first.
- Smoke tests that require live TWS/Gateway must be tagged @pytest.mark.smoke.
- Coverage must not drop below 90% on core engine modules.

## Code Style
- ALL monetary values: Decimal, never float.
- ALL UUIDs generated with uuid4().
- ALL datetimes stored and compared in UTC.
- ALL config values read from settings.yaml — never hardcode tunables.
- ALL secrets read from .env — never in code or settings.yaml.
- Repository pattern for ALL database access — never write raw SQL outside data/ layer.
- Abstract interfaces in ib/base.py — never instantiate insync_client.py directly outside dependency injection.

## Security
- .env file permissions must be 600. Verified on every startup.
- SQLite file permissions must be 600. Verified on every startup.
- NEVER commit .env, *.db, logs/, or run/ to git.
- NEVER log secrets, account IDs, or credentials — even at DEBUG level.

## Documentation
- Every new module, class, and public method must have a docstring.
- Every architectural decision must have an ADR in docs/decisions/.
- Every feature addition or change must be recorded in CHANGELOG.md.
- If a change reverses a previous architectural decision, update the relevant ADR.

## Options & Future Security Types
- NEVER hardcode assumptions that only stocks/ETFs exist.
- Quantity fields are generic — do not assume shares.
- Pricing logic must remain pluggable per security type.
- Symbol validation must remain extendable to option symbology.

## Zero Memory State
- The app must be able to crash at any point and restart with full context from SQLite alone.
- The ONLY acceptable data loss on crash is the current reprice step or a pending retry.
- On startup, always check for ABANDONED orders and handle them explicitly.

## Alert Severity
- Only two severity levels exist: CATASTROPHIC and WARNING.
- CATASTROPHIC always halts the daemon and waits for human confirmation before resuming.
- WARNING always logs and shows in TUI amber but never halts background activity.
- The severity enum must be designed so new levels can be inserted later without restructuring.
- Never escalate a WARNING to CATASTROPHIC automatically — only defined triggers can be CATASTROPHIC.

## Process Isolation
- REPL and daemon are fully independent processes.
- They communicate ONLY through SQLite — no sockets, pipes, or shared memory.
- Daemon TUI reads ONLY from SQLite — it never calls IB directly.
- REPL warns if daemon is absent but never blocks trading because of it.
- Daemon alerts on stale REPL heartbeat but never attempts to restart the REPL.
