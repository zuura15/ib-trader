/**
 * Find unbroken trend lines in a window of OHLC bars on a line chart.
 *
 * Algorithm — convex hull peeling (Andrew's monotone chain) +
 * hybrid wick/close validation.
 *
 *   1. Detect pivot lows/highs on bar.close using a strict 3/3
 *      symmetric extremum window (line chart draws closes; pivots
 *      on closes preserve visual anchor-on-line alignment).
 *   2. Lower convex hull of pivot lows = candidate support edges.
 *      Upper convex hull of pivot highs = candidate resistance
 *      edges. Each hull edge is, by definition, a line below
 *      (above) which no other pivot sits, so it satisfies the
 *      pivot-level channel rule by construction.
 *   3. Peel: drop hull vertices, recurse one more layer to surface
 *      sub-trends that don't touch the outermost extremes. Depth=2
 *      is the visual sweet spot — deeper is noise.
 *   4. Per edge, validate against ALL bar data (not just pivots):
 *        • Channel rule: bar.low (support) / bar.high (resistance)
 *          between anchors must not pierce the line by more than
 *          ``tolerance``. A wick poke between anchors is a real
 *          test the close-only check would miss.
 *        • Touches: same-side pivot wicks within tolerance count.
 *          Anchor pair always counts as 2.
 *        • Break (post bIdx): close-basis (textbook). Confirmation
 *          requires either two consecutive closes past the line or
 *          a single close past by 2× tolerance — otherwise a
 *          single-bar wobble silently kills a multi-hour line.
 *   5. Span-aware tolerance: ``avgClose * frac + atrEstimate *
 *      sqrt(span / 60)``. Multi-hour lines get more breathing room
 *      than minute-scale ones; matches what the eye does.
 *   6. Dedup near-identical lines by line price at TWO reference
 *      indices (slice midpoint AND rightmost). Single-point dedup
 *      collapses lines with identical midpoints but divergent
 *      slopes.
 *   7. Rank by composite score: ``touches * log(span) * (broken ?
 *      0.3 : 1)``. Cap output at 6 per side.
 *
 * Why hull peeling instead of brute pair enumeration: same coverage
 * (every line a pair-enum would emit is either a hull edge in some
 * peel layer or has the same anchors as one). Hull is O(N log N) per
 * layer vs O(N²) for pair enum, and hull edges come pre-sorted by
 * structural significance — outermost first, sub-trends second.
 *
 * Reference: Andrew's monotone chain — sort by x, scan left-to-right
 * popping non-CCW turns for the lower hull (non-CW for upper).
 * https://en.wikibooks.org/wiki/Algorithm_Implementation/Geometry/Convex_hull/Monotone_chain
 *
 * The caller slices ``bars`` to the chart's currently visible time
 * range before calling, so detection scope = "what the user is
 * looking at." Zooming out re-detects over the wider slice.
 */

import type { Bar } from './chartUtils';

export interface SRLine {
  type: 'support' | 'resistance';
  /** Bar index of the earliest pivot the line passes through. */
  fromIdx: number;
  /** Bar index of the more-recent pivot that anchors the line. The
   *  line is geometrically defined by ``fromIdx`` and ``anchorBIdx``;
   *  ``toIdx`` only governs how far the line is rendered. */
  anchorBIdx: number;
  /** Bar index where the line should stop being drawn — either the
   *  last bar of the slice (active) or the bar at which the line was
   *  broken (kept on screen for ``breakStaleBars``). */
  toIdx: number;
  /** Line in (idx, price) space: ``price = slope * idx + intercept``. */
  slope: number;
  intercept: number;
  /** Number of actual touches (>= ``minTouches``). */
  touches: number;
  /** Bar index at which a close violated the line (start of the
   *  confirmed run), or ``null`` if the line is still being respected. */
  breakIdx: number | null;
}

export interface SROptions {
  /** Bars to the LEFT a candidate pivot must be lower (high) /
   *  higher (low) than. */
  pivotLeft: number;
  /** Bars to the RIGHT — confirmation that the trend reversed.
   *  2/2 symmetric is the tested default. Strict-extremum pivot
   *  detection structurally fails when consecutive same-side
   *  pivots fall inside each other's window: a lower-low N bars
   *  away from a higher-low gets disqualified by the higher-low
   *  in its left window. 2/2 pushes that limit out to 3-bar
   *  spacing (9 min on a 3-min chart). */
  pivotRight: number;
  /** Base tolerance as a fraction of average close. */
  toleranceFraction: number;
  /** Minimum touch count for a valid line (anchor pair counts as 2). */
  minTouches: number;
  /** Bars to keep a broken line visible after the violating close. */
  breakStaleBars: number;
  /** How many hull layers to peel. 1 = outer envelope only.
   *  2 = outer + one sub-trend layer (default). */
  peelDepth: number;
  /** Consecutive closes past the line required to confirm a break.
   *  A single close past by 2× tolerance also confirms — whichever
   *  fires first. Higher = more break tolerance. */
  breakConfirmBars: number;
}

export const SR_DEFAULTS: SROptions = {
  // 2/2 catches consecutive lower-highs / higher-lows spaced 3 bars
  // apart (9 min on a 3-min chart). 3/3 missed any such pair because
  // each pivot's 3-bar window reached back into the previous extremum.
  // The cost is more candidate pivots = a couple more hull edges per
  // zoom; hull peeling + dedup absorb that.
  pivotLeft: 2,
  pivotRight: 2,
  toleranceFraction: 0.0015,
  minTouches: 2,
  breakStaleBars: 20,
  peelDepth: 2,
  // Single close past tolerance = break. The span-aware tolerance is
  // already wide enough to forgive real wobbles (they stay inside the
  // band); requiring a second confirming bar swallows clean single-bar
  // breaches like a sharp candle that closes well below support and
  // bounces. With `Broken: off` toggled in the UI, transient breaks
  // hide for the breakStaleBars lifetime and reappear if respected.
  breakConfirmBars: 1,
};

/** Strict-extremum pivot detection. ``i`` is a pivot if it is
 *  STRICTLY more extreme than every neighbor in the surrounding
 *  ``left + right`` window. */
function findPivots(
  values: number[],
  left: number,
  right: number,
  type: 'high' | 'low',
): number[] {
  const out: number[] = [];
  for (let i = left; i + right < values.length; i++) {
    const v = values[i];
    let isPivot = true;
    for (let j = i - left; j <= i + right; j++) {
      if (j === i) continue;
      const u = values[j];
      if (type === 'high' ? u >= v : u <= v) { isPivot = false; break; }
    }
    if (isPivot) out.push(i);
  }
  return out;
}

/** 2D cross product of (a-o) and (b-o) in (idx, price) space.
 *  Sign convention:
 *    > 0 → counter-clockwise (left turn)
 *    < 0 → clockwise (right turn)
 *    = 0 → collinear */
function cross(
  ox: number, oy: number,
  ax: number, ay: number,
  bx: number, by: number,
): number {
  return (ax - ox) * (by - oy) - (ay - oy) * (bx - ox);
}

/** Lower convex hull via Andrew's monotone chain. Input pivot
 *  indices must be ascending. Returns the subset that lies on the
 *  lower envelope, in ascending order. */
function lowerHull(pivotIdx: number[], y: number[]): number[] {
  const h: number[] = [];
  for (const i of pivotIdx) {
    while (h.length >= 2) {
      const o = h[h.length - 2];
      const a = h[h.length - 1];
      // Pop while the last turn is clockwise or collinear — those
      // points are above the candidate lower-hull edge from o→i.
      if (cross(o, y[o], a, y[a], i, y[i]) <= 0) h.pop();
      else break;
    }
    h.push(i);
  }
  return h;
}

/** Upper convex hull via Andrew's monotone chain. Mirror of
 *  lowerHull — pop on counter-clockwise / collinear. */
function upperHull(pivotIdx: number[], y: number[]): number[] {
  const h: number[] = [];
  for (const i of pivotIdx) {
    while (h.length >= 2) {
      const o = h[h.length - 2];
      const a = h[h.length - 1];
      if (cross(o, y[o], a, y[a], i, y[i]) >= 0) h.pop();
      else break;
    }
    h.push(i);
  }
  return h;
}

/** Build a validated SRLine from a hull edge, or return null if the
 *  line fails wick-channel / break-confirmation checks.
 *
 *  Two tolerances:
 *   - ``tolerance`` is wide (span-aware). Used for the channel rule
 *     and touch counting so multi-hour lines aren't killed by every
 *     bar that drifts a few dollars off the line.
 *   - ``breakTolerance`` is tight (base fraction only). Used for
 *     break detection — a real breach should fire even if the
 *     price excursion is modest. Without this split a clear
 *     breach inside the wider tolerance band silently leaves the
 *     line marked active. */
function validateEdge(
  bars: Bar[],
  closes: number[],
  pivots: number[],
  type: SRLine['type'],
  aIdx: number,
  bIdx: number,
  tolerance: number,
  breakTolerance: number,
  opts: SROptions,
): SRLine | null {
  const slope = (closes[bIdx] - closes[aIdx]) / (bIdx - aIdx);
  const intercept = closes[bIdx] - slope * bIdx;
  const lineAt = (i: number) => slope * i + intercept;
  const lastBarIdx = bars.length - 1;

  // Wick channel rule between anchors. A wick that pierces the
  // line by > tolerance invalidates the line at inception.
  for (let i = aIdx + 1; i < bIdx; i++) {
    const tested = type === 'support' ? bars[i].low : bars[i].high;
    const expected = lineAt(i);
    const violates = type === 'support'
      ? tested < expected - tolerance
      : tested > expected + tolerance;
    if (violates) return null;
  }

  // Break detection on closes after bIdx with confirmation. Uses
  // breakTolerance (tight) so modest-but-clear breaches register;
  // the wider channel/touch tolerance would silently swallow them.
  // ``breakConfirmBars`` consecutive closes past the line confirm
  // the break; breakIdx points at the FIRST close of that run.
  let breakIdx: number | null = null;
  let runStart = -1;
  let runLen = 0;
  for (let i = bIdx + 1; i <= lastBarIdx; i++) {
    const expected = lineAt(i);
    const c = bars[i].close;
    const past = type === 'support'
      ? c < expected - breakTolerance
      : c > expected + breakTolerance;
    if (past) {
      if (runLen === 0) runStart = i;
      runLen += 1;
      if (runLen >= opts.breakConfirmBars) { breakIdx = runStart; break; }
    } else {
      runLen = 0;
    }
  }
  if (breakIdx !== null && lastBarIdx - breakIdx > opts.breakStaleBars) {
    return null;
  }

  // Touch counting on PIVOT wicks within tolerance of the line.
  // Counting at non-pivot bars over-rewards flat ranges; pivots are
  // the structural turn points the user reads as "tested the line."
  let touches = 2;
  const touchEnd = breakIdx ?? lastBarIdx;
  for (const p of pivots) {
    if (p === aIdx || p === bIdx) continue;
    if (p < aIdx || p > touchEnd) continue;
    const tested = type === 'support' ? bars[p].low : bars[p].high;
    const expected = lineAt(p);
    if (Math.abs(tested - expected) <= tolerance) touches += 1;
  }
  if (touches < opts.minTouches) return null;

  return {
    type,
    fromIdx: aIdx,
    anchorBIdx: bIdx,
    toIdx: breakIdx ?? lastBarIdx,
    slope, intercept,
    touches,
    breakIdx,
  };
}

function detectOneSide(
  bars: Bar[],
  type: SRLine['type'],
  opts: SROptions,
  tolerance: number,
  breakTolerance: number,
): SRLine[] {
  const closes = bars.map((b) => b.close);
  const pivots = findPivots(
    closes, opts.pivotLeft, opts.pivotRight,
    type === 'resistance' ? 'high' : 'low',
  );
  if (pivots.length < 2) return [];

  const lastBarIdx = bars.length - 1;
  const candidates: SRLine[] = [];

  // Hull peeling: outer envelope first, then sub-trends after
  // dropping the outer hull's vertices. Each hull edge becomes a
  // candidate line.
  let remaining = pivots.slice();
  for (let depth = 0; depth < opts.peelDepth; depth++) {
    if (remaining.length < 2) break;
    const hull = type === 'support'
      ? lowerHull(remaining, closes)
      : upperHull(remaining, closes);

    for (let h = 0; h < hull.length - 1; h++) {
      const line = validateEdge(
        bars, closes, pivots, type, hull[h], hull[h + 1],
        tolerance, breakTolerance, opts,
      );
      if (line) candidates.push(line);
    }

    if (hull.length < 2) break;
    const hullSet = new Set(hull);
    remaining = remaining.filter((p) => !hullSet.has(p));
  }

  if (candidates.length === 0) return [];

  // Dedup: two lines collapse only if ALL THREE hold:
  //   • Same slope sign (one is up, one is down → keep both).
  //   • Slopes within 15% relative — visually parallel lines can have
  //     similar prices at reference points but represent different
  //     trends; only true duplicates have near-identical slopes.
  //   • Predicted prices match at slice midpoint AND rightmost within
  //     1× tolerance — tighter than before to avoid collapsing two
  //     genuinely-different trend lines that happen to overlap in
  //     price at those reference points.
  // Earlier the threshold was 2× tolerance with no slope check, which
  // was eating valid sub-trends parallel to a stronger outer trend.
  const refMid = lastBarIdx / 2;
  const refRight = lastBarIdx;
  const kept: SRLine[] = [];
  for (const c of candidates) {
    const cMid = c.slope * refMid + c.intercept;
    const cRight = c.slope * refRight + c.intercept;
    const dup = kept.find((k) => {
      if (Math.sign(k.slope) !== Math.sign(c.slope)) return false;
      const slopeAvg = (Math.abs(k.slope) + Math.abs(c.slope)) / 2;
      const slopeDiff = Math.abs(k.slope - c.slope);
      // Avoid div-by-zero on flat lines: fall back to absolute test.
      const slopeRel = slopeAvg > 1e-6 ? slopeDiff / slopeAvg : slopeDiff;
      if (slopeRel > 0.15) return false;
      const kMid = k.slope * refMid + k.intercept;
      const kRight = k.slope * refRight + k.intercept;
      return Math.abs(kMid - cMid) <= tolerance
        && Math.abs(kRight - cRight) <= tolerance;
    });
    if (!dup) {
      kept.push(c);
      continue;
    }
    // Prefer more touches; tiebreak on earlier fromIdx (longer-running).
    if (
      c.touches > dup.touches
      || (c.touches === dup.touches && c.fromIdx < dup.fromIdx)
    ) {
      kept.splice(kept.indexOf(dup), 1, c);
    }
  }

  // Composite score: more touches × longer span × (active-or-broken).
  // log(span) prevents very long but few-touch lines from dominating.
  const score = (l: SRLine): number => {
    const span = Math.max(2, l.toIdx - l.fromIdx);
    const broken = l.breakIdx != null ? 0.3 : 1.0;
    return l.touches * Math.log(span) * broken;
  };
  kept.sort((a, b) => score(b) - score(a));

  return kept.slice(0, 6);
}

/** Span-aware tolerance: short lines get tight tolerance (chart
 *  noise floor); long lines pick up an additive sqrt(span) term so
 *  cumulative drift over hours doesn't disqualify visually-clean
 *  multi-touch trends.
 *
 *  ``baseFrac * avgClose`` matches the previous fixed tolerance.
 *  The ``atr * sqrt(span / 60)`` term adds ~1 ATR for 60 bars (3 h
 *  on a 3-min chart), ~1.4 ATR for 120 bars, etc. */
function spanTolerance(
  baseFrac: number,
  avgClose: number,
  atr: number,
  spanBars: number,
): number {
  return baseFrac * avgClose + atr * Math.sqrt(spanBars / 60);
}

export function detectSupportResistance(
  bars: Bar[],
  options: Partial<SROptions> = {},
): SRLine[] {
  const opts: SROptions = { ...SR_DEFAULTS, ...options };
  if (bars.length < opts.pivotLeft + opts.pivotRight + 1) return [];

  const closes = bars.map((b) => b.close);
  const avgClose = closes.reduce((s, v) => s + v, 0) / closes.length;
  // ATR-ish: mean absolute close-to-close move. Cheap and stable;
  // a true Wilder ATR would also factor wicks but isn't worth the
  // extra cost at this resolution.
  let atr = 0;
  for (let i = 1; i < closes.length; i++) atr += Math.abs(closes[i] - closes[i - 1]);
  atr = atr / Math.max(1, closes.length - 1);

  const tolerance = spanTolerance(
    opts.toleranceFraction, avgClose, atr, bars.length,
  );
  // Break tolerance is the BASE fraction only (no ATR widening). A
  // real breach should fire even when modest; the wider channel/touch
  // tolerance was forgiving real breaks of size ~baseFrac because
  // span-aware widening pushed the break threshold beyond the actual
  // price excursion.
  const breakTolerance = avgClose * opts.toleranceFraction;
  if (!(tolerance > 0) || !(breakTolerance > 0)) return [];

  return [
    ...detectOneSide(bars, 'resistance', opts, tolerance, breakTolerance),
    ...detectOneSide(bars, 'support', opts, tolerance, breakTolerance),
  ];
}
