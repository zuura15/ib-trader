/**
 * Recommendation engine (GH #102): bars + config → grade per bar.
 *
 * Order of operations for bar i:
 *   1. Primary rules vote; score = Σ direction × strength × weight,
 *      clamped to ±1 → base grade via thresholds. No vote → Neutral.
 *   2. Secondary rules adjust a non-neutral grade: up / down one notch
 *      (never flipping sides) or veto to Neutral.
 *   3. Time rules: "block" → No trade; "weak" → capped at Buy / Sell.
 */
import type { Bar } from '../chart/chartUtils';
import { computeRsi, detectDivergences, RSI_DEFAULTS } from '../chart/rsiDivergence';
import { computeAtr, findPivots } from './indicators';
import { PRIMARY_RULES, SECONDARY_RULES } from './rules';
import type {
  Adjustment, Contribution, Grade, Recommendation, SeriesContext,
  SignalConfig, TimeEffect,
} from './types';

export function buildContext(bars: Bar[], cfg: SignalConfig): SeriesContext {
  const { atrPeriod, pivotLeft, pivotRight } = cfg.analysis;
  const { highs, lows } = findPivots(bars, pivotLeft, pivotRight);
  const closes = bars.map((b) => b.close);
  const rsiPeriod = Number(cfg.rules.rsi?.params.period ?? RSI_DEFAULTS.period);
  const rsi = computeRsi(closes, rsiPeriod);
  const divergences = detectDivergences(closes, rsi, { ...RSI_DEFAULTS, period: rsiPeriod })
    .map((d) => ({ toIdx: d.toIdx, kind: d.kind }));
  return {
    bars, atr: computeAtr(bars, atrPeriod), pivotsHigh: highs, pivotsLow: lows,
    rsi, divergences, pivotRight,
  };
}

function gradeFromScore(score: number, cfg: SignalConfig): Grade {
  const a = Math.abs(score);
  const sign = score > 0 ? 1 : -1;
  if (a >= cfg.thresholds.strong) return (2 * sign) as Grade;
  if (a >= cfg.thresholds.buy) return sign as Grade;
  return 0;
}

/** Evaluate bar ``i``. ``atMs`` is the real epoch time the time rules
 *  are checked against (bar close for history, now for the live badge). */
export function evaluateAt(
  ctx: SeriesContext, i: number, cfg: SignalConfig, atMs: number,
): Recommendation {
  const contributions: Contribution[] = [];
  for (const rule of PRIMARY_RULES) {
    const rc = cfg.rules[rule.id];
    if (!rc?.enabled) continue;
    for (const c of rule.evaluate(ctx, i, rc.params)) {
      if (c.strength >= cfg.thresholds.minStrength) {
        contributions.push({ ...c, strength: c.strength * rc.weight });
      }
    }
  }
  const raw = contributions.reduce((s, c) => s + c.direction * c.strength, 0);
  const score = Math.max(-1, Math.min(1, raw));
  const baseGrade = gradeFromScore(score, cfg);

  let grade: Grade = baseGrade;
  const adjustments: Adjustment[] = [];
  if (baseGrade !== 0) {
    const dir = baseGrade > 0 ? 1 : -1;
    for (const rule of SECONDARY_RULES) {
      const rc = cfg.rules[rule.id];
      if (!rc?.enabled) continue;
      adjustments.push(...rule.evaluate(ctx, i, dir, rc.params));
    }
    if (adjustments.some((a) => a.effect === 'veto')) {
      grade = 0;
    } else {
      const net = adjustments.reduce(
        (n, a) => n + (a.effect === 'up' ? 1 : a.effect === 'down' ? -1 : 0), 0,
      );
      const mag = Math.max(0, Math.min(2, Math.abs(baseGrade) + net));
      grade = (dir * mag) as Grade;
    }
  }

  const t = timeRuleAt(atMs, cfg);
  let blocked: Recommendation['blocked'] = null;
  let capped: Recommendation['capped'] = null;
  if (t?.effect === 'block') {
    blocked = { label: t.label };
  } else if (t?.effect === 'weak' && Math.abs(grade) > 1) {
    grade = (grade > 0 ? 1 : -1) as Grade;
    capped = { label: t.label };
  }
  return { grade, blocked, capped, score, baseGrade, contributions, adjustments, idx: i };
}

// ── Time rules ───────────────────────────────────────────────────────

const fmtCache = new Map<string, Intl.DateTimeFormat>();

/** Minute of the day (0–1439) for a real epoch time in ``timeZone``. */
export function minuteOfDay(ms: number, timeZone: string): number {
  let fmt = fmtCache.get(timeZone);
  if (!fmt) {
    try {
      fmt = new Intl.DateTimeFormat('en-US', {
        timeZone, hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      });
    } catch {
      // Bad zone in config — fall back to the browser's own zone.
      fmt = new Intl.DateTimeFormat('en-US', {
        hour: '2-digit', minute: '2-digit', hourCycle: 'h23',
      });
    }
    fmtCache.set(timeZone, fmt);
  }
  const parts = fmt.formatToParts(new Date(ms));
  const h = Number(parts.find((p) => p.type === 'hour')?.value ?? 0);
  const m = Number(parts.find((p) => p.type === 'minute')?.value ?? 0);
  return (h % 24) * 60 + m;
}

function parseHm(s: string): number | null {
  const m = /^(\d{1,2}):(\d{2})$/.exec(s.trim());
  if (!m) return null;
  const h = Number(m[1]);
  const mi = Number(m[2]);
  return h < 24 && mi < 60 ? h * 60 + mi : null;
}

/** True when minute ``m`` is in [start, end) on a 24 h circle. */
function inCircular(m: number, start: number, end: number): boolean {
  const s = ((start % 1440) + 1440) % 1440;
  const e = ((end % 1440) + 1440) % 1440;
  if (s === e) return false;
  return s < e ? m >= s && m < e : m >= s || m < e;
}

/** The strongest time rule active at ``ms`` (block beats weak). */
export function timeRuleAt(
  ms: number, cfg: SignalConfig,
): { effect: TimeEffect; label: string } | null {
  const m = minuteOfDay(ms, cfg.timeZone);
  let weak: { effect: TimeEffect; label: string } | null = null;
  const consider = (effect: TimeEffect, label: string) => {
    if (effect === 'block') return { effect, label };
    weak = weak ?? { effect, label };
    return null;
  };
  for (const ev of cfg.timeEvents) {
    if (!ev.enabled) continue;
    const t = parseHm(ev.time);
    if (t == null) continue;
    if (inCircular(m, t - ev.before, t + ev.after)) {
      const hit = consider(ev.effect, `${ev.label} (${ev.time})`);
      if (hit) return hit;
    }
  }
  for (const r of cfg.timeRanges) {
    if (!r.enabled) continue;
    const s = parseHm(r.start);
    const e = parseHm(r.end);
    if (s == null || e == null) continue;
    if (inCircular(m, s, e)) {
      const hit = consider(r.effect, `${r.label} (${r.start}–${r.end})`);
      if (hit) return hit;
    }
  }
  const hr = cfg.hourRollover;
  if (hr.enabled) {
    const next = Math.ceil(m / 60) * 60;
    const prev = Math.floor(m / 60) * 60;
    if (inCircular(m, next - hr.before, next + hr.after)
        || inCircular(m, prev - hr.before, prev + hr.after)) {
      const hit = consider(hr.effect, 'Hour rollover');
      if (hit) return hit;
    }
  }
  return weak;
}

/** Bar times are browser-local wall clock encoded as UTC seconds (see
 *  ``localUtcSeconds``); convert back to a real epoch in ms. */
export function barTimeToEpochMs(barSec: number): number {
  const asUtc = barSec * 1000;
  return asUtc + new Date(asUtc).getTimezoneOffset() * 60_000;
}

/** Recommendation at every bar in the last ``n`` (time rules at bar close). */
export function evaluateHistory(
  ctx: SeriesContext, cfg: SignalConfig, n: number,
): Recommendation[] {
  const out: Recommendation[] = [];
  const start = Math.max(1, ctx.bars.length - n);
  for (let i = start; i < ctx.bars.length; i++) {
    out.push(evaluateAt(ctx, i, cfg, barTimeToEpochMs(ctx.bars[i].time as number)));
  }
  return out;
}

export const GRADE_LABEL: Record<Grade, string> = {
  [-2]: 'Strong Sell', [-1]: 'Sell', 0: 'Neutral', 1: 'Buy', 2: 'Strong Buy',
};
