/** Default recommendation config (GH #102) and merge-over-defaults. */
import { RULES } from './rules';
import type { RuleConfig, SignalConfig, TimeEvent, TimeRange } from './types';

const ev = (
  id: string, time: string, label: string, effect: TimeEvent['effect'] = 'block',
): TimeEvent => ({ id, time, label, before: 5, after: 0, effect, enabled: true });

/** Pacific-time defaults from the operator's list (2026-11-08). */
export const DEFAULT_TIME_EVENTS: TimeEvent[] = [
  ev('t0400', '04:00', 'Pre-market volatility'),
  ev('t0530', '05:30', 'US economic data (8:30 ET)'),
  ev('t1100', '11:00', 'Fed announcements (2:00 ET)'),
  ev('t1245', '12:45', 'Market-on-close imbalance (3:45 ET)'),
  ev('t1300', '13:00', 'Stock market close'),
  ev('t1400', '14:00', 'Futures daily halt'),
  ev('t1600', '16:00', 'Korea market open'),
  ev('t1700', '17:00', 'Options day end'),
  ev('t2100', '21:00', 'Midnight ET — new trading day'),
];

export const DEFAULT_TIME_RANGES: TimeRange[] = [
  {
    id: 'r0600', start: '06:00', end: '07:30', label: 'Cash-open window',
    effect: 'weak', enabled: true,
  },
];

function defaultRuleConfig(id: string): RuleConfig {
  const def = RULES.find((r) => r.id === id)!;
  return {
    enabled: def.defaultEnabled,
    weight: 1,
    params: Object.fromEntries(def.params.map((p) => [p.key, p.default])),
  };
}

export function defaultSignalConfig(): SignalConfig {
  return {
    version: 1,
    enabled: true,
    timeZone: 'America/Los_Angeles',
    thresholds: { buy: 0.5, strong: 0.85, minStrength: 0.2 },
    analysis: { atrPeriod: 14, pivotLeft: 5, pivotRight: 5 },
    display: { badge: true, history: true, historyMinutes: 30 },
    rules: Object.fromEntries(RULES.map((r) => [r.id, defaultRuleConfig(r.id)])),
    timeEvents: DEFAULT_TIME_EVENTS.map((e) => ({ ...e })),
    hourRollover: { enabled: true, before: 5, after: 0, effect: 'weak' },
    timeRanges: DEFAULT_TIME_RANGES.map((r) => ({ ...r })),
  };
}

/**
 * Overlay a stored (possibly older / partial) config on today's
 * defaults: new rules and params appear with their defaults; stored
 * values win where present. Lists (time events / ranges) are taken
 * whole from the stored config once it has them.
 */
export function mergeSignalConfig(stored: unknown): SignalConfig {
  const d = defaultSignalConfig();
  if (!stored || typeof stored !== 'object') return d;
  const s = stored as Partial<SignalConfig>;
  const rules: Record<string, RuleConfig> = {};
  for (const [id, base] of Object.entries(d.rules)) {
    const r = s.rules?.[id];
    rules[id] = r ? {
      enabled: typeof r.enabled === 'boolean' ? r.enabled : base.enabled,
      weight: Number.isFinite(r.weight) ? r.weight : base.weight,
      params: { ...base.params, ...(r.params ?? {}) },
    } : base;
  }
  return {
    ...d,
    enabled: typeof s.enabled === 'boolean' ? s.enabled : d.enabled,
    timeZone: typeof s.timeZone === 'string' && s.timeZone ? s.timeZone : d.timeZone,
    thresholds: { ...d.thresholds, ...(s.thresholds ?? {}) },
    analysis: { ...d.analysis, ...(s.analysis ?? {}) },
    display: {
      badge: s.display?.badge ?? d.display.badge,
      history: s.display?.history ?? d.display.history,
      historyMinutes: Number.isFinite(s.display?.historyMinutes)
        ? s.display!.historyMinutes : d.display.historyMinutes,
    },
    rules,
    timeEvents: Array.isArray(s.timeEvents) ? s.timeEvents : d.timeEvents,
    hourRollover: { ...d.hourRollover, ...(s.hourRollover ?? {}) },
    timeRanges: Array.isArray(s.timeRanges) ? s.timeRanges : d.timeRanges,
  };
}
