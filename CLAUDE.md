# Engineering Standards — IB Trader

## Data & State
- Monetary values: `Decimal` or integer cents, never `float`.
- **IB is the source of truth for broker-held state** (orders, fills, positions,
  average cost, balances). Never mirror it into SQLite as live state.
- **In-process memory is the source of truth for our own runtime state** (bot
  strategy state, pending commands, reconciler view). SQLite is archival only
  — audit logs, transaction history, closed trade records, raw IB responses.
- Every live operation: do it in IB → update in-memory state → log to SQLite.
  The SQLite write is observational, not gating.
- Future direction: a persistent-memory backend (lightweight Redis) that
  replaces in-process dicts without changing call sites. Crash recovery today
  rebuilds from IB + archival SQLite.

## IB API
- All IB calls go through the abstraction in `ib/base.py`. Never call
  `insync_client` directly from engine/bot/daemon/CLI code.
- Every IB call passes through the global rate limiter (100ms min interval).
- Raw IB responses are persisted to SQLite for audit.

## Logging
- Structured JSON. Every entry has timestamp, level, event name, and relevant
  IDs (trade_id, serial, symbol). Named event types — never freeform strings
  for structured events.
- Levels: DEBUG (throttle/cache), INFO (normal), WARNING (recoverable),
  ERROR (failures).
- Never swallow an exception silently. Log with stack trace and re-raise or
  surface.

## Error Handling
- IB calls are wrapped in retry (default 3, configurable). On final failure:
  full context logged, human-readable message printed, non-zero exit.
- **No silent `except` in `ib_trader/engine/**`, `ib_trader/bots/**`, or any
  broker-facing module.** Every caught exception must either:
  1. Re-raise, OR
  2. Call `ib_trader.logging_.alerts.log_and_alert(...)` (logs ERROR + UI alert).
- `logger.debug(...)`-only except is not acceptable on broker / money-moving
  paths. Benign parse/decode failures (`ValueError`/`TypeError` around a
  JSON/numeric conversion that falls back to a safe sentinel) may stay silent.
- Broker-facing failures (wrapping `ctx.ib.*`) use `severity="CATASTROPHIC"`;
  everything else defaults to `"WARNING"`.
- Order rejections from IB surface the rejection reason clearly — never hide.

## Alert Severity
- Two levels: `CATASTROPHIC` halts the daemon and blocks on human
  confirmation; `WARNING` logs and shows amber in the TUI without halting.
- Severity enum must admit new levels without restructuring.
- Never auto-escalate WARNING to CATASTROPHIC — only defined triggers.

## Testing
- New functions in `engine/`, `ib/`, `data/` get a unit test.
- New CLI commands get an integration test.
- Tests run with no live TWS/Gateway — use the mock IB layer. Smoke tests
  that require a live Gateway are tagged `@pytest.mark.smoke`.
- Full suite passes before any feature ships. Coverage ≥ 90% on core engine.

## Code Style
- `uuid4()` for all UUIDs.
- Datetimes stored, compared, and displayed in server-local timezone.
- Tunables live in `config/settings.yaml`, never hardcoded. Secrets live in
  `.env`, never in code or settings.yaml.
- Repository pattern for all DB access — no raw SQL outside `data/`.
- Use `ib/base.py` interfaces via DI — never instantiate `insync_client.py`
  directly outside the composition root.

## Security
- `.env` and SQLite files are mode 600, verified on startup.
- Never commit `.env`, `*.db`, `logs/`, or `run/`.
- Never log secrets, account IDs, or credentials — even at DEBUG.

## Documentation
- Modules and public classes get a one-line docstring. Methods only when the
  behavior is non-obvious.
- Architectural decisions get an ADR in `docs/decisions/`. Reversing one
  updates the original ADR.
- Feature additions and behavior changes are recorded in `CHANGELOG.md`.

## Options & Future Security Types
- No stock/ETF-only assumptions. Quantity fields are generic (not "shares").
- Pricing logic is pluggable per security type.
- Symbol validation must extend to option symbology.

## Crash Recovery
- Broker-held state (open orders, positions, avg cost, fills) is rebuilt from
  IB on startup by querying orders/positions tagged as ours.
- State that IB does not hold (bot strategy state — trailing stop HWM, signal
  cooldowns, reconciler caches) may be lost on crash today; the persistent
  memory backend is the long-term fix.
- Orphan orders on IB that don't match any in-memory owner surface as
  WARNING — never silently cancelled.

## Process Isolation
- REPL and daemon are independent processes. They communicate through the
  shared state layer (durable command queue + in-memory state, moving to
  Redis); SQLite is not the IPC channel.
- Daemon TUI reads from the shared state layer and from IB directly for
  broker-held data — never from SQLite for live decisions.
- REPL warns if daemon is absent but never blocks trading.
- Daemon alerts on stale REPL heartbeat but never restarts the REPL.

## TUI Output Routing
- All user-facing output in engine and command code goes through
  `ctx.router.emit()`. No direct `print()` in `engine/` or `repl/commands.py`.
- `repl/output_router.py` has no project imports — must be importable without
  Textual installed.
- `repl/tui.py` is the only file that imports Textual. Omitted from test
  coverage via `pyproject.toml`.
- `IBTraderApp` owns the asyncio event loop. Never call `util.startLoop()`
  or `asyncio.run()` in `repl/main.py`.
- Command queue `maxsize=10`. Reject with WARNING when full — never block
  the UI thread.
- Pane layout comes from `config/settings.yaml` `tui.panes`. Defaults live in
  `repl/pane_config.py` `_DEFAULTS`. ≥ 2 enabled panes required. HEADER pane
  height is always 1.
- Routing: DEBUG → file only; ERROR/WARNING → both panes; others → specified
  pane.

## Orders, Transactions, Reconciliation
- IB is authoritative for live order state. The `orders` table is legacy —
  no new writes.
- The `transactions` table is append-only (no UPDATE/DELETE). One
  `TransactionEvent` per IB interaction around an order.
- The TUI orders pane is populated from IB, not SQLite.
- Reconciliation has two modes:
  - `run_reconciliation`: local catchup for fills/cancels IB already
    completed. Writes terminal `RECONCILED` rows and closes trade groups.
    Not auto-healing — IB already acted.
  - `run_transaction_reconciliation`: surfaces unknown discrepancies as
    `DISCREPANCY` rows + WARNING alerts. Never auto-heals.
- Poll and reconciliation intervals are tunables in `settings.yaml`.

## P&L Display
- `realized_pnl` in `trade_groups` is written when a close leg has a known
  fill price and entry price. Calculation is deferred until the close is
  present.
- Commission is summed across all legs and displayed in stats/positions.
- Use `"—"` as the placeholder when P&L data is unavailable, never `0`.

## Gateway Mode
- Paper vs live is auto-detected from IB `managedAccounts` at startup
  (`DU*` = paper, else live). See ADR 015. `--force-mode {paper,live}`
  asserts a mode and fails fast on mismatch. Detection is not bypassable.
