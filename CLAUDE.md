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
- ALL datetimes stored, compared, and displayed in server-local timezone.
  <!-- TODO: If this tool is opened up for multi-user or multi-timezone deployment,
       revisit this decision. UTC storage + per-user display conversion would be
       needed. For now, single-user with gateway on the same machine — local time
       is simpler and less confusing. -->
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

## TUI Output Routing
- ALL user-facing output in engine and command code must go through `ctx.router.emit()`.
- NEVER call `print()` directly in `engine/`, `repl/commands.py`, or any module that
  receives an AppContext — use `ctx.router.emit()` with the appropriate OutputPane and
  OutputSeverity.
- `repl/output_router.py` has NO project imports — it must remain importable without
  Textual or any project module installed.
- `repl/tui.py` is the ONLY file that imports Textual. It is omitted from test coverage
  (requires Textual runtime). Add it to pyproject.toml omit list, not to tests.
- The `IBTraderApp` owns the asyncio event loop. NEVER call `util.startLoop()` or
  `asyncio.run()` in `repl/main.py` — use `IBTraderApp(...).run()`.
- Command queue maxsize is 10. Reject with WARNING when full — never block the UI thread.
- TUI pane layout is driven by `config/settings.yaml` `tui.panes` block. Default layout
  defined in `repl/pane_config.py` `_DEFAULTS`. At least 2 enabled panes required.
- HEADER pane height is always forced to 1 row regardless of settings.
- Routing rules: DEBUG → file only; ERROR/WARNING → BOTH panes; others → specified pane.

## P&L Display
- `realized_pnl` field in `trade_groups` is written when a trade closes with a known fill
  price and entry price. The calculation is deferred until a full close leg is present.
- Commission is summed across all legs and displayed in the stats/positions pane.
- Never display P&L as zero when data is unavailable — use "—" as the placeholder.

## IB as Source of Truth (Addendum #2)
- IB is the authoritative source for all live order state. The local `orders` table is
  legacy and must not be written to by new code.
- The `transactions` table is append-only — never UPDATE or DELETE rows.
- The orders pane in the TUI is populated from IB's open orders, not from SQLite.
- One `TransactionEvent` row must be written for every interaction with IB around an order.
- Reconciliation (daemon) has two modes:
  - `run_reconciliation`: confirms fills/cancels that IB completed externally by
    writing terminal `RECONCILED` rows and closing trade groups. This is local
    state catchup, not auto-healing — IB already acted.
  - `run_transaction_reconciliation`: surfaces unknown discrepancies as
    `DISCREPANCY` rows (non-terminal) + WARNING alerts. Never auto-heals.
- Poll interval and reconciliation interval are tunables in settings.yaml.
- Live account detection runs on every REPL startup and cannot be bypassed.
