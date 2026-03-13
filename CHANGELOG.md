# Changelog

All notable changes to IB Trader are recorded here.
Format: date, type (Added / Changed / Fixed / Deprecated), description.

## 2026-03-13

### Added
- **Fire-and-forget limit orders** — new `limit` strategy for buy/sell commands.
  Usage: `buy MSFT 1 limit 400.00`. Places a GTC limit order at the user-specified
  price, confirms IB acceptance, and returns immediately. The order persists in IB
  indefinitely — fills are handled by existing callbacks and daemon reconciliation,
  even across app restarts. Supports profit taker on immediate fills.

## 2026-03-12

### Added
- **IB as Source of Truth (Addendum #2)** — IB is now the authoritative source for all
  live order state. The local `orders` table is legacy; new code uses the `transactions`
  table as an append-only audit log.
- **`TransactionEvent` model** (`data/models.py`) — append-only audit log with
  `TransactionAction` enum (PLACE_ATTEMPT, PLACE_ACCEPTED, PLACE_REJECTED, PARTIAL_FILL,
  FILLED, CANCEL_ATTEMPT, CANCELLED, ERROR_TERMINAL, RECONCILED).
- **`TransactionRepository`** (`data/repositories/transaction_repository.py`) — repository
  with `insert()`, `get_open_orders()`, `get_by_ib_order_id()` methods.
- **Alembic migration** `a7c3e2f91b04_add_transactions_table` — creates the `transactions`
  table without touching the existing `orders` table.
- **Transaction event writes** (`engine/order.py`) — writes `TransactionEvent` rows at
  every IB interaction point: PLACE_ATTEMPT before IB call, PLACE_ACCEPTED/REJECTED after,
  FILLED/PARTIAL_FILL on fills, CANCEL_ATTEMPT/CANCELLED on cancellations.
- **IB-sourced orders pane** (`repl/tui.py`) — orders DataTable now fetches open orders
  directly from IB. System-originated orders (matching `transactions` table) are marked
  with `● OUR SYSTEM`; others shown as `EXTERNAL`.
- **Poll elapsed time display** — header pane shows `Last refresh: Xs ago` (or `Xm Xs ago`).
  Stale indicator (`⚠ stale`) shown when last poll failed.
- **Poll interval tunable** — `poll_interval_seconds: 60` in settings.yaml drives the IB
  poll cycle. Header refreshes at `tui_refresh_interval_seconds` independently.
- **Transaction-based reconciliation** (`daemon/reconciler.py`) —
  `run_transaction_reconciliation()` compares non-terminal transactions against IB open
  orders. Discrepancies produce RECONCILED rows and WARNING alerts. Never auto-heals.
- **Live account detection** — on REPL startup, warns if account ID does not start with
  `DU` (paper trading prefix). Logs `LIVE_ACCOUNT_CONNECTED` event.
- **`AppContext.transactions`** — optional `TransactionRepository` field (backward-compatible
  with existing tests).
- Unit tests: `test_transaction_repository.py` (11 tests), `test_reconciler.py` (6 tests),
  `test_live_account_warning.py` (5 tests).

### Changed
- `config/settings.yaml` — `reconciliation_interval_seconds` changed from 1800 to 3600;
  `poll_interval_seconds: 60` added.
- `CLAUDE.md` — appended IB as Source of Truth rules (Addendum #2).

## 2026-03-11 (patch 5)

### Changed
- **Migrated from `ib_insync` to `ib_async`** — `ib_insync` is abandoned; `ib_async` is the
  actively maintained fork (drop-in replacement, same API). When `includeOvernight` support
  lands in a future `ib_async` release, we can switch from the `tif=OND` + `exchange=OVERNIGHT`
  workaround to the proper `includeOvernight=True` + `exchange=SMART` + `tif=DAY` pattern.

### Fixed
- **Daemon cannot reconcile REPL orders** (#1) — `get_open_orders()` used
  `reqOpenOrdersAsync()` (same-client-ID only). Changed to `reqAllOpenOrdersAsync()` so the
  daemon (which connects with `CLIENT_ID + 1`) can see REPL-placed orders.
- **Partial market fill treated as full fill** (#5) — `_execute_market_order` used
  `qty_filled > 0` instead of `qty_filled >= qty`. Partial fills now route to
  `_handle_partial()` instead of `_handle_fill()`.
- **Partial handler never cancels remainder at IB** (#6) — `_handle_partial()` emitted
  "canceled (timeout)" but never called `cancel_order()`. Now actually cancels the IB order.
- **Reconciler never closes trade groups** (#7) — Added `_maybe_close_trade_group()` to
  `daemon/reconciler.py`. When all legs reach terminal state, computes realized P&L and
  transitions `TradeGroup.status` to CLOSED.
- **Over-close possible** (#8) — `execute_close()` now subtracts filled close/profit-taker
  legs from `qty_to_close`. Returns with WARNING if position is already fully closed.
- **PARTIAL not treated as terminal** (#9) — Added `OrderStatus.PARTIAL` to the terminal
  state set in repository queries, consistent with `_handle_partial()` now canceling the
  remainder at IB.
- **Callback leak** (#10) — Fill/status callbacks now keyed by `ib_order_id` and
  auto-removed when order reaches terminal state. Prevents unbounded growth over long sessions.
- **`close bid`/`close ask` silently became market orders** (#13) — Added explicit bid/ask
  limit-order branches in `execute_close()`.
- **Close orders were fire-and-forget** (#3) — `execute_close()` now registers tracker +
  callbacks, polls for IB acknowledgment, runs reprice loop (mid strategy), waits for fill,
  and handles outcome (full fill → CLOSED + P&L, partial → cancel remainder, timeout →
  CANCELED). Added `_handle_close_fill()` and `_handle_close_partial()` helpers.

## 2026-03-10 (patch 4)

### Fixed
- **Overnight orders rejected by IB** — IB does not accept `tif=GTC` for the overnight
  session (8:00 PM – 3:50 AM ET). Orders must use `tif=OND` (Overnight + Day) and route
  through `exchange=OVERNIGHT`. Added `is_overnight_session()` to `engine/market_hours.py`
  and `_session_tif()` helper to `engine/order.py`. All four `place_limit_order` call sites
  (mid entry, bid/ask entry, profit taker, close) now automatically select OND during
  overnight and GTC otherwise. `insync_client.py` creates an OVERNIGHT-exchange contract
  copy whenever `tif=OND` is passed. 13 new unit tests for `is_overnight_session`.

## 2026-03-10 (patch 3)

### Fixed
- **Double error display** (`engine/order.py`) — `_execute_mid_order` PreSubmitted-during-active-session
  branch previously `raise IBOrderRejectedError` after already emitting `✗ NOT ACTIVE`. The exception
  propagated to `_process_commands` which emitted a second `✗ Error:` line. Changed to `return` since
  the error is already routed and the order is already marked ABANDONED.
- **Overnight reprice amendments stripped outsideRth** (`ib/insync_client.py`) — ib_insync resets
  `trade.order.outsideRth = False` when TWS echoes the order back after `placeOrder` (GitHub issue #141).
  `amend_order` now explicitly sets `trade.order.outsideRth = True` before each amendment `placeOrder`
  call, ensuring reprice steps during overnight/extended-hours sessions are not rejected by IB.

## 2026-03-10 (patch 2)

### Added
- **`engine/market_hours.py`** — US equity session detection based on IB official trading
  hours (source: interactivebrokers.com/en/trading/us-overnight-trading.php).
  Functions: `is_weekend_closure`, `is_session_break`, `is_ib_session_active`,
  `presubmitted_reason`, `session_label`. DST-aware via `zoneinfo.ZoneInfo("America/New_York")`.
- **`whyHeld` field** in `get_order_status()` return dict — exposes the IB `whyHeld`
  field that IB always populates when an order is `Inactive` (per IB API docs:
  interactivebrokers.github.io/tws-api/order_submission.html).

### Changed
- **PreSubmitted handling** (both mid and bid/ask strategies) now distinguishes two cases:
  1. **Weekend/break** (Fri 8 PM ET – Sun 8 PM ET, or 3:50–4:00 AM session break):
     expected IB behaviour — emits ⚠ QUEUED with reopen time, leaves GTC order OPEN.
  2. **Active session** (overnight/pre-market/RTH/after-hours): order should be `Submitted`
     at the exchange. If it remains `PreSubmitted`, emits ✗ NOT ACTIVE with `whyHeld`
     detail, cancels the order, marks ABANDONED.
- **`Inactive` status** (mid orders, after reprice loop) now emits ✗ INACTIVE with IB
  error code + `whyHeld` reason instead of the generic "EXPIRED" message.
- **`Inactive` vs `Cancelled`** distinction in bid/ask rejection branch: shows
  ✗ INACTIVE or ✗ REJECTED with the specific IB reason.
- 41 new unit tests for `market_hours.py`; 4 new integration tests.

## 2026-03-10 (patch)

### Fixed
- **Command input text invisible** (`repl/tui.py`) — removed `dock: bottom` from `#command-input`
  CSS (was docking to Screen, not the Vertical container, hiding the widget). Set `#command-output`
  to `height: 1fr` and `#command-input` to `height: 3` (Textual's natural Input height).
- **IB order rejections not surfaced** (`ib/insync_client.py`) — `_on_error` previously only
  stored errors in `_order_errors` for codes 110 and 200–299. After-hours and other non-standard
  rejection codes were logged as warnings but not captured. Now any error from IB referencing an
  active order is stored so the real rejection reason reaches the user.
- **Rejected bid/ask orders left as OPEN** (`engine/order.py`) — when IB rejects/cancels a
  bid/ask order, `notify_canceled()` sets `fill_event`, unblocking the 30 s wait. The previous
  code unconditionally emitted "LIVE GTC" and left the order OPEN. Now checks `track.is_canceled`;
  if true, marks order CANCELED and trade CLOSED and shows the real rejection reason.
- **PreSubmitted orders (market closed) not handled** (`engine/order.py`) — when IB accepts an
  order but holds it as `PreSubmitted` (market session closed, queued for next session), mid orders
  would start a pointless reprice loop and eventually expire CANCELED. Bid/ask orders would show
  "LIVE GTC" with no indication the market was closed. Both paths now detect `PreSubmitted`,
  emit a ⚠ QUEUED warning, skip the reprice loop (mid only), and leave the GTC order OPEN for the
  daemon reconciler to catch on fill.

### Added
- `bid_ask_wait_seconds: 30` in `config/settings.yaml` — makes the immediate-fill window for
  bid/ask GTC orders configurable (was hardcoded 30 s).

## 2026-03-10

### Added
- **Command Center TUI** (`repl/tui.py`) — full-screen Textual application replaces the plain REPL
  loop. Five panes: header, log, positions, command output, orders. Dynamic layout from settings.
- `repl/output_router.py` — `OutputRouter`, `OutputPane`, `OutputSeverity`, `RendererProtocol`.
  Single wiring point for all engine and command output. Pre-TUI messages are buffered and flushed
  when the Textual renderer attaches.
- `repl/pane_config.py` — `PaneName`, `PaneConfig`, `load_pane_configs()`. Reads `tui.panes` block
  from settings.yaml with built-in defaults and validation (≥2 enabled panes, unique ranks, header
  forced to 1 row).
- `AppContext.router` — `OutputRouter` field with `default_factory`, backward-compatible with all
  existing tests.
- `config/settings.yaml` — `tui` block with default pane layout and `tui_refresh_interval_seconds`.
- ADR-013 — documents event loop decision: Textual App owns the event loop; `util.startLoop()` and
  `asyncio.run()` removed; ib_insync coroutines run as tasks within Textual's loop.
- Integration tests for bid/ask order strategy (`tests/integration/test_bid_ask_order.py`).
- Unit tests: `test_output_router.py` (19 tests), `test_pane_config.py` (13 tests),
  `test_command_queue.py` (7 tests).

### Changed
- `repl/main.py` — `util.startLoop()` and `asyncio.run(run_repl(...))` replaced with
  `IBTraderApp(...).run()`. Removed `ib_insync.util` import.
- `engine/order.py` — all `print()` calls replaced with `ctx.router.emit()` with correct
  `OutputPane` and `OutputSeverity` routing. Reprice steps go to LOG pane; fills/errors go to
  COMMAND pane.
- `repl/commands.py` — `parse_command`, `parse_buy_sell`, `parse_close`, `parse_modify` accept
  optional `router: OutputRouter | None = None`; error output goes through router when provided,
  falls back to `print()` when None (tests unaffected).
- `pyproject.toml` — `ib_trader/repl/tui.py` added to coverage omit list (requires Textual runtime).

## 2026-03-08

### Added
- Initial project structure: REPL, daemon, engine, IB abstraction, data layer
- `CLAUDE.md` — non-negotiable engineering standards for all contributors
- Architecture Decision Records (ADRs) 001–012 in `docs/decisions/`
- SQLAlchemy ORM models: `TradeGroup`, `Order`, `RepriceEvent`, `Contract`, `Metric`, `SystemHeartbeat`, `SystemAlert`
- Repository pattern: `TradeRepository`, `OrderRepository`, `RepriceEventRepository`, `ContractRepository`, `HeartbeatRepository`, `AlertRepository`
- Alembic migrations from day one — initial schema migration `48f9a117` applied
- SQLite WAL mode and foreign key enforcement on every connection
- Abstract IB interface (`IBClientBase`) with built-in throttle layer (default 100ms)
- `InsyncClient` — `ib_insync` concrete implementation (isolated to `ib/insync_client.py`)
- `MockIBClient` — fully mockable IB layer for unit and integration tests
- Pure pricing functions: `calc_mid`, `calc_step_price`, `calc_profit_taker_price`, `calc_shares_from_dollars`
- `OrderTracker` — in-flight order state for reprice/fill coordination (ephemeral, rebuilt from SQLite on restart)
- Crash recovery: `recover_in_flight_orders` scans for REPRICING/AMENDING orders on startup, marks ABANDONED
- `execute_order` — full order execution: place, reprice loop, fill handling, profit taker placement
- `execute_close` — close position by serial number; cancels linked profit taker first
- `place_profit_taker` — GTC profit taker after fill, inverse side of entry
- `reprice_loop` — amend-in-place reprice loop (not cancel-replace) with configurable steps/interval
- REPL interactive session loop with `buy`, `sell`, `close`, `modify` (stub) commands
- `shlex.split()` command parsing — no argparse in REPL
- Safety limit enforcement: `max_order_size_shares` checked before any IB call
- Symbol whitelist validation from `config/symbols.yaml` before any IB call
- Stop loss flag: accepted, stored, logged — no IB action (stub)
- Modify command: accepted, logged — no action (stub)
- REPL heartbeat every 30 seconds to `system_heartbeats` table
- Daemon: background reconciliation, REPL heartbeat monitoring, SQLite integrity checks
- Daemon: CATASTROPHIC/WARNING alert system with `system_alerts` table
- Daemon TUI: Textual live dashboard with auto-refresh and command input
- CATASTROPHIC state: TUI goes red, all loops pause, waits for Enter to resume
- Mutual watchdog: REPL and daemon watch each other via SQLite heartbeats only
- Structured JSON logging with rotation and gzip compression
- `config/settings.yaml` — all tunables (no secrets)
- `config/symbols.yaml` — symbol whitelist
- `.env.example` — environment variable template
- `AppContext` — dependency injection container, no global singletons
- Unit tests: pricing, serial numbers, repositories, commands, config, exceptions, tracker, recovery, heartbeat, integrity
- Integration tests: order placement, profit taker, reprice loop, close command, reconciliation, mid-price flow
- Smoke tests: marked `@pytest.mark.smoke`, skipped if IB Gateway unreachable, clean up after themselves
- 91.75% test coverage on core engine modules (>90% target met)
- `Makefile` with `install`, `test`, `smoke`, `docs`, `lint`, `typecheck`, `clean` targets
- `mkdocs.yml` with Material theme
