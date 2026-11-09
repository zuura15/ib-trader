/**
 * Rule registry for the recommendation gauge (GH #102).
 *
 * Adding a rule = appending a definition here: its params drive the
 * config UI and its defaults seed the stored config. Tolerances are in
 * ATR units so one setting works across instruments.
 */
import {
  alternatingSwings, clusterLevels, confirmedPivots,
} from './indicators';
import type {
  Adjustment, Contribution, ParamValue, PrimaryRuleDef, RuleDef,
  SecondaryRuleDef, SeriesContext,
} from './types';

const num = (p: Record<string, ParamValue>, k: string): number => Number(p[k]);
const bool = (p: Record<string, ParamValue>, k: string): boolean => p[k] === true;
const clamp01 = (x: number) => Math.max(0, Math.min(1, x));

const pivotParams = (fresh: number) => [
  {
    key: 'lookback', label: 'Lookback', type: 'number' as const, default: 240,
    min: 30, max: 2000, step: 10, unit: 'bars',
    help: 'Only swing points within this many bars count.',
  },
  {
    key: 'fresh', label: 'Signal stays live for', type: 'number' as const, default: fresh,
    min: 1, max: 30, step: 1, unit: 'bars',
    help: 'How many bars after the trigger the rule keeps voting (fading each bar).',
  },
];

/** Fade a signal that fired ``age`` bars ago. */
const fade = (age: number) => Math.pow(0.85, age);

// ── Group 1 — primary ────────────────────────────────────────────────

const srBounce: PrimaryRuleDef = {
  id: 'srBounce',
  group: 'primary',
  label: 'Support / resistance bounce',
  description:
    'Horizontal levels from clustered swing points. Buy when price dips into '
    + 'a support zone and closes back above it; sell on the mirror at resistance.',
  defaultEnabled: true,
  params: [
    ...pivotParams(1),
    {
      key: 'zoneAtr', label: 'Zone half-width', type: 'number', default: 0.3,
      min: 0.05, max: 2, step: 0.05, unit: '× ATR',
      help: 'Margin for error around a level — touches inside it count.',
    },
    {
      key: 'minTouches', label: 'Min prior touches', type: 'number', default: 3,
      min: 1, max: 6, step: 1,
      help: 'Swing points the level needs before a bounce off it counts.',
    },
  ],
  evaluate(ctx, i, p) {
    const atr = ctx.atr[i];
    if (atr == null || atr <= 0) return [];
    const tol = num(p, 'zoneAtr') * atr;
    const lookback = num(p, 'lookback');
    const fresh = num(p, 'fresh');
    const minTouches = num(p, 'minTouches');
    const piv = [
      ...confirmedPivots(ctx.pivotsHigh, i, ctx.pivotRight, lookback),
      ...confirmedPivots(ctx.pivotsLow, i, ctx.pivotRight, lookback),
    ];
    const levels = clusterLevels(piv, tol).filter((l) => l.touches >= minTouches);
    const out: Contribution[] = [];
    for (const lvl of levels) {
      for (let age = 0; age < fresh && i - age >= 1; age++) {
        const k = i - age;
        const b = ctx.bars[k];
        const prev = ctx.bars[k - 1];
        if (k <= lvl.lastIdx) break;   // bounce must come after the level formed
        const range = Math.max(b.high - b.low, 1e-9);
        // Support: approached from above, wick into the zone, close back out.
        if (prev.close > lvl.price && b.low <= lvl.price + tol
            && b.low >= lvl.price - tol && b.close >= lvl.price + tol) {
          const wick = (Math.min(b.open, b.close) - b.low) / range;
          out.push({
            ruleId: this.id, label: this.label, direction: 1,
            strength: clamp01((0.45 + 0.12 * Math.min(lvl.touches - 1, 3)
              + (wick >= 0.5 ? 0.1 : 0)) * fade(age)),
            detail: `bounce off support ${lvl.price.toFixed(2)} (${lvl.touches} touches)`,
          });
          break;
        }
        // Resistance: approached from below, wick into the zone, close back under.
        if (prev.close < lvl.price && b.high >= lvl.price - tol
            && b.high <= lvl.price + tol && b.close <= lvl.price - tol) {
          const wick = (b.high - Math.max(b.open, b.close)) / range;
          out.push({
            ruleId: this.id, label: this.label, direction: -1,
            strength: clamp01((0.45 + 0.12 * Math.min(lvl.touches - 1, 3)
              + (wick >= 0.5 ? 0.1 : 0)) * fade(age)),
            detail: `rejected at resistance ${lvl.price.toFixed(2)} (${lvl.touches} touches)`,
          });
          break;
        }
      }
    }
    // One vote per side: the strongest level wins.
    return pickStrongestPerSide(out);
  },
};

const multiTouch: PrimaryRuleDef = {
  id: 'multiTouch',
  group: 'primary',
  label: 'Double / triple bottom & top',
  description:
    '2–3 swing lows (highs) at the same level with a real bounce between them. '
    + 'Votes only once price closes through the neckline — unconfirmed patterns fail often.',
  defaultEnabled: true,
  params: [
    ...pivotParams(2),
    {
      key: 'levelAtr', label: 'Same-level tolerance', type: 'number', default: 0.3,
      min: 0.05, max: 2, step: 0.05, unit: '× ATR',
    },
    {
      key: 'minSeparation', label: 'Min bars between touches', type: 'number',
      default: 8, min: 2, max: 200, step: 1, unit: 'bars',
    },
    {
      key: 'minDepthAtr', label: 'Min bounce between touches', type: 'number',
      default: 1.0, min: 0.2, max: 10, step: 0.1, unit: '× ATR',
    },
  ],
  evaluate(ctx, i, p) {
    const atr = ctx.atr[i];
    if (atr == null || atr <= 0) return [];
    const out: Contribution[] = [];
    const bottom = findMultiTouch(ctx, i, p, atr, 'low');
    if (bottom) out.push({ ruleId: this.id, label: this.label, ...bottom });
    const top = findMultiTouch(ctx, i, p, atr, 'high');
    if (top) out.push({ ruleId: this.id, label: this.label, ...top });
    return out;
  },
};

function findMultiTouch(
  ctx: SeriesContext, i: number, p: Record<string, ParamValue>, atr: number,
  side: 'low' | 'high',
): Omit<Contribution, 'ruleId' | 'label'> | null {
  const isBottom = side === 'low';
  const pivots = confirmedPivots(
    isBottom ? ctx.pivotsLow : ctx.pivotsHigh, i, ctx.pivotRight, num(p, 'lookback'),
  );
  const tol = num(p, 'levelAtr') * atr;
  const minSep = num(p, 'minSeparation');
  const fresh = num(p, 'fresh');
  // Walk back from the most recent pivot collecting same-level touches.
  for (let a = pivots.length - 1; a >= 1; a--) {
    const group = [pivots[a]];
    for (let b = a - 1; b >= 0 && group.length < 3; b--) {
      const ref = group[group.length - 1];
      if (ref.idx - pivots[b].idx < minSep) continue;
      if (Math.abs(pivots[b].price - group[0].price) <= tol) group.push(pivots[b]);
    }
    if (group.length < 2) continue;
    group.reverse();
    const first = group[0].idx;
    const last = group[group.length - 1].idx;
    let neck = isBottom ? -Infinity : Infinity;
    for (let k = first; k <= last; k++) {
      neck = isBottom ? Math.max(neck, ctx.bars[k].high) : Math.min(neck, ctx.bars[k].low);
    }
    const mean = group.reduce((s, g) => s + g.price, 0) / group.length;
    if (Math.abs(neck - mean) < num(p, 'minDepthAtr') * atr) continue;
    for (let age = 0; age < fresh && i - age > last; age++) {
      const k = i - age;
      const c = ctx.bars[k].close;
      const pc = ctx.bars[k - 1].close;
      const broke = isBottom ? c > neck && pc <= neck : c < neck && pc >= neck;
      if (!broke) continue;
      const name = group.length >= 3 ? 'triple' : 'double';
      return {
        direction: isBottom ? 1 : -1,
        strength: clamp01((group.length >= 3 ? 0.8 : 0.6) * fade(age)),
        detail: `${name} ${isBottom ? 'bottom' : 'top'} ~${mean.toFixed(2)}, `
          + `neckline ${neck.toFixed(2)} broken`,
      };
    }
    return null;   // most recent qualifying pattern hasn't broken out
  }
  return null;
}

const headShoulders: PrimaryRuleDef = {
  id: 'headShoulders',
  group: 'primary',
  label: 'Head & shoulders (and inverse)',
  description:
    'Five alternating swings with the middle one the extreme and the two '
    + 'shoulders level. Votes on a close through the neckline (line through the two troughs).',
  defaultEnabled: true,
  params: [
    ...pivotParams(2),
    {
      key: 'shoulderAtr', label: 'Shoulder level tolerance', type: 'number',
      default: 1.0, min: 0.1, max: 5, step: 0.1, unit: '× ATR',
    },
    {
      key: 'headAtr', label: 'Head beyond shoulders by', type: 'number',
      default: 0.5, min: 0.1, max: 10, step: 0.1, unit: '× ATR',
    },
  ],
  evaluate(ctx, i, p) {
    const atr = ctx.atr[i];
    if (atr == null || atr <= 0) return [];
    const lookback = num(p, 'lookback');
    const swings = alternatingSwings(
      confirmedPivots(ctx.pivotsHigh, i, ctx.pivotRight, lookback),
      confirmedPivots(ctx.pivotsLow, i, ctx.pivotRight, lookback),
    );
    const out: Contribution[] = [];
    for (const top of [true, false]) {
      // Last five swings ending on a high (top) or a low (inverse).
      let end = swings.length - 1;
      while (end >= 0 && swings[end].type !== (top ? 'high' : 'low')) end--;
      if (end < 4) continue;
      const [s1, t1, h, t2, s2] = swings.slice(end - 4, end + 1);
      const beyond = (a: number, b: number) => (top ? a - b : b - a);
      const headMin = num(p, 'headAtr') * atr;
      if (beyond(h.price, s1.price) < headMin || beyond(h.price, s2.price) < headMin) continue;
      if (Math.abs(s1.price - s2.price) > num(p, 'shoulderAtr') * atr) continue;
      const neckAt = (k: number) =>
        t1.price + (t2.price - t1.price) * (k - t1.idx) / Math.max(t2.idx - t1.idx, 1);
      for (let age = 0; age < num(p, 'fresh') && i - age > s2.idx; age++) {
        const k = i - age;
        const c = ctx.bars[k].close;
        const pc = ctx.bars[k - 1].close;
        const broke = top
          ? c < neckAt(k) && pc >= neckAt(k - 1)
          : c > neckAt(k) && pc <= neckAt(k - 1);
        if (!broke) continue;
        const symmetry = 1 - Math.abs(s1.price - s2.price) / (num(p, 'shoulderAtr') * atr);
        out.push({
          ruleId: this.id, label: this.label, direction: top ? -1 : 1,
          strength: clamp01((0.7 + 0.15 * symmetry) * fade(age)),
          detail: `${top ? '' : 'inverse '}head & shoulders, head ${h.price.toFixed(2)}, `
            + `neckline broken at ${neckAt(k).toFixed(2)}`,
        });
        break;
      }
    }
    return out;
  },
};

function pickStrongestPerSide(cs: Contribution[]): Contribution[] {
  const best = new Map<number, Contribution>();
  for (const c of cs) {
    const cur = best.get(c.direction);
    if (!cur || c.strength > cur.strength) best.set(c.direction, c);
  }
  return [...best.values()];
}

// ── Group 2 — secondary ──────────────────────────────────────────────

const rsiFilter: SecondaryRuleDef = {
  id: 'rsi',
  group: 'secondary',
  label: 'RSI',
  description:
    'Reinforces a call when RSI agrees, vetoes it when RSI is already at the '
    + 'extreme in the call\'s direction, and downgrades it on an opposite divergence.',
  defaultEnabled: true,
  params: [
    { key: 'period', label: 'Period', type: 'number', default: 14, min: 2, max: 100, step: 1, unit: 'bars' },
    { key: 'overbought', label: 'Overbought', type: 'number', default: 70, min: 50, max: 95, step: 1 },
    { key: 'oversold', label: 'Oversold', type: 'number', default: 30, min: 5, max: 50, step: 1 },
    {
      key: 'veto', label: 'Veto a buy when overbought / a sell when oversold',
      type: 'boolean', default: true,
    },
    {
      key: 'confirm', label: 'Upgrade when RSI turns with the call from its own half',
      type: 'boolean', default: true,
      help: 'Buy: RSI below 50 and rising. Sell: RSI above 50 and falling.',
    },
    {
      key: 'divergence', label: 'Use divergences', type: 'boolean', default: true,
      help: 'Matching divergence upgrades; opposite divergence downgrades.',
    },
    {
      key: 'divergenceBars', label: 'Divergence counts for', type: 'number',
      default: 15, min: 1, max: 120, step: 1, unit: 'bars',
    },
  ],
  evaluate(ctx, i, direction, p) {
    const r = ctx.rsi[i];
    const rp = ctx.rsi[i - 1];
    if (r == null) return [];
    const out: Adjustment[] = [];
    const base = { ruleId: this.id, label: this.label };
    if (bool(p, 'veto')) {
      if (direction === 1 && r >= num(p, 'overbought')) {
        return [{ ...base, effect: 'veto', detail: `RSI ${r.toFixed(0)} overbought` }];
      }
      if (direction === -1 && r <= num(p, 'oversold')) {
        return [{ ...base, effect: 'veto', detail: `RSI ${r.toFixed(0)} oversold` }];
      }
    }
    let up = false;
    if (bool(p, 'confirm') && rp != null) {
      if (direction === 1 && r < 50 && r > rp) {
        up = true;
        out.push({ ...base, effect: 'up', detail: `RSI ${r.toFixed(0)} rising from below 50` });
      } else if (direction === -1 && r > 50 && r < rp) {
        up = true;
        out.push({ ...base, effect: 'up', detail: `RSI ${r.toFixed(0)} falling from above 50` });
      }
    }
    if (bool(p, 'divergence')) {
      const recent = ctx.divergences.filter(
        (d) => d.toIdx <= i && i - d.toIdx < num(p, 'divergenceBars'),
      );
      const want = direction === 1 ? 'bullish' : 'bearish';
      if (recent.some((d) => d.kind !== want)) {
        out.push({ ...base, effect: 'down', detail: `opposite (${want === 'bullish' ? 'bearish' : 'bullish'}) RSI divergence` });
      } else if (!up && recent.some((d) => d.kind === want)) {
        out.push({ ...base, effect: 'up', detail: `${want} RSI divergence` });
      }
    }
    return out;
  },
};

export const RULES: RuleDef[] = [srBounce, multiTouch, headShoulders, rsiFilter];

export const PRIMARY_RULES = RULES.filter((r): r is PrimaryRuleDef => r.group === 'primary');
export const SECONDARY_RULES = RULES.filter((r): r is SecondaryRuleDef => r.group === 'secondary');
