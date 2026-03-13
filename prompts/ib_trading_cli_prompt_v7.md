# Claude Code Prompt: Interactive Brokers Trading Engine (v6)

---

> **You are a senior Python engineer building a reliable local trading engine for Interactive Brokers.**
> **Follow this specification exactly.**
> **If any instruction conflicts with convenience or your own judgment, follow the specification.**
> **Do not invent interfaces, method signatures, column names, event names, or behavior not specified here.**
> **When something is marked STUB, implement only the stub — no more.**
> **When something is marked FUTURE, do not implement it at all — only ensure the architecture does not prevent it.**

---

## Project Overview

Build a Python-based trading engine for Interactive Brokers. This is a **trading engine** with two persistent processes:

- **CLI REPL** (`guava-trader`) — an interactive session you start once and trade from. Accepts commands like `buy MSFT 100 mid 500` at a live prompt. Owns the IB connection, startup health checks, and all order execution.
- **Daemon** (`guava-daemon`) — a persistent background process with a Textual TUI. Owns monitoring, reconciliation, SQLite integrity, and system health alerts. Watches the CLI REPL process and vice versa.

Bots will eventually be a third client, calling the core engine directly as a Python module at high frequency, bypassing the REPL entirely.

The system must be designed around one non-negotiable rule: **zero in-memory state**. The only acceptable data loss on crash is the current position in a reprice loop or a pending retry. Every other event — order placed, IB order ID assigned, fill received, profit taker placed, amendment made — must be written to SQLite before the next operation proceeds.

---

## Technology Stack

- **Language:** Python 3.11+
- **Broker API:** `ib_insync` library connecting to TWS or IB Gateway running locally via TCP socket
- **Database:** SQLite in WAL mode (Write-Ahead Logging), via SQLAlchemy ORM with repository pattern
- **Migrations:** Alembic from day one
- **CLI REPL:** `click` + custom REPL loop — interactive session, starts once, accepts commands at a `>` prompt
- **Testing:** `pytest`, >90% coverage on core engine
- **Logging:** Structured JSON via Python `logging`, with rotation and compression
- **Secrets:** `.env` file via `python-dotenv` (gitignored, never in `settings.yaml`)
- **Config:** `settings.yaml` for all non-secret tunables
- **Daemon TUI:** `textual` — live auto-refreshing dashboard with interactive command input in same terminal window

---

## Core Architectural Principles

### 1. Zero Memory State
- No order state, position state, or trade state lives in memory
- Every operation follows: **do the thing in IB → write result to SQLite → proceed**
- On startup, the app reconstructs all context from SQLite alone
- Cache is allowed only for static contract data (refreshed daily), never for order or trade state

### 2. Full Process Isolation
- CLI and daemon are completely independent processes
- They communicate exclusively through SQLite — no sockets, no pipes, no shared memory
- Either process can crash at any time without affecting the other
- SQLite WAL mode must be explicitly enabled to allow safe concurrent access

### 3. Write-Ahead Audit Trail
- Every state transition is written to SQLite before acting on it
- Every IB API response (raw JSON) is stored for audit
- Nothing is assumed — always verify against IB and update SQLite accordingly

### 4. Crash Recovery
- On REPL startup, scan SQLite for any orders in `REPRICING` or `AMENDING` state
- For each: mark as `ABANDONED` in SQLite, log clearly with timestamp and last known step
- Do NOT attempt to cancel or continue — the order may still be open in IB
- Print a warning at the REPL prompt listing any abandoned orders by serial number
- The user is responsible for handling abandoned orders manually in IB or via `daemon cleanup` (stubbed)
- Rationale: by the time the app restarts, market prices have moved — automated repricing decisions would be uninformed

### 5. Mutual Watchdog
The CLI REPL and daemon watch each other exclusively through SQLite — no sockets, no signals.

**CLI REPL responsibilities:**
- Writes a heartbeat timestamp to `system_heartbeats` table every 30 seconds while running
- Writes `REPL_STARTED` and `REPL_EXIT_CLEAN` events to SQLite on startup and clean exit
- On startup, checks daemon's last heartbeat — if stale, prints a **WARNING** (not catastrophic): `⚠ Daemon is not running — reconciliation and monitoring are offline`
- Does not block trading if daemon is absent

**Daemon responsibilities:**
- Writes its own heartbeat to `system_heartbeats` every 30 seconds
- Monitors REPL heartbeat every 30 seconds
- If REPL heartbeat goes stale beyond threshold (configurable, default 5 minutes): triggers a **CATASTROPHIC** alert
- Performs passive IB Gateway ping every 30 minutes — 3 consecutive failures triggers **CATASTROPHIC**
- Runs `PRAGMA integrity_check` on SQLite at startup and every 6 hours — failure triggers **CATASTROPHIC**

---

## Alert Severity Levels

Two severity levels for now. The enum must be designed so additional levels (`CRITICAL`, `ERROR`) can be inserted later without restructuring.

### `CATASTROPHIC`
Daemon halts all background activity, TUI goes fully red, waits for human confirmation before resuming.

**Triggers:**
- REPL heartbeat stale beyond threshold (likely crashed)
- SQLite `PRAGMA integrity_check` returns errors
- IB passive connectivity check fails 3 consecutive times (daemon attempts a lightweight `ib_insync` connection check every 30 minutes)

**Behavior:**
1. Log `SYSTEM_ALERT` event to SQLite: severity, trigger, timestamp, context
2. TUI dashboard goes red, shows exactly what broke and when
3. All background loops (reconciliation, monitoring) pause
4. Bottom prompt changes to:
```
⚠ CATASTROPHIC: CLI heartbeat lost at 10:32:01 (last seen 4 min ago)
Fix the issue then press Enter to resume...
```
5. On Enter: log `SYSTEM_ALERT_RESOLVED`, resume all loops, TUI returns to normal

### `WARNING`
Daemon logs it, TUI shows it in amber, background activity continues uninterrupted.

**Triggers:**
- Single failed reconciliation attempt
- Single failed IB connectivity check (not yet 3 consecutive)
- ABANDONED order detected
- Any other non-fatal anomaly

**Behavior:**
1. Log `SYSTEM_ALERT` event to SQLite: severity `WARNING`, trigger, timestamp
2. TUI amber indicator shown alongside the warning message
3. All loops continue running normally
4. No human input required

---

## Secrets & Configuration

### `.env` (gitignored — never commit)
```
IB_HOST=127.0.0.1
IB_PORT=7497
IB_CLIENT_ID=1
IB_ACCOUNT_ID=U1234567
```
TWS or IB Gateway handles all authentication. The app connects via `ib_insync` using host/port — no credentials handled by the app directly.

### `config/settings.yaml` (safe to commit)
All tunables — no secrets:
```yaml
max_order_size_shares: 10
max_retries: 3
retry_delay_seconds: 2
reprice_interval_seconds: 1
reprice_duration_seconds: 10
log_level: INFO
log_file_path: logs/ib_trader.log
log_rotation_max_bytes: 10485760      # 10MB
log_rotation_backup_count: 10
log_compress_old: true
cache_ttl_seconds: 86400
```

### `config/symbols.yaml`
Editable symbol whitelist. Any symbol not in this file is rejected before any network call is made. Symbols are never in `settings.yaml` — they live exclusively in `symbols.yaml` so they can be edited without touching other config.

---

## CLI Commands & Syntax

All commands follow consistent verb-first syntax.

### `buy`
```
buy MSFT 100 mid              # Limit order at mid, no profit taker
buy MSFT 100 mid 500          # Mid limit, profit taker at +$500 total profit
buy MSFT 100 market           # Market order
buy MSFT 100 market 500       # Market order with profit taker
```

### `sell`
```
sell MSFT 100 mid             # Short sell at mid
sell MSFT 100 mid 300         # Short sell, profit taker at -$300 (cover lower)
sell MSFT 100 market          # Market short
```
Sell automatically cancels any linked open orders (profit taker, stop loss stub) on the opposite side before placing.

### `close`
```
close 4                       # Close position from serial #4 at mid
close 4 market                # Close at market
close 4 mid 200               # Close at mid, with a profit taker on the close leg
```
Close:
- Looks up serial #4 in SQLite
- Cancels all linked open IB orders (profit taker, stop loss stub)
- Places a closing order using the same pricing logic as `buy`
- Logs as `CLOSED_MANUAL`, distinct from `CLOSED_EXTERNAL`

### `modify`
```
modify 4 ...                  # STUBBED — accepts command, logs it, does nothing
```

### Optional flags (all commands):
```
--dollars 5000                # Notional size instead of shares
--take-profit-price 420.00    # Explicit limit price for profit taker
--stop-loss 300               # STUBBED — accept, store, log, no action
```

### Daemon TUI
The daemon runs as a `textual` terminal UI — a single dedicated terminal window with two zones:

**Top zone — live dashboard (auto-refreshes every 5 seconds, configurable):**
```
┌─────────────────────────────────────────────────┐
│ IB TRADER DAEMON                   ● CONNECTED  │
├──────────────┬─────────────────┬────────────────┤
│ Gateway      │ ✓ Connected     │ 10:32:01       │
│ Last Recon   │ 2 min ago       │ 0 changes      │
│ CLI Status   │ ✓ Running       │ PID 4821       │
├──────────────┴─────────────────┴────────────────┤
│ TODAY                                           │
│ Orders Placed    4    Open Now       1          │
│ Filled           3    Abandoned      0          │
│ Canceled         0    Ext. Closed    1          │
│ Realized P&L    +$234.50                        │
│ Commissions      -$4.00                         │
├─────────────────────────────────────────────────┤
│ > _                                             │
└─────────────────────────────────────────────────┘
```

**Bottom zone — interactive command input:**
Accepts daemon commands typed at the `>` prompt. Dashboard continues refreshing while waiting for input.

### Daemon admin commands (typed at `>` prompt or via CLI):
```
refresh                       # Force immediate IB reconciliation
orders                        # List all open orders with serial numbers
stats                         # P&L summary, fills, cancels, commissions
status                        # Gateway health, last reconciliation time
cleanup                       # STUBBED — would cancel all ABANDONED orders in IB
```

---

## CLI REPL

The CLI runs as a persistent interactive REPL session — not a one-shot script. You start it once and trade from it throughout the session.

### Startup sequence:
1. Run health check (`.env`, SQLite permissions, schema, IB connection via `ib_insync`, account accessible)
2. Scan for ABANDONED orders — warn if any exist
3. Check daemon heartbeat — warn if daemon is not running
4. Write `REPL_STARTED` event and PID to SQLite
5. Warm contract cache for whitelisted symbols
6. Show prompt

### Session experience:
```
$ guava-trader
IB Trader v1.0 — connected to Gateway @ 127.0.0.1:5000
Account: U1234567 | 5 symbols loaded
⚠ Warning: Order #2 was ABANDONED on last session (check IB manually)
⚠ Warning: Daemon is not running — reconciliation offline

> buy MSFT 100 mid 500
[10:32:01] Placed @ $412.30 (bid: $412.20 ask: $412.40)
[10:32:02] Amended → $412.31 | step 1/10
[10:32:04] ✓ Filled 10/10 @ avg $412.33
✓ FILLED: 10 shares MSFT @ $412.33 | Commission: $1.00
  Profit taker placed @ $462.33 (linked to #4)

> close 4 market
> orders
> exit
Goodbye. Session logged.
```

### Heartbeat:
- Writes heartbeat timestamp to `system_heartbeats` table every 30 seconds
- On clean `exit`: writes `REPL_EXIT_CLEAN`, removes heartbeat row
- On crash: heartbeat row goes stale — daemon detects this



- On every command, validate symbol against `config/symbols.yaml` first
- Reject immediately with a clear error if not on whitelist
- No network call is made for invalid symbols
- Whitelist is reloaded from file on each run (no restart required to add a symbol)

---

## Order Execution Logic

### Session Hours
Always use extended hours on every order. Set `order.outsideRth = True` on every `ib_insync` Order object. Orders must work pre-market, regular hours, and after-hours.

### Pricing Modes

**Mid-price (default):**
1. Fetch live bid and ask from IB
2. Calculate mid = (bid + ask) / 2
3. Place limit order at mid — write IB order ID to SQLite immediately
4. Every `reprice_interval_seconds` (default: 1):
   - Fetch live bid and ask again
   - Calculate next price step: `mid + (step_number / total_steps) * (ask - mid)`
   - **Amend the existing IB order** (do not cancel and replace — keeps IB order book clean)
   - Write amendment to SQLite (timestamp, new price, step number)
5. After `reprice_duration_seconds` (default: 10), cancel remainder
6. Partial fills are acceptable

**Market:**
Place immediately at market. No reprice loop.

### Order Amendment vs. Cancel-Replace
**Explicit decision: order amendment.**
Each reprice modifies the existing IB order in place using `ib_insync`'s `modifyOrder()` — one IB order ID per entry leg, regardless of how many reprice steps occur. This keeps the IB mobile app and TWS clean. Each amendment is logged in SQLite with full detail. If amendment is rejected for a specific order type, fall back to cancel-replace and log a warning.

### Profit Taker
- Triggered only after entry order confirms a fill (full or partial)
- If `profit_amount` given (positional arg):
  - **BUY entry (long):** `profit_price = avg_fill_price + (profit_amount / qty_filled)` — profit taker placed as SELL
  - **SELL entry (short):** `profit_price = avg_fill_price - (profit_amount / qty_filled)` — profit taker placed as BUY (cover lower)
- If `--take-profit-price` given: use directly as-is, regardless of side
- Profit taker `side` is always the **inverse** of the entry leg `side`: BUY entry → SELL profit taker, SELL entry → BUY profit taker
- Place a GTC limit order in IB — this order lives entirely in IB's system
- Write profit taker IB order ID to SQLite, linked to the trade group
- On `close` or `sell` of the same position: cancel the profit taker in IB first

### Close Command Side Rule
The close order side is always the inverse of the entry leg side:
- BUY entry → close places a SELL order
- SELL entry → close places a BUY order (covers the short)

The quantity closed is `entry_order.qty_filled` — the total filled quantity on the entry leg. If qty_filled is zero (order never filled), reject `close` with `✗ Error: order #N has no filled quantity to close`.

### Stop Loss (Stub)
- Accept `--stop-loss` flag on any command
- Store the value in SQLite against the trade
- Log that stop loss was requested but not implemented
- Take no action in IB

### Safety Limits
All configurable in `settings.yaml`:
- Max shares per order: 10 (default)
- Reject orders exceeding this limit before any IB call

---

## IB API Throttling & Pacing

`ib_insync` is async-native and event-driven — fills, order status changes, and market data are pushed as events rather than polled. However IB still enforces pacing limits and the app must respect them.

- Minimum interval between requests: 100ms (configurable as `ib_min_call_interval_ms: 100`)
- Implement as a thin throttle layer inside the IB abstraction (`ib/base.py`) — all methods pass through it automatically
- Handle IB pacing violation errors explicitly — back off with exponential backoff, do not retry immediately
- Thread-safe — the reprice loop and reconciliation may issue requests concurrently
- Log a `THROTTLED` event at DEBUG level whenever a call is delayed

`ib_insync` connection settings in `settings.yaml`:
```yaml
ib_host: 127.0.0.1
ib_port: 7497          # 7497 for TWS live, 7496 for TWS paper, 4001 for IB Gateway live, 4002 for paper
ib_client_id: 1        # Must be unique per connected client
ib_min_call_interval_ms: 100
```

---



### General API errors:
- Wrap all IB API calls in retry logic
- Default: 3 retries with configurable delay
- On final retry failure: log full error context with stack trace, print clear human-readable error, exit non-zero
- Never swallow exceptions silently

### IB connection errors:
- `ib_insync` raises connection errors when TWS/Gateway is unreachable or disconnects
- Detect via `ib_insync` disconnect event or connection exception
- Log the disconnect event with timestamp
- Print clear error to terminal:
  ```
  ✗ IB connection lost (TWS/Gateway at 127.0.0.1:7497)
  Please check that TWS or IB Gateway is running, then press Enter to retry...
  ```
- Wait for user to press Enter, then attempt reconnect via `ib_insync`
- If reconnect succeeds, continue operation and log `IB_RECONNECTED`
- If reconnect fails after retries, exit with full error log

### Order rejection by IB:
- Surface the rejection reason from IB's response clearly
- Log the full IB response
- Do not retry order rejections (these are logical errors, not transient failures)

---

## Data Model & Identifiers

### Two identifiers per order:

| Identifier | Visibility | Purpose |
|---|---|---|
| **Local serial number** | User-facing | Human handle (0–999, reuse lowest available unused) |
| **Internal UUID** | Internal only | Permanent, never shown, used for metrics and future cloud migration |

### Trade Group
Every trade is modeled as a **trade group** containing linked legs:
- Entry leg (the buy/sell order)
- Profit taker leg (optional)
- Stop loss leg (optional, stub for now)
- Close leg (when closed)

A trade is `OPEN` until all legs are resolved. P&L and metrics roll up at the trade group level.

### Orders table (per leg):
- `id` (UUID, primary key)
- `trade_id` (UUID, foreign key to trade group)
- `serial_number` (integer, user-facing, on entry leg only)
- `ib_order_id` (string)
- `leg_type` (ENTRY, PROFIT_TAKER, STOP_LOSS, CLOSE)
- `symbol`, `side` (BUY/SELL)
- `security_type` (STK, ETF, OPT, FUT — extensible enum, currently STK/ETF only)
- `expiry` (date, nullable — for options/futures)
- `strike` (Decimal, nullable — for options)
- `right` (CALL/PUT, nullable — for options)
- `qty_requested`, `qty_filled` (in contracts for options, shares for equities)
- `order_type` (MID, MARKET)
- `price_placed`, `avg_fill_price`
- `stop_loss_requested` (Decimal, nullable)
- `commission` (Decimal)
- `status` (PENDING, OPEN, REPRICING, AMENDING, FILLED, PARTIAL, CANCELED, ABANDONED, CLOSED_MANUAL, CLOSED_EXTERNAL, REJECTED)
- `placed_at`, `filled_at`, `canceled_at`, `last_amended_at`
- `raw_ib_response` (JSON)

### System tables:

`system_heartbeats`:
- `process` (REPL / DAEMON), `last_seen_at`, `pid`

`system_alerts`:
- `id` (UUID), `severity` (CATASTROPHIC / WARNING), `trigger`, `message`, `created_at`, `resolved_at` (nullable)


- `id`, `order_id` (UUID)
- `step_number`, `bid`, `ask`, `new_price`
- `amendment_confirmed` (bool)
- `timestamp`

### Trade groups table:
- `id` (UUID), `serial_number`
- `symbol`, `direction` (LONG/SHORT)
- `status` (OPEN, CLOSED, PARTIAL)
- `realized_pnl` (Decimal), `total_commission` (Decimal)
- `opened_at`, `closed_at`

---

## Contract Cache

- On first order for a symbol, fetch contract details from IB (conId, exchange, currency, multiplier, etc.)
- Store in SQLite `contracts` table with `fetched_at` timestamp
- On subsequent orders, use cached value if `fetched_at` is within `cache_ttl_seconds` (default: 86400)
- If IB rejects an order with a contract-related error, invalidate cache for that symbol and re-fetch
- Cache applies only to contract details — never to order state or market data

---

## IB Reconciliation

### Background (daemon):
- Every 30 minutes, query IB for status of all locally-tracked open orders
- For each discrepancy (IB shows filled/canceled but SQLite shows open):
  - Update SQLite to match IB reality
  - Log event as `RECONCILED_EXTERNAL` with full detail
  - Record in metrics as externally closed/filled

### On startup (CLI):
- Before accepting any command, check SQLite for orders in `REPRICING` or `AMENDING` state
- Query IB for each, resolve as described in crash recovery section above

### Manual (`daemon refresh` or `--refresh-orders` flag):
- Immediately triggers full reconciliation
- Prints summary of any changes found

---

## Logging

### Format: structured JSON, one object per line
```json
{
  "timestamp": "2024-01-15T10:32:01.234Z",
  "level": "INFO",
  "event": "ORDER_AMENDED",
  "trade_id": "uuid-here",
  "serial": 4,
  "symbol": "MSFT",
  "step": 3,
  "new_price": 412.38,
  "bid": 412.30,
  "ask": 412.50
}
```

### Every operation must be logged:
- App startup and health check result
- Every CLI command received (with args, redacted of any sensitive values)
- Symbol validation pass/fail
- IB API call made (endpoint, params)
- IB API response received (status, summary)
- Order placed (all details)
- Every reprice/amendment (step, prices)
- Every fill event (qty, price, commission)
- Partial fill events
- Cancel events (reason: timeout, manual, external, recovery)
- Profit taker placed
- Profit taker canceled
- Reconciliation events
- Cache hits and misses
- All errors with full stack traces
- All retries (attempt number, error)
- Gateway disconnect and reconnect
- Daemon start, stop, reconciliation runs
- CLI process start (PID recorded)
- CLI clean exit
- CLI crash detected (PID gone unexpectedly)

### Log rotation:
- Max file size: 10MB (configurable)
- Keep last 10 files (configurable)
- Compress rotated files (gzip)
- Log to both file and stdout

---

## Metrics (stored in SQLite `metrics` table)

Collect and store for every trade:
- **Volume:** orders placed, by symbol, by type (mid/market), by side
- **Outcomes:** filled, partially filled, canceled, rejected, closed external
- **Fill quality:** slippage from mid at placement (basis points), avg slippage per symbol
- **Speed:** time to fill (seconds), reprice count before fill
- **Amendments:** total amendments per order, avg amendments per filled order
- **P&L:** realized P&L per trade, per symbol, cumulative
- **Commissions:** per trade, per symbol, cumulative
- **Reconciliation:** externally closed count, externally filled count
- **Errors:** API errors by type, retry counts, gateway disconnects
- **Daemon:** reconciliation run count, discrepancies found per run
- **CLI process:** total runs, clean exits, crashes, last crash timestamp
- **Abandoned orders:** count, by symbol, total value at time of abandonment

All metrics queryable via raw SQL. Schema designed for future migration to time-series or cloud store. Every metric row includes `trade_id` UUID for cross-referencing.

---

## Terminal Output

### During repricing:
```
Order #4 — BUY 100 MSFT @ mid
[10:32:01] Placed @ $412.30 (bid: $412.20 ask: $412.40)
[10:32:02] Amended → $412.32 | step 1/10 (still open: 0/100 filled)
[10:32:03] Amended → $412.34 | step 2/10 (still open: 0/100 filled)
[10:32:04] ✓ Filled 100/100 @ avg $412.33
```

### On fill:
```
✓ FILLED: 100 shares MSFT @ $412.33 avg
  Commission: $1.00
  Profit taker placed @ $417.33 (linked to #4)
  Serial: #4
```

### On partial + cancel:
```
⚠ PARTIAL: 60/100 filled @ avg $412.31 | 40 shares canceled (timeout)
  Commission: $0.60
  Serial: #4
```

### On full cancel:
```
✗ CANCELED: 0/100 filled | timeout after 10 seconds
  Serial: #4
```

---

## Startup Health Check

On every startup (CLI and daemon), verify:
1. `.env` file exists, required variables are present, and file permissions are `600`
2. SQLite file permissions are `600`
3. `settings.yaml` is valid and all required fields present
4. `symbols.yaml` is valid and non-empty
5. SQLite file is accessible and schema is current (run Alembic check)
6. `ib_insync` connection succeeds (`ib.connect(host, port, clientId)` — timeout after 10 seconds)
7. IB account is accessible and matches `IB_ACCOUNT_ID`

On any failure: print clear error message identifying exactly what failed, exit non-zero. Do not proceed with a broken configuration.

### Future: Encryption
SQLite encryption (via SQLCipher) is not implemented now but is a planned future addition. Use SQLAlchemy abstractions throughout so swapping the underlying engine requires minimal changes. Do not use any SQLite-specific syntax that would block this migration.

---

## DB Migrations (Alembic)

- Alembic configured from day one
- Every schema change requires a migration file
- Migrations run automatically on startup if pending
- Schema version tracked in `alembic_version` table
- Designed for future migration to Postgres with minimal changes (use SQLAlchemy abstractions, avoid SQLite-specific syntax)

---

## Testing Requirements

### Unit tests:
- Price calculation (mid, step increments, profit taker price from dollar amount)
- Serial number assignment (reuse logic, wrap at 999, boundary cases)
- Symbol validation (whitelist enforcement, reload behavior)
- Cache invalidation (TTL expiry, error-triggered invalidation)
- Config and `.env` loading and validation
- All repository methods (in-memory SQLite)
- Trade group state transitions
- Crash recovery logic (REPRICING state on startup)
- Safety limit enforcement

### Integration tests:
- Full order placement flow (mocked IB layer)
- Reprice loop with mocked time — verify price steps and amendment calls
- Profit taker placement after fill
- Partial fill handling
- Cancel-after-timeout
- Retry logic on API failure (mock failures)
- Close command cancels linked legs
- Reconciliation loop updates SQLite correctly
- Startup health check failure modes

### Smoke tests (`pytest -m smoke`):
A separate suite of end-to-end tests that require a live TWS or IB Gateway connection. Run manually from the command line or as part of a nightly CI job. Never run automatically on every startup — that is a planned future feature.

Smoke test cases:
- Fetch a live quote for each whitelisted symbol — verify bid/ask returned
- Contract lookup for each whitelisted symbol — verify conId returned and cached
- Place a 1-share limit order far outside the market (will not fill) — verify IB accepts it, then immediately cancel it
- Verify reconciliation loop can query IB and receive a valid response
- Verify profit taker order placement and immediate cancellation flow (1-share, safe price)

Smoke tests must:
- Be clearly marked with `@pytest.mark.smoke`
- Skip automatically if TWS/IB Gateway is unreachable (with a clear skip message)
- Clean up after themselves — no open orders left in IB after the suite runs
- Be documented in README with instructions on how to run them safely

**Future feature (stub now):** `daemon start --smoke` runs the smoke suite before the daemon fully initializes. If any smoke test fails, the daemon exits with a clear error. Stub the `--smoke` flag — accept it, log that it is not yet implemented, proceed normally.

### Test infrastructure:
- IB API layer fully mockable via abstract interface — unit and integration tests run with no TWS/Gateway connection
- In-memory SQLite for all DB tests
- `pytest` with fixtures for common setup
- Smoke tests tagged separately (`-m smoke`) and excluded from default `pytest` run
- CI-friendly: default suite has no external dependencies required
- Coverage target: >90% on core engine

---

## Project Structure

```
ib_trader/
├── repl/
│   ├── main.py                  # REPL entry point — interactive session loop
│   └── commands.py              # Command handlers (buy, sell, close, modify stub)
├── daemon/
│   ├── main.py                  # Daemon entry point
│   ├── tui.py                   # Textual TUI — live dashboard + command input
│   ├── reconciler.py            # IB reconciliation logic
│   ├── monitor.py               # REPL heartbeat watcher + alert trigger
│   └── integrity.py             # SQLite integrity checks
├── engine/
│   ├── order.py                 # Core order placement logic — execute_order, place_profit_taker
│   ├── pricing.py               # Pure pricing functions — calc_mid, calc_step_price, calc_profit_taker_price, calc_shares_from_dollars
│   ├── tracker.py               # In-flight order state: maps ib_order_id → asyncio.Event for fill notification; allows reprice_loop and fill callbacks to coordinate without shared memory
│   └── recovery.py              # Startup scan for ABANDONED orders — queries DB, marks status, prints warnings
├── ib/
│   ├── base.py                  # Abstract IB interface + throttle layer
│   └── insync_client.py         # ib_insync implementation
├── data/
│   ├── base.py                  # Abstract repository interfaces
│   ├── models.py                # SQLAlchemy models (trade_groups, orders, reprice_events, contracts, metrics, system_heartbeats, system_alerts)
│   └── repository.py            # All repository implementations — contract caching logic lives in ContractRepository here, not a separate module
├── config/
│   ├── settings.yaml            # All tunables
│   └── symbols.yaml             # Allowed symbol whitelist
├── migrations/                  # Alembic migration files
│   └── alembic.ini
├── logging/
│   └── logger.py                # Structured JSON logger + rotation
├── docs/
│   ├── decisions/               # Architecture Decision Records
│   └── index.md
├── tests/
│   ├── unit/
│   ├── integration/
│   └── smoke/                   # Requires live IB Gateway — run with: pytest -m smoke
├── CLAUDE.md                    # Non-negotiable engineering standards
├── CHANGELOG.md
├── mkdocs.yml
├── Makefile
├── .env.example
├── .gitignore                   # Must include .env, *.db, logs/
├── requirements.txt
└── README.md
```

---

## Documentation & Living Standards

### `CLAUDE.md` — Non-Negotiable Engineering Standards
This file must be created in the project root. Claude Code reads it automatically at the start of every session. It contains standing rules that apply to every change — small or large — no exceptions. Treat these as team-level engineering guidelines that every contributor (human or AI) must follow.

Generate this file with exactly the following content:

```markdown
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
```

### Architecture Decision Records (`docs/decisions/`)
Generate an ADR for each key decision made during the initial build:
- `001-order-amendment-not-cancel-replace.md`
- `002-zero-memory-state.md`
- `003-sqlite-wal-mode.md`
- `004-shared-sqlite-no-ipc.md`
- `005-decimal-not-float.md`
- `006-options-future-readiness.md`
- `007-ib-abstraction-layer.md`
- `008-ib-rate-limiting.md`
- `009-repl-not-one-shot-cli.md`
- `010-mutual-watchdog-via-heartbeat.md`
- `011-two-severity-levels-catastrophic-warning.md`
- `012-ib-insync-over-rest-api.md`

Each ADR must follow this format:
```markdown
# ADR-00N: Title
**Date:** YYYY-MM-DD
**Status:** Accepted

## Decision
What was decided.

## Reasoning
Why this decision was made.

## Consequences
What this means for the codebase going forward.

## Future Considerations
When/how this decision might be revisited.
```

### Auto-generated Docs
- Set up `mkdocs` with the `material` theme
- Docstrings on every module, class, and public method — written from day one
- `mkdocs.yml` configured to pull from docstrings and `docs/` folder
- `make docs` command to build and serve locally

### `CHANGELOG.md`
- Maintained in project root
- Every feature, fix, or architectural change gets an entry
- Format: date, change type (Added / Changed / Fixed / Deprecated), description
- Must be updated as part of every feature implementation — not optional

---



---

## Exact SQLAlchemy Column Definitions

Use these exact column names, types, and constraints. Do not rename, reorder, or add columns not listed here.

```python
# data/models.py
import uuid
from sqlalchemy import (
    Column, String, Integer, Numeric, Boolean,
    DateTime, Enum, ForeignKey, Text
)
from sqlalchemy.orm import declarative_base
import enum

Base = declarative_base()
def _uuid(): return str(uuid.uuid4())

class LegType(enum.Enum):
    ENTRY = "ENTRY"
    PROFIT_TAKER = "PROFIT_TAKER"
    STOP_LOSS = "STOP_LOSS"
    CLOSE = "CLOSE"

class OrderStatus(enum.Enum):
    PENDING = "PENDING"
    OPEN = "OPEN"
    REPRICING = "REPRICING"
    AMENDING = "AMENDING"
    FILLED = "FILLED"
    PARTIAL = "PARTIAL"
    CANCELED = "CANCELED"
    ABANDONED = "ABANDONED"
    CLOSED_MANUAL = "CLOSED_MANUAL"
    CLOSED_EXTERNAL = "CLOSED_EXTERNAL"
    REJECTED = "REJECTED"

class SecurityType(enum.Enum):
    STK = "STK"
    ETF = "ETF"
    OPT = "OPT"   # FUTURE — no trading logic
    FUT = "FUT"   # FUTURE — no trading logic

class TradeStatus(enum.Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    PARTIAL = "PARTIAL"

class AlertSeverity(enum.Enum):
    CATASTROPHIC = "CATASTROPHIC"
    WARNING = "WARNING"

class TradeGroup(Base):
    __tablename__ = "trade_groups"
    id               = Column(String(36), primary_key=True, default=_uuid)
    serial_number    = Column(Integer, unique=True, nullable=False)
    symbol           = Column(String(20), nullable=False)
    direction        = Column(String(5), nullable=False)    # LONG / SHORT
    status           = Column(Enum(TradeStatus), nullable=False, default=TradeStatus.OPEN)
    realized_pnl     = Column(Numeric(18, 8), nullable=True)
    total_commission = Column(Numeric(18, 8), nullable=True)
    opened_at        = Column(DateTime, nullable=False)
    closed_at        = Column(DateTime, nullable=True)

class Order(Base):
    __tablename__ = "orders"
    id                  = Column(String(36), primary_key=True, default=_uuid)
    trade_id            = Column(String(36), ForeignKey("trade_groups.id"), nullable=False)
    serial_number       = Column(Integer, nullable=True)        # entry leg only
    ib_order_id         = Column(String(50), nullable=True)
    leg_type            = Column(Enum(LegType), nullable=False)
    symbol              = Column(String(20), nullable=False)
    side                = Column(String(4), nullable=False)     # BUY / SELL
    security_type       = Column(Enum(SecurityType), nullable=False, default=SecurityType.STK)
    expiry              = Column(String(10), nullable=True)     # YYYYMMDD
    strike              = Column(Numeric(18, 4), nullable=True)
    right               = Column(String(4), nullable=True)      # CALL / PUT
    qty_requested       = Column(Numeric(18, 4), nullable=False)
    qty_filled          = Column(Numeric(18, 4), nullable=False, default=0)
    order_type          = Column(String(10), nullable=False)    # MID / MARKET
    price_placed        = Column(Numeric(18, 4), nullable=True)
    avg_fill_price      = Column(Numeric(18, 4), nullable=True)
    profit_taker_amount = Column(Numeric(18, 4), nullable=True)
    profit_taker_price  = Column(Numeric(18, 4), nullable=True)
    stop_loss_requested = Column(Numeric(18, 4), nullable=True) # stored, no IB action
    commission          = Column(Numeric(18, 8), nullable=True)
    status              = Column(Enum(OrderStatus), nullable=False, default=OrderStatus.PENDING)
    placed_at           = Column(DateTime, nullable=True)
    filled_at           = Column(DateTime, nullable=True)
    canceled_at         = Column(DateTime, nullable=True)
    last_amended_at     = Column(DateTime, nullable=True)
    raw_ib_response     = Column(Text, nullable=True)           # JSON string

class RepriceEvent(Base):
    __tablename__ = "reprice_events"
    id                  = Column(String(36), primary_key=True, default=_uuid)
    order_id            = Column(String(36), ForeignKey("orders.id"), nullable=False)
    step_number         = Column(Integer, nullable=False)
    bid                 = Column(Numeric(18, 4), nullable=False)
    ask                 = Column(Numeric(18, 4), nullable=False)
    new_price           = Column(Numeric(18, 4), nullable=False)
    amendment_confirmed = Column(Boolean, nullable=False, default=False)
    timestamp           = Column(DateTime, nullable=False)

class Contract(Base):
    __tablename__ = "contracts"
    symbol       = Column(String(20), primary_key=True)
    con_id       = Column(Integer, nullable=False)
    exchange     = Column(String(20), nullable=False)
    currency     = Column(String(5), nullable=False)
    multiplier   = Column(String(10), nullable=True)
    raw_response = Column(Text, nullable=True)
    fetched_at   = Column(DateTime, nullable=False)

class Metric(Base):
    __tablename__ = "metrics"
    id          = Column(String(36), primary_key=True, default=_uuid)
    trade_id    = Column(String(36), ForeignKey("trade_groups.id"), nullable=True)
    event_type  = Column(String(50), nullable=False)
    symbol      = Column(String(20), nullable=True)
    value       = Column(Numeric(18, 8), nullable=True)
    meta        = Column(Text, nullable=True)    # JSON string
    recorded_at = Column(DateTime, nullable=False)

class SystemHeartbeat(Base):
    __tablename__ = "system_heartbeats"
    process      = Column(String(10), primary_key=True)  # REPL / DAEMON
    last_seen_at = Column(DateTime, nullable=False)
    pid          = Column(Integer, nullable=True)

class SystemAlert(Base):
    __tablename__ = "system_alerts"
    id          = Column(String(36), primary_key=True, default=_uuid)
    severity    = Column(Enum(AlertSeverity), nullable=False)
    trigger     = Column(String(100), nullable=False)
    message     = Column(Text, nullable=False)
    created_at  = Column(DateTime, nullable=False)
    resolved_at = Column(DateTime, nullable=True)
```

Enable WAL mode and foreign keys on every new connection:
```python
from sqlalchemy import event as sa_event
@sa_event.listens_for(engine, "connect")
def set_pragmas(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA foreign_keys=ON")
```

---

## Exact Repository Interface

Implement exactly these method signatures in `data/repository.py`. No additional methods in v1.

```python
class TradeRepository:
    def create(self, trade: TradeGroup) -> TradeGroup
    def get_by_serial(self, serial: int) -> TradeGroup | None
    def get_open(self) -> list[TradeGroup]
    def update_status(self, trade_id: str, status: TradeStatus) -> None
    def update_pnl(self, trade_id: str, pnl: Decimal, commission: Decimal) -> None
    def next_serial_number(self) -> int    # lowest unused integer 0–999

class OrderRepository:
    def create(self, order: Order) -> Order
    def get_by_id(self, order_id: str) -> Order | None
    def get_by_ib_order_id(self, ib_order_id: str) -> Order | None
    def get_open_for_trade(self, trade_id: str) -> list[Order]
    def get_all_open(self) -> list[Order]
    def get_in_states(self, states: list[OrderStatus]) -> list[Order]
    def update_status(self, order_id: str, status: OrderStatus) -> None
    def update_fill(self, order_id: str, qty_filled: Decimal,
                    avg_price: Decimal, commission: Decimal) -> None
    def update_ib_order_id(self, order_id: str, ib_order_id: str) -> None
    def update_amended(self, order_id: str, new_price: Decimal) -> None
    def set_raw_response(self, order_id: str, raw: str) -> None

class RepriceEventRepository:
    def create(self, evt: RepriceEvent) -> RepriceEvent
    def get_for_order(self, order_id: str) -> list[RepriceEvent]
    def confirm_amendment(self, event_id: str) -> None

class ContractRepository:
    def get(self, symbol: str) -> Contract | None
    def upsert(self, contract: Contract) -> None
    def invalidate(self, symbol: str) -> None
    def is_fresh(self, symbol: str, ttl_seconds: int) -> bool

class HeartbeatRepository:
    def upsert(self, process: str, pid: int) -> None
    def get(self, process: str) -> SystemHeartbeat | None
    def delete(self, process: str) -> None

class AlertRepository:
    def create(self, alert: SystemAlert) -> SystemAlert
    def get_open(self) -> list[SystemAlert]
    def resolve(self, alert_id: str) -> None
```

All methods use `scoped_session`. Never expose sessions outside the repository layer. Never write raw SQL strings.

---

## Exact `ib_insync` Usage Patterns

Use exactly these patterns. Do not use deprecated or alternative methods.

```python
from ib_insync import IB, Stock, LimitOrder, MarketOrder, util

# Connection (inside async main):
ib = IB()
await ib.connectAsync(host, port, clientId=client_id, timeout=10)
ib.disconnectedEvent += on_disconnect

# Contract qualification:
contract = Stock(symbol, 'SMART', 'USD')
[qualified] = await ib.qualifyContractsAsync(contract)
# qualified.conId is the IB contract ID

# Place limit order:
order = LimitOrder(side, float(qty), float(price))
order.outsideRth = True
order.tif = 'GTC'
trade = ib.placeOrder(qualified, order)
ib_order_id = str(trade.order.orderId)   # write to SQLite immediately

# Amend order (same order object):
order.lmtPrice = float(new_price)
ib.placeOrder(qualified, order)   # modifies in place

# Cancel order:
ib.cancelOrder(order)

# Market data snapshot for bid/ask:
ticker = ib.reqMktData(qualified, '', True, False)
await asyncio.sleep(0.2)
bid = Decimal(str(ticker.bid)) if ticker.bid else None
ask = Decimal(str(ticker.ask)) if ticker.ask else None
ib.cancelMktData(qualified)

# Fill event subscription:
ib.execDetailsEvent += on_exec_details
# def on_exec_details(trade: Trade, fill: Fill) -> None

# Order status event subscription:
ib.orderStatusEvent += on_order_status
# def on_order_status(trade: Trade) -> None

# Fetch open orders for reconciliation:
open_trades = await ib.reqOpenOrdersAsync()

# asyncio integration:
util.startLoop()   # call once at process startup for interactive use
```

**Critical:** Always convert IB float prices to `Decimal` via `Decimal(str(value))` — never `Decimal(float_value)` directly.

---

## Exact REPL Command Grammar

Parse commands with `shlex.split()`. Do not use `argparse` in the REPL (it calls `sys.exit()` on errors).

```
buy  SYMBOL QTY   STRATEGY [PROFIT] [--take-profit-price N] [--stop-loss N] [--dollars N]
sell SYMBOL QTY   STRATEGY [PROFIT] [--take-profit-price N] [--stop-loss N] [--dollars N]
close SERIAL      [STRATEGY]        [--take-profit-price N]
modify SERIAL     -- STUB: log and return, no action

SYMBOL   = string, must be in symbols.yaml whitelist (checked before any IB call)
QTY      = positive integer (ignored if --dollars given)
STRATEGY = "mid" | "market"
PROFIT   = positive Decimal (total dollar profit → calculates per-share price)
SERIAL   = integer 0–999
```

Parsed command dataclasses (exact field names):
```python
@dataclass
class BuyCommand:
    symbol: str
    qty: Decimal | None
    dollars: Decimal | None
    strategy: str
    profit_amount: Decimal | None
    take_profit_price: Decimal | None
    stop_loss: Decimal | None

@dataclass
class SellCommand:
    symbol: str
    qty: Decimal | None
    dollars: Decimal | None
    strategy: str
    profit_amount: Decimal | None
    take_profit_price: Decimal | None
    stop_loss: Decimal | None

@dataclass
class CloseCommand:
    serial: int
    strategy: str               # default "mid"
    profit_amount: Decimal | None
    take_profit_price: Decimal | None

@dataclass
class ModifyCommand:
    serial: int                 # STUB — no other fields
```

On any parse or validation error: print `✗ Error: <message>` and return to prompt. Never raise to the user.

---

## Exact Async Engine Coroutine Signatures

All engine functions receive `AppContext` and call IB exclusively through `ctx.ib` (`IBClientBase`). No engine function ever imports or references `ib_insync` directly — that is the job of `insync_client.py` alone.

```python
# engine/order.py

async def execute_order(
    cmd: BuyCommand | SellCommand,
    ctx: AppContext,
) -> None:
    # Exact sequence:
    # 1. validate safety limits → raise SafetyLimitError if exceeded
    # 2. qualify contract via ctx.ib.qualify_contract() (cache-first via ctx.contracts)
    # 3. create TradeGroup + Order in DB via ctx.trades / ctx.orders (status=PENDING)
    # 4. fetch bid/ask via ctx.ib.get_market_snapshot()
    # 5. place IB order via ctx.ib.place_limit_order() or place_market_order()
    # 6. write ib_order_id to DB immediately via ctx.orders.update_ib_order_id()
    # 7. update order status → OPEN
    # 8. register fill/status callbacks via ctx.ib.register_fill_callback() / register_status_callback()
    # 9. create_task(reprice_loop(...)) if strategy == "mid"
    # 10. await fill event or timeout (asyncio.Event)
    # 11. on fill: write fill details, call place_profit_taker() if profit target set
    # 12. on timeout: ctx.ib.cancel_order(), write final status

async def reprice_loop(
    order_id: str,
    ib_order_id: str,           # string ID only — no raw ib_insync objects
    con_id: int,                 # IB contract ID for market data snapshots
    symbol: str,
    side: str,
    ctx: AppContext,
    total_steps: int,
    interval_seconds: float,
) -> None:
    # Loops total_steps times with asyncio.sleep(interval_seconds)
    # Each step:
    #   snapshot = await ctx.ib.get_market_snapshot(con_id)
    #   new_price = calc_step_price(snapshot["bid"], snapshot["ask"], step, total_steps)
    #   await ctx.ib.amend_order(ib_order_id, new_price)
    #   ctx.reprice_events.create(RepriceEvent(...))
    #   ctx.orders.update_amended(order_id, new_price)

async def place_profit_taker(
    trade_id: str,
    entry_order_id: str,
    entry_side: str,             # "BUY" or "SELL" — profit taker uses opposite side
    avg_fill_price: Decimal,
    qty_filled: Decimal,
    profit_amount: Decimal | None,
    take_profit_price: Decimal | None,
    con_id: int,
    symbol: str,
    ctx: AppContext,
) -> None:
    # Profit price calculation depends on entry side:
    #   BUY entry:  profit_price = avg_fill_price + (profit_amount / qty_filled)
    #   SELL entry: profit_price = avg_fill_price - (profit_amount / qty_filled)
    # If take_profit_price given directly, use it as-is regardless of side.
    # Place GTC limit order via ctx.ib.place_limit_order() with opposite side.
    # Write to DB as PROFIT_TAKER leg via ctx.orders.create()
```

---

## Exact Logging Event Names

Use exactly these strings as the `event` field. Never use freeform strings for structured events.

```
APP_STARTED              APP_STOPPED
HEALTH_CHECK_PASSED      HEALTH_CHECK_FAILED
SYMBOL_VALIDATED         SYMBOL_REJECTED
SAFETY_LIMIT_EXCEEDED
IB_CONNECTED             IB_DISCONNECTED         IB_RECONNECTED
IB_THROTTLED             IB_PACING_VIOLATION
CONTRACT_CACHE_HIT       CONTRACT_CACHE_MISS      CONTRACT_FETCHED
ORDER_CREATED            ORDER_PLACED             ORDER_AMENDED
ORDER_FILLED             ORDER_PARTIAL_FILL       ORDER_CANCELED
ORDER_REJECTED           ORDER_ABANDONED          ORDER_CLOSED_MANUAL
ORDER_CLOSED_EXTERNAL
PROFIT_TAKER_PLACED      PROFIT_TAKER_CANCELED
REPRICE_STEP             REPRICE_TIMEOUT
RECONCILIATION_STARTED   RECONCILIATION_COMPLETE  RECONCILED_EXTERNAL
HEARTBEAT_WRITTEN        HEARTBEAT_STALE
SYSTEM_ALERT_RAISED      SYSTEM_ALERT_RESOLVED
REPL_STARTED             REPL_EXIT_CLEAN          CLI_CRASH_DETECTED
DB_INTEGRITY_PASSED      DB_INTEGRITY_FAILED
STOP_LOSS_STUB_RECEIVED  MODIFY_STUB_RECEIVED
```

---

## Exact Settings Keys

`config/settings.yaml` must contain exactly these keys:

```yaml
max_order_size_shares: 10
max_retries: 3
retry_delay_seconds: 2
retry_backoff_multiplier: 2.0
reprice_interval_seconds: 1
reprice_duration_seconds: 10
ib_host: 127.0.0.1
ib_port: 7497
ib_client_id: 1
ib_min_call_interval_ms: 100
cache_ttl_seconds: 86400
log_level: INFO
log_file_path: logs/ib_trader.log
log_rotation_max_bytes: 10485760
log_rotation_backup_count: 10
log_compress_old: true
heartbeat_interval_seconds: 30
heartbeat_stale_threshold_seconds: 300
reconciliation_interval_seconds: 1800
db_integrity_check_interval_seconds: 21600
daemon_tui_refresh_seconds: 5
```

`.env` exact keys:
```
IB_HOST=127.0.0.1
IB_PORT=7497
IB_CLIENT_ID=1
IB_ACCOUNT_ID=U1234567
```

---

## Shared Contracts — Must Be Consistent Across All Modules

These six things must be defined explicitly because they are shared between multiple files that Claude Code will write independently. If left ambiguous, the files will not agree with each other.

---

### 1. Abstract IB Interface (`ib/base.py`)

All engine code imports and depends on this interface. `insync_client.py` implements it. Tests mock it. Every method must match exactly.

```python
from abc import ABC, abstractmethod
from decimal import Decimal

class IBClientBase(ABC):

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def qualify_contract(self, symbol: str, sec_type: str = "STK",
                                exchange: str = "SMART", currency: str = "USD") -> dict:
        # returns: {"con_id": int, "exchange": str, "currency": str,
        #           "multiplier": str | None, "raw": str}
        ...

    @abstractmethod
    async def get_market_snapshot(self, con_id: int) -> dict:
        # returns: {"bid": Decimal, "ask": Decimal, "last": Decimal}
        ...

    @abstractmethod
    async def place_limit_order(self, con_id: int, symbol: str, side: str,
                                 qty: Decimal, price: Decimal,
                                 outside_rth: bool = True,
                                 tif: str = "GTC") -> str:
        # returns: ib_order_id as string
        ...

    @abstractmethod
    async def place_market_order(self, con_id: int, symbol: str, side: str,
                                  qty: Decimal, outside_rth: bool = True) -> str:
        # returns: ib_order_id as string
        ...

    @abstractmethod
    async def amend_order(self, ib_order_id: str, new_price: Decimal) -> None: ...

    @abstractmethod
    async def cancel_order(self, ib_order_id: str) -> None: ...

    @abstractmethod
    async def get_order_status(self, ib_order_id: str) -> dict:
        # returns: {"status": str, "qty_filled": Decimal,
        #           "avg_fill_price": Decimal | None, "commission": Decimal | None}
        ...

    @abstractmethod
    async def get_open_orders(self) -> list[dict]:
        # returns list of: {"ib_order_id": str, "symbol": str, "status": str,
        #                    "qty_filled": Decimal, "avg_fill_price": Decimal | None}
        ...

    @abstractmethod
    def register_fill_callback(self, callback) -> None:
        # callback signature: async def on_fill(ib_order_id: str, qty_filled: Decimal,
        #                                       avg_price: Decimal, commission: Decimal)
        ...

    @abstractmethod
    def register_status_callback(self, callback) -> None:
        # callback signature: async def on_status(ib_order_id: str, status: str)
        ...
```

---

### 2. AppContext — Dependency Injection Container

All modules receive dependencies via an `AppContext` object. No global singletons. No module-level imports of live objects. This is the single wiring point for the entire application.

```python
# config/context.py
from dataclasses import dataclass
from ib.base import IBClientBase
from data.repository import (TradeRepository, OrderRepository,
                              RepriceEventRepository, ContractRepository,
                              HeartbeatRepository, AlertRepository)

@dataclass
class AppContext:
    ib: IBClientBase
    trades: TradeRepository
    orders: OrderRepository
    reprice_events: RepriceEventRepository
    contracts: ContractRepository
    heartbeats: HeartbeatRepository
    alerts: AlertRepository
    settings: dict          # loaded from settings.yaml
    account_id: str         # from .env
```

`AppContext` is created once at process startup and passed to every engine function, command handler, and background loop. Nothing constructs its own repositories or IB client.

---

### 3. Pricing Function Signatures (`engine/pricing.py`)

Pure functions — no IB calls, no DB access. Fully unit testable in isolation.

```python
from decimal import Decimal

def calc_mid(bid: Decimal, ask: Decimal) -> Decimal:
    """Returns (bid + ask) / 2, rounded to 4 decimal places."""

def calc_step_price(bid: Decimal, ask: Decimal, step: int, total_steps: int) -> Decimal:
    """
    Returns the price for reprice step `step` (1-indexed).
    Formula: mid + (step / total_steps) * (ask - mid)
    Rounded to 4 decimal places.
    step=0 returns mid exactly.
    step=total_steps returns ask exactly.
    """

def calc_profit_taker_price(avg_fill_price: Decimal, qty_filled: Decimal,
                             profit_amount: Decimal) -> Decimal:
    """
    Returns: avg_fill_price + (profit_amount / qty_filled)
    Rounded to 4 decimal places.
    Raises ValueError if qty_filled is zero.
    """

def calc_shares_from_dollars(dollars: Decimal, price: Decimal,
                              max_shares: int) -> Decimal:
    """
    Returns: floor(dollars / price), capped at max_shares.
    Uses mid price as `price`.
    Raises ValueError if price is zero or negative.
    """
```

---

### 4. Dollars-to-Shares Conversion Rule

When `--dollars` is specified, shares are calculated as:
```
shares = floor(dollars / mid_price)
```
Where `mid_price` is the live mid at the time the command is parsed — fetched via `get_market_snapshot()` before the order is placed. The result is then validated against `max_order_size_shares`. If `shares == 0` after flooring, reject with `✗ Error: dollar amount too small for current price`.

---

### 5. Custom Exception Hierarchy (`engine/exceptions.py`)

All custom exceptions live in one file. All modules import from here. No inventing exception names elsewhere.

```python
class IBTraderError(Exception):
    """Base exception for all application errors."""

class SafetyLimitError(IBTraderError):
    """Order exceeds configured safety limits."""

class SymbolNotAllowedError(IBTraderError):
    """Symbol not in whitelist."""

class IBConnectionError(IBTraderError):
    """Cannot connect to or communicate with IB."""

class IBOrderRejectedError(IBTraderError):
    """IB rejected the order. Contains rejection reason."""
    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(f"Order rejected by IB: {reason}")

class ContractNotFoundError(IBTraderError):
    """Could not qualify IB contract for symbol."""

class TradeNotFoundError(IBTraderError):
    """No trade found for given serial number."""

class ConfigurationError(IBTraderError):
    """Invalid or missing configuration."""

class DBIntegrityError(IBTraderError):
    """SQLite integrity check failed."""
```

---

### 6. asyncio Ownership

**REPL process:**
```python
# repl/main.py
from ib_insync import util
util.startLoop()   # patches the running event loop for ib_insync

async def run_repl(ctx: AppContext) -> None:
    # all REPL logic runs here as coroutines
    ...

asyncio.run(run_repl(ctx))
```

**Daemon process:**
The daemon does NOT use `util.startLoop()`. It runs its own independent event loop:
```python
# daemon/main.py
async def run_daemon(ctx: AppContext) -> None:
    # reconciliation loop, monitor loop, integrity check loop
    # all as asyncio.create_task(...)
    ...

asyncio.run(run_daemon(ctx))
```

The REPL and daemon are separate OS processes with completely independent event loops. They never share an event loop. The daemon's `ib_insync` connection (for passive health checks only) uses its own `IB()` instance with a different `clientId` than the REPL.

**Daemon `clientId`:** REPL uses `IB_CLIENT_ID` from `.env`. Daemon uses `IB_CLIENT_ID + 1` automatically. Both must be unique. Document this in README.

---

## Exact Makefile Targets

```makefile
install:    pip install -r requirements.txt
test:       pytest tests/unit tests/integration -v --cov=ib_trader
smoke:      pytest tests/smoke -v -m smoke
docs:       mkdocs serve
lint:       ruff check .
typecheck:  mypy ib_trader/
clean:      find . -type d -name __pycache__ -exec rm -rf {} +
```

---

Build in this sequence — do not skip ahead:

1. **`CLAUDE.md` and `docs/decisions/` first** — standards and ADRs before any code
2. **Data layer** — all models including `system_heartbeats` and `system_alerts`, repositories, Alembic
3. **IB abstraction** — abstract interface + throttle layer + mock implementation
4. **Core engine** — pricing, order placement, reprice loop, profit taker, recovery
5. **Unit tests** — alongside each engine module
6. **REPL** — interactive session loop, commands, heartbeat, startup sequence
7. **Daemon** — reconciliation, integrity checks, REPL monitor, alert system
8. **Daemon TUI** — Textual dashboard with CATASTROPHIC/WARNING states, command input
9. **Integration tests** — full flows with mock IB layer
10. **Startup health check** — wired into both REPL and daemon startup
11. **Real IB client** — swap in `ib_insync` implementation, test against live TWS/Gateway
12. **Smoke test suite** — against real Gateway, cleans up after itself
13. **mkdocs + docstrings pass** — confirm `make docs` works

---

## Key Implementation Notes

- All monetary values stored as `Decimal` or integer cents — never `float`
- SQLite WAL mode: `PRAGMA journal_mode=WAL` on every new connection
- SQLAlchemy session management must be thread-safe (`scoped_session`)
- `ib_insync` is async-native — use `asyncio` throughout the engine; the reprice loop runs as a coroutine, not a thread
- Fill and order status events are pushed by IB — register callbacks via `IBClientBase`, never poll
- `clientId` must be unique per connected IB client — REPL uses `IB_CLIENT_ID`, daemon uses `IB_CLIENT_ID + 1`
- All `outsideRth = True` is enforced inside `insync_client.py` — engine code never sets IB order fields directly
- On `close` or `sell`: cancel all linked open orders (profit taker) before placing the closing order
- REPL and daemon are fully independent — daemon does not need to be running for REPL to work
- Mock IB layer: implement `IBClientBase` with a `MockIBClient` in `tests/` — all unit and integration tests use it, no live TWS required
- README must include: TWS/IB Gateway setup steps, port configuration, `.env` setup, how to start both processes, first-run walkthrough
