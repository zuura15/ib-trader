# ADR 019: Tick-driven fuzzy-line strategy ("tick_fuzzy")

Date: 2026-05-19
Status: **v0.1 implemented** — exit-first scope on branch
`feature/fuzzy-trader`. Entry FSM deferred to next iteration; bot
stays `manual_entry_only: true` and operator force-enters to exercise
the exit lifecycle.

## Context

The existing `chart_signal` strategy evaluates entries on bar-close
boundaries (3-min cadence). This introduces structural latency between
the operator's perceived signal moment ("the line just held") and the
bot's evaluation moment (bar close, up to 3 min later). Live trading
exposes the gap: the operator's manual fills today were significantly
better than the bot's, because the operator reads the tape in real
time and reacts before the next bar closes.

Layer-1 of the operator's roadmap (cf. session 2026-05-19) was
explicitly: **tick-driven entry + exit, no bar-close dependency.**
This ADR specifies the first such strategy — `tick_fuzzy` — built on
the fuzzy SR lines already detected by
`ib_trader.signals.fuzzy_lines.detect_fuzzy` (RANSAC inlier-counted
lines + scored pivots + parallel channels). The bar-close detector
still produces the LINES; the tick-driven strategy reacts to PRICE
relative to those lines.

## Scope

NEW strategy module `ib_trader/bots/strategies/tick_fuzzy.py`. NEW bot
config (slot 6 on the fuzzytrader chart, MES symbol for low-loss
testing). NEW order-strategy `mid_only` in the engine.

EXISTING code paths untouched: `chart_signal`, `fuzzy_signal`,
`signals/fuzzy_lines.py`, the SR fan, the engine's walking-mid loop.

## Locked parameters

All thresholds in dollars per contract. Values are per-contract
MINIMUMS designed around MNQ (multiplier $2/pt). Smaller-value
contracts (e.g. anything with a smaller $/tick than MNQ) use the
same dollar floors rather than proportional scaling. Larger contracts
(MES, MGC, MCL) inherit the same floor too — operator can tune up
per-bot in YAML if a contract warrants a wider stop.

| Param                       | Value      | MES (mult=$5, tick=0.25) | MNQ (mult=$2, tick=0.25) |
|-----------------------------|-----------:|-------------------------:|-------------------------:|
| Initial SL                  | $10        | 8 ticks                  | 20 ticks                 |
| Trail activation threshold  | $20        | 16 ticks (4 pts)         | 40 ticks (10 pts)        |
| Trail give-back             | $5         | 4 ticks (1 pt)           | 10 ticks (2.5 pts)       |

Entry-side timing:

| Param                          | Value |
|--------------------------------|------:|
| Entry hold-window (no breach)  | 10 s  |
| Entry bounce trigger (alt)     | 5 ticks of move away from line |
| Mid-fill timeout               | 10 s  |
| Re-arm cooloff                 | 0 s   |

## Entry FSM (per watched line)

The strategy maintains one watch state per qualifying fuzzy line.
Multiple lines run in parallel; the first to confirm wins, the rest
get cancelled when the bot transitions out of `AWAITING_ENTRY_TRIGGER`.

```
                                              tick: breach >1 tick
                                              ┌──── (reset hold timer) ────┐
                                              │                            ▼
   line   touch within tol          touch event             10 s
  detected  (tick price ≈ line) ──────────────► TOUCHED ───── elapsed ─────► CONFIRMED
                                              │       OR                       │
                                              │       bounce ≥ 5 ticks         │
                                              │       in our direction         │
                                              │                                │
                                              ▼                                ▼
                                       CANCELLED                          FIRE ENTRY
                                       (line was broken                   (mid_only order)
                                        before confirmation)
```

State on each watch:
- `line_id` — frozen at detection time; the line's slope/intercept don't repaint after we start watching it.
- `direction` — long for support, short for resistance.
- `touch_first_ts` — wall-clock when the price first entered the touch tolerance.
- `hwm_bounce_distance` — high-water-mark of |price − line_value| since touch, used for the 5-tick-bounce trigger.
- `breaches` — count of ticks beyond the line on the wrong side. Resets the hold timer.

### "Touch" definition

Price within ±1 tick of the line value at the current bar index.
Distance is recomputed every tick (the line's value at the current
bar index, NOT the line value at touch time, so a sloped line tracks).

### "Breach" definition

A single tick that closes more than 1 tick past the line on the wrong
side. Single-tick noise within ±1 tick of the line is tolerated (the
operator's eye dismisses one wick-print). Two or more breaches in a
row → CANCELLED.

### Multi-line race

When the bot is in `AWAITING_ENTRY_TRIGGER`, multiple watches can sit
in TOUCHED concurrently. The first to reach CONFIRMED fires the entry.
The instant the entry order is submitted, all OTHER watches transition
to `SUSPENDED` (not cancelled — they may re-arm on exit). On position
exit, all suspended watches reset to `WATCHING` and rebuild from fresh
fuzzy-line data.

## Order plumbing — `mid_only`

A new order-strategy on top of the existing `mid` and `bid`/`ask`/`market`:

- Place a limit at the current mid.
- On every reprice tick (existing loop, runs every `reprice_interval_seconds`), re-price the limit to the CURRENT mid. Skip the IB amend if rounded-to-tick price is unchanged (existing dedup logic).
- **Never walk toward the touch.** This is the key distinction from
  the existing `mid` strategy: the walker tightens by stepping toward
  bid/ask over `reprice_steps`; `mid_only` does not.
- Timeout (10 s): if the order isn't filled, cancel and signal abort.
- Caller (the strategy) reacts to the abort by transitioning the entry
  watch state back to TOUCHED so the same line can still re-fire on a
  subsequent confirmation event, instead of consuming the setup.

Implementation: new branch in `engine/order.py`'s `_place_*` dispatcher
that starts a NO-WALK variant of the reprice loop. Existing tests for
the walking loop are unaffected (different code path).

## Position lifecycle

Once filled, the entry transitions the bot to `AWAITING_EXIT_TRIGGER`
and starts the tick-level monitor:

```
pseudo-code, runs on every tick:
  pnl = realized_when_filled (= 0 initially)
        + unrealized_at_current_tick
  hwm  = max(hwm_so_far, pnl)

  if not trail_active and hwm >= +$20:
     trail_active = True
     trail_stop_pnl = hwm - $5   # +$15 at activation

  if trail_active:
     trail_stop_pnl = max(trail_stop_pnl, hwm - $5)
     if pnl <= trail_stop_pnl:
        fire_exit("trail_stop")
        return

  if not trail_active and pnl <= -$10:
     fire_exit("hard_sl")
     return
```

Exits use the existing **walking `mid`** strategy — aggressive enough
to fill, no special handling required.

On exit fill, the strategy transitions to `AWAITING_ENTRY_TRIGGER`
with `cooldown_until = now` (i.e. immediate re-arm). All entry-side
watches re-initialize from the latest fuzzy-line payload.

## Bot config skeleton

```yaml
# config/bots/chart-bot-6.yaml (proposed)
id: chart-bot-6
name: "Tick Fuzzy · MES"
strategy: strategy_bot
broker: ib
tick_interval_seconds: 1     # poll cadence — tick handler is event-driven, this is the supervisor heartbeat
manual_entry_only: true      # start safe — bot evaluates and emits SKIPs to audit; only Force-LONG / -SHORT actually trade
auto_start: false
config:
  strategy_name: tick_fuzzy
  strategy_config: config/strategies/tick_fuzzy.yaml
  ref_id: chart-bot-6-mes
  symbol: MESM6
  sec_type: FUT
  contract_multiplier: 5
  stop_on_exit: false
  cooldown_seconds: 0
symbols:
- MESM6
```

```yaml
# config/strategies/tick_fuzzy.yaml (proposed)
# All thresholds in USD per contract — strategy converts to ticks at
# the engine boundary using contract_multiplier.
entry_hold_seconds: 10
entry_bounce_ticks: 5
mid_fill_timeout_seconds: 10
initial_sl_dollars: 10
trail_activation_dollars: 20
trail_giveback_dollars: 5
# Line selection — final shape TBD pending live-chart observation
# (ADR open question). Conservative defaults below; expect to tune
# after first session.
min_line_score: 0.5
max_watched_lines: 5
```

## Open questions (deferred)

1. **Line selection criteria** — which subset of `detect_fuzzy`'s
   lines do we watch? Top-N by score? Only channel boundaries? Only
   lines that have respected ≥ K seconds? Decision deferred until
   operator has watched the live overlay for a session.

2. **Curve-based entries** — the polynomial trajectory curve is
   currently visualization-only. A future revision could add a watch
   state on the curve itself (e.g. trade when price touches the curve
   after holding away from it). Out of scope for v1.

3. **Symbol scaling** — running tick_fuzzy on MGC, MCL, ESM6 with the
   same dollar parameters. Tick math is symbol-agnostic at the engine
   boundary; just needs config rows. Schedule after MES proves out.

## Testing approach

- **Unit tests** for the entry FSM: simulate tick sequences and
  assert TOUCHED → CONFIRMED transitions fire at the right moments.
  Cover all the corners: hold-timer reset on breach, bounce-trigger
  race vs hold-timer, multi-line CONFIRMED race.
- **Unit tests** for the position lifecycle: P&L computation, trail
  activation, ratcheting, hard SL.
- **Mid_only order test** in the engine's order-placement test suite.
- **Live smoke** on MES with `manual_entry_only=true` first — the bot
  emits SKIP/CONFIRMED audit rows on every line touch so the operator
  can validate the strategy's decisions against their own eye before
  flipping `manual_entry_only=false`.

## Consequences

- **First tick-driven strategy.** Pipeline + audit + persistence
  middleware all run unchanged; the strategy decides on tick events
  rather than bar-close events. This validates the runtime supports
  both timeframes.
- **New `mid_only` order-strategy** is a small, contained engine
  addition. Existing `mid` walker is unchanged.
- **No bar-close evaluation** means the strategy doesn't need the
  bar-aggregator subscription for entry decisions. (It still needs
  bars for the FUZZY-LINE detector via the existing
  `/api/sr/fuzzy` endpoint, but the line set is refreshed periodically,
  not on every bar.)
- **Dollar-denominated parameters** decouple the strategy from
  symbol-specific tick sizes. Same YAML works on MES/MNQ/MGC/MCL
  with only `contract_multiplier` and `symbol` changes.
- **Manual-entry-only default** gives the operator a non-trading
  observation period to validate the strategy's behavior before
  enabling auto entries.

## See also

- ADR 016 (collapse FSM into bot methods) — the lifecycle hooks the
  tick handler ties into.
- `ib_trader/signals/fuzzy_lines.py` — line / pivot / channel
  detector this strategy consumes.
- `ib_trader/engine/order.py` — the `_place_mid_order` function that
  the new `mid_only` branch will sit alongside.
