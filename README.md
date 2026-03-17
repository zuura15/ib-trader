# IB Trader

Trading platform for Interactive Brokers with a web GUI, central engine, and bot framework. Designed for extensibility — Alpaca broker support is built in (not yet connected).

## Architecture

Five processes, communicating only through SQLite:

```
┌─────────┐   ┌───────────┐   ┌────────────┐
│  REPL   │   │ API Server│   │ Bot Runner │
│ (CLI)   │   │ (FastAPI) │   │ (process)  │
└────┬────┘   └─────┬─────┘   └─────┬──────┘
     │              │               │
     └──────┬───────┴───────┬───────┘
            │   SQLite      │
            │  pending_cmds │
            ▼               │
     ┌──────────────┐       │
     │Engine Service │◄──────┘
     │(sole broker  │
     │ connection)  │
     └──────┬───────┘
            │
     ┌──────┴──────┐
     │             │
  ┌──▼──┐    ┌────▼───┐
  │ IB  │    │Alpaca  │
  └─────┘    └────────┘
```

- **`ib-engine`** — Central command loop. Sole process with broker connections. Polls `pending_commands` from SQLite, executes orders via IB.
- **`ib-api`** — FastAPI server. REST + WebSocket for the GUI. No broker connection — reads SQLite, submits commands to `pending_commands`.
- **`ib-daemon`** — Background monitoring. Reconciliation, heartbeat checks, integrity verification. Own IB connection (read-only).
- **`ib-bots`** — Bot runner. Manages trading bot lifecycle. Submits commands via `pending_commands`.
- **`ib-trader`** — Interactive REPL (legacy CLI). Can also submit commands via `pending_commands`.

## Requirements

- Python 3.11+
- Node.js 18+ (for the GUI frontend)
- IB Gateway or TWS running locally
- `uv` package manager

## Setup

### 1. Install

```bash
git clone git@github.com:zuura15/ib-trader.git
cd ib-trader
uv venv && uv pip install -e .
uv pip install websockets httpx
```

### 2. Configure

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your IB connection details:

```
IB_HOST=127.0.0.1
IB_PORT=4001              # 4001 = GW live, 4002 = GW paper
IB_CLIENT_ID=1
IB_ACCOUNT_ID=U1234567
IB_PORT_PAPER=4002
IB_ACCOUNT_ID_PAPER=DU1234567
```

### 3. Configure symbols

Edit `config/symbols.yaml`:

```yaml
- AAPL
- MSFT
- QQQ
- SPY
```

### 4. Apply database migrations

```bash
uv run alembic -c migrations/alembic.ini upgrade head
```

### 5. Install frontend

```bash
cd frontend && npm install && cd ..
```

### 6. Start IB Gateway

Enable API connections: Configuration → Settings → API → Enable ActiveX and Socket Clients.

### 7. Start the platform

Four terminals (or use Tilix for tiled panes):

```bash
# Terminal 1 — Engine (start first)
.venv/bin/ib-engine

# Terminal 2 — API Server
.venv/bin/ib-api

# Terminal 3 — Frontend
cd frontend && VITE_DATA_MODE=live npm run dev

# Terminal 4 — Daemon (optional, for reconciliation)
.venv/bin/ib-daemon
```

Open `http://localhost:5173` in your browser.

## GUI

The web GUI provides a professional trading workstation with dockable, resizable panels:

- **Console** — Command entry with live status, output history, copy button
- **Positions** — Live IB positions (refreshed every 30s) with STK/OPT/Other filters
- **Orders** — Open orders with status badges, auto-refresh on command completion
- **Trades** — Trade history with P&L, filterable by all/open/closed
- **Alerts** — Active/Dismissed tabs, severity-aware badges, dismiss with API resolution
- **Logs** — Live log stream from engine (newest first, polling every 5s)
- **Quick Orders** — Saved order templates with one-click fire
- **Help** — Copyable command reference

Dark/light theme toggle persists across sessions.

## Trading Commands

Enter in the GUI console or the REPL:

```
buy AAPL 10 mid                    # Buy 10 shares at mid price with reprice loop
buy AAPL 10 mid --profit 500       # Buy with $500 profit taker
buy AAPL 10 market                 # Market order
buy AAPL 10 limit 180.50           # Limit order at $180.50
buy AAPL 10 bid                    # Passive buy at bid
sell TSLA 5 ask                    # Aggressive sell at ask
close 42 mid                       # Close trade #42 at mid
close 42 market                    # Close at market
status                             # System status and P&L summary
orders                             # List open orders
help                               # Command reference
```

Strategies: `mid` (reprice loop), `market`, `bid`, `ask`, `limit PRICE`

Options: `--profit N`, `--stop-loss N`, `--dollars N`, `--broker alpaca`

## Testing

```bash
.venv/bin/python -m pytest tests/unit/                    # Unit tests (no IB needed)
.venv/bin/python -m pytest tests/integration/             # Integration tests (no IB needed)
.venv/bin/python -m pytest -m smoke tests/smoke/          # Smoke tests (requires live IB)
```

415 tests total (362 unit + 53 integration).

## Configuration

`config/settings.yaml` — all tunables:

| Setting | Default | Description |
|---------|---------|-------------|
| `max_order_size_shares` | 10 | Safety limit per order |
| `reprice_duration_seconds` | 10 | Reprice window before cancel |
| `reprice_interval_seconds` | 1 | Seconds between reprice steps |
| `ib_min_call_interval_ms` | 100 | Rate limiter (ms between IB calls) |
| `ib_market_data_type` | 1 | 1=live, 3=delayed |
| `heartbeat_stale_threshold_seconds` | 300 | Stale heartbeat alert threshold |
| `reconciliation_interval_seconds` | 1800 | Daemon reconciliation interval |

## Logs

Structured JSON at `logs/ib_trader.log`. Server-local timestamps. Rotates at 10MB, keeps 10 backups with gzip compression.

```json
{"timestamp": "2026-03-17T10:32:01.234-07:00", "level": "INFO", "event": "ORDER_FILLED",
 "serial": 42, "symbol": "AAPL", "qty_filled": "10", "avg_price": "182.15"}
```

## Deploy

```bash
# systemd (production)
sudo bash deploy/setup.sh
sudo systemctl start ib-engine ib-api ib-daemon

# Background processes with log tailing
./deploy/start.sh              # live
./deploy/start.sh --paper      # paper trading
./deploy/stop.sh               # stop all
```

## Key Design Principles

- **Zero memory state** — crash at any point, restart from SQLite
- **Central engine** — only one process holds broker connections
- **SQLite as IPC** — all processes communicate through the database only
- **Decimal not float** — all monetary values
- **Broker-agnostic** — engine works through `BrokerClientBase` abstraction
- **Cancel-vs-fill safety** — 3-second settle window after cancel to catch late fills
