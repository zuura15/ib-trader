/** Backend-driven SR adapter.
 *
 *  Single source of truth for support/resistance lines, wedges, and
 *  pivots is now the engine's ``/api/sr`` endpoint (which proxies to
 *  ``/engine/sr`` → ``find_wedges`` + ``detect_lines`` from the Python
 *  ``sr_fan`` module — the same code path the bot uses). This module
 *  fetches that data and maps the timestamp-based payload into the
 *  index-based ``SRLine`` shape the existing SymbolChart renderer
 *  expects, so we can drop the client-side ``detectSupportResistance``
 *  without rewriting the render pipeline.
 */
import type { Bar } from './chartUtils';
import { BAR_SECONDS } from './chartUtils';
import type { SRLine } from './supportResistance';

export interface BackendSrLine {
  type: 'support' | 'resistance';
  from_ts: string | null;
  from_price: number | null;
  anchor_b_ts: string | null;
  anchor_b_price: number | null;
  to_ts: string | null;
  to_price: number | null;
  touches: number;
  is_broken: boolean;
  break_ts: string | null;
  break_price: number | null;
  third_touch_ts: string | null;
  slope_per_bar: number;
  intercept: number;
}

export interface BackendWedge {
  apex_bars_ahead: number;
  apex_idx_float: number;
  overlap_start_ts: string | null;
  right_ts: string | null;
  vertices: {
    support_left: { ts: string; price: number };
    support_right: { ts: string; price: number };
    resistance_right: { ts: string; price: number };
    resistance_left: { ts: string; price: number };
  };
}

export interface BackendSrPayload {
  bars_count: number;
  last_ts: string | null;
  lines: BackendSrLine[];
  wedges: BackendWedge[];
  pivot_lows: string[];
  pivot_highs: string[];
}

/** Convert a backend ISO timestamp into the lightweight-charts
 *  ``UTCTimestamp`` (seconds, shifted by the local tz offset so the
 *  axis displays wallclock-local, plus the slot-end labeling shift
 *  used everywhere else in the chart). Mirrors ``toBars`` in
 *  chartUtils.ts so backend and frontend bars line up by time. */
export function backendTsToChartTime(ts: string): number {
  const d = new Date(ts);
  const ms = d.getTime();
  if (!Number.isFinite(ms)) return 0;
  const tzOffsetMs = d.getTimezoneOffset() * 60_000;
  return Math.floor((ms - tzOffsetMs) / 1000) + BAR_SECONDS;
}

/** Find the index of the bar whose ``time`` is closest to the chart-
 *  time derived from ``backendTs``. Returns ``-1`` when no match is
 *  within one bar of the target. */
function _idxForTs(allBars: Bar[], backendTs: string | null): number {
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

/** Map a backend line payload into the ``SRLine`` shape that
 *  ``SymbolChart``'s rendering loop expects. Indices are resolved
 *  against ``allBars`` (full price array). Returns ``null`` when
 *  any required anchor falls outside the bar set (e.g. backend
 *  returned a line anchored to an older bar that's been rolled
 *  off the chart's history). */
export function backendLineToSRLine(
  bl: BackendSrLine, allBars: Bar[],
): SRLine | null {
  const fromIdx = _idxForTs(allBars, bl.from_ts);
  const anchorBIdx = _idxForTs(allBars, bl.anchor_b_ts);
  const toIdx = _idxForTs(allBars, bl.to_ts);
  if (fromIdx < 0 || anchorBIdx < 0 || toIdx < 0) return null;
  // ``from_price`` is the trusted price-axis anchor; without it we
  // can't safely rebase the intercept into frontend index space.
  // Drop the line rather than fall back to backend's raw intercept
  // (which is in backend-bar-index space and would skew the line).
  if (bl.from_price == null) return null;
  const breakIdx = bl.is_broken && bl.break_ts
    ? _idxForTs(allBars, bl.break_ts)
    : null;
  const thirdTouchIdx = bl.third_touch_ts
    ? _idxForTs(allBars, bl.third_touch_ts)
    : null;
  // Recompute index-space intercept so the renderer's
  // ``slope * idx + intercept`` math stays consistent with the
  // frontend-resolved indices regardless of any drift between the
  // backend's and frontend's first-bar timestamp.
  const intercept = bl.from_price - bl.slope_per_bar * fromIdx;
  return {
    type: bl.type,
    fromIdx,
    anchorBIdx,
    toIdx,
    slope: bl.slope_per_bar,
    intercept,
    touches: bl.touches,
    breakIdx: breakIdx != null && breakIdx >= 0 ? breakIdx : null,
    thirdTouchIdx: thirdTouchIdx != null && thirdTouchIdx >= 0
      ? thirdTouchIdx : null,
  };
}

export interface FetchSrOptions {
  /** Bot's ``near_touch_tolerance_fraction`` if a per-bot YAML value
   *  exists; ``null``/undefined → backend uses the shared default. */
  nearTouchToleranceFraction?: number | null;
  /** Operator's "broken visible for N min" toolbar value, converted
   *  to bars (3-min granularity). ``undefined`` → backend default. */
  breakStaleBars?: number | null;
  /** Match the chart toolbar's "show broken" toggle so wedges that
   *  depend on a broken leg appear / hide consistently. */
  includeBrokenWedges?: boolean;
}

/** Fetch ``/api/sr`` for the given target over a configurable
 *  history window. Returns ``null`` on error / 503. */
export async function fetchBackendSr(
  target: { conId: number | null; symbol: string; secType: string },
  hours: number,
  barSize: string,
  opts: FetchSrOptions = {},
): Promise<BackendSrPayload | null> {
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
  if (opts.nearTouchToleranceFraction != null
      && Number.isFinite(opts.nearTouchToleranceFraction)) {
    params.set(
      'near_touch_tolerance_fraction',
      String(opts.nearTouchToleranceFraction),
    );
  }
  if (opts.breakStaleBars != null
      && Number.isFinite(opts.breakStaleBars)) {
    params.set('break_stale_bars', String(Math.max(1, opts.breakStaleBars)));
  }
  if (opts.includeBrokenWedges) {
    params.set('include_broken_wedges', 'true');
  }
  // Abort timeout: the SR endpoint refreshes every 15 s and can be
  // heavy (pivot detection + line fitting over the lookback window).
  // Without a bound, a stalled ``/engine/sr`` hangs the fetch and the
  // 15 s refresh piles up behind it. 12 s keeps each call below the
  // refresh interval so aborts don't accumulate. A failed/aborted
  // fetch returns null (existing behaviour) — the chart keeps its
  // last-good SR lines until the next successful refresh.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 12_000);
  try {
    const r = await fetch(`/api/sr?${params.toString()}`, {
      signal: controller.signal,
    });
    if (!r.ok) return null;
    const data = (await r.json()) as BackendSrPayload;
    return data;
  } catch {
    return null;
  } finally {
    clearTimeout(timer);
  }
}
