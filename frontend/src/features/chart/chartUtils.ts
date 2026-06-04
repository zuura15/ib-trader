import type { UTCTimestamp } from 'lightweight-charts';
import type { HistoryBar } from '../../api/client';

export const VISIBLE_MINUTES = 90;
export const PRELOAD_HOURS = 48;
// SR detection lookback — separate from PRELOAD_HOURS because SR's
// per-bar cost (pivot detection + line fitting + wedge search) scales
// roughly linearly with bar count, and 48 h × 1-min bars = 2880 bars
// times out the API → engine HTTP request before the algo finishes.
// 12 h × 1-min = 720 bars matches the OLD 48 h × 3-min workload (960
// bars) without losing any structure the operator actually scans
// (they zoom within the last few hours; SR over older bars isn't
// load-bearing). The chart history fetch still uses PRELOAD_HOURS
// so the operator can pan back into prior sessions and see bars,
// just not redrawn SR.
export const SR_LOOKBACK_HOURS = 12;
export const REFRESH_INTERVAL_MS = 30_000;
// 1-min bars: tuned for the operator's manual-scan workflow (eyeball
// triggers on the chart, execute via the console). Replaces the prior
// 3-min cadence which was inherited from the bot framework's aggregator
// — bots still aggregate 3-min internally via their own YAML config;
// the chart display is independent and now favours fresher signal
// resolution over the smoother swing-context view. With
// VISIBLE_MINUTES=90, the default view shows 90 bars; RSI(14) lookback
// is 14 min of price action, closer to tape than the prior 42 min.
export const BAR_SIZE = '1 min';
// Seconds per bar — used to round live ticks down to the bar's start
// time so sub-bar updates land on the same bar (no per-tick shift).
export const BAR_SECONDS = 60;

// v2: bumped to discard ranges saved by an earlier build where transient
// auto-fit states could persist as ~2-min "zooms". Old `v1` entries are
// orphaned (small handful of bytes per target — left for the browser to
// evict on its own).
const ZOOM_STORAGE_KEY = 'ib-chart-zoom-v2';
export type SavedRange = {
  from: number;
  to: number;
  /** Optional price-axis (Y) range. lightweight-charts has no
   *  priceScale-change event, so we capture this opportunistically
   *  (on time-range change + on the 30s refresh) and persist it
   *  alongside the time range. Older entries without these fields
   *  still load correctly — the chart auto-fits Y. */
  priceFrom?: number;
  priceTo?: number;
  /** Bar-relative X persistence: right-edge gap in bar units and
   *  visible width in bars at the moment of capture. Restored on
   *  hard refresh so the chart re-anchors to where the operator
   *  was looking, with new bars sliding old ones off the left
   *  while the right-edge gap stays constant. Optional so older
   *  saved entries (time-only) still load. */
  rightSpaceBars?: number;
  widthBars?: number;
};
export type Point = { time: UTCTimestamp; value: number };
/** Full OHLC bar with the same local-as-UTC time shift as Point. Used
 *  by support/resistance detection (which needs wick high/low) and by
 *  the live-tick handler (which folds sub-bar ticks into high/low/close
 *  in place). */
export type Bar = {
  time: UTCTimestamp;
  open: number;
  high: number;
  low: number;
  close: number;
  /** TRADES-feed volume for the bar. Optional because the live-tick
   *  WebSocket pushes price-only updates — the most-recent bar's
   *  volume only refreshes on the 30s history refetch. */
  volume?: number;
};

export function targetKey(t: { symbol: string; secType: string; conId: number | null }): string {
  // con_id is unambiguous per IB contract; symbol+secType is the
  // fallback for watchlist clicks where qualify hasn't run.
  return t.conId != null ? `c:${t.conId}` : `s:${t.secType}:${t.symbol}`;
}

export function loadSavedRange(key: string): SavedRange | null {
  try {
    const raw = localStorage.getItem(ZOOM_STORAGE_KEY);
    if (!raw) return null;
    const map = JSON.parse(raw) as Record<string, SavedRange>;
    const r = map[key];
    if (!r || typeof r.from !== 'number' || typeof r.to !== 'number') return null;
    // Reject any save under 30 minutes wide. Time-based and bar-
    // relative widths get cross-checked: if EITHER signals a tiny
    // viewport, treat the whole entry as corrupt. This catches saves
    // polluted by lightweight-charts' transient narrow-window events
    // during ``setData`` (the volume histogram aggravated this) — any
    // such entry on reload would otherwise strand the chart at a few
    // minutes of width.
    if (r.to - r.from < 30 * 60) return null;
    if (r.widthBars != null && r.widthBars < 10) return null;
    return r;
  } catch { return null; }
}

export function saveRange(key: string, range: SavedRange | null): void {
  try {
    const raw = localStorage.getItem(ZOOM_STORAGE_KEY);
    const map: Record<string, SavedRange> = raw ? JSON.parse(raw) : {};
    if (range) map[key] = range;
    else delete map[key];
    const keys = Object.keys(map);
    if (keys.length > 50) {
      for (const k of keys.slice(0, keys.length - 50)) delete map[k];
    }
    localStorage.setItem(ZOOM_STORAGE_KEY, JSON.stringify(map));
  } catch { /* quota — ignore */ }
}

export function toBars(bars: HistoryBar[]): Bar[] {
  const out: Bar[] = [];
  for (const b of bars) {
    const date = new Date(b.ts);
    const ms = date.getTime();
    if (!Number.isFinite(ms)) continue;
    // lightweight-charts always formats UTCTimestamp as UTC. Shift each
    // bar's epoch by its own timezone offset so the axis reads as the
    // user's local wall time. Per-bar so DST flips stay correct.
    // ALSO shift by ``+BAR_SECONDS`` so each bar is labelled by its
    // slot-END (= the wall-clock moment it actually closed) instead
    // of its slot-start. This matches operator intuition: the bar
    // "at 15:27" is the bar that just closed at 15:27, not the one
    // that started at 15:27. Pair with the same shift on badges and
    // entry-line anchor times below so they stay co-located.
    const tzOffsetMs = date.getTimezoneOffset() * 60_000;
    const t = Math.floor((ms - tzOffsetMs) / 1000) + BAR_SECONDS;
    out.push({
      time: t as UTCTimestamp,
      open: b.open, high: b.high, low: b.low, close: b.close,
      volume: typeof b.volume === 'number' ? b.volume : 0,
    });
  }
  out.sort((a, b) => a.time - b.time);
  for (let i = out.length - 1; i > 0; i--) {
    if (out[i].time === out[i - 1].time) out.splice(i, 1);
  }
  return out;
}

export function toPoints(bars: HistoryBar[]): Point[] {
  return toBars(bars).map((b) => ({ time: b.time, value: b.close }));
}

export function localUtcSeconds(date: Date): UTCTimestamp {
  return Math.floor(
    (date.getTime() - date.getTimezoneOffset() * 60_000) / 1000,
  ) as UTCTimestamp;
}

export function themeColors() {
  const css = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) =>
    (css.getPropertyValue(name).trim() || fallback);
  return {
    background: v('--panel-bg', '#ffffff'),
    text: v('--text-primary', '#222'),
    grid: v('--border-default', '#e5e7eb'),
    line: v('--accent-blue', '#2563eb'),
    rsi: v('--accent-purple', '#a855f7'),
    bullish: v('--accent-green', '#16a34a'),
    bearish: v('--accent-red', '#dc2626'),
    archived: v('--accent-yellow', '#f7bd5c'),
  };
}
