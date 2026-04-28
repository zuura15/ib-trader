/**
 * RSI(14) + divergence detection — port of the TradingView "RSI Divergence
 * Indicator" config the user shipped (see docs/rsi-config.png).
 *
 * Settings (hardcoded to match the screenshot):
 *   period       = 14
 *   pivotLeft    = 5
 *   pivotRight   = 5
 *   minLookback  = 5
 *   maxLookback  = 60
 *   plotBullish  = ON  → RSI higher low + price lower low
 *   plotBearish  = ON  → RSI lower high + price higher high
 *   hidden bull/bear off
 *   timeframe    = chart  (we use whatever bars came back from /api/history)
 *   wait for close = on (we operate on closed 1-min bars; live ticks update
 *                       only the price series, not RSI)
 */

export interface RsiSettings {
  period: number;
  pivotLeft: number;
  pivotRight: number;
  minLookback: number;
  maxLookback: number;
  plotBullish: boolean;
  plotBearish: boolean;
}

export const RSI_DEFAULTS: RsiSettings = {
  period: 14,
  pivotLeft: 5,
  pivotRight: 5,
  minLookback: 5,
  maxLookback: 60,
  plotBullish: true,
  plotBearish: true,
};

export interface Divergence {
  fromIdx: number;          // earlier pivot bar index
  toIdx: number;            // current pivot bar index
  kind: 'bullish' | 'bearish';
}

/** Wilder's RSI. Returns one value per close (null until the period is filled). */
export function computeRsi(closes: number[], period: number): (number | null)[] {
  const out: (number | null)[] = new Array(closes.length).fill(null);
  if (closes.length < period + 1) return out;

  let gainSum = 0;
  let lossSum = 0;
  for (let i = 1; i <= period; i++) {
    const diff = closes[i] - closes[i - 1];
    if (diff > 0) gainSum += diff;
    else lossSum -= diff;
  }
  let avgGain = gainSum / period;
  let avgLoss = lossSum / period;
  out[period] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);

  for (let i = period + 1; i < closes.length; i++) {
    const diff = closes[i] - closes[i - 1];
    const gain = diff > 0 ? diff : 0;
    const loss = diff < 0 ? -diff : 0;
    avgGain = (avgGain * (period - 1) + gain) / period;
    avgLoss = (avgLoss * (period - 1) + loss) / period;
    out[i] = avgLoss === 0 ? 100 : 100 - 100 / (1 + avgGain / avgLoss);
  }
  return out;
}

/**
 * A pivot at index i means values[i] is a strict extremum vs all neighbors
 * within [-left, +right]. Returns confirmed pivot indices only — i.e.
 * pivots whose right window is fully realized (i + right < length).
 */
function findPivots(
  values: (number | null)[],
  left: number,
  right: number,
  type: 'high' | 'low',
): number[] {
  const out: number[] = [];
  for (let i = left; i + right < values.length; i++) {
    const v = values[i];
    if (v == null) continue;
    let isPivot = true;
    for (let j = i - left; j <= i + right; j++) {
      if (j === i) continue;
      const u = values[j];
      if (u == null) { isPivot = false; break; }
      if (type === 'high' ? u > v : u < v) { isPivot = false; break; }
    }
    if (isPivot) out.push(i);
  }
  return out;
}

export function detectDivergences(
  closes: number[],
  rsi: (number | null)[],
  s: RsiSettings,
): Divergence[] {
  const out: Divergence[] = [];

  if (s.plotBullish) {
    const lows = findPivots(rsi, s.pivotLeft, s.pivotRight, 'low');
    for (let i = 1; i < lows.length; i++) {
      const cur = lows[i];
      const prev = lows[i - 1];
      const dist = cur - prev;
      if (dist < s.minLookback || dist > s.maxLookback) continue;
      const rsiCur = rsi[cur]!;
      const rsiPrev = rsi[prev]!;
      const priceCur = closes[cur];
      const pricePrev = closes[prev];
      // Bullish: RSI made a higher low while price made a lower low.
      if (rsiCur > rsiPrev && priceCur < pricePrev) {
        out.push({ fromIdx: prev, toIdx: cur, kind: 'bullish' });
      }
    }
  }

  if (s.plotBearish) {
    const highs = findPivots(rsi, s.pivotLeft, s.pivotRight, 'high');
    for (let i = 1; i < highs.length; i++) {
      const cur = highs[i];
      const prev = highs[i - 1];
      const dist = cur - prev;
      if (dist < s.minLookback || dist > s.maxLookback) continue;
      const rsiCur = rsi[cur]!;
      const rsiPrev = rsi[prev]!;
      const priceCur = closes[cur];
      const pricePrev = closes[prev];
      // Bearish: RSI made a lower high while price made a higher high.
      if (rsiCur < rsiPrev && priceCur > pricePrev) {
        out.push({ fromIdx: prev, toIdx: cur, kind: 'bearish' });
      }
    }
  }

  return out;
}
