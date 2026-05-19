/** Fuzzy SR backend adapter (Layer-2 testbed).
 *
 *  Mirrors ``srBackend.ts`` but for the experimental ``/api/sr/fuzzy``
 *  detector: RANSAC fuzzy trendlines + prominence-scored pivots +
 *  parallel channels. Kept separate so the canonical SR fan
 *  rendering pipeline is untouched while we A/B them on the same
 *  chart.
 */
import type { Bar } from './chartUtils';
import { BAR_SECONDS } from './chartUtils';
import { backendTsToChartTime } from './srBackend';

export interface FuzzyPivot {
  idx: number;
  ts: string;
  price: number;
  kind: 'low' | 'high';
  prominence: number;
  width: number;
  rank: number;
}

export interface FuzzyLine {
  type: 'support' | 'resistance';
  slope: number;        // backend bar-index space
  intercept: number;    // backend bar-index space
  from_idx: number;     // backend
  to_idx: number;       // backend
  from_ts: string;
  to_ts: string;
  from_price: number;
  to_price: number;
  inlier_count: number;
  inlier_idxs: number[];
  score: number;        // 0..1
  age_bars: number;
  residual_threshold: number;
}

export interface FuzzyChannel {
  support: FuzzyLine;
  resistance: FuzzyLine;
  width_at_mid: number;
  slope_diff: number;
  span_bars: number;
  score: number;
}

export interface FuzzyCurvePoint {
  ts: string;
  value: number;
}

export interface FuzzyCurve {
  degree: number;
  window_bars: number;
  start_idx: number;
  end_idx: number;
  points: FuzzyCurvePoint[];
  r_squared: number;
  coeffs: number[];
}

export interface FuzzyPayload {
  bars_count: number;
  pivots: FuzzyPivot[];
  lines: FuzzyLine[];
  channels: FuzzyChannel[];
  curve: FuzzyCurve | null;
  config: Record<string, number>;
  warning?: string;
}

/** Rebase a backend line into frontend (allBars) index space.
 *  ``from_price`` is trusted; the new intercept is derived from
 *  ``from_price - slope * frontendFromIdx``. Returns null when the
 *  line's anchor falls outside the bar set. */
export interface FuzzyChartLine {
  type: 'support' | 'resistance';
  fromIdx: number;
  toIdx: number;
  slope: number;          // frontend index space
  intercept: number;
  fromPrice: number;
  toPrice: number;
  inlierCount: number;
  score: number;
}

function idxForTs(allBars: Bar[], backendTs: string | null): number {
  if (!backendTs) return -1;
  const target = backendTsToChartTime(backendTs);
  let bestIdx = -1;
  let bestDelta = Number.POSITIVE_INFINITY;
  for (let i = 0; i < allBars.length; i++) {
    const delta = Math.abs((allBars[i].time as number) - target);
    if (delta < bestDelta) {
      bestDelta = delta;
      bestIdx = i;
    }
  }
  if (bestDelta > BAR_SECONDS) return -1;
  return bestIdx;
}

export function fuzzyLineToChartLine(
  bl: FuzzyLine, allBars: Bar[],
): FuzzyChartLine | null {
  const fromIdx = idxForTs(allBars, bl.from_ts);
  const toIdx = idxForTs(allBars, bl.to_ts);
  if (fromIdx < 0 || toIdx < 0) return null;
  const slope = bl.slope;
  const intercept = bl.from_price - slope * fromIdx;
  const toPrice = slope * toIdx + intercept;
  return {
    type: bl.type,
    fromIdx,
    toIdx,
    slope,
    intercept,
    fromPrice: bl.from_price,
    toPrice,
    inlierCount: bl.inlier_count,
    score: bl.score,
  };
}

export async function fetchFuzzy(
  target: { conId: number | null; symbol: string; secType: string },
  hours: number = 8,
  barSize: string = '3 mins',
): Promise<FuzzyPayload | null> {
  const params = new URLSearchParams({
    hours: String(Math.max(1, Math.min(72, Math.ceil(hours)))),
    bar_size: barSize,
  });
  if (target.conId != null) {
    params.set('con_id', String(target.conId));
  } else {
    params.set('symbol', target.symbol);
    params.set('sec_type', target.secType);
  }
  try {
    const r = await fetch(`/api/sr/fuzzy?${params.toString()}`);
    if (!r.ok) return null;
    return (await r.json()) as FuzzyPayload;
  } catch {
    return null;
  }
}
