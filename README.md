# IB Trader

Python trading engine for Interactive Brokers. Two persistent processes:

- **`ib-trader`** — Interactive REPL. Start once, trade from the prompt.
- **`ib-daemon`** — Background TUI. Monitoring, reconciliation, system health.

## Requirements

- Python 3.11+
- TWS (Trader Workstation) or IB Gateway running locally
- `uv` package manager (installed automatically via `make install`)

## Setup

### 1. Install dependencies

```bash
make install
```

### 2. Configure

```bash
cp .env.example .env
chmod 600 .env
```

Edit `.env` with your IB connection details:

```
IB_HOST=127.0.0.1
IB_PORT=7497          # 7497 = TWS live, 7496 = TWS paper, 4001 = GW live, 4002 = GW paper
IB_CLIENT_ID=1        # Unique per connected client
IB_ACCOUNT_ID=U1234567
```

> **Note:** The daemon uses `IB_CLIENT_ID + 1` automatically. If REPL uses client ID 1, daemon uses 2. Both must be unique in TWS.

### 3. Add symbols to trade

Edit `config/symbols.yaml` — no restart required to add symbols:

```yaml
- MSFT
- AAPL
- NVDA
```

### 4. Start TWS or IB Gateway

Enable API connections in TWS: File → Global Configuration → API → Settings → Enable ActiveX and Socket Clients.

### 5. Start the REPL

```bash
ib-trader
```

Or with custom paths:

```bash
ib-trader --db trader.db --env .env --settings config/settings.yaml
```

### 6. (Optional) Start the daemon in a second terminal

```bash
ib-daemon
```

The daemon is not required for trading. If absent, the REPL shows a warning but operates normally. The daemon adds background reconciliation, monitoring, and the TUI dashboard.

## Trading Commands

```
> buy MSFT 5 mid              # Buy 5 shares at mid price, reprice toward ask over 10s
> buy MSFT 5 mid 500          # Buy at mid, place profit taker at +$500 total profit
> buy MSFT 5 market           # Buy at market
> buy MSFT 5 mid --dollars 1000   # Buy with $1000 notional (5 shares at ~$200 each)
> sell MSFT 5 mid             # Short sell at mid
> close 4                     # Close position from serial #4 at mid
> close 4 market              # Close at market
> orders                      # List open orders
> status                      # Gateway and system status
> exit                        # Clean exit
> help                        # Full command reference
```

## Testing

```bash
make test         # Unit + integration tests (no IB Gateway needed)
make smoke        # Smoke tests (requires live IB Gateway — cleans up after itself)
make lint         # Ruff linting
make typecheck    # mypy type checking
```

### Running smoke tests safely

```bash
pytest -m smoke --tb=short
```

Smoke tests:
- Skip automatically if IB Gateway is unreachable
- Place only 1-share limit orders far outside the market (will not fill)
- Cancel all test orders immediately after verification
- Leave no open orders in IB

## Architecture

See `docs/decisions/` for Architecture Decision Records explaining all key design choices.

Key principles:
- **Zero in-memory state** — all state lives in SQLite
- **Amendment not cancel-replace** — one IB order ID per entry leg
- **Process isolation** — REPL and daemon communicate via SQLite only
- **Mutual watchdog** — via `system_heartbeats` table, no sockets or signals
- **Crash recovery** — on restart, REPRICING/AMENDING orders are marked ABANDONED
- **Decimal not float** — all monetary values use `Decimal`

## Configuration

`config/settings.yaml` contains all tunables. Key settings:

| Setting | Default | Description |
|---|---|---|
| `max_order_size_shares` | 10 | Safety limit — orders exceeding this are rejected |
| `reprice_duration_seconds` | 10 | How long to reprice before canceling |
| `reprice_interval_seconds` | 1 | Interval between reprice steps |
| `heartbeat_stale_threshold_seconds` | 300 | Seconds before REPL heartbeat is considered stale |
| `reconciliation_interval_seconds` | 1800 | How often daemon reconciles with IB |

## Logs

Structured JSON logs at `logs/ib_trader.log`. Each event is a single JSON object:

```json
{"timestamp": "2026-03-08T10:32:01.234Z", "level": "INFO", "event": "ORDER_FILLED",
 "serial": 4, "symbol": "MSFT", "qty_filled": "5", "avg_price": "412.33"}
```

Logs rotate at 10MB, keep 10 backups, and are gzip-compressed.
