# IB Trader — Technical Design

Version: 1.0.0

---

## System Architecture

```
┌─────────────┐   ┌─────────────┐   ┌─────────────┐
│  REPL (CLI) │   │ API Server  │   │ Bot Runner  │
│  no broker  │   │  (FastAPI)  │   │  no broker  │
│  connection │   │  port 8000  │   │  connection │
└──────┬──────┘   └──────┬──────┘   └──────┬──────┘
       │                 │                 │
       │  INSERT INTO    │  INSERT INTO    │  INSERT INTO
       │  pending_cmds   │  pending_cmds   │  pending_cmds
       │                 │                 │
       └────────┬────────┴────────┬────────┘
                │    SQLite       │
                │   (WAL mode)    │
                ▼                 │
       ┌─────────────────┐       │
       │  Engine Service  │◄──────┘
       │  (sole broker    │
       │   connection)    │  polls pending_cmds
       │  client_id = 1   │  executes via broker
       │                  │  writes results back
       └────────┬─────────┘
                │
        ┌───────┴───────┐
        │               │
   ┌────▼────┐    ┌─────▼─────┐
   │   IB    │    │  Alpaca   │
   │ Gateway │    │  REST API │
   └─────────┘    └───────────┘

       ┌─────────────┐
       │   Daemon     │  (separate, own IB connection)
       │  client_id=2 │  reconciliation + monitoring
       └─────────────┘
```

### Central Engine Pattern

Only the engine service holds broker connections. All other processes are clients that:
1. Write commands to the `pending_commands` SQLite table
2. Read results from the same table
3. Read state from other tables (orders, trades, positions, alerts)

This eliminates the IB client_id conflict problem (where multiple processes placing
orders on different client_ids can't see each other's fills) and follows the project's
core principle: **communicate only through SQLite**.

### Process Roles

| Process | Broker Connection | Writes | Reads |
|---------|-------------------|--------|-------|
| Engine (`ib-engine`) | YES (IB client_id=1) | Orders, trades, transactions, heartbeat | pending_commands |
| Daemon (`ib-daemon`) | YES (IB client_id=2, read-only) | Reconciliation, alerts, heartbeat | Orders, trades |
| API (`ib-api`) | NO | pending_commands, templates, heartbeat | Everything (for REST/WebSocket) |
| REPL (`ib-trader`) | NO | pending_commands, heartbeat | Everything (for display) |
| Bots (`ib-bots`) | NO | pending_commands, bot status, bot_events | Positions, bot config |

---

## Command Execution Flow

```
User types "buy AAPL 10 mid" in GUI
    │
    ▼
POST /api/commands {"command": "buy AAPL 10 mid"}
    │
    ▼
API inserts into pending_commands (status=PENDING)
Returns 202 Accepted with command_id
    │
    ▼
Engine polls pending_commands every 100ms
Picks up PENDING row, sets status=RUNNING
    │
    ▼
Engine parses command → BuyCommand
Engine calls execute_order(cmd, ctx)
    │
    ├── resolve_instrument (contract cache)
    ├── get_market_snapshot (bid/ask/last)
    ├── create TradeGroup + Order in SQLite
    ├── place_limit_order via IB
    ├── reprice_loop (amend every 1s for 10s)
    ├── wait for fill (callback or poll)
    └── handle_fill (update DB, place profit taker)
    │
    ▼
Engine sets pending_commands status=SUCCESS with output
    │
    ▼
GUI polls GET /api/commands/{id} every 500ms
Updates console with result
Refreshes positions, orders, trades panels
```

### Concurrent Execution

The engine executes commands concurrently via `asyncio.create_task()` with a
semaphore (default max 5 concurrent). Each command gets an isolated `AppContext`
copy (via `dataclasses.replace`) so output routing doesn't leak between commands.

### Crash Recovery

On startup, the engine:
1. Marks any `RUNNING` commands as `FAILURE` (stale from previous crash)
2. Marks `PENDING` orders with no `ib_order_id` as `ABANDONED`
3. Closes orphaned trade groups where all legs are terminal

### Cancel-vs-Fill Race Condition

When the reprice window expires, the engine cancels the order. But IB may fill
the order between the cancel request and confirmation. The engine now waits up
to 3 seconds after cancelling, polling `get_order_status()` to check if a fill
arrived. If qty_filled > 0, it processes the fill instead of marking EXPIRED.

This applies to both entry orders and close orders.

---

## Broker Abstraction

```
ib_trader/broker/
├── base.py              # BrokerClientBase (abstract)
├── types.py             # Instrument, Snapshot, OrderResult, FillResult
├── fill_stream.py       # FillStream (push for IB, WebSocket for Alpaca)
├── market_hours.py      # MarketHoursProvider (per-broker session logic)
├── factory.py           # create_broker(name, settings)
├── ib/
│   ├── client.py        # IBClient wrapping InsyncClient
│   └── hours.py         # IB session windows (overnight, RTH, etc.)
└── alpaca/
    ├── client.py         # AlpacaClient (REST + WebSocket fills)
    └── hours.py          # Alpaca session windows
```

### BrokerClientBase

All broker IDs are strings. No `int con_id` in the interface.

Key methods:
- `resolve_instrument(symbol)` → Instrument (replaces `qualify_contract`)
- `get_snapshot(asset_id)` → Snapshot (bid, ask, last)
- `place_limit_order(asset_id, symbol, side, qty, price, extended_hours, tif)` → broker_order_id
- `amend_order(broker_order_id, new_price)` → broker_order_id (same for IB, new for Alpaca)
- `cancel_order(broker_order_id)`
- `create_fill_stream()` → FillStream

### BrokerCapabilities

Each broker declares what it supports:
- `supports_in_place_amend` — IB=True (same order ID), Alpaca=False (PATCH gives new ID)
- `supports_overnight` — IB=True, Alpaca=False
- `commission_free` — IB=False, Alpaca=True
- `fill_delivery` — "push" (IB callbacks) or "websocket" (Alpaca TradingStream)

### Market Hours

Each broker has session-aware order parameters:

| Session | IB | Alpaca |
|---------|----|----|
| RTH | tif=GTC, outsideRth=True | tif=gtc, extended_hours=False |
| Overnight | tif=DAY, includeOvernight=True | No overnight session |
| Extended hours | tif=GTC, outsideRth=True | tif=day, extended_hours=True |
| Market orders | Always allowed | Rejected during extended hours |

### Legacy Compatibility

The existing engine code uses `ctx.ib` (IBClientBase). `BrokerClientBase` provides
legacy methods (`qualify_contract`, `get_market_snapshot` with int con_id) that
delegate to the new interface. No engine changes needed for IB to keep working.

---

## API Server

FastAPI application with no broker connection. Reads from SQLite, submits commands.

### REST Endpoints

| Method | Path | Purpose |
|--------|------|---------|
| POST | /api/commands | Submit command (returns 202) |
| GET | /api/commands/{id} | Poll command status |
| GET | /api/orders | Open orders |
| GET | /api/trades | Trade groups (filterable) |
| GET | /api/positions | IB positions from cache |
| GET | /api/alerts | System alerts |
| POST | /api/alerts/{id}/resolve | Resolve alert |
| GET | /api/status | Heartbeats, connection, P&L |
| GET | /api/bots | Bot list |
| POST | /api/bots/{id}/start\|stop | Bot lifecycle |
| GET/POST/DELETE | /api/templates | Order templates |
| GET | /api/logs | Recent log entries from file |

### WebSocket

`/ws` — subscribes to channels, receives snapshots and diffs.

```
Client → Server: {"type": "subscribe", "channels": ["orders","trades","alerts","commands","heartbeats"]}
Server → Client: {"type": "snapshot", "data": {...}}
Server → Client: {"type": "diff", "channel": "orders", "added": [...], "updated": [...], "removed": [...]}
```

Polls SQLite every 1.5 seconds. Computes diffs per channel using content hashing.

### Auth

Optional API key auth via `API_SECRET_KEY` in `.env`. When set, all endpoints
require `Authorization: Bearer <key>` header. WebSocket requires `?token=<key>`
query parameter.

---

## Bot Framework

```
ib_trader/bots/
├── base.py              # BotBase abstract class
├── runner.py            # BotRunner (manages bot lifecycle)
├── registry.py          # Strategy name → class mapping
├── main.py              # CLI entry point
└── examples/
    └── mean_revert.py   # Example strategy
```

### BotBase

Bots have NO broker connection. They submit commands via `pending_commands`:

```python
class BotBase(ABC):
    async def on_tick(self) -> None: ...          # Called every tick_interval
    async def on_startup(self, positions) -> None # Crash recovery
    async def place_order(self, command, broker)   # → pending_commands
    async def wait_for_fill(self, serial, timeout) # Poll trade_groups
    def get_open_positions(self) -> list            # Read from SQLite
```

### BotRunner

Separate process. Polls the `bots` table every second:
- `status=RUNNING` + not in running_tasks → start asyncio task
- `status!=RUNNING` + in running_tasks → cancel task
- Task done with exception → set `status=ERROR`

Zero memory state: on startup, restarts any bots with `status=RUNNING`.

---

## Database Schema

### Core Tables

| Table | Purpose |
|-------|---------|
| `trade_groups` | Trade lifecycle (OPEN → CLOSED) with P&L |
| `orders` | Order legs (ENTRY, PROFIT_TAKER, CLOSE) with fill data |
| `reprice_events` | Amendment history per order |
| `transactions` | Append-only audit log of every IB interaction |
| `contracts` | Cached instrument details (TTL-based) |

### System Tables

| Table | Purpose |
|-------|---------|
| `system_heartbeats` | Process liveness (ENGINE, DAEMON, API, BOT_RUNNER) |
| `system_alerts` | CATASTROPHIC / WARNING conditions |
| `pending_commands` | Command queue (engine-client communication) |
| `position_cache` | IB positions snapshot (refreshed every 30s by engine) |

### Bot Tables

| Table | Purpose |
|-------|---------|
| `bots` | Bot config, status, heartbeat, last signal/action |
| `bot_events` | Append-only bot audit log with payload_json |
| `order_templates` | Saved quick-fire order templates |

### Key Constraints

- All monetary values: `Numeric(18, 8)` mapped to Python `Decimal`
- All primary keys: UUID strings (except autoincrement on transactions, bot_events, position_cache)
- All datetimes: server-local timezone
- WAL mode enabled on every connection
- Foreign keys enforced

---

## Dependency Injection

`AppContext` is created once at process startup and passed through the call stack:

```python
@dataclass
class AppContext:
    ib: IBClientBase                    # Primary broker (legacy alias)
    trades: TradeRepository
    orders: OrderRepository
    reprice_events: RepriceEventRepository
    contracts: ContractRepository
    heartbeats: HeartbeatRepository
    alerts: AlertRepository
    tracker: OrderTracker               # Ephemeral fill coordination
    settings: dict
    account_id: str
    transactions: TransactionRepository
    pending_commands: PendingCommandRepository
    bots: BotRepository
    bot_events: BotEventRepository
    templates: OrderTemplateRepository
    router: OutputRouter
    _brokers: dict                      # Multi-broker support

    def get_broker(self, name: str) -> BrokerClientBase
```

---

## Frontend

React 19 + TypeScript + Vite + Tailwind CSS v4 + flexlayout-react + Zustand.

### Data Flow

- **Mock mode** (`VITE_DATA_MODE` unset): Local simulation with mock data
- **Live mode** (`VITE_DATA_MODE=live`): Connected to API server
  - Commands → `POST /api/commands` → poll for completion
  - Positions → poll `/api/positions` every 30s + on command completion
  - Orders/Trades → poll on command completion
  - Alerts/Heartbeats → WebSocket snapshot + diffs
  - Logs → poll `/api/logs` every 5s

### Panels

| Panel | Data Source | Refresh Trigger |
|-------|------------|-----------------|
| Console | Local state + API polling | Command submission |
| Positions | `/api/positions` | 30s interval + command completion |
| Orders | `/api/orders` | Command completion |
| Trades | `/api/trades` | Command completion |
| Alerts | Store + WebSocket | Real-time |
| Logs | `/api/logs` | 5s polling |
| Quick Orders | `/api/templates` | CRUD operations |
| Help | Static | — |

---

## Key Design Decisions

| # | Decision | Rationale |
|---|----------|-----------|
| 1 | Central engine with SQLite command queue | Eliminates multi-client IB conflicts |
| 2 | Zero memory state | Crash at any point, restart from SQLite |
| 3 | SQLite as sole IPC | No sockets, pipes, or shared memory |
| 4 | Decimal not float | Monetary precision |
| 5 | Broker abstraction | Extensible to Alpaca and future brokers |
| 6 | Cancel-vs-fill settle window | 3s wait after cancel to catch late fills |
| 7 | API returns 202 for commands | Non-blocking, result via polling/WebSocket |
| 8 | Bots have no broker connection | Submit via pending_commands like any client |
| 9 | Position cache table | API serves IB positions without broker connection |
| 10 | Server-local timestamps | Single-user deployment, avoids UTC confusion |
| 11 | FillStream abstraction | Push (IB callbacks) and poll (Alpaca WebSocket) unified |
| 12 | PreSubmitted settle wait | 3s grace before declaring order NOT ACTIVE |
