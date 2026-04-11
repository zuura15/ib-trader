# Centralized State Management — Eliminate Phantom Positions

> **Historical note (2026-04-10).** This is the detailed plan from an earlier
> Claude Code plan-mode session (originally at
> `~/.claude/plans/fluttering-soaring-wadler.md`). It is preserved here as
> context for the ongoing state-management redesign.
>
> **It predates the tenet 3 reversal** captured in
> `state-management-high-level-approach.md` and in the updated `CLAUDE.md`
> "Data & State" section. Wherever this plan describes `bot_positions` as a
> **SQLite table** or treats SQLite as a "cache not an archive", read that as
> **in-memory state** instead — the structure and reconciliation logic are
> still broadly correct, but the storage layer has moved from SQLite to
> process memory (with a lightweight persistent-memory backend such as Redis
> as the long-term direction). SQLite is now archival only.
>
> Use this file for the shape of the reconciler, the orderRef flow, and the
> field-level design of the position record — not for the "which store?"
> question.

## Context

The bot shows phantom positions — open positions in the UI that don't exist in IB. Root cause: state is scattered across 3 places (SQLite trade_groups, JSON files, in-memory dicts) that constantly diverge from IB. We need a single state store where IB is the source of truth.

## Key Enabler: IB `orderRef`

IB's Order object has an `orderRef` field (string, 128 chars) that persists through the entire order lifecycle — placement, fills, status updates, open order queries. We tag every bot order with `"bot:{bot_id_short}:{symbol}"` to reliably identify which bot owns which order.

## Architecture

```
IB Gateway (source of truth)
     │
     │  reconciler queries every 2-3s
     ▼
┌─────────────────────────────────┐
│   bot_positions table (SQLite)  │  ← single state store
│   position_state, ib_qty,      │
│   entry_price, trailing_stop,  │
│   pending_order, etc.          │
└──────┬────────────┬────────────┘
       │            │
   engine writes    bot runner reads
   (position_state, (position_state)
    ib_qty, fills)  bot runner writes
                    (strategy_state_json only)
       │            │
       ▼            ▼
    UI/API reads from bot_positions
```

**Rules:**
- Engine ONLY writes: `position_state`, `ib_qty`, `ib_avg_price`, `entry_price`, `entry_time`, `trade_serial`, `pending_order_ref`, `pending_cmd_id`, `last_reconciled_at`
- Bot runner ONLY writes: `strategy_state_json` (trailing stop, HWM, current_stop)
- JSON state files: **eliminated**
- SQLite trade_groups: **never read for position decisions** (audit only)

## `bot_positions` Table

```
bot_id            PK, FK bots.id
symbol            PK, String(20)
position_state    String(20) — FLAT, ENTERING, OPEN, EXITING
ib_qty            Numeric(18,4) — shares held per IB
ib_avg_price      Numeric(18,8) — avg cost per IB
ib_order_ref      String(128) — the orderRef tag on this bot's orders
trade_serial      Integer — engine trade serial
entry_price       Numeric(18,8) — fill price
entry_time        DateTime
entry_qty         Numeric(18,4)
pending_order_ref String(128) — orderRef of working order
pending_order_side String(4) — BUY or SELL
pending_cmd_id    String(36) — pending_commands.id of active command
strategy_state_json Text — bot-writable JSON (trailing stop, HWM, etc.)
last_reconciled_at DateTime
updated_at        DateTime
```

## Reconciler (engine-side, every 2-3 seconds)

New file: `ib_trader/engine/bot_reconciler.py`

Wired as a background task in `engine/main.py` alongside `_position_cache_loop`.

### Logic

Each cycle:
1. Query IB open orders → parse `orderRef` → build map of `{(bot_id, symbol): order_info}`
2. Query IB positions → match to known bot+symbol pairs in `bot_positions`
3. For each `bot_positions` row, reconcile:

| Local State | IB Has Order? | IB Has Position? | Action |
|-------------|--------------|-----------------|--------|
| ENTERING | Yes (BUY) | No | Wait (order working) |
| ENTERING | No | Yes | → OPEN (filled fast) |
| ENTERING | No | No | → FLAT (cancelled/rejected) |
| OPEN | — | Yes | OK (healthy) |
| OPEN | — | No | → FLAT (closed in TWS) |
| EXITING | Yes (SELL) | Yes | Wait (exit working) |
| EXITING | No | No | → FLAT (exit completed) |
| EXITING | No | Yes | → OPEN (exit cancelled) |
| FLAT | Yes (with our ref) | — | Log WARNING (orphan order) |

Every transition: update `bot_positions` + insert `BotEvent` for audit trail.

### Immediate Updates (fast path)

The reconciler is the safety net. For normal operation, the engine also updates `bot_positions` immediately:
- On order placement: set ENTERING + `pending_cmd_id`
- On fill callback: set OPEN + `entry_price`, `entry_qty`, `trade_serial`
- On close fill: set FLAT

This gives sub-second state updates in the happy path. The reconciler catches edge cases (TWS manual close, engine crash, etc.).

## orderRef Flow

1. **Bot** submits: `buy QQQ 16 mid --ref bot:7d5a8b20:QQQ`
2. **Engine** parses `--ref`, passes to `ctx.ib.place_limit_order(..., order_ref="bot:7d5a8b20:QQQ")`
3. **insync_client** sets `order.orderRef = order_ref` before `placeOrder()`
4. **IB** preserves it through fills, status updates, open order queries
5. **Reconciler** reads `trade.order.orderRef` from `get_open_orders()`, parses bot_id + symbol

## Changes by File

### New files
| File | Purpose |
|------|---------|
| `ib_trader/engine/bot_reconciler.py` | Reconciler loop — queries IB, updates bot_positions |
| `ib_trader/data/repositories/bot_position_repository.py` | CRUD for bot_positions table |

### Modified files
| File | Changes |
|------|---------|
| `ib_trader/data/models.py` | Add `BotPosition` model |
| `ib_trader/ib/base.py` | Add `order_ref` param to `place_limit_order`, `place_market_order` |
| `ib_trader/ib/insync_client.py` | Set `order.orderRef`, return orderRef in `get_open_orders()` |
| `ib_trader/engine/main.py` | Wire reconciler as background task |
| `ib_trader/engine/order.py` | Pass `--ref` through to IB client |
| `ib_trader/repl/commands.py` | Parse `--ref` flag from command text |
| `ib_trader/bots/runtime.py` | Read from `bot_positions` instead of JSON files. Remove `_reconcile_state`, `_load_persisted_state`, `_check_pending_fills`. Add `--ref` to order commands |
| `ib_trader/bots/middleware.py` | `PersistenceMiddleware` writes to `bot_positions.strategy_state_json`. `ExecutionMiddleware` appends `--ref` |
| `ib_trader/api/routes/bots.py` | `GET /{bot_id}/state` reads from `bot_positions` instead of JSON file |
| `tests/conftest.py` | Add `order_ref` to MockIBClient methods |

### Eliminated
- `~/.ib-trader/bot-state/*.json` — replaced by `bot_positions.strategy_state_json`
- `_load_persisted_state()` in runtime.py
- `_reconcile_state()` in runtime.py
- `_check_pending_fills()` in runtime.py — replaced by reconciler
- `_parse_fill_from_output()` in runtime.py — no longer needed

## Build Order

1. **Models + repository** — BotPosition table, Alembic migration, repository
2. **orderRef plumbing** — base.py, insync_client.py, commands.py, order.py
3. **Reconciler** — bot_reconciler.py, wire into engine/main.py
4. **Bot runner migration** — runtime.py reads bot_positions, middleware writes strategy_state_json
5. **API update** — state endpoint reads bot_positions
6. **Cleanup** — remove JSON file code, remove old fill detection

## Verification

1. Start bot, force-buy → confirm `bot_positions` row shows ENTERING then OPEN
2. Let trailing stop trigger → confirm row shows EXITING then FLAT
3. Force-buy, then close manually in TWS → confirm reconciler detects and sets FLAT within 3 seconds
4. Kill engine mid-trade, restart → confirm reconciler reconstructs state from IB orderRef
5. No JSON files created at any point
