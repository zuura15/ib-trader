# ADR 016: Collapse FSM into bot methods

Date: 2026-04-20
Status: Accepted

## Context

The bot lifecycle was managed by a dedicated finite state machine
module at `ib_trader/bots/fsm.py` (775 lines). It encoded six states
(OFF / ERRORED / AWAITING_ENTRY_TRIGGER / ENTRY_ORDER_PLACED /
AWAITING_EXIT_TRIGGER / EXIT_ORDER_PLACED), fifteen event types, and
a 27-row `(state, event) → handler` transition table. Each handler
returned a `TransitionResult` with a state patch plus a declarative
`side_effects` list (cancel_order, record_trade_closed, pager_alert,
…). The runtime executed the side effects via an
`_execute_side_effects` case-switch keyed by action string.

`FSM.dispatch` was invoked from 29 call sites across four files
(runner.py, internal_api.py, runtime.py, api/routes/bots.py). Each
call traversed: caller → `BotEvent` dataclass → `FSM.load(redis)` →
handler lookup → `TransitionResult` → `FSM.save(redis)` → caller
iterates side_effects → caller dispatches to `_execute_side_effects`
→ case-switch on action name → real work.

In practice:

- The FSM was always invoked in the same process as the bot instance
  (the bot runner). The module-level `_DISPATCH_LOCKS` existed as a
  hedge against cross-process dispatch that never materialised.
- `TransitionResult.side_effects` was never polymorphic — every
  action string maps to exactly one runtime method. The indirection
  added vocabulary (events + actions + handlers) without adding
  behaviour.
- Bugs kept appearing at the seams: the April 19 runaway needed a
  stoic-mode in-memory flag (`_order_submit_in_flight`) because the
  FSM transition happened *after* the engine HTTP response; the
  April 20 fast-paper-fill race needed a `_recent_terminal_order_ids`
  buffer because the terminal event arrived before the FSM knew the
  `ib_order_id`. Both were symptoms of the FSM being driven by
  return values rather than owning the flow.

## Decision

Collapse the FSM module. State transitions become methods on
`StrategyBotRunner` directly; side effects become inline method
calls inside the handler bodies; the persisted state doc stays in
Redis (`bot:<id>:fsm`) with the same fields.

What goes away:

- The `fsm.py` module (775 lines).
- `BotEvent`, `TransitionResult`, `SideEffect` dataclasses.
- The `_TRANSITIONS` dispatch table.
- The module-level `_DISPATCH_LOCKS`.
- The `_execute_side_effects` case-switch in `runtime.py` (~100 lines).
- ~145 lines of dispatch boilerplate at the 29 call sites.
- Stoic mode (`_order_submit_in_flight`, `_awaiting_terminal_ib_order_id`,
  `_stoic_mode_set_at`, `_check_stoic_mode_timeout`).
- The `_recent_terminal_order_ids` race buffer.

What stays:

- The 6-state `BotState` enum — moved to `bots/lifecycle.py`.
- The Redis doc shape (same keys, same values).
- The strategy host, middleware pipeline, event loop, and
  supervisor loop — all untouched.
- The FSM_TRANSITION audit log lines (preserved by name for
  grep-compatibility; the `trigger` field is now a method name like
  `on_entry_filled` rather than an `EventType.value`).

## Design details

**State transitions live on the bot.** Each event type becomes an
`on_*` method (`on_start`, `on_stop`, `on_force_stop`, `on_crash`,
`on_place_entry_order`, `on_entry_filled`, `on_exit_filled`,
`on_entry_cancelled`, `on_exit_cancelled`, `on_entry_timeout`,
`on_manual_close`, `on_ib_position_mismatch`, …). Each method takes
a per-bot `asyncio.Lock` (`self._state_lock`), loads the doc,
validates the from-state with an explicit
`if self._state not in (…): log; return` guard, mutates the doc,
persists, and runs its side effects inline.

**State-based gating replaces stoic mode.** Previously the runtime
would transition the FSM *after* the engine HTTP response returned
(so it had the `ib_order_id` to include in the transition payload).
During the HTTP wait, a concurrent quote tick could pass the
`state == AWAITING_EXIT_TRIGGER` check and re-run the strategy,
emitting a duplicate order — which is why `_order_submit_in_flight`
was added as a second gate.

Now `on_place_entry_order` / `on_place_exit_order` flip the state
to `ENTRY_ORDER_PLACED` / `EXIT_ORDER_PLACED` **synchronously before
the pipeline runs**. Quote / bar / position stream handlers gate on
that state and return early, so no duplicate orders. The
`_order_submit_in_flight` flag is redundant and gone. On pipeline
failure the pre-transition is reverted so the bot can retry.

**Operator contract change (visible).** A crashed or errored bot no
longer auto-clears to `AWAITING_ENTRY_TRIGGER` on re-START. Instead,
`/bots/<id>/start` now calls `is_clean_for_start` on the doc and
refuses to start unless `state=OFF` and all position fields are
zeroed. The operator must explicitly `/bots/<id>/reset` first, which
calls `force_off_state` — the same helper the startup panic path
uses. Resetting without restarting the app avoids the asymmetric
blast radius of "to fix bot X, take down the whole platform."

**Reservation pattern for `/start`.** The HTTP handler previously had
a check-then-await-then-insert race where two concurrent START
requests both passed the `if bot_id in bot_instances` check during
`_create_and_start_bot`'s warmup. A module-level `_RESERVED`
sentinel is now inserted synchronously before the await. asyncio is
single-threaded; the check + reserve pair is atomic with respect to
every other coroutine.

## Consequences

- `-775` lines from `fsm.py`, plus `~250` lines of indirection
  deleted from runtime.py and callers. Net: `~1000` lines out.
- One vocabulary (method names) instead of three (events + action
  strings + state names).
- Ordering of operations is explicit: state transitions that were
  previously coupled to HTTP responses now happen where they belong
  (before the HTTP call, so the state is the gate during the wait).
- Tests that exercised `FSM.dispatch` / `TransitionResult` directly
  are retired; the runtime's `on_*` methods are now tested through
  their call sites (startup panic, order-submit-guard, force-sell,
  stale-quote, etc.).
- Crashed / errored bots require an explicit operator reset before
  re-START. This is a tightening — the old auto-clear behaviour is
  gone.

## Alternatives considered

1. **Keep the FSM, just fix the ordering bugs.** Would have kept the
   775 lines + 29 dispatch sites. Every new state or event requires
   touching the enum, the transition table, and a handler function.
   The mechanical overhead was the complaint, not any single bug.
2. **Separate runner / bot / FSM processes with Redis-backed
   dispatch.** Would have justified the indirection but none of the
   dispatches actually crossed a process boundary. Strictly more
   complexity than the problem called for.
3. **Partial collapse: keep `BotState` + transition table, remove
   `SideEffect` registry only.** Removes some indirection but leaves
   the ordering bug (FSM after HTTP) in place. Didn't address the
   real pain.

## References

- `docs/decisions/011-two-severity-levels-catastrophic-warning.md` —
  pager alert semantics preserved under the collapse.
- `ib_trader/bots/lifecycle.py` — the surviving `BotState` enum,
  `force_off_state` helper, and `is_clean_for_start` predicate.
- `ib_trader/bots/runtime.py` — the on_* methods that replaced
  `FSM.dispatch`. The "FSM collapse (ADR 016)" block marks the
  section.
