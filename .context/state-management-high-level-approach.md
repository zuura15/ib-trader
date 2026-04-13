# State Management — High-Level Approach

<!-- Scratchpad for iterating on the approach before committing to a detailed plan. -->

## Guiding Tenets

### 1. No persisted duplicates of live broker-held data

IB is treated as the canonical broker example — multi-broker nuances are deferred.
The rule is about **persistence**, not about transit through memory: when we read
data from IB the object obviously lives in memory for as long as we're using it,
and that is fine. What is forbidden is writing a copy of live, still-mutable
broker state into any of our own stores.

- **Scope.** This tenet applies mainly to discrete transactional records —
  **orders and trade (fill/execution) records**. Live broker-held aggregates
  (open positions, working orders, current balances, average cost) fall under
  the same rule from the other direction: they are never persisted by us, they
  are always read from the broker.
- **The test is mutability at the broker.** If the broker still considers a
  record live/mutable — a working order, an open position — our system does
  not hold a persisted copy of it. Once the broker considers the record
  terminal and frozen (an order that has reached Filled or Cancelled, an
  execution that has been reported), the record is immutable and we are free
  to persist it.
- **In-memory transit is not duplication.** Reading an IB order or trade into
  a Python object so the app can act on it is normal memory use, not
  persistence. This tenet does not restrict that.
- **Storage venue is a separate concern.** *Where* immutable records get
  persisted once they're allowed to be persisted (SQLite archive, log file,
  future persistent-memory backend) is governed by tenet 3 and subsequent
  design decisions, not by this tenet.

### 2. SQLite is purely an analytics and audit store

SQLite is scoped tightly: it exists for **analytics and audit only**. Analytics
covers both trading analytics (bot performance, closed-trade records analyzed
after the fact) and app-level analytics (usage patterns, diagnostic signals
inspected later). Audit covers the forensic "what happened" trail — most
notably the raw IB API response log already required by CLAUDE.md. SQLite is
**not** a home for any kind of live, operational, or configuration state.

- **Analytics / audit read path.** SQLite writes feed later analysis,
  reporting, and post-hoc investigation. The app's live/hot path never reads
  SQLite to decide what to do next. Any read from SQLite is out-of-band — an
  analytics query, a dashboard, a forensic lookup — not a runtime decision.
- **What SQLite is *not* for:**
  - Bot or strategy configuration (stays in YAML files)
  - Watchlists, symbol lists (stays in YAML files)
  - UI preferences, layout, tab state (frontend-local storage, not SQLite)
  - Bot enable/disable or other live operational flags
  - Any reconstruction of live broker-held state
  - Any cache or mirror of IB data used to make live decisions
- **What SQLite *is* for:**
  - Terminal trading records (filled/cancelled orders, executions) persisted
    so they can be analyzed later
  - Bot event history used for performance analysis
  - App-level analytics signals
  - Forensic audit trails — including the raw IB API response log
- **Relationship to tenet 1.** Tenet 1 governs *whether* something may be
  persisted at all (only once the broker considers it terminal / immutable).
  Tenet 2 governs *where* that persistence lives when the purpose is
  analytics or audit: SQLite. Other persistence venues remain available for
  other purposes, and other live stores (in-memory, future persistent-memory
  backend — see tenet 3) handle anything that isn't analytics or audit.

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
- **Redis is the persistent-memory backend.** The earlier framing of "future
  concern" has been upgraded: Redis is being brought in now as the concrete
  runtime backbone. Redis keys store live state (bot config, lifecycle status,
  strategy state, position state). Redis Streams transport all real-time data
  (quotes, bars, fills, order status, bot events, alerts). RDB snapshots
  persist strategy state across restarts. The app reads and writes as if to
  memory; Redis handles durability transparently.
- **Redis Streams for all real-time transport, not pub/sub.** Streams persist
  until consumed (no lost messages if a consumer is temporarily down), support
  consumer groups for independent consumption, allow late joiners to catch up
  from recent history, and are bounded via MAXLEN trimming. One primitive
  instead of mixing pub/sub and streams. The latency motivation is critical:
  bots monitoring stop-losses need sub-second price data, where 3 seconds can
  be the difference between a $100 and $500 loss. Current SQLite polling
  delivers 7-35s latency.
- **Crash recovery** for broker-held data is trivial: re-query IB on startup
  (IB is the source of truth — see tenet 1). For our-own state (trailing stop
  HWM, cooldown timers, etc.), Redis RDB snapshots provide persistence across
  restarts without changing app code.

CLAUDE.md has been updated to reflect this reversal — the previous "Zero Memory
State" section is now "Crash Recovery", and the "Data & State" and "Process
Isolation" sections have been rewritten to remove the SQLite-as-source-of-truth
rule.

### 4. Tag broker objects with self-contained, human-readable identity

We use IB's order tagging features — canonically the `orderRef` string — to
attach our own identifying context directly to broker-held objects, instead of
maintaining a parallel sidecar in our own system. The tag is the durable
identity that survives restarts and offline actions; without it we have no
reliable way to recognise our own orders after the process or the user has
moved on.

- **Canonical hook: IB `orderRef`.** It is preserved through the entire order
  lifecycle (placement, fills, status updates, open-order queries) and is the
  natural surface for our tagging schema. Other IB annotation hooks may be
  used opportunistically, but the design assumes `orderRef` as the primary
  vehicle.
- **Self-contained.** A reader looking at the tag alone — in TWS, in an IB
  log, in our reconciler, in a stored execution record — must be able to
  understand what the order is and who placed it, without cross-referencing
  any other store. The tag does not encode a foreign key into a lookup
  table; it carries its own meaning.
- **Human-readable *and* machine-parseable.** Two constraints, both binding:
  the tag must be eyeballable by a human in TWS or logs (no opaque hashes,
  no raw UUIDs — abbreviations are acceptable and necessary given the
  128-char ceiling), and it must also be deterministically parseable by our
  app, since the reconciler and other code will need to extract structured
  fields from it. The schema therefore has to be structured enough for code
  to round-trip cleanly, not freeform prose.
- **Identity and context, not mutable state.** The tag carries who/what/why
  (e.g. bot identity, strategy, intent), not operationally mutable data like
  entry price or trailing stop level. Mutable runtime state lives in memory
  per tenet 3; the tag is the stable name we use to re-find the broker
  object that state corresponds to.
- **Purpose: survive restarts and offline actions.** The motivating cases are
  (a) the engine restarts mid-trade and needs to re-recognise its own working
  orders and positions, and (b) a human (or another app) manually closes,
  cancels, or modifies an order outside our system, and we still need to
  attribute the action correctly when we later see it. The tag is what makes
  both of those recoveries possible.
- **Durable identity vs. ephemeral references.** Within a live process, code
  may freely hold IB's own identifiers (`permId`, `orderId`, in-memory
  `Trade` objects) for convenience. Those are ephemeral. The tag is the
  identity that crosses process boundaries and offline gaps — it is what the
  reconciler keys off.
- **Schema is a follow-on design exercise.** The exact field layout of the
  tag string (which fields, ordering, separator, abbreviation conventions)
  is not fixed by this tenet. It will be designed once tenets are settled.

### 5. (considered and dropped)

A fifth tenet was originally drafted along the lines of "a new order can be
either human-driven or bot-driven; the tag distinguishes between them." On
reflection it isn't load-bearing — the underlying observation is closer to
"every order is created by our system, and humans are just one kind of
input source" — and even that framing isn't needed as a guiding principle.
Origin/source can still be encoded in the tag if and when the schema design
calls for it (under tenet 4), but it does not need its own tenet.

