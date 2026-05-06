/**
 * Trend-line detection on the close polyline (line chart).
 *
 * Algorithm — for each pivot P (most-recent → oldest), enumerate
 * the fan of candidate trend lines through P (one per older pivot
 * Q). Validate each against:
 *   • the chart polyline (no close on the wrong side between the
 *     two anchors)
 *   • all already-emitted trend lines (no crossing within their
 *     overlapping drawn range)
 *
 * Walking pivots most-recent-first means the freshest lines get
 * drawn unconstrained, then older fans are added below/around them.
 * Within a pivot's fan, candidates are sorted by slope so the
 * outermost (steepest) is emitted first and shallower sub-trends
 * fit inside it.
 *
 * Magnetic mode: no tolerance. A close that goes one cent below a
 * support line invalidates it; same for resistance. Floating-point
 * EPS is the only fudge.
 */

import type { Bar } from './chartUtils';

export interface SRLine {
  type: 'support' | 'resistance';
  fromIdx: number;
  anchorBIdx: number;
  toIdx: number;
  slope: number;
  intercept: number;
  touches: number;
  breakIdx: number | null;
}

export interface SROptions {
  /** Bars to keep a broken line visible after the violating close. */
  breakStaleBars: number;
  /** Allowed deviation, as a fraction of the slice's average close,
   *  for channel-rule, touch, and break checks. 0.0005 = 0.05%
   *  (≈$2.35 on gold at $4700, ≈$0.025 on a $50 stock) absorbs
   *  cent-level wobbles without letting macro breaches pass.
   *  Set to 0 (or use EPS) for strict magnetic-mode. */
  toleranceFraction: number;
}

export const SR_DEFAULTS: SROptions = {
  breakStaleBars: 20,
  toleranceFraction: 0.0005,
};

const EPS = 1e-6;

/** 1/1 strict pivot detection on a 1D series. A bar is a pivot iff
 *  its value is strictly more extreme than both immediate neighbors.
 *  First and last bars have only one neighbor → never pivots. */
function findPivots(values: number[], type: 'high' | 'low'): number[] {
  const out: number[] = [];
  for (let i = 1; i < values.length - 1; i++) {
    const v = values[i];
    const l = values[i - 1];
    const r = values[i + 1];
    if (type === 'high' ? (v > l && v > r) : (v < l && v < r)) out.push(i);
  }
  return out;
}

/** Does a candidate line cross any line in ``others`` within the
 *  overlap of their drawn ranges? Two parallel lines never cross
 *  (or coincide; we treat that as no-cross — duplicates are
 *  harmless visually). */
function crossesAny(
  slope: number,
  intercept: number,
  fromIdx: number,
  toIdx: number,
  others: SRLine[],
): boolean {
  for (const e of others) {
    const lo = Math.max(fromIdx, e.fromIdx);
    const hi = Math.min(toIdx, e.toIdx);
    if (lo >= hi) continue;
    const dm = slope - e.slope;
    if (Math.abs(dm) < EPS) continue;
    const x = (e.intercept - intercept) / dm;
    if (x > lo + EPS && x < hi - EPS) return true;
  }
  return false;
}

/** Find the earliest x in [fromIdx, toIdx] where the candidate
 *  crosses any line in ``others``. Returns null if no crossing in
 *  that range. Used to set breakIdx on the post-anchor extension. */
function earliestCrossing(
  slope: number,
  intercept: number,
  fromIdx: number,
  toIdx: number,
  others: SRLine[],
): number | null {
  let earliest: number | null = null;
  for (const e of others) {
    const lo = Math.max(fromIdx, e.fromIdx);
    const hi = Math.min(toIdx, e.toIdx);
    if (lo > hi) continue;
    const dm = slope - e.slope;
    if (Math.abs(dm) < EPS) continue;
    const x = (e.intercept - intercept) / dm;
    if (x < lo - EPS || x > hi + EPS) continue;
    const idx = Math.ceil(x);
    if (idx >= fromIdx && idx <= toIdx) {
      if (earliest === null || idx < earliest) earliest = idx;
    }
  }
  return earliest;
}

/** Build all valid trend lines for one side (support or resistance)
 *  by walking pivots newest → oldest, generating a fan through each
 *  pivot, and validating each candidate against the chart line and
 *  already-emitted lines. */
function detectOneSide(
  closes: number[],
  pivots: number[],
  type: SRLine['type'],
  opts: SROptions,
): SRLine[] {
  const out: SRLine[] = [];
  const lastBarIdx = closes.length - 1;
  // Tolerance as a price-domain band. EPS still guards FP noise on
  // exact-anchor evaluation; tolerance absorbs cent-scale wobbles
  // that would otherwise kill visually-clean lines.
  const avgClose =
    closes.reduce((s, v) => s + v, 0) / Math.max(1, closes.length);
  const tol = Math.max(EPS, avgClose * opts.toleranceFraction);

  // Process pivots most-recent → oldest. Newer pivots' lines are
  // emitted first and constrain older candidates, not the other way
  // around — what's freshly true on the chart sets the priority.
  for (let pi = pivots.length - 1; pi >= 1; pi--) {
    const P = pivots[pi];

    // Fan of candidates: line through (Q, P) for every older Q.
    const candidates: { q: number; slope: number }[] = [];
    for (let qi = pi - 1; qi >= 0; qi--) {
      const q = pivots[qi];
      const slope = (closes[P] - closes[q]) / (P - q);
      candidates.push({ q, slope });
    }
    // Steepest first. Support → desc (large positive first, then 0,
    // then negative). Resistance → asc (large negative first, then
    // 0, then positive). Maps to "rotate clockwise from vertical."
    // Tiebreak: older Q first, so collinear-pivot dedup below keeps
    // the longest line (Q closer to 0) instead of the shortest.
    candidates.sort((a, b) => {
      const dSlope = type === 'support' ? b.slope - a.slope : a.slope - b.slope;
      if (Math.abs(dSlope) > EPS) return dSlope;
      return a.q - b.q;
    });

    for (const { q, slope } of candidates) {
      const intercept = closes[P] - slope * P;

      // Channel rule against the chart polyline between q and P.
      let valid = true;
      for (let i = q + 1; i < P; i++) {
        const lineAt = slope * i + intercept;
        const violates = type === 'support'
          ? closes[i] < lineAt - tol
          : closes[i] > lineAt + tol;
        if (violates) { valid = false; break; }
      }
      if (!valid) continue;

      // Channel rule against existing trend lines between q and P.
      if (crossesAny(slope, intercept, q, P, out)) continue;

      // Post-P break detection on the chart line.
      let breakIdx: number | null = null;
      for (let i = P + 1; i <= lastBarIdx; i++) {
        const lineAt = slope * i + intercept;
        const violates = type === 'support'
          ? closes[i] < lineAt - tol
          : closes[i] > lineAt + tol;
        if (violates) { breakIdx = i; break; }
      }

      // Post-P break detection against existing lines — whichever
      // comes first wins.
      const xCross = earliestCrossing(
        slope, intercept, P + 1, lastBarIdx, out,
      );
      if (xCross !== null && (breakIdx === null || xCross < breakIdx)) {
        breakIdx = xCross;
      }

      if (breakIdx !== null && lastBarIdx - breakIdx > opts.breakStaleBars) {
        continue;
      }

      // Skip if any already-emitted line is coincident (same slope
      // AND same intercept within EPS). Three or more collinear
      // pivots produce identical lines through every (Q, P) pair on
      // them; without this guard the renderer draws overlapping
      // duplicates. ``crossesAny`` deliberately ignores parallel
      // pairs (no crossing point), so the dedup has to live here.
      const isCoincident = out.some(
        (e) => Math.abs(e.slope - slope) < EPS
            && Math.abs(e.intercept - intercept) < EPS,
      );
      if (isCoincident) continue;

      out.push({
        type,
        fromIdx: q,
        anchorBIdx: P,
        toIdx: breakIdx ?? lastBarIdx,
        slope, intercept,
        touches: 2,
        breakIdx,
      });
    }
  }

  return out;
}

export function detectSupportResistance(
  bars: Bar[],
  options: Partial<SROptions> = {},
): SRLine[] {
  const opts: SROptions = { ...SR_DEFAULTS, ...options };
  if (bars.length < 3) return [];

  const closes = bars.map((b) => b.close);
  const lows = findPivots(closes, 'low');
  const highs = findPivots(closes, 'high');

  // Each side runs independently — they don't cross-validate
  // against each other. A support and a resistance can cross
  // (triangle apex / converging channel), which is visually
  // correct, so the ``out`` array in detectOneSide is per-side.
  return [
    ...detectOneSide(closes, lows, 'support', opts),
    ...detectOneSide(closes, highs, 'resistance', opts),
  ];
}
