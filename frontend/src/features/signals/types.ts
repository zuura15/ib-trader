/**
 * Trade-recommendation gauge (GH #102) — shared types.
 *
 * Label-only: the gauge never places orders. Three rule groups:
 *   1. primary   — make the call (direction + strength per rule)
 *   2. secondary — reinforce / detract / veto a call (RSI today)
 *   3. time      — mechanical blocks ("No trade") and weak caps
 */
import type { Bar } from '../chart/chartUtils';

/** -2 Strong Sell · -1 Sell · 0 Neutral · 1 Buy · 2 Strong Buy */
export type Grade = -2 | -1 | 0 | 1 | 2;

export type ParamValue = number | boolean | string;

/** One editable rule parameter. The config UI renders these generically,
 *  so a new rule only declares its params — no UI changes needed. */
export interface ParamDef {
  key: string;
  label: string;
  type: 'number' | 'boolean';
  default: ParamValue;
  min?: number;
  max?: number;
  step?: number;
  /** Unit suffix shown after the input (e.g. "× ATR", "bars"). */
  unit?: string;
  help?: string;
}

/** Per-rule stored settings. */
export interface RuleConfig {
  enabled: boolean;
  /** Primary rules: multiplier on the rule's strength in the score. */
  weight: number;
  params: Record<string, ParamValue>;
}

export type TimeEffect = 'block' | 'weak';

/** A moment of the day with a guard window around it. */
export interface TimeEvent {
  id: string;
  /** "HH:MM", 24 h, in ``SignalConfig.timeZone``. */
  time: string;
  label: string;
  /** Minutes before the moment the window opens. */
  before: number;
  /** Minutes after the moment the window stays open. */
  after: number;
  effect: TimeEffect;
  enabled: boolean;
}

/** A span of the day ("HH:MM"–"HH:MM", may wrap past midnight). */
export interface TimeRange {
  id: string;
  start: string;
  end: string;
  label: string;
  effect: TimeEffect;
  enabled: boolean;
}

export interface SignalConfig {
  version: 1;
  /** Master switch for the gauge on the charts. */
  enabled: boolean;
  /** IANA zone the time rules are written in. */
  timeZone: string;
  thresholds: {
    /** |score| at or above → Buy / Sell. */
    buy: number;
    /** |score| at or above → Strong Buy / Strong Sell. */
    strong: number;
    /** A primary rule below this strength is ignored. */
    minStrength: number;
  };
  /** Shared analysis settings every rule builds on. */
  analysis: {
    atrPeriod: number;
    /** Fractal pivot size: bars each side. Right side = confirmation lag. */
    pivotLeft: number;
    pivotRight: number;
  };
  display: {
    /** Current-recommendation badge in the chart's top-left. */
    badge: boolean;
    /** Labels on the bars where the recommendation changed. */
    history: boolean;
    /** How many recent bars get history labels. */
    historyBars: number;
  };
  /** Primary + secondary rules, keyed by rule id (see registry). */
  rules: Record<string, RuleConfig>;
  timeEvents: TimeEvent[];
  /** Guard window before every hour not covered by a time event. */
  hourRollover: { enabled: boolean; before: number; after: number; effect: TimeEffect };
  timeRanges: TimeRange[];
}

/** Swing point from the fractal pivot detector. */
export interface Pivot {
  idx: number;
  price: number;
  type: 'high' | 'low';
}

/** Series computed once per bar set and shared by every rule. */
export interface SeriesContext {
  bars: Bar[];
  atr: (number | null)[];
  pivotsHigh: Pivot[];
  pivotsLow: Pivot[];
  rsi: (number | null)[];
  /** Divergences as [toIdx, kind] — the bar where each completes. */
  divergences: Array<{ toIdx: number; kind: 'bullish' | 'bearish' }>;
  /** Pivot confirmation lag (right-side bars) used to build pivots. */
  pivotRight: number;
}

/** A primary rule's vote. */
export interface Contribution {
  ruleId: string;
  label: string;
  direction: 1 | -1;
  strength: number;
  detail: string;
}

/** A secondary rule's adjustment to the call. */
export interface Adjustment {
  ruleId: string;
  label: string;
  effect: 'up' | 'down' | 'veto';
  detail: string;
}

export interface Recommendation {
  grade: Grade;
  /** Set when a time rule blocks trading — shown as "No trade". */
  blocked: { label: string } | null;
  /** Set when a weak-time rule capped the grade. */
  capped: { label: string } | null;
  score: number;
  /** Grade from the primary score alone, before group 2 / 3. */
  baseGrade: Grade;
  contributions: Contribution[];
  adjustments: Adjustment[];
  /** Bar index evaluated. */
  idx: number;
}

export interface PrimaryRuleDef {
  id: string;
  group: 'primary';
  label: string;
  description: string;
  defaultEnabled: boolean;
  params: ParamDef[];
  evaluate: (
    ctx: SeriesContext, i: number, params: Record<string, ParamValue>,
  ) => Contribution[];
}

export interface SecondaryRuleDef {
  id: string;
  group: 'secondary';
  label: string;
  description: string;
  defaultEnabled: boolean;
  params: ParamDef[];
  /** Adjustments for a call in ``direction`` at bar ``i``. */
  evaluate: (
    ctx: SeriesContext, i: number, direction: 1 | -1,
    params: Record<string, ParamValue>,
  ) => Adjustment[];
}

export type RuleDef = PrimaryRuleDef | SecondaryRuleDef;
