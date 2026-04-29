import type { UTCTimestamp } from 'lightweight-charts';
import type { HistoryBar } from '../../api/client';

export const VISIBLE_MINUTES = 90;
export const PRELOAD_HOURS = 24;
export const REFRESH_INTERVAL_MS = 30_000;
export const BAR_SIZE = '1 min';

// v2: bumped to discard ranges saved by an earlier build where transient
// auto-fit states could persist as ~2-min "zooms". Old `v1` entries are
// orphaned (small handful of bytes per target — left for the browser to
// evict on its own).
const ZOOM_STORAGE_KEY = 'ib-chart-zoom-v2';
export type SavedRange = { from: number; to: number };
export type Point = { time: UTCTimestamp; value: number };

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
    // Reject stale ranges narrower than 5 min — these were polluted by
    // an earlier bug where transient auto-fit states got persisted.
    // Falls back to the default 90m window which is what the user
    // expects for a fresh chart.
    if (r.to - r.from < 5 * 60) return null;
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

export function toPoints(bars: HistoryBar[]): Point[] {
  const out: Point[] = [];
  for (const b of bars) {
    const date = new Date(b.ts);
    const ms = date.getTime();
    if (!Number.isFinite(ms)) continue;
    // lightweight-charts always formats UTCTimestamp as UTC. Shift each
    // bar's epoch by its own timezone offset so the axis reads as the
    // user's local wall time. Per-bar so DST flips stay correct.
    const tzOffsetMs = date.getTimezoneOffset() * 60_000;
    const t = Math.floor((ms - tzOffsetMs) / 1000);
    out.push({ time: t as UTCTimestamp, value: b.close });
  }
  out.sort((a, b) => a.time - b.time);
  for (let i = out.length - 1; i > 0; i--) {
    if (out[i].time === out[i - 1].time) out.splice(i, 1);
  }
  return out;
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
  };
}
