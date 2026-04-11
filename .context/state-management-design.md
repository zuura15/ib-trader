# State Management Redesign — Context for New Session

## The Problem

The bot shows phantom positions — positions in the UI that don't exist in IB. This has caused real unintended trades across multiple sessions. Root cause: state is scattered across 3 places that diverge from IB:

1. **SQLite `trade_groups` table** — legacy trade records, often stale
2. **JSON files** at `~/.ib-trader/bot-state/{bot_id}-{symbol}.json` — trailing stop, entry price
3. **In-memory dicts** in runtime.py — `_pending_cmd_id`, `position_state`

The bot, engine, and IB have no reliable synchronization. Fill detection parses command output text (fragile). Manual closes in TWS aren't detected. Engine restarts leave commands in RUNNING state that never complete.

## Current Bugs (confirmed)

1. **Fill parsing failure silently abandons detection** (`runtime.py:_check_pending_fills`): If `_parse_fill_from_output` returns None on a SUCCESS command, `_pending_cmd_id` is cleared but state stays ENTERING. Phantom forever.

2. **Exit failure recovery uses stale commands** (`runtime.py:on_tick` EXITING block): Scans `get_by_source(limit=5)` without filtering by command ID, can match old failed commands.

3. **Startup reconciliation checks SQLite, not IB** (`runtime.py:_reconcile_state`): Trusts `TradeRepository.get_open()` which shows stale OPEN trades after manual TWS closes.

4. **Warmup fires signals on historical bars**: Fixed (only last bar in batch evaluated) but cascade effects still visible in running bots.

5. **Engine restart leaves RUNNING commands orphaned**: `recover_stale_commands` marks them FAILURE, which the bot then treats as rejection of its current order.

## Key Insight: IB `orderRef`

IB's Order object has an **`orderRef` field** (string, up to 128 chars) that:
- Is set when placing an order (`order.orderRef = "bot:meta-trend:sawtooth"`)
- **Preserved through the entire lifecycle** — fills, status updates, `reqAllOpenOrders`
- Returned via `trade.order.orderRef` and `fill.execution.orderRef`

Currently `insync_client.py:place_limit_order` and `place_market_order` don't use it. Adding it is trivial.

This means we can **reliably identify which bot owns which IB order/position** without any SQLite matching.

## User's Design Direction

Quote from conversation: "use memory but the contents are actually stored on the disk without the process needing to know about it... single memory location with all state all our components use. there should be a state reconciler that runs every few seconds (even less than 5s), and the reconciler will rely on this custom tag to understand when something was closed offline."

## Proposed Architecture

### Single State Store: `bot_positions` table

One SQLite table, treated as a **cache not an archive**. Can be reconstructed from IB at any time. Replaces both JSON files and scattered in-memory state.

```
bot_id            PK, FK bots.id
symbol            PK, String(20)
position_state    String(20) — FLAT, ENTERING, OPEN, EXITING
ib_qty            Numeric(18,4) — shares held per IB (reconciler writes)
ib_avg_price      Numeric(18,8) — avg cost per IB (reconciler writes)
ib_order_ref      String(128) — the orderRef tag on this bot's orders
trade_serial      Integer
entry_price       Numeric(18,8)
entry_time        DateTime
entry_qty         Numeric(18,4)
pending_order_ref String(128) — orderRef of working order
pending_cmd_id    String(36) — pending_commands.id of active command
strategy_state_json Text — bot-writable (trailing stop, HWM, current_stop)
last_reconciled_at DateTime
updated_at        DateTime
```

**Write zones:**
- **Engine only**: `position_state`, `ib_qty`, `ib_avg_price`, `entry_price`, `entry_time`, `trade_serial`, `pending_order_ref`, `pending_cmd_id`, `last_reconciled_at`
- **Bot only**: `strategy_state_json` (trailing stop mechanics, updated every 1-2 seconds)

### State Flow

```
IB Gateway (source of truth)
     │
     │  reconciler queries every 2-3s via orderRef
     ▼
┌─────────────────────────────────┐
│   bot_positions table (SQLite)  │
│   — Engine writes lifecycle     │
│   — Bot writes strategy state   │
└──────┬────────────┬─────────────┘
       │            │
   Bot reads    API/UI reads
```

### The Reconciler (new file: `ib_trader/engine/bot_reconciler.py`)

Background task in engine, runs every 2-3 seconds. Three phases:

1. **Query IB**: `ctx.ib._ib.positions()` + `get_open_orders()` (with orderRef in response)
2. **Build maps**: `{(bot_id, symbol): ib_state}` parsed from orderRef
3. **Reconcile each bot_positions row**:

| Local | IB Order | IB Position | Action |
|-------|---------|------------|--------|
| ENTERING | Yes BUY | No | Wait |
| ENTERING | No | Yes | → OPEN (fill completed) |
| ENTERING | No | No | → FLAT (cancelled) |
| OPEN | — | Yes | OK |
| OPEN | — | No | → FLAT (closed in TWS) |
| EXITING | Yes SELL | Yes | Wait |
| EXITING | No | No | → FLAT (exit filled) |
| EXITING | No | Yes | → OPEN (exit cancelled) |
| FLAT | Yes (our ref) | — | WARNING (orphan) |

### Fast Path (Engine's Immediate Updates)

Reconciler is the safety net. Normal flow:
- Engine places order → immediately writes ENTERING + pending_cmd_id to bot_positions
- Fill callback fires → immediately writes OPEN + entry_price
- Close fill callback → immediately writes FLAT

Sub-second updates in happy path. Reconciler catches discrepancies.

### orderRef Flow

1. **Bot** submits command: `buy QQQ 16 mid --ref bot:7d5a8b20:QQQ`
2. **Engine** parses `--ref`, passes to IB client
3. **insync_client**: `order.orderRef = order_ref` before `placeOrder()`
4. **IB** preserves it through fills
5. **Reconciler** reads back from IB via `trade.order.orderRef`

## Changes by File

### New files
- `ib_trader/engine/bot_reconciler.py` — reconciler loop
- `ib_trader/data/repositories/bot_position_repository.py` — CRUD

### Modified
- `ib_trader/data/models.py` — add BotPosition model
- `migrations/versions/xxx_bot_positions.py` — Alembic migration
- `ib_trader/ib/base.py` — add `order_ref` param to place methods
- `ib_trader/ib/insync_client.py` — set `order.orderRef`, return it in `get_open_orders()`
- `ib_trader/engine/main.py` — wire reconciler background task
- `ib_trader/engine/order.py` — pass `--ref` through to IB client
- `ib_trader/repl/commands.py` — parse `--ref` flag
- `ib_trader/bots/runtime.py` — read from bot_positions (remove _reconcile_state, _check_pending_fills, _load_persisted_state, _parse_fill_from_output)
- `ib_trader/bots/middleware.py` — PersistenceMiddleware writes strategy_state_json, ExecutionMiddleware appends --ref
- `ib_trader/api/routes/bots.py` — state endpoint reads bot_positions
- `tests/conftest.py` — MockIBClient gets order_ref param

### Eliminated
- `~/.ib-trader/bot-state/*.json` files
- `_load_persisted_state()`, `_reconcile_state()`, `_check_pending_fills()`, `_parse_fill_from_output()` in runtime.py
- `STATE_DIR` constant

## Build Order

1. **Models + repository** — BotPosition table, migration, repo
2. **orderRef plumbing** — base.py, insync_client.py, commands.py, order.py, MockIBClient
3. **Reconciler** — bot_reconciler.py, wire into engine/main.py
4. **Engine fast path** — immediate writes to bot_positions on place/fill
5. **Bot runner migration** — runtime.py reads bot_positions, middleware adjustments
6. **API update** — state endpoint
7. **Cleanup** — remove JSON file code, old fill detection, dead methods

## Verification

1. Start bot, force-buy → `bot_positions` row shows ENTERING then OPEN within 1s of fill
2. Let trailing stop fire → row goes EXITING → FLAT
3. Force-buy, manually close in TWS → reconciler sets FLAT within 3 seconds
4. Kill engine mid-trade, restart → reconciler reconstructs state from orderRef
5. No JSON files created anywhere
6. UI shows position state driven entirely by `bot_positions` table

## Historical Context (bugs fixed along the way)

- Timezone-naive datetime crashes (fromisoformat) — fixed
- Risk middleware blocking SELL orders — fixed (only BUY goes through risk)
- SELL orders using wrong command format — fixed (uses `close SERIAL` now)
- Stale quote thresholds too tight — fixed (45s/120s)
- Entry timeout only checked on 3-min bars — fixed (every tick)
- Warmup fires signals on historical bars — fixed (only last bar in batch evaluated)
- 15-second signal cooldown after startup — added
- Decimal format spec crash in logs — fixed with float() wrapper
- Command ID tracking (partial) — added `_pending_cmd_id` but still fragile

## Notes for New Session

- The fill parsing approach is fundamentally fragile. The redesign around orderRef is the correct path.
- The user wants ALL state in one place, read through a single interface.
- The bot runner has no IB connection — only the engine does. State reconciliation must run in the engine.
- SQLite stays for audit logs (bot_events, pending_commands queue) but NEVER for position state decisions.
- JSON state files must be deleted, not kept as backup.
- Current plan file is at `/home/zuura/.claude/plans/fluttering-soaring-wadler.md` with the detailed plan.
