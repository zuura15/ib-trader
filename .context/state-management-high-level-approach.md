# State Management — High-Level Approach

<!-- Scratchpad for iterating on the approach before committing to a detailed plan. -->

## Guiding Tenets

### 3. Live state lives in memory, not SQLite

**Reversal of a previous rule.** The project previously mandated that all state
live in SQLite and that the app be crash-recoverable from SQLite alone. Over the
last several weeks, the middle-layer SQLite state management has been the source
of most of the pain (phantom positions, drift from IB, stale `trade_groups`
rows), while the app process itself has been stable. The 180° flip:

- **All live state lives in process memory.** Order lifecycle, position state,
  bot runtime state, reconciler view of IB — all in memory.
- **SQLite is demoted to archival / activity storage only.** Audit logs,
  transaction history, closed trade records, bot events, raw IB responses. It
  is never in the critical path for a live decision.
- **Persistence of our-own state is a future concern, not a current one.** The
  direction is a **lightweight persistent-memory backend (e.g. Redis)** where
  the app reads and writes as if it were plain memory and the backend handles
  durability transparently. Until that lands, in-process memory is the store.
- **Crash recovery** for broker-held data is trivial: re-query IB on startup
  (IB is the source of truth — see tenet 1). For our-own state (trailing stop
  HWM, cooldown timers, etc.), the interim cost is that those may be lost on
  crash; the Redis step closes that gap later without changing app code.

CLAUDE.md has been updated to reflect this reversal — the previous "Zero Memory
State" section is now "Crash Recovery", and the "Data & State" and "Process
Isolation" sections have been rewritten to remove the SQLite-as-source-of-truth
rule.

