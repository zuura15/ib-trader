# IB Trader

Python trading engine for Interactive Brokers with persistent REPL and monitoring daemon.

## Quick Start

1. Install dependencies: `make install`
2. Copy `.env.example` to `.env` and fill in your IB connection details
3. Set permissions: `chmod 600 .env`
4. Start TWS or IB Gateway
5. Start the REPL: `ib-trader`
6. (Optional) Start daemon in another terminal: `ib-daemon`

## Architecture

See [Architecture Decision Records](decisions/) for the key design decisions.

## Commands

See the REPL `help` command for full command reference.
