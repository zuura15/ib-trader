# State Management Redesign — Final Plan

> This is the single plan document for the migration from SQLite-based polling
> to Redis Streams. It supersedes `state-management-design.md` and
> `state-management-detailed-plan.md` (both preserved for historical context
> but no longer authoritative).
>
> Guiding tenets are in `state-management-high-level-approach.md`.

## Why

State is scattered across 4 locations (SQLite tables, JSON files, in-memory
dicts, IB Gateway) that constantly diverge, causing phantom positions. All
inter-component communication goes through SQLite polling (100ms–30s
intervals), giving 7–35s latency on price data that bots need in sub-second
to avoid stop-loss blowouts. The fill detection path parses command output
text via regex — when it silently fails, positions are stuck as phantoms
forever.

## Target

Redis replaces SQLite as the runtime backbone. Redis Streams carry all
real-time data. Redis keys hold all live state. SQLite shrinks to analytics
and audit only. IB `orderRef` tagging makes every order self-identifying
across restarts and offline actions.

## Architecture

```
IB Gateway (source of truth)
     │
     │  ib_async push (ticks, fills, status)
     ▼
Engine Process
  ├─ XADD quotes:{symbol}              → Redis Stream
  ├─ XADD bars:{symbol}:{interval}     → Redis Stream
  ├─ XADD orders:fills:{bot_id}        → Redis Stream
  ├─ XADD orders:status:{bot_id}       → Redis Stream
  ├─ XADD alerts:{severity}            → Redis Stream
  ├─ SET  state:position:{bot_id}:*    → Redis Key (reconciler)
  ├─ POST /internal/orders             ← Bot runner calls this
  └─ Reconciler (every 2-3s)
       queries IB open orders/positions
       matches by orderRef → updates Redis state keys
            │
            ▼
Redis (runtime backbone)
  ├─ Streams: quotes, bars, fills, status, bot events, alerts
  ├─ Keys: bot config, bot status, strategy state, position state
  └─ Persistence: RDB snapshots (strategy state survives restart)
            │
     ┌──────┼──────┐
     ▼      ▼      ▼
Bot Runner  API    Future processes
  │         │
  │    WebSocket ──→ Browser
  │
  ├─ XREAD quotes:{symbol}        (real-time exit monitoring)
  ├─ XREAD orders:fills:{bot_id}  (fill detection, replaces text parsing)
  ├─ XADD  bot:events:{bot_id}    (activity log)
  ├─ SET   bot:{id}:strategy:*    (trailing stop, HWM)
  └─ POST  engine/internal/orders (place orders)
```

## Redis Stream Layout

| Stream | Publisher | Consumers | MAXLEN | Content |
|--------|-----------|-----------|--------|---------|
| `quotes:{symbol}` | Engine | Bots, API→WS | ~10000 | bid, ask, last, volume, ts |
| `bars:{symbol}:{interval}` | Engine | Bots | ~50000 | OHLCV bar |
| `orders:fills:{bot_id}` | Engine | Bots, API→WS | ~1000 | orderRef, symbol, side, qty, price, commission |
| `orders:status:{bot_id}` | Engine | Bots, API→WS | ~1000 | orderRef, symbol, status, filled, remaining |
| `bot:events:{bot_id}` | Bot runner | API→WS | ~5000 | event_type, message, payload |
| `alerts:{severity}` | Engine/Reconciler | API→WS | ~1000 | alert message, context |

## Redis Key Layout

| Key Pattern | Owner | Content |
|-------------|-------|---------|
| `bot:{bot_id}:config` | Engine (from YAML) | Static bot configuration |
| `bot:{bot_id}:status` | Engine/API | STOPPED / RUNNING / ERROR / PAUSED |
| `bot:{bot_id}:strategy:{symbol}` | Bot runner | Strategy state (trailing stop, HWM, etc.) |
| `state:position:{bot_id}:{symbol}` | Reconciler | FLAT / ENTERING / OPEN / EXITING + metadata |
| `heartbeat:{process}` | Each process | PID, last seen timestamp |

## Reconciler (engine-side, every 2-3s)

| Local State | IB Has Order | IB Has Position | Action |
|-------------|-------------|----------------|--------|
| ENTERING | Yes (BUY) | No | Wait |
| ENTERING | No | Yes | → OPEN |
| ENTERING | No | No | → FLAT |
| OPEN | — | Yes | OK |
| OPEN | — | No | → FLAT (closed in TWS) |
| EXITING | Yes (SELL) | Yes | Wait |
| EXITING | No | No | → FLAT |
| EXITING | No | Yes | → OPEN (exit cancelled) |
| FLAT | Yes (our ref) | — | WARNING (orphan) |

Every transition: update Redis state key + XADD to status stream + write
terminal events to SQLite for audit.

## orderRef Tagging

Format: human-readable, machine-parseable, fits 128 chars. Schema TBD but
encodes: bot identity, symbol, intent (entry/exit), trade serial. Example
sketch: `IBT|saw-rsi|QQQ|entry|42|0411`

- Set on `Order.orderRef` before `placeOrder()`
- Preserved through fills, status updates, open order queries
- Reconciler parses it to identify bot-owned orders
- On restart: query IB open orders → parse orderRef → rebuild position state

## Implementation Phases

### Phase 0: Redis Foundation (no behavioral changes)

Add Redis as a dependency and create the abstraction layer.

**Create:**
- `ib_trader/redis/__init__.py`
- `ib_trader/redis/client.py` — connection factory, pool, health check. Reads `redis_url` from settings.yaml.
- `ib_trader/redis/streams.py` — `StreamPublisher` (XADD + MAXLEN + JSON serialization of Decimal/datetime), `StreamConsumer` (XREAD/XREADGROUP + consumer groups), `StreamNames` (channel name constants).
- `ib_trader/redis/state.py` — `RedisStateStore` (GET/SET/HSET with JSON serialization, TTL), `StateKeys` (key name constants).
- `docs/decisions/015-redis-streams-for-real-time-data.md`

**Modify:**
- `pyproject.toml` — add `redis[hiredis]>=5.0`, `fakeredis[lua]` (test dep)
- `config/settings.yaml` — add `redis_url`, `redis_stream_maxlen`
- AppContext — add optional `redis` field

**Test:** Unit tests with fakeredis. All existing tests pass with `redis=None`.

### Phase 1: Engine Publishes to Redis (dual-write)

Engine writes to both SQLite and Redis. No consumers changed yet. Safety net — if Redis breaks, SQLite path still works.

**Modify:**
- `ib_trader/engine/main.py` — create Redis connection on startup, attach to ctx. After writing `run/positions.json` and `run/watchlist.json`, also XADD to streams.
- `ib_trader/engine/service.py` — after SQLite upsert to `market_quotes`, also XADD to `quotes:{symbol}`. After `market_bars` insert, also XADD to `bars:{symbol}:5s`.
- `ib_trader/ib/insync_client.py` — expose fill/status events via asyncio.Queue (keeps Redis out of the IB layer).
- `ib_trader/engine/main.py` — new `_ib_event_relay_loop` background task: drains InsyncClient event queue, publishes fills to `orders:fills:all` stream (bot-specific routing after Phase 5 adds orderRef).

**Test:** Mock IB fill → verify event on Redis stream. Quote poll → verify both SQLite and Redis contain same data.

### Phase 2: Bot Runner Consumes from Redis

Bot reads quotes/bars from Redis Streams instead of SQLite. Fill detection moves from text parsing to stream consumption. This is the critical latency win.

**Modify:**
- `ib_trader/bots/runtime.py`:
  - `_read_new_bars()` → XREAD from `bars:{symbol}:5s` (track stream ID, not timestamp)
  - `_get_latest_quote()` → XREVRANGE from `quotes:{symbol}` (last entry)
  - **Delete** `_check_pending_fills()` and `_parse_fill_from_output()` — replaced by fill stream consumer task
  - New `_start_stream_consumers()` with asyncio tasks for fill, status, and quote streams
  - Quote consumer uses `XREAD BLOCK 1000` for sub-second latency on stop-loss monitoring
- `ib_trader/bots/middleware.py`:
  - `PersistenceMiddleware` → Redis SET instead of JSON file (keep JSON as backup during migration)
  - `LoggingMiddleware` → XADD to `bot:events:{bot_id}` stream (keep SQLite write for audit)
- `ib_trader/bots/bar_aggregator.py` — state persistence to Redis alongside file
- `ib_trader/bots/runtime.py` — `_load_persisted_state()` tries Redis first, falls back to JSON

**Eliminated:** `_parse_fill_from_output()`, `_check_pending_fills()`, JSON file primary writes, 7-35s quote/bar latency.

### Phase 3: WebSocket API Consumes from Redis

Replace 1.5s SQLite polling in ws.py with Redis Stream subscriptions.

**Modify:**
- `ib_trader/api/ws.py` — replace `_fetch_channel_data()` SQLite polling with XREAD tasks per channel. Stream-backed channels push events directly instead of hash-and-diff. SQLite-backed channels (trades — terminal data) keep existing diff logic.
- `ib_trader/api/main.py` — create Redis connection on startup.
- Frontend WebSocket handler — handle new `event` message type alongside existing `diff` type.

**Test:** Engine publishes quote → WebSocket client receives within 100ms.

### Phase 4: Bot→Engine Commands via HTTP

Replace `pending_commands` SQLite table as the bot→engine command queue.

**Create:**
- `ib_trader/engine/internal_api.py` — FastAPI router: `POST /internal/orders`, `POST /internal/orders/{serial}/cancel`, `GET /internal/health`. Auth via shared secret from .env.

**Modify:**
- `ib_trader/engine/main.py` — start internal uvicorn on separate port (e.g., 8081)
- `ib_trader/bots/middleware.py` — `ExecutionMiddleware` POSTs to engine HTTP API instead of inserting `PendingCommand` row. Gets trade serial back synchronously.
- `ib_trader/engine/service.py` — still polls `pending_commands` for REPL/API commands. Bot commands bypass it.

**Eliminated:** Bot's dependency on PendingCommandRepository for orders, `wait_for_command()` polling.

### Phase 5: Reconciler + orderRef Tagging

Engine-side reconciler replaces bot-side `_reconcile_state()`. orderRef makes every order self-identifying.

**Create:**
- `ib_trader/engine/reconciler.py` — background task (every 2-3s): queries IB open orders/positions, parses orderRef, updates Redis position state keys, publishes transitions to status streams, writes terminal events to SQLite for audit.
- `ib_trader/engine/order_ref.py` — `encode_order_ref()` and `parse_order_ref()` with round-trip guarantees.

**Modify:**
- `ib_trader/engine/order.py` — set orderRef on every order
- `ib_trader/engine/main.py` — start reconciler background task
- `ib_trader/engine/main.py` — `_ib_event_relay_loop` now routes fills to `orders:fills:{bot_id}` using parsed orderRef (upgrade from Phase 1's `orders:fills:all`)
- `ib_trader/bots/runtime.py` — `_reconcile_state()` reads from Redis `state:position:{bot_id}:{symbol}` instead of trade_groups

### Phase 6: Cleanup

Remove all dual-write paths, SQLite polling, JSON files, and dead code. Only execute after all prior phases are verified in production for at least one trading week.

**Remove from engine:**
- SQLite writes to `market_quotes` and `market_bars`
- `run/positions.json` and `run/watchlist.json` file writes
- `_position_cache_loop` and `_watchlist_cache_loop` JSON paths

**Remove from bot runner:**
- SQLite fallbacks in `_read_new_bars()` and `_get_latest_quote()`
- JSON file reads/writes in PersistenceMiddleware and bar_aggregator
- `STATE_DIR` constant and all `~/.ib-trader/bot-state/` references

**Remove from API:**
- SQLite polling code in ws.py for stream-backed channels

**Deprecate models:**
- `MarketBar`, `MarketQuote` SQLAlchemy models (or remove if no remaining references)

**Add feature flag removal:** delete `use_redis_streams` flag from settings.yaml.

## Phase Dependencies

```
Phase 0 (foundation) ──┐
                        ├──→ Phase 1 (engine publishes) ──┬──→ Phase 2 (bot consumes)
                        │                                  ├──→ Phase 3 (WS consumes)
                        ├──→ Phase 4 (HTTP commands)       │
                        └──→ Phase 5 (reconciler + orderRef)
                                                           │
                                            all verified ──┴──→ Phase 6 (cleanup)
```

- Phase 0 first (everything depends on it)
- Phase 1 required before Phases 2 and 3
- Phases 2, 3, 4, 5 can be worked in parallel after their prerequisites
- Phase 6 only after all of 1–5 verified in production

## What Gets Eliminated

| Current | Replaced By |
|---------|-------------|
| `~/.ib-trader/bot-state/*.json` | Redis strategy state keys |
| `run/positions.json` | Redis stream → WS push |
| `run/watchlist.json` | Redis stream → WS push |
| `market_bars` SQLite table | Redis bars stream |
| `market_quotes` SQLite table | Redis quotes stream |
| SQLite polling in ws.py (1.5s) | Redis stream subscriptions |
| `_check_pending_fills()` text parsing | Redis fill stream + reconciler |
| `_parse_fill_from_output()` regex | Eliminated |
| `_reconcile_state()` in runtime.py | Engine-side reconciler |
| `_load_persisted_state()` from JSON | Redis GET |
| PersistenceMiddleware JSON writes | Redis SET |
| `pending_commands` as bot→engine queue | HTTP POST to engine API |

## Latency Improvement

| Data Path | Before | After |
|-----------|--------|-------|
| IB quote → bot exit check | ~7s (2s write + 5s poll) | <1s (stream XREAD BLOCK) |
| IB bar → bot strategy eval | ~35s (30s poll + 5s poll) | <1s (stream push) |
| IB fill → bot detection | ~5s + fragile text parse | <1s (stream push, no parsing) |
| Any state → browser UI | 1.5–15s (SQLite poll + WS diff) | ~50ms (stream → WS push) |

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| Redis goes down | Phases 1–5 dual-write to SQLite as fallback. Feature flag to revert. After Phase 6, Redis is a hard dependency — use RDB persistence + systemd restart. |
| Lost messages on consumer restart | Streams persist entries. Consumer reads from last-known ID (or `0` for full catchup). MAXLEN bounds replay cost. |
| Engine crash mid-trade | orderRef tagging makes recovery safe. On restart, reconciler queries IB for all open orders with our prefix, rebuilds Redis state. |
| Cross-stream ordering | Each consumer handles events idempotently. Reconciler is the authority for position state (safety net). |
| Memory pressure | MAXLEN ~10000 per stream, ~20 streams ≈ 50-100MB. Monitor with `XLEN`. |
| Breaking WS wire protocol | Keep backward-compatible diff format for SQLite-backed channels. Add `event` type for stream-backed channels. Coordinate frontend. |

## Rollback

Feature flag in settings.yaml: `use_redis_streams: true/false`. Each phase
checks this and falls back to SQLite if false. Phase 6 removes the flag.
Before Phase 6, run on Redis-only for at least one full trading week in
paper mode.

## Verification (end-to-end)

1. Start engine + bot + API. Bot subscribes to QQQ.
2. IB tick arrives → verify quote appears on `quotes:QQQ` stream within 100ms.
3. Bot evaluates strategy → places order via HTTP POST to engine.
4. Engine places order with orderRef → verify tag on IB open order.
5. IB fills order → fill event on `orders:fills:{bot_id}` stream → bot detects fill within 1s.
6. Reconciler confirms OPEN state in Redis → position visible in UI via WS.
7. Manually close position in TWS → reconciler sets FLAT within 3s → bot and UI updated.
8. Kill engine mid-trade, restart → reconciler rebuilds from IB orderRef → no phantom.
9. Kill bot runner, restart → reads strategy state from Redis → resumes trailing stop from where it left off.
10. No JSON files created. No SQLite reads on any hot path.
