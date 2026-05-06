# Support / Resistance Trend-Line Algorithm

This document describes the auto-detection algorithm in
`supportResistance.ts` and the rendering integration in
`SymbolChart.tsx`.

## Goal

Given the close polyline of a line chart over a visible time window,
produce **all unbroken support trend lines** that:

1. Pass through at least two pivot lows.
2. Have no close on the chart polyline that goes *below* the line
   between their two anchors (the "channel rule" — line must not be
   intersected by the price).
3. Have not crossed any already-emitted trend line within their
   overlapping drawn range.

Resistance is the mirror — pivot highs, no close above, etc. Both
sides are wired in. Each runs independently (separate emitted-lines
arrays); a support and a resistance can cross (triangle apex /
converging channel) without cross-validating.

## Definitions

- **Pivot low**: a bar whose close is *strictly* less than both its
  immediate neighbors (1/1 detection — the simplest definition,
  appropriate for a magnetic-mode line chart). The first and last
  bars are never pivots (only one neighbor).
- **Anchor pair**: an older pivot `Q` and a more-recent pivot `P`
  that together define a line.
- **Channel rule**: between `Q` and `P`, no close is below
  `slope * idx + intercept` by more than a small floating-point
  epsilon (`1e-6`).
- **Break**: the first bar after `P` whose close goes below the
  line. The line is rendered as "broken" until `breakStaleBars`
  bars after that breach, then dropped.
- **Magnetic mode**: no tolerance is applied to channel-rule or
  break checks beyond `EPS = 1e-6` for floating-point safety. A
  $0.01 dip below the line *will* invalidate / break it. Tolerance
  may be added later as a single number in the validator.

## Algorithm

For each side (currently support only):

```
pivots ← findPivots(closes, type)         # 1/1 strict
emitted ← []                              # already-emitted lines

for pi from len(pivots)-1 downto 1:       # newest → oldest
    P ← pivots[pi]
    candidates ← []
    for qi from pi-1 downto 0:
        Q ← pivots[qi]
        slope ← (closes[P] - closes[Q]) / (P - Q)
        candidates.push({Q, slope})

    # Steepest first → "rotate clockwise from vertical at P"
    sort candidates by slope DESC for support  (ASC for resistance)

    for each {Q, slope} in candidates:
        intercept ← closes[P] - slope * P

        # Channel rule against the chart polyline
        for i from Q+1 to P-1:
            if closes[i] < slope*i + intercept - EPS:
                skip this candidate

        # Channel rule against already-emitted lines
        if line(slope, intercept) crosses any emitted line in [Q, P]:
            skip this candidate

        # Break detection — closes after P
        breakIdx ← first i in (P, lastBarIdx] where
                   closes[i] < slope*i + intercept - EPS

        # Break detection — emitted-line crossing after P
        xCross ← earliest i in (P, lastBarIdx] where
                 line crosses any emitted line at i
        breakIdx ← min(breakIdx, xCross)   # whichever comes first

        if breakIdx is recent enough (within breakStaleBars):
            emit {fromIdx=Q, anchorBIdx=P, slope, intercept, breakIdx}
        else if breakIdx is null:
            emit unbroken
        else:
            drop (stale break)
```

### Why most-recent → oldest?

A user reads the chart starting from "now" and the most-recent
structure is what's actively being respected or tested. Newer
pivots' fans get to claim space first; older fans add lines that fit
into the remaining space (constrained by `crossesAny` against
already-emitted lines).

### Why steepest first within a pivot's fan?

The user's mental model: "from the most-recent pivot, draw a line
parallel to the Y axis (vertical) and rotate clockwise. Each pivot
the line touches is a 2-point trend line. Continue rotating clockwise
to find shallower sub-trends through the same anchor."

Sweeping clockwise from vertical means slopes go from `+∞` to `0`
to `-∞`. For support, we sort `DESC`. For resistance (mirror), we
sort `ASC` — sweep counter-clockwise from vertical-down.

### Dedup of coincident lines

Two distinct fans (different P anchors) producing nearly-parallel
*but distinct* lines are kept separately — they're different trends.

But three or more **collinear** pivots produce *identical* lines
through every (Q, P) pair, and `crossesAny` deliberately ignores
parallel pairs (no crossing). We dedup at emission time: skip a
candidate whose `(slope, intercept)` matches any already-emitted
line within `EPS`. Combined with a tiebreak that prefers the older
`Q` first, this keeps the longest line and drops shorter
duplicates.

## Implementation map

### `supportResistance.ts`

- `findPivots(values, type)` — 1/1 strict-extremum scan.
- `cross(o, a, b)` — 2D cross product; sign tells CCW vs CW.
- `crossesAny(slope, intercept, fromIdx, toIdx, others)` — strict
  channel-rule check against existing lines. Returns true if the
  candidate intersects any other line in the overlap of their drawn
  ranges.
- `earliestCrossing(slope, intercept, fromIdx, toIdx, others)` —
  earliest intersection point with any existing line; used as a
  break-detection signal post-anchor.
- `detectOneSide(closes, pivots, type, opts)` — main loop. Walks
  pivots newest → oldest, builds fan per pivot, validates against
  chart polyline + already-emitted lines.
- `detectSupportResistance(bars, options)` — entry point. Strips
  bars to closes, finds pivots, calls `detectOneSide`. Resistance is
  currently disabled; flipping it on is a 1-line uncomment.

### `SymbolChart.tsx`

- `recomputeSr()` — slices bars to the visible time window, calls
  `detectSupportResistance`, draws each returned line as its own
  `LineSeries` with `autoscaleInfoProvider: () => null` (so SR
  lines don't affect the price-scale auto-fit).
- `scheduleSrRecomputeRef` — throttled wrapper (1s leading +
  trailing edge). Fired by zoom/pan/tick events and by the
  broken-toggle effect (with `force=true` to bypass the
  `SR_MIN_BARS` early-exit). The throttle tracks a high-water
  `pendingForce` across the window so a `force=true` call placed
  after a `force=false` queued the trailing timer still wins —
  without that, the toggle could silently no-op at narrow zoom.
- `srHiddenRef` — "Clear S/R" button toggle. Hides without
  re-detecting.
- `showBrokenSrRef` — "Broken: on/off" toggle. Filters out lines
  with `breakIdx != null` when off.

### Rendering

- Active line (`breakIdx == null`): solid color (green for support,
  reserved for resistance).
- Tentative (`touches < 3`): dotted blue.
- Broken (`breakIdx != null`, within `breakStaleBars`): dashed amber.
  Hidden by default; shown when "Broken: on" toggle is set.

## Edge cases / known behaviors

- **Down-sloping supports through a higher older low**: valid by the
  channel rule when there's an intermediate even-higher close. They
  produce an X-shape with up-sloping fans through the same anchor.
  Intentional.
- **Lines through three or more collinear pivots**: emitted as a
  single 2-point line geometrically; `touches` field is currently
  fixed at 2. (Pre-existing — touch-counting was deferred until
  tolerance is reintroduced.)
- **Below `SR_MIN_BARS` (30 bars / 90 min) zoom**: passive
  zoom/pan triggers don't re-run detection — the lines drawn at
  default zoom persist. Toggle and explicit recomputes (`force`)
  bypass this guard.
- **No tolerance**: a $0.01 dip kills a line. Acceptable for now;
  tolerance will be a single value in the validator (channel rule
  + break detection) once the geometry is locked in.

## Performance

Worst-case complexity is `O(P² · B)` per side, where `P` is pivot
count and `B` is bar count in the visible slice. The `P²` comes
from the candidate enumeration; the `B` comes from the channel-rule
scan between anchors plus the post-anchor break scan. Plus
`O(emitted)` per candidate for `crossesAny` / `earliestCrossing`.

For typical zooms (≤100 pivots, ≤500 visible bars) this is fine.
On wide zoom-outs the runtime grows visibly — if it becomes a
problem, the channel-rule scan can be bounded by index (only check
bars between anchors that are pivots, not every close), and the
crossing checks can be partitioned by drawn-range overlap.

## Future work

- **Tolerance** — wide for channel/touches (so multi-hour lines
  aren't killed by drift), tight for break detection (so real
  breaches register).
- **Pseudo-pivots** — points of high curvature in a strict descent
  (a "shelf"). Detect via second-difference scan; add to candidate
  pivot list.
- **Touch counting** — currently fixed at 2. With tolerance back
  in, count any pivot/close within `tolerance` of the line as a
  touch. Renderer already has solid-vs-dotted styling for ≥3 vs 2
  touches.
- **Sub-trends through older anchors that the most-recent line
  invalidates** — current behavior emits all surviving lines per
  anchor. Once we add a quality score, may want to filter aggressively.
