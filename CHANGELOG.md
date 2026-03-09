# Changelog

All notable changes to IB Trader are recorded here.
Format: date, type (Added / Changed / Fixed / Deprecated), description.

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
