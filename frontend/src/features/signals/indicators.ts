/** Shared building blocks for the recommendation rules (GH #102). */
import type { Bar } from '../chart/chartUtils';
import type { Pivot } from './types';

/** Wilder ATR. Index i covers bars[0..i]; null until ``period`` bars. */
export function computeAtr(bars: Bar[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(bars.length).fill(null);
  if (bars.length === 0 || period < 1) return out;
  let atr = 0;
  for (let i = 0; i < bars.length; i++) {
    const b = bars[i];
    const prevClose = i > 0 ? bars[i - 1].close : b.close;
    const tr = Math.max(
      b.high - b.low, Math.abs(b.high - prevClose), Math.abs(b.low - prevClose),
    );
    if (i < period) {
      atr += tr;
      if (i === period - 1) {
        atr /= period;
        out[i] = atr;
      }
    } else {
      atr = (atr * (period - 1) + tr) / period;
      out[i] = atr;
    }
  }
  return out;
}

/**
 * Fractal swing pivots: bar j is a pivot high when its high is the
 * highest of the ``left`` bars before and ``right`` bars after it (ties
 * go to the earlier bar). A pivot at j is only known at bar j + right —
 * callers must ignore pivots with ``idx > i - right`` when evaluating
 * bar i, or the rules would see the future.
 */
export function findPivots(
  bars: Bar[], left: number, right: number,
): { highs: Pivot[]; lows: Pivot[] } {
  const highs: Pivot[] = [];
  const lows: Pivot[] = [];
  for (let j = left; j < bars.length - right; j++) {
    let isHigh = true;
    let isLow = true;
    for (let k = j - left; k <= j + right && (isHigh || isLow); k++) {
      if (k === j) continue;
      if (k < j ? bars[k].high >= bars[j].high : bars[k].high > bars[j].high) isHigh = false;
      if (k < j ? bars[k].low <= bars[j].low : bars[k].low < bars[j].low) isLow = false;
    }
    if (isHigh) highs.push({ idx: j, price: bars[j].high, type: 'high' });
    if (isLow) lows.push({ idx: j, price: bars[j].low, type: 'low' });
  }
  return { highs, lows };
}

/** Pivots usable at bar ``i``: confirmed (idx ≤ i − right) and within
 *  ``lookback`` bars. */
export function confirmedPivots(
  pivots: Pivot[], i: number, right: number, lookback: number,
): Pivot[] {
  const hi = i - right;
  const lo = i - lookback;
  return pivots.filter((p) => p.idx <= hi && p.idx >= lo);
}

/**
 * Merge highs and lows into a strictly alternating swing sequence —
 * consecutive same-type pivots collapse to the more extreme one.
 */
export function alternatingSwings(highs: Pivot[], lows: Pivot[]): Pivot[] {
  const all = [...highs, ...lows].sort((a, b) => a.idx - b.idx);
  const out: Pivot[] = [];
  for (const p of all) {
    const last = out[out.length - 1];
    if (last && last.type === p.type) {
      const moreExtreme = p.type === 'high' ? p.price > last.price : p.price < last.price;
      if (moreExtreme) out[out.length - 1] = p;
    } else {
      out.push(p);
    }
  }
  return out;
}

export interface Level {
  price: number;
  touches: number;
  /** Index of the most recent pivot in the cluster. */
  lastIdx: number;
}

/**
 * Horizontal levels: greedy clustering of pivot prices where each member
 * is within ``tol`` of the cluster's running mean.
 */
export function clusterLevels(pivots: Pivot[], tol: number): Level[] {
  const sorted = [...pivots].sort((a, b) => a.price - b.price);
  const levels: Level[] = [];
  let sum = 0;
  let members: Pivot[] = [];
  const flush = () => {
    if (members.length === 0) return;
    levels.push({
      price: sum / members.length,
      touches: members.length,
      lastIdx: Math.max(...members.map((m) => m.idx)),
    });
  };
  for (const p of sorted) {
    if (members.length > 0 && Math.abs(p.price - sum / members.length) > tol) {
      flush();
      members = [];
      sum = 0;
    }
    members.push(p);
    sum += p.price;
  }
  flush();
  return levels;
}
