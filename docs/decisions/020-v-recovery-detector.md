# ADR 020: On-Demand Multi-Scale V-Recovery Detector for Sell-to-Open Gating

**Date:** 2026-05-23  
**Status:** Proposed for review — implementation in PR #XXX (branch `feature/v-recovery-detector-v1`)

## Context

When a bot strategy is about to submit a sell-to-open (short) order it must have the option to refuse if price is currently in the recovery phase of one or more V-shaped bottoms. The same logic applies symmetrically for buy-to-open orders on inverted-V recoveries.

Requirements collected during design discussions:

- The detector is **on-demand only** — it is invoked by the bot immediately before order submission, not on every bar.
- Maximum lookback is a **hard internal cap of 48 hours** (not supplied by the caller and not tunable via settings.yaml).
- The detector must surface **multiple** Vs at different scales inside the 48-hour window (a 40-minute V and a 7-hour V can both be active and must both be reported).
- **Flattish / slow recoveries must qualify**. No momentum, slope, consecutive-up-bar, or "sharp bounce" requirements are allowed. A gradual grind higher after a material drop is still a V-recovery for gating purposes.
- Rich **diagnostics** must be produced for the just-completed bar so the operator can see exactly why a short was blocked:
  ```
  V: 09:15(s38), 11:42(m61), 14:03(L29)
  ```
  Each entry shows the trough timestamp (HH:mm) and a strength token that encodes both **depth** of the impulse leg and **recovery ratio** achieved so far.
- High recall is explicitly preferred: it is acceptable to skip some valid short setups to avoid entering during any detected V-recovery.

The implementation must be a fresh module. It deliberately does not reuse or extend any existing pivot detection, fuzzy lines, regime classifier, trajectory curves, or SR fan code.

## Decision

Introduce a new, self-contained, pure module:

**`ib_trader/bots/strategies/v_recovery.py`**

The public surface for v1 is intentionally small:

```python
def detect_v_recoveries(
    bars: list[dict],
    *,
    min_depth_pct: float = 0.012,
    min_recovery_ratio: float = 0.25,
    max_horizon_hours: int = 48,   # internal hard cap — do not change lightly
) -> tuple[bool, str]:
    """
    Returns (has_active_v, diagnostic_line_or_empty).
    The diagnostic line (when non-empty) has the form:
        V: 09:15(s38), 11:42(m61), 14:03(L29)
    """
```

### Input expectations (v1)

- `bars`: list of dicts in chronological order, each containing at minimum:
  - `"ts"`: ISO-8601 string or datetime (the bar's close time)
  - `"close"`: numeric price
- The caller (bot strategy) is responsible for fetching the bars (via `/engine/history` or equivalent) with whatever `bar_size` and `hours` it considers relevant (up to 48 h). The detector will internally truncate to the hard 48-hour cap from the last bar.

### Strength encoding (v1)

Each reported trough uses the compact token `LetterRecovery%`:

- **Letter (depth bucket)** — percentage drop from its reference peak to the trough, measured against the peak price:
  - `s` : 0.8 % – 1.8 %  (small)
  - `m` : 1.8 % – 3.5 %  (medium)
  - `L` : > 3.5 %       (large)
- **Number** — integer recovery ratio `(current_close - trough) / (peak - trough) * 100`

Example:
- `09:15(s38)` → small-depth V whose bottom was at 09:15, 38 % of the drop has been recovered.
- `14:03(L29)` → large-depth V, only 29 % recovered so far (still early in the recovery leg).

The thresholds above are starting values for v1 and are easy to adjust after live observation.

### Flattish recoveries

A trough qualifies for the diagnostic (and therefore blocks the S order) as soon as:
- Its depth bucket is at least `s`, **and**
- The recovery ratio from that trough to the current bar meets or exceeds `min_recovery_ratio`.

No additional conditions on bar-to-bar slope, number of up bars, or volatility are applied. This satisfies the "even flattish recoveries" requirement.

### Multiple scales

Because the search examines the entire capped 48-hour slice and records every qualifying (peak, trough) pair that still shows sufficient recovery at call time, both short-horizon and long-horizon Vs appear naturally in the same diagnostic line without any pre-declared list of windows.

### Diagnostics contract

- When no active V-recovery exists inside the cap: the function returns `(False, "")`.
- When one or more exist: the function returns `(True, "V: 09:15(s38), 11:42(m61), ...")`.
- The line always uses the local/session time of the trough bar (HH:mm of the bar's timestamp).
- The list is sorted by trough time (oldest first).

The bot strategy that calls the detector is expected to:
1. Log the diagnostic line at INFO level when it is non-empty.
2. Skip the sell-to-open submission for this decision cycle.

### 48-hour hard cap

`MAX_HORIZON_HOURS = 48` is a module-level constant. The detector never reasons about bars older than this, even if the caller supplies a longer series. This bound makes the (deliberately heavy) search safe for an on-demand call.

## Consequences

### Positive
- Single place owns the "do not short into V recovery" policy.
- Rich operator-visible diagnostics on every attempted S order.
- High recall by design; easy to tune later toward more precision if too many valid shorts are being suppressed.
- Completely independent of all existing signal / regime machinery.

### Risks / follow-ups
- The initial depth and recovery thresholds are starting points only. Live MES / equity / futures data will be needed to calibrate them.
- Deduplication of nearby troughs (micro-Vs inside a larger bottom) is deliberately minimal in v1. If the diagnostic becomes noisy we will add a "minimum separation" or "most significant trough per cluster" rule in a follow-up.
- The same module will later expose an inverted-V helper for long-entry gating; the core logic is symmetric.

## Implementation notes for reviewers

- The module contains only standard library + `datetime` / `zoneinfo` (no numpy, pandas, scipy, or project signal libraries).
- All logic is exercised via the returned diagnostic string and the boolean; there are no side effects.
- A unit-test skeleton exercising the diagnostic formatter and the 48 h truncation is included in the PR.

This ADR + the accompanying implementation constitute the CR candidate for the first production-ready version of the V-recovery gate.