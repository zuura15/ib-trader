# Bot Stabilization TODOs

These are known gaps identified during the Redis migration. None are urgent
for the user's current usage pattern, but they need addressing before the
system is treated as production-ready.

## Critical (real money risk)

- [ ] **Stop bot with open position**
  Currently: clicking Stop leaves the IB position open with no exit
  monitoring. The strategy state in Redis stays as OPEN. If the user
  restarts the bot later, it loads stale entry/HWM data.
  Need: confirmation dialog + choice (close position vs. leave it).
  Strategy state in Redis should be cleared if the user chooses to close,
  preserved if they leave it. Optionally a "stopped but monitoring" state
  where exits still trigger but no new entries.

- [ ] **IB connection hiccup → bot dies**
  Currently: any IB exception during order placement propagates up,
  bot enters ERROR state, stops monitoring. Single transient network
  blip leaves a position unsupervised.
  Need: retry with backoff in `ExecutionMiddleware._submit_via_http`.
  On persistent failure, surface a CATASTROPHIC alert but keep the bot
  alive in a degraded mode (continues monitoring quotes, retries the
  exit order on next eligible event).

- [ ] **Bot crash with open position**
  Currently: state survives in Redis but trail-stop monitoring is gone
  for the crash window. On restart, recovery is automatic but the gap
  is unmonitored.
  Need: alerting when a bot with an open position enters ERROR or stops
  unexpectedly. Optional: external watchdog process that pings the bot
  runner and raises an alert if a bot's heartbeat goes stale while its
  position state is OPEN.

## High

- [ ] **PersistenceMiddleware writes ENTERING before order is placed**
  If engine HTTP call fails, we have rollback logic but it's racy.
  The pre-pipeline state snapshot is taken before PersistenceMiddleware
  runs, so on rollback we revert to FLAT — but the rollback may run
  after another middleware has already mutated something else.
  Cleaner: only persist OPEN/ENTERING state after the engine HTTP call
  returns success. Bot stays FLAT in Redis during the engine round-trip;
  state transitions on confirmed acceptance.

- [ ] **Close-order idempotency**
  Clicking "close 70" twice could place two close orders before the
  first one fills. Need server-side dedup by serial — engine should
  reject a close request for a serial that already has a working close
  order.

- [ ] **No `bot.uptime` calculation**
  UI always shows 0 in the bot card uptime field. Compute as
  `now - bot.created_at` or `now - bot.last_status_change`.

## Medium

- [ ] **Multi-bot same-symbol races**
  Untested. Both bots subscribe to the same `bot:control:global` stream
  and consume from the same `quote:{symbol}` stream. Should be safe
  (each bot has its own consumer state) but unverified.

- [ ] **REPL was never migrated to HTTP**
  REPL still has its own IB connection (clientId=REPL_CLIENT_ID) and
  bypasses the orderRef tagging entirely. Manual orders placed via REPL
  show as untagged in the engine and get logged as `bot_ref=unknown`.
  Either route REPL through the engine HTTP API (loses REPL's offline
  capability) or accept that REPL orders are untagged.

- [ ] **Daily P&L / trades_today aggregation**
  Unclear when `bots.pnl_today` and `bots.trades_today` columns get
  updated. The fill processing path writes individual transaction rows
  but doesn't roll up into the bots table. UI shows stale or zero.

## Low

- [ ] **Reconciler runs every 60s but doesn't validate Redis vs IB**
  The reconciler's "sanity check" function exists but only runs on
  startup. The 60s scheduled run should detect drift between Redis state
  and IB state and surface alerts. Currently silent.

- [ ] **`order_ref` length validation**
  No length check before sending to IB. IB has a 128-char limit on the
  orderRef field. Long bot_ref + symbol + serial could exceed it (very
  unlikely but possible).

- [ ] **No bot configuration validation on startup**
  If a strategy YAML is malformed (missing required field, bad value),
  the bot fails at first tick instead of refusing to start. Validate
  config at bot startup before entering the event loop.
