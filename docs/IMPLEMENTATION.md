# IB Trader — Implementation Reference

Version: 0.1.0
Status: Live tested against IB Gateway with real paper trading orders.

---

## What Is Built

A two-process algorithmic trading terminal for Interactive Brokers. The REPL is an
interactive shell for placing and managing orders. The daemon is a background watchdog
that monitors IB connectivity, reconciles order state, and runs a live TUI dashboard.
Both processes are fully independent and communicate exclusively through SQLite.

---

## Module Map

```
ib_trader/
├── config/
│   ├── context.py        AppContext — dependency injection container
│   └── loader.py         load_settings, load_env, load_symbols, check_file_permissions
├── daemon/
│   ├── integrity.py      SQLite PRAGMA integrity_check → CATASTROPHIC alert on failure
│   ├── main.py           Daemon entry point, background loops, TUI wiring
│   ├── monitor.py        REPL heartbeat check, IB connectivity check
│   ├── reconciler.py     IB reconciliation against SQLite open orders
│   └── tui.py            Textual TUI dashboard with live refresh and command input
├── data/
│   ├── base.py           Abstract repository interfaces (ABCs)
│   ├── models.py         SQLAlchemy ORM models — exact column names from spec
│   └── repository.py     Concrete repos + create_db_engine, create_session_factory
├── engine/
│   ├── exceptions.py     IBTraderError hierarchy (all custom exceptions)
│   ├── order.py          execute_order, reprice_loop, place_profit_taker, execute_close
│   ├── pricing.py        Pure pricing functions — Decimal only, no IB calls
│   ├── recovery.py       Startup crash recovery for REPRICING/AMENDING orders
│   └── tracker.py        OrderTracker — ib_order_id → asyncio.Event coordination
├── ib/
│   ├── base.py           IBClientBase abstract interface + throttle layer
│   └── insync_client.py  ib_insync concrete implementation (only file importing ib_insync)
├── logging_/
│   └── logger.py         Structured JSON logging, gzip-rotating file handler
└── repl/
    ├── commands.py       Command parsing (shlex), Strategy enum, command dataclasses
    └── main.py           REPL interactive loop, heartbeat, startup sequence
```

---

## What Works

### Order Placement

**`buy SYMBOL QTY STRATEGY [PROFIT] [--take-profit-price N] [--stop-loss N] [--dollars N]`**
**`sell SYMBOL QTY STRATEGY [PROFIT] [--take-profit-price N] [--stop-loss N] [--dollars N]`**

Strategies:

| Strategy | Behaviour |
|----------|-----------|
| `mid`    | Limit at mid-price. Reprice loop walks BUY orders toward ask, SELL orders toward bid, over `reprice_duration_seconds`. |
| `market` | Market order. Waits up to 30 s for fill. |
| `bid`    | Fixed limit at current bid. GTC, no repricing. |
| `ask`    | Fixed limit at current ask. GTC, no repricing. |

The `--dollars N` flag calculates quantity as `floor(dollars / mid_price)`, capped at `max_order_size_shares`.

**Profit taker**: placed automatically after fill if `PROFIT` or `--take-profit-price` is given. GTC limit on the inverse side. Price = `avg_fill ± (profit / qty)`.

**Stop loss**: accepted, stored in the DB, logged. No IB action — stub only.

### Order Lifecycle

```
PENDING → OPEN/REPRICING → AMENDING → FILLED / PARTIAL / CANCELED / ABANDONED
```

- `REPRICING`: order placed, reprice loop active.
- `AMENDING`: amendment in flight to IB.
- `ABANDONED`: order was REPRICING or AMENDING when REPL crashed. Requires manual check in IB.
- `CLOSED_EXTERNAL`: fill or cancel detected by the daemon reconciler, not by the REPL.

### Reprice Loop

- Amends the existing order in place (single IB order ID throughout — not cancel-replace).
- Polls every `reprice_interval_seconds` (default 1 s) for `reprice_duration_seconds` (default 10 s).
- Fetches live bid/ask snapshot each step.
- BUY: walks mid → ask. SELL: walks mid → bid.
- Deduplicates: if the new 2dp price equals the last sent price, the amendment is skipped.
- Waits for IB to acknowledge (`PendingSubmit` → `Submitted`) before sending the first amendment, avoiding IB error 103.
- Records each amendment as a `RepriceEvent` row in SQLite.

### Position Close

**`close SERIAL [STRATEGY] [--take-profit-price N]`**

- Finds the open trade by serial number.
- Cancels any linked profit taker order first.
- Places a closing order on the inverse side for the full entry quantity.
- Supports `mid`, `market`, `bid`, `ask` strategies.

### IB Error Handling

- Error 300 (`cancelMktData` on already-cancelled snapshot): fixed — no explicit cancel called after `snapshot=True` requests.
- Error 103 (duplicate order ID): fixed — PendingSubmit wait loop before reprice loop starts.
- Error 10147 (cancel on already-terminal order): fixed — terminal status check before `cancelOrder`.
- Error 435 (account not set): fixed — `order.account` set on every order from `.env`.
- Error 110 / price rejection: fixed — prices rounded to 2dp (`Decimal("0.01")`).
- Order rejection surfaced immediately via `errorEvent` callback — real IB message shown instead of timeout.
- IB informational codes (1100-1102, 2104-2158) logged at INFO, not WARNING/ERROR.
- `ib_insync` internal logger silenced at CRITICAL to eliminate duplicate log lines.

### Safety

- `max_order_size_shares` (default 10) checked before any IB call — raises `SafetyLimitError`.
- Symbol whitelist validated from `config/symbols.yaml` before any IB call.
- `.env` and `trader.db` file permissions checked (must be 600) on every startup.
- Secrets never logged — account ID, credentials excluded from all log events.

### Crash Recovery

On REPL startup, orders in `REPRICING` or `AMENDING` state are marked `ABANDONED`. The REPL prints a warning listing serial numbers to check manually in IB. The order is not cancelled from IB — it may still be working.

### Daemon

Background process (`ib-daemon`) running independently of the REPL:

- **Reconciliation**: every `reconciliation_interval_seconds` (default 1800 s), queries IB for open order status and updates SQLite for any discrepancies. Orders filled or cancelled externally are marked `CLOSED_EXTERNAL` or `CANCELED`.
- **REPL heartbeat monitor**: alerts `CATASTROPHIC` if REPL heartbeat is stale beyond `heartbeat_stale_threshold_seconds` (default 300 s).
- **IB connectivity monitor**: alerts `CATASTROPHIC` after 3 consecutive connectivity failures.
- **SQLite integrity check**: runs `PRAGMA integrity_check` on startup and every `db_integrity_check_interval_seconds` (default 6 hours). Failure → CATASTROPHIC alert.
- **TUI**: Textual live dashboard showing gateway status, order counts, P&L, and active alerts. CATASTROPHIC state turns TUI red and pauses all background loops until Enter is pressed.

### Mutual Watchdog

REPL writes heartbeat to `system_heartbeats` every 30 s. Daemon reads it. Daemon writes its own heartbeat. REPL reads it and warns if daemon is absent. Communication is SQLite-only — no sockets, no pipes.

### Logging

Structured JSON to `logs/ib_trader.log`. Rotating with gzip compression. Every log entry includes `timestamp`, `level`, `event`, and relevant IDs. Named event types for all structured events (no freeform strings).

### Contract Cache

Contracts are cached in `contracts` table with a TTL (`cache_ttl_seconds`, default 24 h). On cache hit, the engine also checks the ib_insync in-memory cache — if that is empty (e.g. after a restart), `qualify_contract` is called to repopulate it, preventing the `PendingSubmit` indefinitely bug.

---

## What Is Stubbed

| Feature | Status | Notes |
|---------|--------|-------|
| Stop loss | Stored, logged | No IB bracket order placed |
| `modify` command | Accepts serial, returns stub | No IB action |
| Options (`OPT`) | `SecurityType.OPT` defined in models | No trading logic |
| Futures (`FUT`) | `SecurityType.FUT` defined in models | No trading logic |
| `Metric` table | Schema exists | Not written to anywhere |
| Partial fill continuation | Recorded as `PARTIAL`, printed | Remaining quantity not re-placed |

---

## What Is Not Yet Implemented

- Bracket orders (entry + stop loss + profit taker submitted atomically to IB)
- Position sizing beyond `max_order_size_shares` (no portfolio-level risk management)
- Multi-leg strategies (spreads, pairs)
- Options and futures trading logic
- P&L calculation from fills (stored `realized_pnl` field is never written on close)
- Historical order analytics via the `metrics` table
- Account switching (single account per session)
- `modify` command — no amendment of open GTC orders from REPL

---

## Test Coverage

- 175 tests: unit + integration (no live IB required).
- Smoke tests (`@pytest.mark.smoke`) require live IB Gateway — excluded from default run.
- Coverage: 91.75% on core engine modules (target: 90%).
- Mock IB layer (`MockIBClient` in `tests/conftest.py`) supports: order placement, fills, amendments, cancels, market snapshots, error simulation.

Run tests:
```bash
uv run pytest tests/unit tests/integration -v
uv run pytest -m smoke   # requires live IB Gateway
```

---

## Configuration

All tunables in `config/settings.yaml`. All secrets in `.env`.

Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `max_order_size_shares` | 10 | Safety hard limit |
| `reprice_interval_seconds` | 1 | Reprice loop poll interval |
| `reprice_duration_seconds` | 10 | Total reprice window |
| `ib_port` | 4001 | Gateway port (TWS = 7497) |
| `ib_market_data_type` | 1 | 1=live, 3=delayed (paper accounts) |
| `cache_ttl_seconds` | 86400 | Contract cache TTL |
| `heartbeat_interval_seconds` | 30 | REPL heartbeat frequency |
| `reconciliation_interval_seconds` | 1800 | Daemon reconciliation frequency |

Required `.env` keys: `IB_ACCOUNT_ID`, `DB_PATH`.

---

## Live Testing Notes

The system has been tested against IB Gateway with real paper trading orders. Issues
encountered and fixed during live testing:

- IB error 300 on market data snapshot (removed explicit `cancelMktData` call)
- IB error 103 on first amendment (added PendingSubmit wait loop)
- IB error 10147 on cancel of terminal order (added terminal status guard)
- IB error 435 account not set (set `order.account` on every order)
- IB error 110 price precision (changed from 4dp to 2dp rounding)
- Fill reporting showing 0 shares (gated fill path on `qty > 0 and avg_price is not None`)
- Commission showing $0 (captured from `execDetailsEvent` callback, not post-loop poll)
- Sell reprice direction wrong (fixed: BUY walks toward ask, SELL walks toward bid)
- Duplicate IB error log lines (silenced `ib_insync` internal logger on connect)
