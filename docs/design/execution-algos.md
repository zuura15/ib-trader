# Execution & Exit Algorithm Refactor — Design Notes

**Status:** Design in progress, not yet implemented. Planning ahead of the next refactor pass.

## Context

Today's stop-loss and trailing-stop logic is duplicated across
`ib_trader/bots/strategies/sawtooth_rsi.py:289-401` and
`ib_trader/bots/strategies/close_trend_rsi.py:269-367`. Order execution
logic (mid-walker, limit, market) lives in `ib_trader/engine/order.py`
as long functions with duplicated reprice-loop structure. Bots currently
declare a coarse `order_type` string ("mid" / "market" / "limit") and
both layers are tangled.

This doc captures the target architecture so the refactor can be staged
safely.

---

## Separation of concerns

Two distinct abstractions, currently conflated:

### 1. Exit-decision algorithms — *"should I emit an exit order?"*
Strategy-side. **Per-trade stateful** evaluators that run on each quote.
- Hard stop: `price <= entry * (1 - hard_sl_pct)` → emit SELL
- Trailing stop: maintain HWM; emit SELL when `price < HWM * (1 - width)`
- Time stop: `now - entry_time > limit` → emit SELL
- Take-profit (future): `price >= target` → emit SELL
- Composite: run a list, first trigger wins

Target module: `ib_trader/algos/exits/`

### 2. Execution algorithms — *"how do I get this order filled?"*
Engine-side. **Stateless per-order** state machines that turn intent
into IB order activity.
- Patient limit: submit + wait + allow explicit cancel only
- Patient mid: reprice at mid every N sec, expire on timeout (skip signal)
- Aggressive mid + terminal: reprice fast, cross-to-market (RTH) or
  cap-at-slippage-floor (ETH)
- Market (RTH only, never sent from bots — only as a terminal phase)
- Bid/ask peg, explicit-price limit

Target module: `ib_trader/algos/execution/`

---

## Bot-facing API changes

### New `fill_type` enum (replaces `order_type` string)

Declarative intent. Finite set the engine understands.

| `fill_type`  | Bot means                              | Sent to IB as       |
|--------------|----------------------------------------|---------------------|
| `MID`        | Patient entry — try mid, skip on timeout | LMT (repriced)      |
| `MARKET`     | Urgent exit — get out now (session-aware) | LMT → MKT / cap    |
| `PATIENT_LIMIT` (future) | Take-profit at price — rest until filled | LMT         |
| `LIMIT_AT_PRICE` (future) | Explicit-price limit                  | LMT                 |

Minimum set to start: `MID` and `MARKET`. Others added as strategies
need them.

### `origin` stays as provenance (unchanged)

`"strategy" | "exit" | "manual_override"`. Distinct axis from fill_type.
Used by `ManualEntryMiddleware` for gating, by the audit trail, and
for authorization checks. **Not** used to pick the execution algo —
that's fill_type's job.

### Bot never specifies session or execution mechanics

- No native MKT from bots. `fill_type=MARKET` → middleware decides.
- No session awareness in strategy code.
- `max_slippage_pct` is a risk-tolerance config value honored only by
  the ETH terminal of `AGGRESSIVE_MID`; ignored under RTH.

---

## Session-aware translation (middleware)

`ExecutionMiddleware` is where intent meets context. Session detection
uses `ib_trader/broker/ib/hours.py`. Translation:

```
fill_type=MID                  → PatientMid(reprice=1s, duration=10s, terminate=skip)

fill_type=MARKET + RTH         → AggressiveMid(reprice=100ms,
                                               duration=10s,
                                               terminate=market)

fill_type=MARKET + ETH/overnight → AggressiveMid(reprice=100ms,
                                                  duration=unbounded,
                                                  terminate=cap_at_floor,
                                                  floor=trigger_price *
                                                        (1 - max_slippage_pct),
                                                  on_cap=alert_and_rest)
```

---

## Aggression asymmetry

| Axis | Entry | Exit |
|---|---|---|
| Intent | "Would like to" | "Must" |
| Skip acceptable | Yes (wait for next signal) | No |
| Cross the spread? | No (eats edge) | Yes, RTH |
| Reprice interval | 1000 ms | 100 ms |
| Duration | 10 s → skip | RTH: 10 s → market; ETH: unbounded → cap |

---

## Reprice loop semantics

- **Interval** — time between reprice cycles (100 ms / 1 s).
- **Duration** — total wall-clock budget for the algo before the
  terminal phase fires. ETH exits have no duration.
- **Event-driven reprice**: instead of a blind timer, reprice *on
  quote change OR every `interval` ms, whichever is first*. Avoids
  redundant cancel/replace traffic when quotes are stale; avoids lag
  when quotes are fast-moving.

---

## ETH / overnight cap semantics

"Unbounded duration" is NOT actually unbounded — bounded by the
`max_slippage_pct` floor:

1. Each reprice moves the limit toward the bid (SELL) / ask (BUY).
2. The limit never walks past `trigger_price × (1 − max_slippage_pct)`.
3. When the floor is reached: stop repricing, leave the order resting
   at the floor price, raise a `CATASTROPHIC` alert
   (`EXIT_PRICE_CAP_REACHED`). Human intervenes.

Terminal states in ETH:
- **Filled** — happy path
- **Capped-and-resting** — order alive at floor, human must resolve
- **Cancelled by operator** — explicit kill

No "timed out" in ETH. Duration is not a terminal condition there.

---

## Partial fills across phases

A 10-share exit gets 4 filled at mid during phase 1. On phase transition
(RTH → market terminal), submit **6** shares, not 10. Track remaining
qty from IB's `filledQuantity` on the cancelled parent order.

---

## Session-transition edge case

Exit triggers 30s before RTH close. Phase 1 runs past 16:00 into ETH.
Phase 2 choice:
- **Lock session at trigger** — decided at trigger time, phase 2 follows
  through even if now in ETH (may fail: MKT rejected in ETH)
- **Re-evaluate at phase transition** — if we've crossed into ETH, fall
  back to cap-at-slippage instead of market

Preferred: **re-evaluate at phase transition**. Slightly more code, but
safe across the boundary.

---

## Target module layout

```
ib_trader/
  algos/
    __init__.py
    execution/                          # how to fill (stateless per order)
      base.py         # ExecutionAlgo Protocol
      patient_mid.py
      aggressive_mid.py
      patient_limit.py
      limit_at_price.py
      market.py       # engine-internal only
    exits/                              # when to exit (stateful per trade)
      base.py         # ExitPolicy Protocol, TradeState dataclass
      hard_stop.py
      time_stop.py
      trailing_stop.py
      take_profit.py
      composite.py    # runs a list of policies, first trigger wins
```

Strategies compose an `ExitPolicy` list once at `__init__`; `_on_quote`
delegates:

```python
actions = self.exits.evaluate(quote, ctx.state)
```

Engine routes execution on `fill_type` after middleware has resolved
the session-aware variant.

---

## Each policy owns a single atomic state object

P1 finding from the block-by-block review: `TrailingStop` currently
splits state across two coupled-but-independent dict keys
(`trail_activated: bool`, `high_water_mark: Decimal str`). Drift is
possible if either is written/restored without the other.

Rule for each extracted policy: **one state object, atomic read/write**.
Presence of the object == armed/active; absence == inactive. No
separate flags.

```python
# Good
trailing_stop:
  hwm: "500.25"
  activated_at: "..."

# Bad (today)
trail_activated: true
high_water_mark: "500.25"
```

`TrailingStop.evaluate(...)` reads `state.trailing_stop` as one thing.
Either you have a trail in flight or you don't. Same rule for any
future policy carrying per-trade memory (partial-take-profit levels,
pyramid-add counters, etc.).

---

## Bugs this extraction solves for free

From the stop-loss review (see conversation history):

- **#1 Rejected SELL strands bot in EXITING** — already fixed in the
  FSM refactor, but this design prevents regression: exit-policy code
  becomes a pure function of (quote, trade_state), no state-machine
  coupling.
- **#2 Market-only exit rejects in ETH** — fixed by `fill_type=MARKET`
  → aggressive-mid, which uses LMT in ETH.
- **#3 `current_stop` field has two meanings** — fixed by each exit
  policy owning its own state keys (e.g., `trailing_stop.hwm`,
  `trailing_stop.current_stop` instead of a shared `current_stop`).
- **#4 Duplicated exit logic across strategies** — fixed by extraction.
- **#5 `last_price` dropped on trigger tick** — fixed by composite
  policy returning accumulated actions + trigger actions.

---

## Enum vocabulary

Replace string literals across strategies / runtime / middleware with
typed enums so typos become compile-time (ruff / mypy) errors and
downstream code can exhaustive-match.

```python
class Side(str, Enum):           BUY, SELL
class Origin(str, Enum):         STRATEGY, EXIT, MANUAL_OVERRIDE
class FillType(str, Enum):       MID, MARKET, PATIENT_LIMIT, LIMIT_AT_PRICE
class QuoteField(str, Enum):     BID, ASK, LAST
class ExitType(str, Enum):       HARD_STOP_LOSS, TRAILING_STOP, TIME_STOP,
                                  TAKE_PROFIT   # add members as policies grow;
                                                # covers both "policy category"
                                                # and "runtime trigger reason"
class LogEventType(str, Enum):   BAR, SIGNAL, SKIP, FILL, STATE,
                                  EXIT_CHECK, CLOSED, RISK, ERROR
```

Rename `LogSignal.payload["exit_type"]` values from inconsistent strings
to `ExitType` enum values; keep the key as `"exit_type"` for audit
compatibility.

**Non-enum but still worth const-ifying** — state dict keys
(`entry_price`, `high_water_mark`, `current_stop`, `trail_activated`,
etc.) become attributes on a `TradeStateKeys` frozen dataclass. Each
exit policy owns its own keys once extraction lands.

**Config keys (e.g. `hard_stop_loss_pct`, `trail_activation_pct`,
`exit_price`) are NOT an enum/const problem.** They're schema. The
extraction gives each exit policy class its own `Config` dataclass
with typed fields, Decimal coercion, and defaults declared once.
`exit_cfg.get("hard_stop_loss_pct", "0.001")` becomes
`self.config.pct` — no string literal survives in strategy code. Typos
fail at load time via pydantic (or a dataclass reader), not silently
at first quote tick.

Staging:
1. `Side`, `Origin`, `ExitType`, `LogEventType` enums — small standalone PR
   before the big refactor. Ruff catches misses.
2. `FillType`, `QuoteField` enums — land with the `order_type` → `fill_type`
   migration.
3. `TradeStateKeys` constants — land with the exit-policy extraction; each
   policy class owns its own keys.

---

## Naming cleanup: `_pct` is actually `_frac`

Today's config keys (`hard_stop_loss_pct: 0.001`, `trail_activation_pct:
0.0005`, `trail_width_pct: 0.0015`) and the computed `pnl_pct` variable
hold **fractions**, not percentages. `0.001` means 10 bps = 0.1%, not
10%. The `_pct` suffix is a misnomer — Python's `{:.4%}` formatter
multiplies by 100 for display, making the code's arithmetic fraction-
based while the log output looks percent-correct.

Two options when extracting:

1. **Rename to `_frac`** — honest to the math; breaks every YAML config
   (migration script or a one-time rename).
2. **Keep `_pct` but change semantics** — `hard_stop_loss_pct: 0.1`
   means 0.1%. Read-side divides by 100. More human-friendly YAML,
   one conversion point.

Pick option 2 for the extraction pass — less cognitive overhead for
anyone editing strategy configs.

---

## Exit policies are opt-in, not default

Time stop is the canonical example: it's a **strategy opinion**, not a
safety rule. A mean-reversion bot wants it at ~100 min; a trend-follower
would be destroyed by it. Current code bakes `time_stop_minutes: 108`
as a hardcoded fallback in both strategies — that's wrong.

Rule for the extraction:

- **No policy is active unless the strategy's YAML names it.**
- **No code-level defaults for config values.** If a policy class needs
  a numeric value and the strategy didn't supply one, the policy isn't
  instantiated at all. Missing config → code path doesn't run, full
  stop.
- Exception: hard stop on every strategy is reasonable to require, but
  enforce by failing strategy init if absent, not by silently applying
  a default.

YAML shape:

```yaml
# Include this stanza — time stop is active.
exits:
  - hard_stop:     { pct: 0.001 }
  - trailing_stop: { activation: 0.0005, width: 0.0015 }
  - time_stop:     { minutes: 108 }
```

```yaml
# Omit the time_stop entry — time stop never runs.
exits:
  - hard_stop:     { pct: 0.001 }
  - trailing_stop: { activation: 0.0005, width: 0.0015 }
```

Composite policy instantiates only the entries present in the YAML
list. Typos in policy names fail loud at load time (unknown policy →
ValueError), typos in inner keys fail loud via the policy's dataclass
schema (unknown field → ValueError).

Interim behavior (before extraction): sawtooth + close_trend keep
today's time stop because their theses assume it. The extraction
preserves that via explicit YAML entries; no code change to exit
trigger behavior in the extraction step itself.

---

## Open design questions

1. **Where does the execution algo run?** Inside the engine process
   (engine imports from `algos/execution/`) or below it (engine
   delegates to algos, algos use an abstract broker client)? Former
   simpler; latter testable without engine.
2. **Manual override `fill_type=MARKET`** — apply same session-aware
   safety, or trust the operator? Default: same safety; operator can
   use a distinct endpoint if they truly want raw MKT.
3. **New `origin` values** — reconciliation-driven orders (system
   closes an orphan)? Future; not urgent.
4. **Fourth fill_type: `PEG`** — peg at bid/ask, not mid. Useful for
   patient entries in wide-spread instruments. Future.

---

## Staging

This is a real refactor. Suggested order (separate PRs):

1. Introduce `fill_type` enum; bots migrate from `order_type` string.
   `AGGRESSIVE_MID` behavior stays inline in engine for now — no algo
   extraction yet. Landing this alone unblocks the execution-algo
   naming.
2. Extract execution algos into `algos/execution/`. Engine becomes a
   thin dispatcher. `aggressive_mid.py` picks up session-aware terminal
   logic.
3. Extract exit policies into `algos/exits/`. Strategies shrink: bar
   signals only. `_on_quote` becomes a one-line delegation.
4. Add `PatientLimit`, `TakeProfit`, etc. as new primitives once the
   extraction is stable.

Each stage is independently valuable and doesn't block the others.
