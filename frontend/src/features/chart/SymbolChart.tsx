import {
  forwardRef, useEffect, useImperativeHandle, useRef, useState,
} from 'react';
import {
  createChart, ColorType, LineSeries, HistogramSeries,
  type IChartApi, type ISeriesApi, type UTCTimestamp,
} from 'lightweight-charts';
import { getHistory } from '../../api/client';
import {
  type SavedRange, type Bar,
  VISIBLE_MINUTES, PRELOAD_HOURS, SR_LOOKBACK_HOURS, REFRESH_INTERVAL_MS, BAR_SIZE, BAR_SECONDS,
  targetKey, loadSavedRange, saveRange,
  toBars, themeColors, localUtcSeconds,
} from './chartUtils';
import { computeRsi, detectDivergences, RSI_DEFAULTS } from './rsiDivergence';
import { detectSupportResistance } from './supportResistance';
import {
  fetchBackendSr, backendLineToSRLine, backendTsToChartTime,
  type BackendSrPayload,
} from './srBackend';
import { useStore, type ChartTarget } from '../../data/store';
import { useUserSetting } from '../../data/userSettings';
import { useVisibilityWake } from '../../hooks/useVisibilityWake';

export interface SymbolChartHandle {
  resetZoom: () => void;
  /** Hide the auto-drawn support/resistance lines for the current
   *  target. They re-detect (and re-show) on the next refresh tick or
   *  when the user changes target. */
  clearSupportResistance: () => void;
  /** Inverse of ``clearSupportResistance``: re-enable auto SR overlay
   *  for the current target without waiting for a target switch. The
   *  next detection tick redraws lines + badges. */
  showSupportResistance: () => void;
  /** Whether SR is currently hidden via clearSupportResistance. Bot
   *  panes need this to render the right toggle label. */
  isSupportResistanceHidden: () => boolean;
  /** Toggle between canonical backend SR (default, "BE") and the
   *  legacy local frontend detector ("FE"). Diagnostic only — once
   *  parity is verified the FE path can be deleted. */
  toggleSrSourceMode: () => 'be' | 'fe';
  /** Current SR source: ``'be'`` = backend (canonical), ``'fe'`` =
   *  local detection. */
  getSrSourceMode: () => 'be' | 'fe';
  /** Snapshot the chart's current X (logical) and Y (price) zoom so
   *  it can be reapplied after a flow that's known to disturb the
   *  visible range — e.g. html2canvas cloning the pane DOM, which
   *  trips lightweight-charts' autoSize observer and re-fits the
   *  chart. Pair with ``restoreZoomSnapshot``. */
  snapshotZoom: () => ZoomSnapshot | null;
  restoreZoomSnapshot: (snap: ZoomSnapshot | null) => void;
  /** Freeze every input + auto-fit on the chart for the duration of
   *  an async callback. Disables wheel/pan/pinch, locks Y autoscale,
   *  and forces the time scale to keep its current visible range
   *  even when new bars arrive. Used by the screenshot path so
   *  html2canvas's DOM clone + ResizeObserver firings can't move
   *  the viewport. */
  withFrozenViewport: <T>(fn: () => Promise<T>) => Promise<T>;
}

export interface ZoomSnapshot {
  logical?: { from: number; to: number };
  price?: { from: number; to: number };
}

interface Props {
  target: ChartTarget | null;
  /** Visible minutes default. Defaults to chartUtils VISIBLE_MINUTES (90). */
  visibleMinutes?: number;
  /** Show the RSI sub-pane. Default true. */
  showRsi?: boolean;
  /** Render the placeholder "Click a row…" message when target is null. */
  placeholder?: string | null;
  /** Run S/R detection at all. Set to false on sparkline instances
   *  (stacked-charts) — they're overview thumbnails, not analysis
   *  surfaces, and detection on a 24h slice is the single biggest
   *  per-instance cost (O(N²) pivot fan, runs every 1s on every
   *  chart). Default true. */
  enableSr?: boolean;
  /** Show broken/archived S/R lines (amber dashed). Off by default;
   *  the line stays in the SR engine state for ``breakStaleBars`` but
   *  is filtered out of render unless this is true. */
  showBrokenSr?: boolean;
  /** How far back to keep broken S/R lines visible, in minutes.
   *  Maps internally to ``SROptions.breakStaleBars`` via the 3-min
   *  bar size. Only effective when ``showBrokenSr`` is true. */
  brokenMinutes?: number;
  /** Show down-sloping support lines. Off by default — a support
   *  whose pivots step DOWN through time is counter-trend (price is
   *  making lower lows on each touch), so the line predicts further
   *  weakness rather than a bounce. Useful for some traders, off by
   *  default for the standard with-trend reading. */
  showCounterSupport?: boolean;
  /** Show up-sloping resistance lines. Off by default — symmetric to
   *  ``showCounterSupport``: a resistance whose pivots step UP is
   *  also counter-trend (price is making higher highs on each
   *  rejection), arguing the resistance is weakening. */
  showCounterResistance?: boolean;
  /** Optional callback invoked whenever loading state changes. */
  onLoadingChange?: (loading: boolean) => void;
  /** Optional callback for errors. */
  onError?: (msg: string | null) => void;
  /** Bot-mode overlay. When set, draws the chart_signal bot's frozen
   *  entry line in a distinct orange weight on top of the regular SR
   *  overlay. The line is projected across the entire price history
   *  via ``price = anchorPrice + slopePerSec * (chartTime - anchorChartTime)``.
   *  Chart times are shifted-UTC (see ``localUtcSeconds``); the prop
   *  carries the bot's real-UTC ISO anchor and we shift internally. */
  entryLine?: {
    /** Real-UTC ISO timestamp of the line's anchor pivot (P bar). */
    anchorTime: string;
    /** Price at the anchor pivot. */
    anchorPrice: number;
    /** Per-second slope in price-per-second. */
    slopePerSec: number;
    /** Optional Real-UTC ISO of Q (the older construction pivot).
     *  When present, the chart clips the line render to start here
     *  instead of projecting backward to ``bars[0]``. Pre-Q the line
     *  carries no validation — drawing it across pre-Q history looks
     *  like a trend the bot validated when it never did. */
    fromTime?: string;
  } | null;
  /** Bot-mode badge highlight. Real-UTC ISO of the bar whose B/S badge
   *  represents the bot's active entry — the badge gets a green ring
   *  drawn behind it so the user can see "this is the signal we acted
   *  on". Time is matched against the badge's chart time after
   *  converting via ``localUtcSeconds``.
   *
   *  Also drives **synthetic badge injection**: when the bot's slice or
   *  algorithm finds a signal the chart's own detection missed, a B or
   *  S badge is added to ``srSignalsRef`` at this time so the user can
   *  still see what the bot acted on. ``entrySide`` controls whether
   *  the injected badge is a buy or a sell. */
  activeEntryBarTime?: string | null;
  /** Direction of the bot's active entry — ``'long'`` injects a B
   *  badge, ``'short'`` injects an S. Ignored when
   *  ``activeEntryBarTime`` is null. Optional for back-compat; defaults
   *  to ``'long'`` so existing callers keep their behavior. */
  entrySide?: 'long' | 'short' | null;
  /** Optional CSS color applied to the chart's layout background.
   *  Used by chart-bot panes to tint the entire pane (faint blue)
   *  when a position is held, so the active bot is unmistakable
   *  across the multi-pane workstation. When ``null``/undefined,
   *  the chart falls back to the theme's panel background. */
  paneBackground?: string | null;
  /** Bot-pane semantics: suppress auto-generated B/S badges from
   *  the SR detector. In bot panes the only badge that should ever
   *  appear is one tied to an actual order — the bot's active entry
   *  (driven by ``activeEntryBarTime`` + ``entrySide``) — so the
   *  operator can trust "if there's a B/S on the chart, the bot
   *  placed an order at that bar". Defaults to false (view-only
   *  panes continue to show auto-detected signals). */
  suppressAutoSignals?: boolean;
  /** Explicit historical-fire markers: one entry per actual bot
   *  order placed in the lookback window. ``barTime`` is the
   *  trigger bar's slot-start ISO (real-UTC), ``side`` drives
   *  whether a B (long) or S (short) renders. Used alongside
   *  ``suppressAutoSignals`` so the bot pane always shows the
   *  operator a 24 h record of where it fired, even after the
   *  active entry has exited. */
  historicalFires?: Array<{ barTime: string; side: 'long' | 'short'; price: number }>;
}

export const SymbolChart = forwardRef<SymbolChartHandle, Props>(function SymbolChart(
  {
    target,
    visibleMinutes = VISIBLE_MINUTES,
    showRsi = true,
    placeholder = 'Click a row in Positions or Watchlist to chart it.',
    enableSr = true,
    showBrokenSr = false,
    brokenMinutes = 30,
    showCounterSupport = false,
    showCounterResistance = false,
    onLoadingChange,
    onError,
    entryLine = null,
    activeEntryBarTime = null,
    entrySide = null,
    paneBackground = null,
    suppressAutoSignals = false,
    historicalFires,
  }: Props,
  ref,
) {
  const targetRef = useRef(target);
  useEffect(() => { targetRef.current = target; }, [target]);

  // Visibility-wake plumbing. Each data-source useEffect (historical
  // load, SR, fuzzy, regime, live-tick WS) pushes a callback onto this
  // set on mount and removes it on cleanup. When the browser tab
  // returns to ``visible`` after being throttled / discarded, the
  // single ``useVisibilityWake`` below fires every registered callback
  // so the chart catches up immediately instead of waiting for the
  // next 15-30 s poll cycle or for the WS to detect the dead socket.
  //
  // ``useState`` (lazy init) gives us a Set whose identity is stable
  // across renders without the ref-cleanup lint warning a ``useRef``
  // would trigger.
  const [wakeSubs] = useState<Set<() => void>>(() => new Set());
  useVisibilityWake(() => {
    for (const fn of wakeSubs) {
      try { fn(); } catch { /* one subscriber crashing must not block others */ }
    }
  });

  // Operator-driven resync (top-header Resync button). Subscribed
  // here so the chart's long-lived effects (WS, history, SR, the
  // sticky-signal-clear at the next useEffect) can include it in
  // their dep arrays. Selector-scoped subscribe — re-renders only
  // when the token actually changes. MUST stay above the next
  // useEffect or its dep-array reference TDZs at mount time.
  const resyncToken = useStore((s) => s.resyncToken);

  // Wipe sticky buy/sell signals + cached backend SR payload when
  // the user switches to a different symbol. Without this, MGCM6's
  // historical badges would render on ESM6's chart at the same
  // wallclock pivot times, AND the cached /api/sr payload from the
  // previous symbol would map its timestamps onto the new symbol's
  // ``allBars`` for the ~15s gap before the next fetch arrives,
  // painting wrong-symbol SR lines briefly.
  useEffect(() => {
    srSignalsRef.current = [];
    botPinnedBadgeRef.current = null;
    srBackendDataRef.current = null;
    srBackendApexRef.current = null;
    srArrowsRef.current = [];
    repositionSrSignalsRef.current?.();
  }, [target?.conId, target?.symbol, target?.secType, resyncToken]);

  // Global setting: how many bars after firing a signal counts as
  // "active" before it dims to historical. Read via store hook so a
  // settings change re-runs the reposition immediately.
  const signalActiveBars = useUserSetting('signalActiveBars');
  const signalActiveBarsRef = useRef(signalActiveBars);
  useEffect(() => {
    signalActiveBarsRef.current = signalActiveBars;
    repositionSrSignalsRef.current?.();
  }, [signalActiveBars]);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const rsiSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const divergenceSeriesRef = useRef<ISeriesApi<'Line'>[]>([]);
  // Auto-detected support/resistance lines on the price pane. Recreated
  // every refresh; `srHiddenRef` lets the user dismiss them for the
  // current target via the Clear-SR button (re-shows on target switch).
  const srSeriesRef = useRef<ISeriesApi<'Line'>[]>([]);
  // Bot-mode: the chart_signal strategy's frozen entry line. Lives on
  // its own series so the SR recompute (which wipes ``srSeriesRef``)
  // doesn't erase it on every pan/zoom. Created/torn down with the
  // chart; data updated by an effect when the ``entryLine`` prop or
  // the bar history changes.
  const entryLineSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  // Volume overlay: faint histogram pinned to the bottom 15% of the
  // price pane via its own price scale (``priceScaleId: 'volume'``)
  // with ``scaleMargins: { top: 0.85, bottom: 0 }``. Sits underneath
  // the price line without affecting the auto-fit of the middle
  // third where the candle polyline lives.
  const volumeSeriesRef = useRef<ISeriesApi<'Histogram'> | null>(null);
  // Buy/Sell letters drawn on an SVG overlay positioned over the
  // chart canvas. Each signal is { time, price, side } — the letter
  // floats above (S) or below (B) the bar with a black leader line
  // back to the exact (time, price) on the polyline. Repositioned on
  // every visible-range change and resize.
  type Signal = {
    time: UTCTimestamp;
    price: number;
    side: 'B' | 'S';
  };
  const srSignalsRef = useRef<Signal[]>([]);
  // Bot's entry-bar badge lives in its OWN ref so it never gets
  // evicted by the bounded ``srSignalsRef`` queue. The bounded queue
  // holds the SR detector's organic 3-touch badges; in a long session
  // those accumulate and pushed the bot's synthetic entry badge off
  // the chart (capacity 50). The pinned badge is at most one entry
  // per chart and is rendered in the same paint loop alongside the
  // organic badges.
  const botPinnedBadgeRef = useRef<Signal | null>(null);
  // Arrowhead overlay — pairs of (support, resistance) lines converging
  // ahead of the right edge. Each entry holds three (time, price)
  // vertices that the repaint loop projects into pixel coordinates
  // for an SVG <polygon>. Refilled on every SR recompute; positioned
  // alongside the B/S badges on every visible-range change.
  type Arrow = {
    // Four-vertex trapezoid (bottom-left, bottom-right, top-right,
    // top-left). The right edge is clipped to ``sliceLast`` (the
    // last fully-closed bar) because lightweight-charts'
    // ``timeToCoordinate`` returns null for times past the visible
    // range — using the off-screen apex as a vertex silently
    // dropped the polygon on every paint.
    aTime: UTCTimestamp; aPrice: number;   // support, left
    bTime: UTCTimestamp; bPrice: number;   // support, right (at sliceLast)
    cTime: UTCTimestamp; cPrice: number;   // resistance, right (at sliceLast)
    dTime: UTCTimestamp; dPrice: number;   // resistance, left
    apexBarsAhead: number;
  };
  const srArrowsRef = useRef<Arrow[]>([]);
  const srApexBadgeRef = useRef<HTMLDivElement | null>(null);
  // Canonical SR data: full /api/sr payload (lines + wedges +
  // pivots, with timestamps + prices). The chart NO LONGER computes
  // SR locally; this ref is the single source of truth for what
  // gets drawn. Updated by the periodic fetch effect below.
  const srBackendDataRef = useRef<BackendSrPayload | null>(null);
  // SR source toggle: 'be' = backend canonical (default), 'fe' =
  // legacy frontend ``detectSupportResistance`` (diagnostic — lets
  // the operator confirm whether a missing line is a backend issue
  // vs a rendering issue). Stored as a ref so the SR recompute
  // closure reads the latest value without a chart rebuild.
  const srSourceModeRef = useRef<'be' | 'fe'>('be');
  const [srSourceModeTick, setSrSourceModeTick] = useState(0);
  // Apex distances surfaced in the top-left badge — derived from the
  // canonical backend payload's wedges. ``null`` until first fetch
  // settles, then either an empty array (no wedges) or a list of
  // up to three nearest apex distances.
  const srBackendApexRef = useRef<number[] | null>(null);
  // Tooltip text for the apex badge — one line per wedge with the
  // four vertex timestamps. Rebuilt on every SR recompute alongside
  // ``srBackendApexRef``.
  const srBackendTooltipRef = useRef<string>('');
  const srOverlayRef = useRef<SVGSVGElement | null>(null);
  const srHiddenRef = useRef(false);
  // Bumps on Clear/Show toggle so callers (BotChart) can re-render
  // the toolbar label without polling.
  const [srHiddenTick, setSrHiddenTick] = useState(0);
  // ``chartVersion`` is bumped by the chart-create effect once the
  // chart instance + entry-line series exist. Multiple effects key
  // off it so they re-fire when the chart is re-built (theme /
  // showRsi toggle). Declared up here so effects defined below the
  // refs (e.g. ``historicalFires`` at line ~410) can reference it
  // in their deps without tripping the JS temporal-dead-zone.
  const [chartVersion, setChartVersion] = useState(0);
  // CHART_BAR_STALL diagnostic. Surfaces a thin amber pill in the
  // top-center of the chart canvas when the live-tick handler sees
  // a new bar-slot more than 90 s past the previous one (i.e. the
  // bar-materialisation skipped at least one minute). Visible in the
  // app — no DevTools required. Clears itself when bars resume
  // normal cadence. Pure observation; no behaviour change.
  const [barStall, setBarStall] = useState<
    { gapSec: number; sinceMs: number; prevBarIso: string; newBarIso: string } | null
  >(null);
  // Track filter toggles via refs so the throttled recompute closure
  // sees fresh values without re-subscribing on every prop change.
  const showBrokenSrRef = useRef(showBrokenSr);
  const brokenMinutesRef = useRef(brokenMinutes);
  const showCounterSupportRef = useRef(showCounterSupport);
  const showCounterResistanceRef = useRef(showCounterResistance);
  const suppressAutoSignalsRef = useRef(suppressAutoSignals);
  // Historical bot fires, kept in chart-time (shifted UTC) form so
  // the SR recompute loop can splice them directly into srSignalsRef
  // without converting on every iteration.
  const historicalFiresRef = useRef<Signal[]>([]);
  // Companion render data: one entry per historical fire describing
  // the pivot→fill segment and the ``+``/``−`` marker at the fill
  // endpoint. Painted alongside the B/S badges so every order has a
  // visible "this is where we acted, this is where we filled" cue.
  type FillRender = {
    side: 'long' | 'short';
    pivotTime: UTCTimestamp;
    pivotPrice: number;
    fillTime: UTCTimestamp;
    fillPrice: number;
  };
  const fillRenderRef = useRef<FillRender[]>([]);
  // lightweight-charts LineSeries handles for the pivot→fill segments.
  // Recreated whenever historicalFires changes; cleaned up here so a
  // stale segment never lingers after a trade exits the 24 h window.
  const fillSegmentSeriesRef = useRef<ISeriesApi<'Line'>[]>([]);
  // Set true by ``withFrozenViewport`` for the duration of an async
  // callback (e.g. html2canvas capture). The custom ResizeObserver
  // checks this and skips its setAutoScale(true) re-fit so a layout
  // reflow during capture can't undo the explicit setVisibleRange
  // we pinned at freeze-start.
  const viewportFrozenRef = useRef(false);
  // Bot-mode prop mirrors. Read by the SVG badge painter and the
  // entry-line series effect.
  const entryLineRef = useRef(entryLine);
  const activeEntryBarTimeRef = useRef<number | null>(null);
  useEffect(() => { entryLineRef.current = entryLine; }, [entryLine]);
  useEffect(() => {
    // Convert real-UTC ISO to chart-time (shifted UTC), then shift
    // ``+BAR_SECONDS`` so the badge sits at the bar's slot-END
    // label — co-located with the bar at that x in ``toBars``.
    if (!activeEntryBarTime) {
      activeEntryBarTimeRef.current = null;
    } else {
      const d = new Date(activeEntryBarTime);
      activeEntryBarTimeRef.current = Number.isFinite(d.getTime())
        ? (localUtcSeconds(d) as number) + BAR_SECONDS : null;
    }

    // Synthetic badge injection: if the bot tells us it acted on a
    // signal at this time AND no matching badge exists in
    // ``srSignalsRef`` (the chart's local SR detector either ran on a
    // narrower slice or hasn't caught up yet), we add one so the user
    // sees the same signal the bot did. Without this, the chart-bot
    // pane can silently "miss" a B/S that produced a real order — the
    // exact "no B/S badge but order placed" gap reported 2026-05-11.
    const chartT = activeEntryBarTimeRef.current;
    const side: 'B' | 'S' | null = activeEntryBarTime && entrySide
      ? (entrySide === 'long' ? 'B' : 'S')
      : null;
    if (chartT != null && side != null) {
      // Store the bot's entry badge in its own pinned slot —
      // separate from the bounded ``srSignalsRef`` queue so a busy
      // session can't evict it. Anchor the badge at the TRENDLINE's
      // value at the pivot bar's x (computed from entryLine's
      // anchor + slope) so the S/B sits on the line that triggered
      // the entry. With the loose 4th+ touch rule the pivot bar's
      // close can be up to ``near_tol`` away from the line, which
      // made the badge appear to hang in the air relative to the
      // trendline. Falls back to bar.close when entryLine isn't
      // available yet.
      let anchorPrice: number | null = null;
      if (entryLine && entryLine.anchorTime
          && typeof entryLine.anchorPrice === 'number'
          && typeof entryLine.slopePerSec === 'number') {
        const aDate = new Date(entryLine.anchorTime);
        if (Number.isFinite(aDate.getTime())) {
          const aChartSec = (localUtcSeconds(aDate) as number) + BAR_SECONDS;
          anchorPrice = entryLine.anchorPrice
            + entryLine.slopePerSec * (chartT - aChartSec);
        }
      }
      if (anchorPrice == null) {
        const bar = barsRef.current.find(
          (b) => Math.abs((b.time as number) - chartT) < 1,
        );
        if (bar) anchorPrice = bar.close;
      }
      if (anchorPrice != null && Number.isFinite(anchorPrice)) {
        botPinnedBadgeRef.current = {
          time: chartT as UTCTimestamp, price: anchorPrice, side,
        };
      }
    } else {
      botPinnedBadgeRef.current = null;
    }

    // Re-paint badges so the ring appears/disappears immediately.
    repositionSrSignalsRef.current?.();
  }, [activeEntryBarTime, entrySide, entryLine]);
  // Convert prop fires (real-UTC ISO + price) into chart-time
  // Signal entries on every change. The painter reads from
  // ``historicalFiresRef`` which the SR recompute splices in.
  useEffect(() => {
    try {
      const chart = chartRef.current;
      // Clean up any prior fill-segment LineSeries before rebuilding.
      if (chart) {
        for (const ds of fillSegmentSeriesRef.current) {
          try { chart.removeSeries(ds); } catch { /* already gone */ }
        }
      }
      fillSegmentSeriesRef.current = [];

      if (!historicalFires || historicalFires.length === 0) {
        historicalFiresRef.current = [];
        fillRenderRef.current = [];
        srSignalsRef.current = [];
        repositionSrSignalsRef.current?.();
        return;
      }
      const out: Signal[] = [];
      const fillOut: FillRender[] = [];
      for (const f of historicalFires) {
        const d = new Date(f.barTime);
        if (!Number.isFinite(d.getTime())) continue;
        // +BAR_SECONDS shifts to slot-end labelling (matches toBars).
        const pivotT = (localUtcSeconds(d) as number) + BAR_SECONDS;
        // Anchor the B/S badge at the pivot bar's CLOSE so the badge
        // sits on the price polyline at the trigger pivot. Fill
        // price is recorded separately for the ±-marker endpoint.
        // Falls back to the fill price when bars haven't loaded yet.
        const pivotBar = barsRef.current.find(
          (b) => (b.time as number) === pivotT,
        );
        const pivotClose = pivotBar ? pivotBar.close : f.price;
        const fillT = (pivotT + BAR_SECONDS) as UTCTimestamp;
        out.push({
          time: pivotT as UTCTimestamp,
          price: pivotClose,
          side: f.side === 'short' ? 'S' : 'B',
        });
        fillOut.push({
          side: f.side,
          pivotTime: pivotT as UTCTimestamp,
          pivotPrice: pivotClose,
          fillTime: fillT,
          fillPrice: f.price,
        });
      }
      historicalFiresRef.current = out;
      fillRenderRef.current = fillOut;
      // Build a lightweight-charts LineSeries per pivot→fill segment.
      // Each addSeries / setData is wrapped because the chart can
      // be mid-teardown when chartVersion just bumped — an addSeries
      // on a disposed chart instance throws, and an uncaught throw
      // bubbles up to flexlayout's "Error rendering component"
      // overlay.
      if (chart) {
        const colors = themeColors();
        for (const fr of fillOut) {
          const colour = fr.side === 'long' ? colors.bullish : colors.bearish;
          try {
            const ds = chart.addSeries(LineSeries, {
              color: colour,
              lineWidth: 2,
              lineStyle: 0,
              priceLineVisible: false,
              lastValueVisible: false,
              crosshairMarkerVisible: false,
              autoscaleInfoProvider: () => null,
            });
            ds.setData([
              { time: fr.pivotTime, value: fr.pivotPrice },
              { time: fr.fillTime, value: fr.fillPrice },
            ]);
            fillSegmentSeriesRef.current.push(ds);
          } catch { /* chart disposed / range invalid — skip */ }
        }
      }
      // Force the painter to repaint so newly-fetched fires show up
      // immediately rather than waiting for the next SR recompute.
      srSignalsRef.current = out;
      repositionSrSignalsRef.current?.();
    } catch (err) {
      // Last-resort guard: never let a fill-render hiccup take the
      // whole pane down. Log and clear so subsequent recomputes can
      // re-attempt cleanly.
      try { console.warn('historical fires effect failed', err); }
      catch { /* console missing — ignore */ }
      historicalFiresRef.current = [];
      fillRenderRef.current = [];
    }
  }, [historicalFires, chartVersion]);

  useEffect(() => {
    showBrokenSrRef.current = showBrokenSr;
    brokenMinutesRef.current = brokenMinutes;
    showCounterSupportRef.current = showCounterSupport;
    showCounterResistanceRef.current = showCounterResistance;
    suppressAutoSignalsRef.current = suppressAutoSignals;
    // Re-render lines on toggle. The viewport gate is gone (detection
    // now runs on a fixed recent-bars slice), so this is just a
    // straight re-detect to pick up the new filter state.
    scheduleSrRecomputeRef.current?.(true);
  }, [showBrokenSr, brokenMinutes, showCounterSupport, showCounterResistance,
      suppressAutoSignals]);

  // Debounced SR recompute. Set inside the chart-create effect (since
  // it captures the chart instance); the visible-range subscription
  // calls into it via this ref so we don't have to re-subscribe each
  // time the closure changes.
  const scheduleSrRecomputeRef = useRef<((force?: boolean) => void) | null>(null);
  // Repaints the SVG overlay with current ``srSignalsRef`` content.
  // Set inside the chart-create effect (captures chart + container).
  // Called from the SR recompute and from pan/zoom/resize events.
  const repositionSrSignalsRef = useRef<(() => void) | null>(null);
  const paintApexBadgeRef = useRef<(() => void) | null>(null);
  // Regime badge — top-left, directly under the SR apex badge.
  // Shows ADX / +DI / −DI / ATR / Donchian extremes from the
  // V-state/ADX regime diagnostic badge fully removed 2026-06-02.
  // No longer fetched, no longer painted. Restore from git history if
  // the diagnostic is needed again.
  // Below this many bars, SR detection is skipped entirely — the
  // existing lines stay on screen. 30 bars × 3 min = 90 min.
  // The old ``SR_MIN_BARS`` viewport gate was removed (2026-05-10): it
  // suppressed *all* SR detection when the visible window was below 90
  // min, which silently dropped 2- and 3-touch lines whose endpoints
  // were all inside a tighter zoom. Detection now runs at every zoom,
  // but still on the **visible-range slice** — the user explicitly
  // opted to keep off-screen pivots out of the picture so the chart
  // doesn't paint lines that look "from nowhere" without zooming out.
  // Wider trend detection is a zoom-out gesture, by design.
  // Tracks the most-recent live-tick's rounded bar time. Used to
  // detect bar-close events (boundary crossings) and trigger an SR
  // recompute. Null until the first live tick arrives for the
  // current target.
  const lastTickBarSecRef = useRef<number | null>(null);
  // Full OHLC bars that mirror the price series. The price series
  // itself only carries close (it's a LineSeries), so SR detection
  // — which needs wick high/low for pivot detection — reads from
  // this ref instead. Kept in sync by load() (replaces the array)
  // and the live-tick handler (in-place fold of high/low/close).
  const barsRef = useRef<Bar[]>([]);
  const userRangeRef = useRef<SavedRange | null>(null);
  // Viewport mirror in bar-relative terms. Captured on every pan/zoom
  // so the periodic ``load()`` refresh can restore the same view past
  // newly-arrived bars. Storing ``{ from, to }`` absolutely would
  // strand the view behind the latest bar after setData expands the
  // dataset — so we store:
  //   ``rightSpaceBars`` = bars past the last-known bar that the user
  //                         kept visible (right-edge gap; rightOffset
  //                         normally puts this at ``rightOffset``).
  //   ``widthBars``      = visible width in bars (zoom level).
  // On restore: newTo = (barCount − 1) + rightSpaceBars; newFrom =
  // newTo − widthBars. Net effect: new bars push the chart LEFT while
  // the right-edge gap and zoom stay constant, which is the live-
  // following behavior the operator wants.
  const userLogicalRangeRef = useRef<
    { rightSpaceBars: number; widthBars: number } | null
  >(null);
  // Y-axis zoom persistence. lightweight-charts doesn't fire an event
  // for price-scale changes (only time-scale), so we sample the
  // current price range at refresh time and restore it after setData.
  // Cleared on target change and on Reset-Zoom (which auto-scales).
  const userPriceRangeRef = useRef<{ from: number; to: number } | null>(null);
  // Gate live-tick updates until historical data has been loaded at
  // least once. Without this, a live quote landing in the ~1-3s window
  // before getHistory() returns adds a single point to an empty series;
  // lightweight-charts then auto-fits the time scale to that one point
  // (a 1-second-wide window that scrolls per-tick) and the historical
  // setVisibleRange that runs later doesn't always reclaim the wide
  // window. Symptom: chart loads "almost realtime per second" instead
  // of the configured 90-min view.
  const historicalLoadedRef = useRef(false);

  const [theme, setTheme] = useState<string>(
    () => document.documentElement.getAttribute('data-theme') || 'light',
  );
  // ``chartVersion`` declared near the top with the other state refs
  // so it's accessible to effects defined above this point.

  // Bot-mode: project the frozen entry line across the bar history.
  // Recomputes whenever the line prop or chart version changes; the
  // chart-create effect bumps ``chartVersion`` once the entryLine
  // series is ready, so we don't fire before the series exists. When
  // the prop is null (no open position / disarmed), clear the series.
  useEffect(() => {
    const ser = entryLineSeriesRef.current;
    if (!ser) return;
    const bars = barsRef.current;
    if (!entryLine || bars.length < 2) {
      try { ser.setData([]); } catch { /* series torn down */ }
      return;
    }
    const anchorDate = new Date(entryLine.anchorTime);
    if (!Number.isFinite(anchorDate.getTime())) {
      try { ser.setData([]); } catch { /* ignore */ }
      return;
    }
    // +BAR_SECONDS keeps the anchor + Q endpoints aligned with the
    // slot-end-labelled bars produced by ``toBars``.
    const anchorChartSec = (localUtcSeconds(anchorDate) as number) + BAR_SECONDS;
    // Clip the line's left endpoint at Q when the bot provides it.
    // Without ``fromTime``, the line is projected backward to
    // ``bars[0]``, which historically read as a long-running trend
    // the bot never validated. With ``fromTime``, the line starts at
    // Q (the first construction pivot) — pre-Q region has no line.
    let startTime = bars[0].time as UTCTimestamp;
    if (entryLine.fromTime) {
      const fromDate = new Date(entryLine.fromTime);
      if (Number.isFinite(fromDate.getTime())) {
        const fromChartSec = (localUtcSeconds(fromDate) as number) + BAR_SECONDS;
        if (fromChartSec > (bars[0].time as number)) {
          startTime = fromChartSec as UTCTimestamp;
        }
      }
    }
    const endTime = bars[bars.length - 1].time as UTCTimestamp;
    const valueAt = (t: number) =>
      entryLine.anchorPrice + entryLine.slopePerSec * (t - anchorChartSec);
    try {
      ser.setData([
        { time: startTime, value: valueAt(Number(startTime)) },
        { time: endTime, value: valueAt(Number(endTime)) },
      ]);
    } catch { /* series torn down between render and effect */ }
  }, [entryLine, chartVersion]);

  // Apply a custom pane background when the parent asks for one
  // (chart-bot panes tint blue while a position is held). Drives the
  // chart's ``layout.background`` directly via applyOptions so we
  // don't have to rebuild the chart on every fsm-state change.
  useEffect(() => {
    const chart = chartRef.current;
    if (!chart) return;
    const fallback = themeColors().background;
    try {
      chart.applyOptions({
        layout: {
          background: {
            type: ColorType.Solid,
            color: paneBackground ?? fallback,
          },
        },
      });
    } catch { /* chart torn down between render and effect */ }
  }, [paneBackground, chartVersion]);

  // Theme observer.
  useEffect(() => {
    const obs = new MutationObserver(() => {
      const t = document.documentElement.getAttribute('data-theme') || 'light';
      setTheme(t);
    });
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
    return () => obs.disconnect();
  }, []);

  // Create / recreate chart on theme change. showRsi changes also trigger
  // a rebuild since the pane structure differs.
  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const colors = themeColors();
    // autoSize=true makes lightweight-charts auto-track its container's
    // size — important when the chart mounts inside a flex layout that
    // hasn't measured yet (otherwise the chart inits at 0x0 and never
    // recovers, even after the container resizes).
    const chart = createChart(el, {
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: colors.background },
        textColor: colors.text,
        attributionLogo: false,
      },
      grid: {
        vertLines: { color: colors.grid },
        horzLines: { color: colors.grid },
      },
      timeScale: {
        timeVisible: true,
        secondsVisible: false,
        borderColor: colors.grid,
        // Push the timeline left when a new bar lands so the right-edge
        // gap (last bar → price axis) stays constant. lightweight-charts
        // only applies the shift when the most-recent bar is already on
        // screen — if the user has panned back to examine an older
        // window, the view stays put. So this gives the live-following
        // behavior the operator wants without yanking them around when
        // they've explicitly scrolled away.
        shiftVisibleRangeOnNewBar: true,
        // Always reserve a few bars of empty space on the right edge.
        // Without this the most-recent bar (which is up to 3 min behind
        // wall-clock with our 3-min bar rounding) sits flush against
        // the right border and the time between "last bar's start"
        // and "now" is invisible — the user sees the chart "stop" 2-3
        // min before clock. 4 bars × 3 min = 12 min of headroom keeps
        // the live mark + horizontal price line clearly in view.
        rightOffset: 4,
      },
      // ``scaleMargins`` reserves the top and bottom thirds of the
      // chart as empty space, so the auto-fitted price polyline fills
      // the middle third — exactly the "amp = 1/3 of chart, centered"
      // rule. Auto-scale is left on (default true) so the polyline
      // re-fits to that middle band as new bars come in, without
      // fighting our setVisibleRange. Manual Y-zoom (wheel/pinch)
      // still overrides this by switching auto-scale off.
      rightPriceScale: {
        borderColor: colors.grid,
        // Price polyline fills 80% of the pane (top 5% padding,
        // bottom 15% for the volume histogram). Matches TradingView
        // proportions where price dominates and volume sits as a
        // thin band underneath. Earlier 0.25/0.25 squashed price
        // into the middle 50% with empty space top and bottom.
        scaleMargins: { top: 0.05, bottom: 0.15 },
      },
      crosshair: { mode: 1 },
    });
    const series = chart.addSeries(LineSeries, {
      color: colors.line,
      lineWidth: 2,
      // ``priceLineVisible: true`` draws a horizontal dashed line at
      // the latest data point's value, with a price label on the
      // right axis. It floats with every ``series.update()`` call,
      // so it's the visible "this chart is live and this is the
      // current price" cue that TradingView shows. Without it the
      // last point updates in place but the visual delta within the
      // bar is hard to perceive on a slow-moving instrument.
      priceLineVisible: true,
      lastValueVisible: true,
    });

    let rsi: ISeriesApi<'Line'> | null = null;
    if (showRsi) {
      rsi = chart.addSeries(LineSeries, {
        color: colors.rsi,
        lineWidth: 1,
        priceLineVisible: false,
        lastValueVisible: true,
        priceScaleId: 'right',
      }, 1);
      rsi.createPriceLine({
        price: 50,
        color: colors.grid,
        lineWidth: 1,
        lineStyle: 1,        // dotted
        axisLabelVisible: false,
        title: '',
      });
      try {
        const panes = chart.panes();
        panes[0]?.setStretchFactor(4);
        panes[1]?.setStretchFactor(1);
      } catch { /* older lightweight-charts builds — silently skip */ }
    }

    // Volume histogram — overlay in the bottom 15% of the price pane.
    // Own price scale id (``'volume'``) so it auto-fits independently
    // of the price polyline; ``scaleMargins`` keeps it pinned to the
    // bottom strip. Faint per-bar colors so it reads as ambient
    // context, not a foreground feature.
    const volume = chart.addSeries(HistogramSeries, {
      priceFormat: { type: 'volume' },
      priceScaleId: 'volume',
      color: 'rgba(120, 144, 156, 0.45)',
      priceLineVisible: false,
      lastValueVisible: false,
    });
    chart.priceScale('volume').applyOptions({
      scaleMargins: { top: 0.85, bottom: 0 },
      // Hide the volume axis labels — lightweight-charts renders one
      // axis per priceScaleId, and the extra column was crowding the
      // chart area and making the price axis labels look oversized.
      // The histogram itself still draws at the bottom of the pane.
      visible: false,
    });

    chartRef.current = chart;
    seriesRef.current = series;
    rsiSeriesRef.current = rsi;
    volumeSeriesRef.current = volume;
    divergenceSeriesRef.current = [];

    // Bot-mode: dedicated series for the chart_signal entry line. Kept
    // separate from ``srSeriesRef`` so the SR recompute (which wipes
    // those on every pan/zoom) doesn't take this with it. Orange and
    // a touch thicker than detected SR lines so it's obviously the
    // one the bot is acting on. ``autoscaleInfoProvider: null`` so a
    // far-projected slope can't squash the price scale.
    entryLineSeriesRef.current = chart.addSeries(LineSeries, {
      color: 'rgba(255, 153, 0, 0.95)',
      lineWidth: 2,
      lineStyle: 0,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      autoscaleInfoProvider: () => null,
    });

    // SVG overlay for SR buy/sell signal letters. Sits on top of the
    // chart canvas; pointer-events: none so it never intercepts
    // pan/zoom. Repositioned on every visible-range change and resize.
    const overlaySvg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    overlaySvg.setAttribute(
      'style',
      'position:absolute;inset:0;width:100%;height:100%;'
      + 'pointer-events:none;overflow:visible;z-index:10;',
    );
    el.style.position = el.style.position || 'relative';
    el.appendChild(overlaySvg);
    srOverlayRef.current = overlaySvg;

    // Inner paint. Don't call directly; use the rAF-coalesced wrapper
    // assigned to ``repositionSrSignalsRef.current`` below. Reading
    // ``ts.timeToCoordinate`` right after a ``series.update()`` returns
    // stale coordinates because lightweight-charts hasn't relaid out
    // yet — deferring to the next animation frame makes the badge
    // positions track the polyline exactly.
    let srRafQueued = false;
    const paintSrSignals = () => {
      const svg = srOverlayRef.current;
      const ch = chartRef.current;
      const ser = seriesRef.current;
      if (!svg || !ch || !ser) return;
      // Empty + repaint. Cheap (handful of children); keeps the
      // logic linear instead of diffing.
      while (svg.firstChild) svg.removeChild(svg.firstChild);

      // Arrowhead polygons — painted FIRST so they sit under badges /
      // leader lines. Faint fill, no stroke. Skipped when there are
      // no detected pairs (most bars).
      // Render ONLY the innermost wedge (smallest apex distance).
      // The detector sorts ``srArrowsRef`` ascending by apexBarsAhead
      // so arrowList[0] is the most-imminent triangle. Showing every
      // overlapping wedge buries the relevant one in stripes; the
      // ``△`` badge already enumerates all apex distances for the
      // operator's situational awareness.
      const arrowList = srArrowsRef.current.slice(0, 1);
      if (arrowList.length > 0) {
        const tsArrow = ch.timeScale();
        let painted = 0;
        let skippedNull = 0;
        // Diagonal-stripe pattern. ID must be UNIQUE PER CHART —
        // four chart-bot panes each create their own SVG and clear
        // it on every paint, so a fixed id="wedge-stripes" gives 4
        // elements with the same id; SVG url(#…) refs resolve to
        // whichever is first in the document, leaving the others
        // with a broken (invisible) fill. Random suffix per paint.
        const patternId = `wedge-stripes-${
          Math.random().toString(36).slice(2, 9)
        }`;
        const defs = document.createElementNS(
          'http://www.w3.org/2000/svg', 'defs',
        );
        const pat = document.createElementNS(
          'http://www.w3.org/2000/svg', 'pattern',
        );
        pat.setAttribute('id', patternId);
        pat.setAttribute('patternUnits', 'userSpaceOnUse');
        pat.setAttribute('width', '6');
        pat.setAttribute('height', '6');
        pat.setAttribute('patternTransform', 'rotate(45)');
        const stripe = document.createElementNS(
          'http://www.w3.org/2000/svg', 'line',
        );
        stripe.setAttribute('x1', '0'); stripe.setAttribute('y1', '0');
        stripe.setAttribute('x2', '0'); stripe.setAttribute('y2', '6');
        stripe.setAttribute('stroke', 'rgba(255, 160, 60, 0.65)');
        stripe.setAttribute('stroke-width', '2');
        pat.appendChild(stripe);
        defs.appendChild(pat);
        svg.appendChild(defs);
        for (const a of arrowList) {
          const xA = tsArrow.timeToCoordinate(a.aTime);
          const xB = tsArrow.timeToCoordinate(a.bTime);
          const xC = tsArrow.timeToCoordinate(a.cTime);
          const xD = tsArrow.timeToCoordinate(a.dTime);
          const yA = ser.priceToCoordinate(a.aPrice);
          const yB = ser.priceToCoordinate(a.bPrice);
          const yC = ser.priceToCoordinate(a.cPrice);
          const yD = ser.priceToCoordinate(a.dPrice);
          if (xA == null || xB == null || xC == null || xD == null
              || yA == null || yB == null || yC == null || yD == null) {
            skippedNull += 1;
            continue;
          }
          const poly = document.createElementNS(
            'http://www.w3.org/2000/svg', 'polygon',
          );
          poly.setAttribute(
            'points',
            `${xA},${yA} ${xB},${yB} ${xC},${yC} ${xD},${yD}`,
          );
          poly.setAttribute('fill', `url(#${patternId})`);
          poly.setAttribute('stroke', 'rgba(255, 160, 60, 0.55)');
          poly.setAttribute('stroke-width', '1');
          poly.setAttribute('stroke-dasharray', '3,3');
          poly.setAttribute('pointer-events', 'none');
          svg.appendChild(poly);
          painted += 1;
        }
        (svg as unknown as { dataset: Record<string, string> })
          .dataset.arrows =
          `n=${arrowList.length} painted=${painted} skipped=${skippedNull}`;
      } else {
        (svg as unknown as { dataset: Record<string, string> })
          .dataset.arrows = 'n=0';
      }
      // Skip on tiny chart panes — the stacked-charts sparklines are
      // ~46px tall, so a letter 28px above the bar lands outside the
      // pane and looks broken. Buy/sell signals are an analyst tool;
      // they belong on the main chart, not on overview thumbnails.
      const svgRect = svg.getBoundingClientRect();
      if (svgRect.height < 120) return;
      const ts = ch.timeScale();
      const LETTER_OFFSET_PX = 36;   // distance from bar to letter
      // Active window is ``signalActiveBars`` bars from the anchor.
      // ``sig.time`` is stored in lightweight-charts' shifted-UTC
      // form (local wallclock as if UTC — see ``localUtcSeconds``),
      // so ``nowSec`` must use the same shift for elapsed-time math
      // and tooltip display to land on the right wallclock moment.
      const nowSec = localUtcSeconds(new Date()) as number;
      const activeWindowSec = signalActiveBarsRef.current * BAR_SECONDS;
      const tzOffsetSec = new Date().getTimezoneOffset() * 60;
      // Render organic SR badges plus the bot's pinned entry badge
      // (if any). Dedupe by (time, side) so a coincidence between the
      // organic detector and the bot's signal renders once.
      const allSignals: Signal[] = [...srSignalsRef.current];
      const pinned = botPinnedBadgeRef.current;
      if (pinned) {
        const dup = allSignals.some(
          (s) => Math.abs((s.time as number) - (pinned.time as number)) < 1
                 && s.side === pinned.side,
        );
        if (!dup) allSignals.push(pinned);
      }
      for (const sig of allSignals) {
        const x = ts.timeToCoordinate(sig.time);
        const yPrice = ser.priceToCoordinate(sig.price);
        if (x == null || yPrice == null) continue;
        const yLetter = sig.side === 'B'
          ? yPrice + LETTER_OFFSET_PX
          : yPrice - LETTER_OFFSET_PX;
        // Active iff signal fired within the last ``signalActiveBars``
        // bars. After that it stays on the chart as a historical mark
        // (~40% opacity) — visible record of the trigger, but no
        // longer claiming the trade window is open.
        const isActive = (nowSec - (sig.time as number)) <= activeWindowSec;
        const opacity = isActive ? '1' : '0.4';
        // Leader line: black, solid, 1px, from the price point on the
        // polyline to the letter's anchor.
        const ln = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        ln.setAttribute('x1', String(x));
        ln.setAttribute('y1', String(yPrice));
        ln.setAttribute('x2', String(x));
        ln.setAttribute('y2', String(yLetter));
        ln.setAttribute('stroke', 'var(--text-primary, #000)');
        ln.setAttribute('stroke-width', '1');
        ln.setAttribute('opacity', opacity);
        svg.appendChild(ln);
        // Badge: colored circle + white letter centered. Designed to
        // be unmistakable on any background — a bare-text approach
        // failed in light mode (white halo blended into the white
        // panel background, leaving only ~10px of green-on-white text
        // that was easy to miss).
        const themeNow = themeColors();
        const badgeColor = sig.side === 'B' ? themeNow.bullish : themeNow.bearish;
        const RADIUS = 12;
        // Bot-mode: when this badge represents the bot's active entry,
        // paint a glowing green ring BEHIND the badge so it's
        // immediately obvious which signal we acted on. Matched on
        // chart-time (shifted-UTC); the ref carries the converted
        // value so the comparison stays cheap.
        const activeEntryT = activeEntryBarTimeRef.current;
        if (activeEntryT != null
            && Math.abs((sig.time as number) - activeEntryT) < 1) {
          const ring = document.createElementNS(
            'http://www.w3.org/2000/svg', 'circle',
          );
          ring.setAttribute('cx', String(x));
          ring.setAttribute('cy', String(yLetter));
          ring.setAttribute('r', String(RADIUS + 6));
          ring.setAttribute('fill', 'none');
          ring.setAttribute('stroke', themeNow.bullish);
          ring.setAttribute('stroke-width', '3');
          ring.setAttribute('opacity', '0.85');
          svg.appendChild(ring);
        }
        const bg = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        bg.setAttribute('cx', String(x));
        bg.setAttribute('cy', String(yLetter));
        bg.setAttribute('r', String(RADIUS));
        bg.setAttribute('fill', badgeColor);
        bg.setAttribute('stroke', themeNow.background);
        bg.setAttribute('stroke-width', '2');
        bg.setAttribute('opacity', opacity);
        // Re-enable pointer events on the badge alone (the parent
        // SVG has pointer-events:none so chart pan/zoom isn't
        // intercepted). Native SVG <title> gives a hover tooltip
        // showing the signal's price + bar time + active state.
        bg.setAttribute('pointer-events', 'auto');
        bg.style.cursor = 'help';
        const title = document.createElementNS('http://www.w3.org/2000/svg', 'title');
        const sideLabel = sig.side === 'B' ? 'Buy' : 'Sell';
        // Convert shifted-UTC back to real UTC so toLocaleString
        // applies the tz offset exactly once and lands on the
        // correct local wallclock moment.
        const sigDate = new Date(((sig.time as number) + tzOffsetSec) * 1000);
        const timeStr = sigDate.toLocaleString(undefined, {
          month: 'short', day: 'numeric',
          hour: '2-digit', minute: '2-digit', hour12: false,
        });
        const ageBars = Math.floor((nowSec - (sig.time as number)) / BAR_SECONDS);
        const stateStr = isActive
          ? `active (${ageBars}/${signalActiveBarsRef.current} bars used)`
          : `expired (${ageBars} bars ago)`;
        title.textContent
          = `${sideLabel} signal\nPrice: ${sig.price.toFixed(2)}\nTime: ${timeStr}\n${stateStr}`;
        bg.appendChild(title);
        svg.appendChild(bg);
        const txt = document.createElementNS('http://www.w3.org/2000/svg', 'text');
        txt.setAttribute('x', String(x));
        txt.setAttribute('y', String(yLetter));
        txt.setAttribute('text-anchor', 'middle');
        txt.setAttribute('dominant-baseline', 'central');
        txt.setAttribute('font-size', '15');
        txt.setAttribute('font-weight', '800');
        txt.setAttribute('font-family', 'system-ui, sans-serif');
        txt.setAttribute('fill', themeNow.background);
        txt.setAttribute('opacity', opacity);
        txt.textContent = sig.side;
        svg.appendChild(txt);
      }

      // ± markers at the fill endpoint of each historical pivot→
      // fill segment. + for longs, − for shorts. Painted small so
      // they don't compete visually with the B/S badge at the
      // pivot bar. Wrapped in try/catch — a transient lookup
      // failure here must not blow up the badge painter (which
      // runs inside the render path; an uncaught throw shows the
      // flexlayout "Error rendering component" overlay).
      try {
        // Re-derive themeColors here — the one inside the badge loop
        // is scoped to that loop and would be undefined outside.
        const themePaint = themeColors();
        for (const fr of fillRenderRef.current) {
          const fx = ts.timeToCoordinate(fr.fillTime);
          const fy = ser.priceToCoordinate(fr.fillPrice);
          if (fx == null || fy == null) continue;
          const dirCol = fr.side === 'long'
            ? themePaint.bullish
            : themePaint.bearish;
          const cir = document.createElementNS(
            'http://www.w3.org/2000/svg', 'circle',
          );
          cir.setAttribute('cx', String(fx));
          cir.setAttribute('cy', String(fy));
          cir.setAttribute('r', '6');
          cir.setAttribute('fill', dirCol);
          cir.setAttribute('opacity', '0.85');
          svg.appendChild(cir);
          const glyph = document.createElementNS(
            'http://www.w3.org/2000/svg', 'text',
          );
          glyph.setAttribute('x', String(fx));
          glyph.setAttribute('y', String(fy));
          glyph.setAttribute('text-anchor', 'middle');
          glyph.setAttribute('dominant-baseline', 'central');
          glyph.setAttribute('font-size', '11');
          glyph.setAttribute('font-weight', '800');
          glyph.setAttribute('font-family', 'system-ui, sans-serif');
          glyph.setAttribute('fill', themePaint.background);
          glyph.textContent = fr.side === 'long' ? '+' : '−';
          svg.appendChild(glyph);
        }
      } catch { /* never fail the whole paint over a marker */ }
    };

    // rAF-coalesced wrapper. Multiple back-to-back ``reposition`` calls
    // (live tick → recompute → SR detect → reposition) collapse to a
    // single paint on the next animation frame, AFTER lightweight-charts
    // has committed its layout update from the preceding setData/update
    // call. Without this, badges would briefly land on pre-update bar
    // coordinates and visibly trail the polyline on every tick.
    repositionSrSignalsRef.current = () => {
      if (srRafQueued) return;
      srRafQueued = true;
      requestAnimationFrame(() => {
        srRafQueued = false;
        paintSrSignals();
      });
    };

    // SR apex (△) badge and ADX/DI regime (◇) badge removed
    // 2026-05-18 — operator decision. Replaced with a single V-
    // detection badge at top-left that's display-only (does NOT
    // gate entries; regime gate continues to filter using ADX/DI
    // internally on the backend). Painters retained as no-ops to
    // keep the surrounding wiring intact.
    paintApexBadgeRef.current = () => {
      // Empty — apex badge intentionally hidden.
      const stale = srApexBadgeRef.current;
      if (stale && stale.parentNode) {
        stale.parentNode.removeChild(stale);
      }
      srApexBadgeRef.current = null;
    };

    // V-detection badge — top-left, replaces the prior apex (△)
    // and ADX (◇) badges. Shows three trigger flags:
    //   BOS  break of structure (price closed past pre-impulse
    //        extreme)
    //   A    impulse-retrace (≥30% of impulse within retrace
    //        window)
    //   B    impulse-exhaustion (≥10 bars without a new extreme)
    // ``detected = A OR B OR BOS``. Direction is "v_up" (V
    // recovery) or "v_down" (inverted V). Display only — does
    // not affect entry gating.
    // V-state/ADX regime diagnostic badge removed 2026-06-02. The
    // painter, the ref, and the 15s /api/regime fetch effect that
    // drove it are all gone. A one-time mount-time DOM sweep below
    // (see useEffect with the badge-cleanup comment) handles any
    // leftover element from a pre-restart bundle that may still be
    // attached when the user views a stale tab.

    setChartVersion((v) => v + 1);

    // SR detection helper, scoped to this chart instance. Runs over
    // the bars currently in the visible time range — so zooming in
    // shows tighter local structure and zooming out picks up longer
    // trends. The earlier ``SR_MIN_BARS`` viewport gate was removed:
    // it bailed out under 90 min visible and suppressed *all* SR
    // detection, even for lines whose endpoints were both inside the
    // tighter window. Detection now runs at every zoom; the
    // throttle in ``scheduleSrRecomputeRef`` (1/sec) keeps it cheap.
    let srTimer: ReturnType<typeof setTimeout> | null = null;
    const recomputeSr = (_forceRecompute = false): void => {
      // Sparkline / overview instances opt out of SR detection
      // entirely — see ``enableSr`` prop. Big CPU win when many
      // small charts are mounted (stacked-charts pane).
      if (!enableSr) return;
      const ch = chartRef.current;
      if (!ch) return;
      const allBars = barsRef.current;
      if (allBars.length < 3) return;
      const range = ch.timeScale().getVisibleRange();
      if (!range || range.from == null || range.to == null) return;
      const fromTime = Number(range.from);
      const toTime = Number(range.to);
      let fromIdx = -1;
      let toIdx = -1;
      for (let i = 0; i < allBars.length; i++) {
        if (allBars[i].time >= fromTime) { fromIdx = i; break; }
      }
      for (let i = allBars.length - 1; i >= 0; i--) {
        if (allBars[i].time <= toTime) { toIdx = i; break; }
      }
      if (fromIdx < 0 || toIdx < 0 || toIdx <= fromIdx) return;

      // Wipe + redraw.
      for (const ds of srSeriesRef.current) {
        try { ch.removeSeries(ds); } catch { /* already gone */ }
      }
      srSeriesRef.current = [];
      // Note: SR LINES wipe on every recompute (they reflect current
      // structure). SIGNALS do NOT — once a B/S has fired at a given
      // pivot, it stays as a historical mark. A signal disappearing
      // because the underlying fan rotated or the line later broke
      // is misleading: the buy/sell was either valid at the time or
      // it wasn't, and the user wants the record either way. Reset
      // happens on target change or Clear-SR (handled elsewhere).
      if (srHiddenRef.current) return;

      // Detection slice = bars in the visible time range. Off-screen
      // pivots are intentionally excluded so the chart never paints a
      // line anchored to something the user can't see; zooming out is
      // the explicit gesture for longer-trend detection.
      let slice = allBars.slice(fromIdx, toIdx + 1);
      // Exclude the in-progress (live) 3-min bar from pivot detection.
      // The live-tick handler appends/updates a bar whose ``time`` is
      // the current 3-min boundary; its ``close`` is the latest tick,
      // not a real close. Letting that bar participate as a pivot
      // neighbor produces TRANSIENT pivot detections — e.g. while the
      // live price stays below the prior bar's close, ``find_pivot_
      // highs`` sees the prior bar as a pivot HIGH and the chart paints
      // an S badge; when the live bar's actual close ends up higher,
      // the pivot disappears. The bot (which uses /engine/history,
      // closed bars only) never sees these phantom pivots and can't
      // act on the S, producing the "S appeared, bot didn't fire"
      // mismatch reported 2026-05-11 (MES 18:57). Strip the live bar
      // so the chart's detector uses the same closed-bars-only input
      // as the bot.
      // Slot-end labelling: bars are now stamped at slot-end. The
      // in-progress bar's stamp = floor(now/BAR_SECONDS)*BAR_SECONDS
      // + BAR_SECONDS. Anything at or past that stamp is the live
      // forming bar — strip it so SR detection runs on closed bars
      // only, matching the bot's view.
      const _nowSec = localUtcSeconds(new Date()) as number;
      const _liveBarStamp = Math.floor(_nowSec / BAR_SECONDS) * BAR_SECONDS
        + BAR_SECONDS;
      if (slice.length > 0 && (slice[slice.length - 1].time as number) >= _liveBarStamp) {
        slice = slice.slice(0, -1);
        if (slice.length < 3) return;
      }
      // Map "show broken for N minutes" to bar count. BAR_SECONDS=180
      // (3 min). When the broken toggle is off the value is irrelevant
      // — broken lines get filtered out at render anyway — but we
      // still keep ``breakStaleBars`` permissive enough that the
      // engine retains them for an immediate toggle-on.
      const breakStaleBars = Math.max(
        1, Math.ceil((brokenMinutesRef.current ?? 30) * 60 / BAR_SECONDS),
      );
      // Canonical SR data source. Backend mode = single source of
      // truth (/api/sr → /engine/sr → find_wedges + detect_lines,
      // same code path the bot uses). FE mode = legacy local
      // detector kept around as a diagnostic so the operator can
      // verify rendering vs detection independently.
      const backendData = srBackendDataRef.current;
      const useBackend = srSourceModeRef.current === 'be';
      let lines: ReturnType<typeof detectSupportResistance>;
      if (useBackend) {
        lines = backendData
          ? backendData.lines
              .map((bl) => backendLineToSRLine(bl, allBars))
              .filter((ln): ln is NonNullable<typeof ln> => ln !== null)
          : [];
      } else {
        // Local detector returns indices in ``slice`` coords. Re-base
        // them to ``allBars`` so the renderer (which reads
        // ``allBars[idx].time``) lines up either way.
        const local = detectSupportResistance(slice, { breakStaleBars });
        lines = local.map((ln) => ({
          ...ln,
          fromIdx: ln.fromIdx + fromIdx,
          anchorBIdx: ln.anchorBIdx + fromIdx,
          toIdx: ln.toIdx + fromIdx,
          breakIdx: ln.breakIdx != null ? ln.breakIdx + fromIdx : null,
          thirdTouchIdx: ln.thirdTouchIdx != null
            ? ln.thirdTouchIdx + fromIdx
            : null,
          intercept: ln.intercept - ln.slope * fromIdx,
        }));
      }
      const colors = themeColors();
      // Counter for human-readable line labels rendered on the right
      // axis. Numbered in detection order; lets the user point at a
      // specific line in conversation ("L3 looks wrong"). Logged to
      // console alongside so the data is one F12 away.
      let labelCounter = 0;
      const lineLog: Record<string, unknown>[] = [];
      // Buy/Sell signals: persistent across recomputes. Once a signal
      // fires at (anchor-bar, side), it stays in ``srSignalsRef``
      // forever (until target change / Clear-SR). The "active vs
      // dimmed" decision is purely time-based: a signal is active
      // for ``signalActiveBars`` bars after its anchor time, then
      // dims regardless of whether the underlying line is still
      // detected. Computed in the painter, not here.
      const newSignals: Signal[] = [];
      // Lines that actually get rendered on the chart (post-filter).
      // Arrow/wedge detection runs against this list so the triangle
      // only highlights structures the user can see.
      const renderedLines: typeof lines = [];
      for (const line of lines) {
        const isBroken = line.breakIdx != null;
        // Broken lines are noisy at zoom-out and clutter the active
        // structure the user is reading. The header toggle decides
        // whether they render; the engine still tracks them so a
        // toggle-on shows them without a recompute lag.
        if (isBroken && !showBrokenSrRef.current) continue;
        // Counter-trend filter: drop down-sloping supports and
        // up-sloping resistances unless explicitly opted-in. These
        // lines argue *against* their own type (lower lows under a
        // support, higher highs above a resistance) so most readings
        // start cleaner without them. Slope==0 (horizontal) is kept
        // either way since it's an honest level, not counter-trend.
        const counterSupport = line.type === 'support' && line.slope < 0;
        const counterResistance = line.type === 'resistance' && line.slope > 0;
        if (counterSupport && !showCounterSupportRef.current) continue;
        if (counterResistance && !showCounterResistanceRef.current) continue;
        renderedLines.push(line);
        labelCounter += 1;
        const label = `L${labelCounter}`;
        // Color is type-driven so support and resistance are always
        // visually distinct. Broken is amber dashed regardless. The
        // earlier "tentative blue" tier collapsed support and
        // resistance into the same color in magnetic mode (every
        // line is 2-touch by construction), making resistance
        // invisible-as-support — fixed by colouring by type always.
        const confirmed = line.touches >= 3;
        const color = isBroken
          ? colors.archived
          : line.type === 'support' ? colors.bullish : colors.bearish;
        // Backend-resolved indices are against ``allBars`` (full
        // history), not the visible slice. Read from allBars so a
        // line anchored to a bar outside the current zoom still
        // renders correctly when the operator pans back.
        //
        // Pure visual projection to the in-progress bar: for live
        // (un-broken) lines, extend the right edge to the latest bar
        // in allBars — the live-tick handler keeps that bar's close
        // current, so the line stays glued to "now" instead of
        // stopping at the previous fully-closed bar. Broken lines
        // are archived and keep their original ``toIdx`` so the dashed
        // segment terminates where the break actually happened.
        // Touch counting / break detection still uses ``line.toIdx`` —
        // we only override the rendering endpoint.
        const projToIdx = isBroken
          ? line.toIdx
          : Math.max(line.toIdx, allBars.length - 1);
        const startTime = allBars[line.fromIdx].time;
        const endTime = allBars[projToIdx].time;
        const startPrice = line.slope * line.fromIdx + line.intercept;
        const endPrice = line.slope * projToIdx + line.intercept;
        const lineStyleVal = isBroken ? 2 : confirmed ? 0 : 1;
        const baseOpts = {
          color,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          // Exclude SR lines from the right price scale's auto-fit.
          // Otherwise a deeply-anchored support that extends below
          // the visible price range pulls the auto-scaled Y window
          // wide, squashing the actual price polyline to a sliver.
          autoscaleInfoProvider: () => null,
          // lightweight-charts LineStyle: 0=solid, 1=dotted, 2=dashed.
          // Visual hierarchy: confirmed = solid (most prominent),
          // tentative 2-touch = dotted (forming, less committed),
          // broken/archived = dashed (gave way; kept on screen briefly).
          lineStyle: lineStyleVal,
        };
        // Always draw a single thin segment. Earlier builds split the
        // line and thickened the post-3rd-touch segment as a "near-
        // touch rule in play" cue, but the thick band overpowered the
        // chart and made multi-line clusters hard to read. Solid vs
        // dotted vs dashed (set via ``lineStyleVal`` on baseOpts)
        // already encodes the confirmed / emerging / broken hierarchy,
        // which is the only distinction the operator wants visually.
        const ds = ch.addSeries(LineSeries, {
          ...baseOpts, lineWidth: 1 as any,
        });
        ds.setData([
          { time: startTime, value: startPrice },
          { time: endTime, value: endPrice },
        ]);
        srSeriesRef.current.push(ds);

        // Signal: uptrending support or downtrending resistance with
        // 3+ confirming pivot touches → buy/sell indicator. Anchored
        // at the line's anchor-B (most-recent construction pivot).
        // Broken lines already filtered above. Price comes from the
        // line algebra (not the close at that bar) so the leader
        // line lands on the exact pivot the line is constructed on.
        //
        // Bot panes set ``suppressAutoSignals=true`` so the only
        // badge that ever appears is one tied to a real order
        // (driven by ``activeEntryBarTime`` + ``entrySide``). The
        // chart still draws the SR lines themselves; just no
        // anchor-emitted Bs/Ss that the bot never acted on.
        if (confirmed && !suppressAutoSignalsRef.current) {
          const isBuy = line.type === 'support' && line.slope > 0;
          const isSell = line.type === 'resistance' && line.slope < 0;
          if (isBuy || isSell) {
            const anchorIdx = line.anchorBIdx;
            // ``anchorIdx`` is in allBars space (post-rebase for fe
            // mode, backend-resolved for be mode). Read from allBars,
            // not slice.
            const anchorTime = allBars[anchorIdx].time as number;
            const dedupKey = (anchorTime << 1) | (isBuy ? 1 : 0);
            // Skip if any existing signal already covers this key.
            const alreadyKnown = srSignalsRef.current.some((s) =>
              ((s.time as number) << 1 | (s.side === 'B' ? 1 : 0)) === dedupKey,
            );
            if (!alreadyKnown) {
              newSignals.push({
                time: allBars[anchorIdx].time as UTCTimestamp,
                price: line.slope * anchorIdx + line.intercept,
                side: isBuy ? 'B' : 'S',
              });
            }
          }
        }

        // Companion debug log — same number as on the chart.
        const startBarTime = new Date((startTime as number) * 1000).toISOString();
        const endBarTime = new Date((endTime as number) * 1000).toISOString();
        const anchorBTime = new Date(
          (allBars[line.anchorBIdx].time as number) * 1000,
        ).toISOString();
        lineLog.push({
          label, type: line.type, touches: line.touches,
          fromIdx: line.fromIdx,
          anchorBIdx: line.anchorBIdx,
          toIdx: line.toIdx,
          startTime: startBarTime,
          anchorBTime,
          endTime: endBarTime,
          startPrice: Number(startPrice.toFixed(2)),
          endPrice: Number(endPrice.toFixed(2)),
          slope: Number(line.slope.toFixed(4)),
          breakIdx: line.breakIdx,
          breakTime: line.breakIdx != null
            ? new Date((allBars[line.breakIdx].time as number) * 1000).toISOString()
            : null,
          isBroken, confirmed,
        });
      }

      // Append newly-fired signals, then cap. Sticky signals are a
      // historical record but they accumulate forever in long
      // sessions (intraday with many setups → hundreds of badges,
      // each of which the painter creates 3 SVG nodes for on every
      // pan/zoom/refresh). Keeping the most recent ``MAX_SIGNALS``
      // bounds memory, repaint cost, and visual clutter.
      const MAX_SIGNALS = 50;
      // Bot panes opt out of the auto-emit cache entirely. Stale
      // badges from earlier recomputes (or older sessions before
      // suppression was enabled) would otherwise stick around even
      // after the gate flips off — defeating the "B/S = real order"
      // invariant. When suppressed, the only badges that survive
      // are the explicit historical bot fires the parent passes in.
      const merged = suppressAutoSignalsRef.current
        ? [...historicalFiresRef.current]
        : [...srSignalsRef.current, ...newSignals];
      if (merged.length > MAX_SIGNALS) {
        merged.sort((a, b) => (a.time as number) - (b.time as number));
        merged.splice(0, merged.length - MAX_SIGNALS);
      }
      srSignalsRef.current = merged;

      // ── Arrowhead detection: converging support + resistance ─────────
      // Pairs of lines with opposite slopes whose apex lies ahead of
      // the latest data within ~50 bars. Stashed as triangle vertices
      // for the repaint loop. Faint overlay only — does NOT affect
      // bot firing. Operator uses these to spot wedges where price
      // action is compressing.
      // Wedges come from the backend payload's ``wedges`` array,
      // already sorted by apex_bars_ahead ascending (innermost
      // first). We just convert the timestamp-based vertices into
      // the four-vertex trapezoid the painter expects. The local
      // wedge math is gone.
      const arrows: Arrow[] = [];
      const arrowReject: Array<Record<string, unknown>> = [];
      const allApexes: number[] = [];
      if (backendData) {
        for (const w of backendData.wedges) {
          allApexes.push(w.apex_bars_ahead);
          const v = w.vertices;
          arrows.push({
            aTime: backendTsToChartTime(v.support_left.ts) as UTCTimestamp,
            aPrice: v.support_left.price,
            bTime: backendTsToChartTime(v.support_right.ts) as UTCTimestamp,
            bPrice: v.support_right.price,
            cTime: backendTsToChartTime(v.resistance_right.ts) as UTCTimestamp,
            cPrice: v.resistance_right.price,
            dTime: backendTsToChartTime(v.resistance_left.ts) as UTCTimestamp,
            dPrice: v.resistance_left.price,
            apexBarsAhead: w.apex_bars_ahead,
          });
        }
      }
      srArrowsRef.current = arrows;

      // Badge: three nearest apex distances across the entire
      // detected set (uncapped). ``∞`` only when no wedges exist at
      // all in the backend window.
      const apexList = Array.from(new Set(allApexes))
        .sort((a, b) => a - b).slice(0, 3);
      srBackendApexRef.current = backendData ? apexList : null;
      // Tooltip: one line per wedge for the three nearest apexes
      // (matches the badge readout). Format:
      // ``△<n> SL <ts> | SR <ts> | RR <ts> | RL <ts>``.
      if (backendData && backendData.wedges.length > 0) {
        const sorted = [...backendData.wedges]
          .sort((a, b) => a.apex_bars_ahead - b.apex_bars_ahead);
        // Pick the first wedge for each distinct apex bars-ahead
        // value, capped at the same 3-entry limit the badge uses.
        const seen = new Set<number>();
        const picked: typeof sorted = [];
        for (const w of sorted) {
          if (seen.has(w.apex_bars_ahead)) continue;
          seen.add(w.apex_bars_ahead);
          picked.push(w);
          if (picked.length === 3) break;
        }
        const fmt = (ts: string) => ts.slice(5, 16).replace('T', ' ');
        // The trapezoid's two right vertices share the same
        // timestamp (both pinned at sliceLast), so listing both is
        // redundant — drop ``RR`` and show three distinct anchor
        // points: support-left, support-right (= shared right-edge
        // ts), resistance-left.
        srBackendTooltipRef.current = picked.map((w) => {
          const v = w.vertices;
          return `△${w.apex_bars_ahead}  `
            + `SL ${fmt(v.support_left.ts)} `
            + `· SR ${fmt(v.support_right.ts)} `
            + `· RL ${fmt(v.resistance_left.ts)}`;
        }).join('\n');
      } else {
        srBackendTooltipRef.current = backendData
          ? 'no wedges detected in the backend window'
          : 'awaiting backend SR fetch';
      }
      paintApexBadgeRef.current?.();

      repositionSrSignalsRef.current?.();

      // Always stash diagnostic snapshot on window — even when 0
      // lines drew, so the user can introspect why. Includes pivot
      // times so we can verify pivot-detection is finding what the
      // user expects (e.g. ``__sr.pivotLowsAt`` lists every detected
      // pivot-low close time in the visible slice).
      type SrLogRow = (typeof lineLog)[number];
      type SliceBar = { t: string; close: number };
      type SrGlobal = {
        lines: SrLogRow[];
        byLabel: (l: string) => SrLogRow | undefined;
        pivotLowsAt: string[];
        pivotHighsAt: string[];
        sliceFrom: string;
        sliceTo: string;
        slice: SliceBar[];
        priceAt: (timeSubstr: string) => SliceBar | undefined;
        arrows: Arrow[];   // detected wedges (apex within 50 bars)
        rawLines: typeof lines;   // pre-filter detector output
      };
      // Recompute pivot indices on the same slice the detector saw,
      // for the diagnostic. Cheap: O(N).
      const sliceCloses = slice.map((b) => b.close);
      const findLocalExtrema = (type: 'low' | 'high'): string[] => {
        const out: string[] = [];
        for (let i = 1; i < sliceCloses.length - 1; i++) {
          const v = sliceCloses[i];
          const l = sliceCloses[i - 1];
          const r = sliceCloses[i + 1];
          const ok = type === 'low' ? (v < l && v < r) : (v > l && v > r);
          if (ok) {
            out.push(new Date((slice[i].time as number) * 1000).toISOString());
          }
        }
        return out;
      };
      const sliceBars: SliceBar[] = slice.map((b) => ({
        t: new Date((b.time as number) * 1000).toISOString(),
        close: b.close,
      }));
      const stash: SrGlobal = {
        lines: lineLog,
        byLabel: (l: string) => lineLog.find((r) => r.label === l),
        pivotLowsAt: findLocalExtrema('low'),
        pivotHighsAt: findLocalExtrema('high'),
        sliceFrom: new Date((slice[0].time as number) * 1000).toISOString(),
        sliceTo: new Date(
          (slice[slice.length - 1].time as number) * 1000,
        ).toISOString(),
        slice: sliceBars,
        priceAt: (substr: string) =>
          sliceBars.find((b) => b.t.includes(substr)),
        arrows: srArrowsRef.current,
        rawLines: lines,
      };
      (window as unknown as { __sr: SrGlobal }).__sr = stash;
      // Stash silently — auto-printing on every recompute drowns out
      // anything the user types in the console. Inspect via __sr.* on
      // demand.
      // Also POST a compact snapshot to the API debug log so the
      // server-side ``logs/sr-debug.log`` is tailable. Throttled by
      // the recompute throttle already; the API rotates by size.
      const snapshot = {
        target: targetRef.current?.symbol,
        sliceFrom: stash.sliceFrom,
        sliceTo: stash.sliceTo,
        pivotLowsAt: stash.pivotLowsAt,
        pivotHighsAt: stash.pivotHighsAt,
        lines: stash.lines,
        slice: stash.slice,
        // Wedge/arrowhead diagnostics — server-side tail tells us
        // which side of the converging-pair check is failing
        // without needing the operator to paste from the console.
        rawLineSummary: lines.map((l) => ({
          type: l.type, slope: l.slope, touches: l.touches,
          fromIdx: l.fromIdx, toIdx: l.toIdx,
          isBroken: l.breakIdx != null,
        })),
        rawLineCounts: {
          supports: lines.filter((l) => l.type === 'support').length,
          resistances: lines.filter((l) => l.type === 'resistance').length,
          supportsUnbroken: lines.filter(
            (l) => l.type === 'support' && l.breakIdx == null,
          ).length,
          resistancesUnbroken: lines.filter(
            (l) => l.type === 'resistance' && l.breakIdx == null,
          ).length,
        },
        arrows: srArrowsRef.current,
        arrowReject,
        renderedCounts: {
          supports: renderedLines.filter((l) => l.type === 'support').length,
          resistances: renderedLines.filter((l) => l.type === 'resistance').length,
        },
        sliceLast: stash.sliceTo,
      };
      // Server-side debug tail is opt-in. Each snapshot is several KB
      // and recomputes fire ~1/s per chart pane; with multiple charts
      // open the Network panel buffers MBs/min and Chrome's memory
      // climbs over a long session. Enable from the browser console
      // when actually debugging SR:
      //     localStorage.setItem('ib-sr-debug', '1')
      // Disable: localStorage.removeItem('ib-sr-debug')
      if (typeof window !== 'undefined'
          && window.localStorage?.getItem('ib-sr-debug') === '1') {
        void fetch('/api/debug/log/sr-debug', {
          method: 'POST',
          headers: { 'content-type': 'application/json' },
          body: JSON.stringify(snapshot),
        }).catch(() => {
          // server-side debug log is best-effort; ignore network errors
        });
      }
    };
    // Throttle: cap at one recompute per ``SR_THROTTLE_MS``. Leading-
    // edge fire so a fresh event takes effect immediately; trailing
    // fire on the cool-down to capture the latest state if more
    // events arrived during the window. Used by zoom/pan, the 30s
    // refresh, the bar-close boundary trigger, AND live ticks — but
    // throttled so a fast tape doesn't churn the detector.
    //
    // Why live ticks too: SR *touches* are evaluated on closed bars
    // (a brief wick shouldn't count as confirmation), but *break
    // detection* on an existing line should reflect the live price.
    // Otherwise a clear breach can sit on screen as a still-active
    // line for up to 3 min until the next bar boundary fires.
    const SR_THROTTLE_MS = 1000;
    let lastSrFireMs = 0;
    // High-water mark of ``force`` across all calls in the current
    // throttle window. If any caller passed force=true, the eventual
    // trailing fire runs with force=true. Without this, a force=false
    // event that queues a trailing timer would shadow a later
    // force=true call (codex flagged: toggle silently no-ops).
    let pendingForce = false;
    scheduleSrRecomputeRef.current = (force = false) => {
      const now = Date.now();
      const elapsed = now - lastSrFireMs;
      if (elapsed >= SR_THROTTLE_MS) {
        lastSrFireMs = now;
        pendingForce = false;
        recomputeSr(force);
        return;
      }
      pendingForce = pendingForce || force;
      if (srTimer) return;  // trailing fire already scheduled
      srTimer = setTimeout(() => {
        srTimer = null;
        lastSrFireMs = Date.now();
        const f = pendingForce;
        pendingForce = false;
        recomputeSr(f);
      }, SR_THROTTLE_MS - elapsed);
    };

    // Capture Y-axis zoom on every user interaction with the chart
    // container. lightweight-charts has no priceScale-change event, so
    // we watch DOM events that *might* have changed it (wheel, mouse
    // release, touch end) and re-read the price range. Debounced
    // (rAF) to avoid hammering localStorage on every wheel tick.
    let yCaptureScheduled = false;
    const captureYRange = (): void => {
      if (yCaptureScheduled) return;
      yCaptureScheduled = true;
      requestAnimationFrame(() => {
        yCaptureScheduled = false;
        const ps = chart.priceScale('right');
        const pr = ps.getVisibleRange();
        // Repaint signals on any Y change (wheel/pinch/axis drag).
        repositionSrSignalsRef.current?.();
        if (!pr) return;
        const cur = userPriceRangeRef.current;
        if (cur && cur.from === pr.from && cur.to === pr.to) return;
        userPriceRangeRef.current = { from: pr.from, to: pr.to };
        // Lock autoScale OFF the moment the operator moves Y. Without
        // this, lightweight-charts re-fits the polyline on every data
        // update (30 s refresh + every live tick), instantly undoing
        // an axis drag. Explicit Reset Zoom re-enables it.
        try { ps.applyOptions({ autoScale: false }); }
        catch { /* older builds — ignore */ }
        const tgt = targetRef.current;
        const xRange = userRangeRef.current;
        if (tgt && xRange) {
          saveRange(targetKey(tgt), {
            ...xRange,
            priceFrom: pr.from,
            priceTo: pr.to,
          });
        }
      });
    };
    const containerEl = containerRef.current;
    // Wheel/touchpad-scroll routing:
    //   over Y axis  → zoom Y only (lightweight-charts default).
    //                   ``captureYRange`` then persists the new Y so
    //                   the next 30s refresh restores it.
    //   over X axis  → zoom X only (lightweight-charts default).
    //                   No Y update; signals repaint after the X
    //                   range change.
    //   over body    → X zooms (lightweight-charts default), AND we
    //                   re-fit Y to centre the polyline in the
    //                   middle third (scaleMargins 0.25/0.25). The
    //                   prior persisted Y is cleared so the autofit
    //                   sticks across the next refresh.
    // Wheel-zoom factor per notch. 3 % per tick — touchpad scrolls
    // fire many small wheel events, so anything higher feels jumpy.
    const Y_ZOOM_STEP = 0.03;
    const onWheel = (e: WheelEvent): void => {
      if (!containerEl) return;
      const rect = containerEl.getBoundingClientRect();
      const mouseX = e.clientX - rect.left;
      const mouseY = e.clientY - rect.top;
      // ``.width()`` / ``.height()`` may report 0 before the chart's
      // first paint or when the axis is collapsed. Floor each zone
      // at a sensible default so wheel routing works from the first
      // tick.
      let psW = 0;
      let tsH = 0;
      try { psW = chart.priceScale('right').width(); } catch { /* ignore */ }
      try { tsH = chart.timeScale().height(); } catch { /* ignore */ }
      const yZoneW = Math.max(psW, 56);
      const xZoneH = Math.max(tsH, 24);
      const overYAxis = mouseX > rect.width - yZoneW;
      const overXAxis = !overYAxis && mouseY > rect.height - xZoneH;
      if (overYAxis) {
        // lightweight-charts' wheel handler always zooms X regardless
        // of mouse position — Y wheel-zoom is not built-in. Intercept
        // here, stop the default X zoom, and resize the price scale's
        // visible range around its current centre by ``Y_ZOOM_STEP``.
        // captureYRange persists the new range for the next refresh.
        e.preventDefault();
        try {
          const ps = chart.priceScale('right');
          const pr = ps.getVisibleRange();
          if (pr) {
            // Anchor the zoom around the most-recent price so the
            // end of the polyline stays at the vertical centre of
            // the pane while the band around it widens/narrows.
            // Falls back to the previous-range centre if no bars
            // are loaded yet (e.g. cold chart).
            const lastBar = barsRef.current[barsRef.current.length - 1];
            const centre = lastBar
              ? lastBar.close
              : (pr.from + pr.to) / 2;
            const halfRange = (pr.to - pr.from) / 2;
            // Wheel-up (deltaY < 0) = zoom in (smaller range).
            // Wheel-down (deltaY > 0) = zoom out (larger range).
            const factor = e.deltaY > 0
              ? 1 + Y_ZOOM_STEP
              : 1 / (1 + Y_ZOOM_STEP);
            const next = halfRange * factor;
            ps.setVisibleRange({ from: centre - next, to: centre + next });
          }
        } catch { /* ignore */ }
        captureYRange();
        return;
      }
      if (overXAxis) {
        // X-axis wheel: lightweight-charts already zooms X. Leave Y
        // untouched — a prior manual zoom (if any) sticks.
        requestAnimationFrame(() => repositionSrSignalsRef.current?.());
        return;
      }
      // Body wheel: lightweight-charts zoomed X. Re-fit Y to centre
      // the polyline and drop the persisted Y so a subsequent
      // refresh respects the auto-fit rather than restoring an
      // older manual zoom.
      requestAnimationFrame(() => {
        try { chart.priceScale('right').setAutoScale(true); }
        catch { /* ignore */ }
        userPriceRangeRef.current = null;
        const tgt = targetRef.current;
        const xr = userRangeRef.current;
        if (tgt && xr) {
          saveRange(targetKey(tgt), { from: xr.from, to: xr.to });
        }
        repositionSrSignalsRef.current?.();
      });
    };
    if (containerEl) {
      // ``passive: false`` so we can preventDefault on Y-axis wheels;
      // otherwise lightweight-charts also zooms X on the same event.
      containerEl.addEventListener('wheel', onWheel, { passive: false });
      // Drag-to-zoom / drag-to-pan on the Y axis ends with a
      // pointerup (lightweight-charts uses pointer events; mouseup
      // may not bubble through). ``pointerup`` covers mouse + touch
      // + pen; the explicit ``mouseup``/``touchend`` are kept as
      // belt-and-braces for older browsers.
      containerEl.addEventListener('pointerup', captureYRange);
      containerEl.addEventListener('mouseup', captureYRange);
      containerEl.addEventListener('touchend', captureYRange);
    }

    chart.timeScale().subscribeVisibleTimeRangeChange((range) => {
      if (!range || range.from == null || range.to == null) return;
      const from = Number(range.from);
      const to = Number(range.to);
      // Skip transient narrow ranges that lightweight-charts emits while
      // a chart is being populated — e.g. if a live tick lands before
      // the historical fetch returns, the time scale auto-fits to that
      // single point and we'd persist a 1-minute "zoom" the user never
      // chose. Anything narrower than 5 min isn't a user interaction.
      if (to - from < 5 * 60) return;
      // X pan/zoom does NOT touch the Y axis — user's manual Y zoom
      // (if any) is respected until the next reset / periodic
      // refresh / container resize. Auto-fit triggers live in those
      // three places, not here.
      const pr = chart.priceScale('right').getVisibleRange();
      const r: SavedRange = {
        from, to,
        priceFrom: pr?.from,
        priceTo: pr?.to,
      };
      userRangeRef.current = r;
      const lr = chart.timeScale().getVisibleLogicalRange();
      if (lr) {
        // Record viewport in bar-relative terms (gap from last bar +
        // visible width). See ``userLogicalRangeRef`` for why.
        const barCount = barsRef.current.length;
        const rightSpaceBars = lr.to - (barCount - 1);
        const widthBars = lr.to - lr.from;
        // Don't persist suspiciously narrow logical ranges. When
        // ``volume.setData()`` lands during a refresh, lightweight-
        // charts briefly re-emits the visible-range-change event
        // with a transient narrow window before settling — those
        // ranges were getting persisted and stranded the chart at
        // a ~10-min view on subsequent loads. Anything below 10
        // bars (= 30 min on 3-min bars) is below any reasonable
        // operator zoom and almost certainly transient.
        if (widthBars >= 10) {
          userLogicalRangeRef.current = { rightSpaceBars, widthBars };
          r.rightSpaceBars = rightSpaceBars;
          r.widthBars = widthBars;
        }
      }
      // Re-run SR detection over the new visible range (debounced —
      // zoom emits many events). Detection self-skips when width is
      // below the 90 min minimum, so zoom-ins below default leave
      // the existing lines alone.
      scheduleSrRecomputeRef.current?.();
      // Reposition the buy/sell letters and their leader lines for
      // the new viewport — independent of whether SR re-detects.
      repositionSrSignalsRef.current?.();
      const tgt = targetRef.current;
      if (tgt) saveRange(targetKey(tgt), r);
    });

    // Re-fit Y when the chart pane resizes (drawer collapse/expand,
    // window resize, full-screen toggle). lightweight-charts updates
    // its internal layout on container size change but doesn't
    // re-autoscale the price axis, so we trigger it explicitly.
    let resizeObserver: ResizeObserver | null = null;
    if (containerEl && typeof ResizeObserver !== 'undefined') {
      resizeObserver = new ResizeObserver(() => {
        // Honour an in-flight ``withFrozenViewport`` freeze — the
        // capture path pinned the price range explicitly and the
        // ResizeObserver firing here is an html2canvas layout side
        // effect, not a real user resize.
        if (!viewportFrozenRef.current) {
          try { chart.priceScale('right').setAutoScale(true); }
          catch { /* ignore */ }
        }
        repositionSrSignalsRef.current?.();
      });
      resizeObserver.observe(containerEl);
    }

    return () => {
      if (srTimer) clearTimeout(srTimer);
      scheduleSrRecomputeRef.current = null;
      repositionSrSignalsRef.current = null;
      if (resizeObserver) resizeObserver.disconnect();
      if (containerEl) {
        containerEl.removeEventListener('wheel', onWheel);
        containerEl.removeEventListener('pointerup', captureYRange);
        containerEl.removeEventListener('mouseup', captureYRange);
        containerEl.removeEventListener('touchend', captureYRange);
      }
      if (srOverlayRef.current && srOverlayRef.current.parentNode) {
        srOverlayRef.current.parentNode.removeChild(srOverlayRef.current);
      }
      srOverlayRef.current = null;
      srSignalsRef.current = [];
      srArrowsRef.current = [];
      if (srApexBadgeRef.current && srApexBadgeRef.current.parentNode) {
        srApexBadgeRef.current.parentNode.removeChild(srApexBadgeRef.current);
      }
      srApexBadgeRef.current = null;
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      rsiSeriesRef.current = null;
      divergenceSeriesRef.current = [];
      srSeriesRef.current = [];
      entryLineSeriesRef.current = null;
      volumeSeriesRef.current = null;
      barsRef.current = [];
    };
  }, [theme, showRsi]);

  // Data fetch + 30s refresh + retry-with-backoff.
  useEffect(() => {
    if (!target) {
      seriesRef.current?.setData([]);
      rsiSeriesRef.current?.setData([]);
      const chart = chartRef.current;
      if (chart) {
        for (const ds of divergenceSeriesRef.current) {
          try { chart.removeSeries(ds); } catch { /* already gone */ }
        }
        for (const ds of srSeriesRef.current) {
          try { chart.removeSeries(ds); } catch { /* already gone */ }
        }
      }
      divergenceSeriesRef.current = [];
      srSeriesRef.current = [];
      srSignalsRef.current = [];
      repositionSrSignalsRef.current?.();
      barsRef.current = [];
      onError?.(null);
      userRangeRef.current = null;
      userPriceRangeRef.current = null;
      userLogicalRangeRef.current = null;
      return;
    }

    let cancelled = false;
    let firstLoad = true;
    userRangeRef.current = null;
    // Reset the gate so live ticks for a new target don't sneak into
    // the previous target's series during the brief window between
    // target change and the new historical load.
    historicalLoadedRef.current = false;
    // New target → re-show SR lines (the dismiss state is per-target,
    // not global). The detection itself runs after setData below.
    srHiddenRef.current = false;
    // Forget the previous target's last-tick bar so the first tick
    // for the new target doesn't trigger a spurious SR recompute.
    lastTickBarSecRef.current = null;
    // Forget the previous target's OHLC bars too — fresh load() will
    // repopulate. Avoids a moment where SR detection runs against
    // mismatched price-series + bars-ref content during target swap.
    barsRef.current = [];
    let retryDelayMs = 1500;
    const RETRY_MAX_MS = 8_000;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    const load = async () => {
      try {
        onLoadingChange?.(true);
        const bars = await getHistory({
          conId: target.conId ?? undefined,
          symbol: target.conId == null ? target.symbol : undefined,
          secType: target.conId == null ? target.secType : undefined,
          hours: PRELOAD_HOURS,
          barSize: BAR_SIZE,
        });
        retryDelayMs = 1500;
        if (cancelled) return;
        const fullBars = toBars(bars);
        const points = fullBars.map((b) => ({ time: b.time, value: b.close }));
        // Stash the OHLC-rich bars so SR detection (which needs wick
        // high/low) has access. The lightweight-charts price series
        // only carries close, so we keep this parallel structure.
        barsRef.current = fullBars;
        const series = seriesRef.current;
        const rsiSeries = rsiSeriesRef.current;
        const chart = chartRef.current;
        if (!series || !chart) return;

        const savedRange = firstLoad ? loadSavedRange(targetKey(target)) : null;
        const prevRange = userRangeRef.current ?? savedRange;
        // Hydrate Y-zoom from localStorage on the very first load so a
        // hard refresh restores the user's price-axis zoom. On
        // subsequent in-session loads, we re-read the live price scale
        // (it may have changed via Y-axis pinch which doesn't fire a
        // time-range event) and persist that updated value.
        const priceScale = chart.priceScale('right');
        if (firstLoad) {
          // Y range is NOT restored on hard refresh. The saved value
          // is in absolute price units, while the scale margins +
          // visible-bar set determine where on the pane those prices
          // get drawn — a stale Y range applied under different
          // margins squishes the polyline and gives an unexpected
          // "weird zoom" on reload. Auto-fit handles the first paint;
          // in-session pan/zoom is captured in ``userPriceRangeRef``
          // immediately afterwards so subsequent refreshes preserve
          // it.
          userPriceRangeRef.current = null;
        } else {
          const currentPriceRange = priceScale.getVisibleRange();
          if (currentPriceRange) {
            userPriceRangeRef.current = {
              from: currentPriceRange.from,
              to: currentPriceRange.to,
            };
            // Persist alongside time range so a hard refresh keeps it.
            const tgt = targetRef.current;
            const cur = userRangeRef.current;
            if (tgt && cur) {
              saveRange(targetKey(tgt), {
                ...cur,
                priceFrom: currentPriceRange.from,
                priceTo: currentPriceRange.to,
              });
            }
          }
        }
        series.setData(points);
        // Historical bars are now in the series — safe to let live
        // ticks update past this point without auto-fit-to-one-point
        // pathology.
        historicalLoadedRef.current = true;

        // Volume overlay — green when close >= open, red when down,
        // both at low alpha so the histogram reads as ambient context
        // under the price line. Updates only here (every 30s refresh);
        // the live-tick WS pushes price-only frames, so the most-recent
        // bar's volume stays whatever IB returned at the last fetch.
        const volSeries = volumeSeriesRef.current;
        if (volSeries) {
          try {
            volSeries.setData(fullBars.map((b) => ({
              time: b.time,
              value: b.volume ?? 0,
              color: (b.close >= b.open)
                ? 'rgba(22, 163, 74, 0.35)'
                : 'rgba(220, 38, 38, 0.35)',
            })));
          } catch { /* series torn down mid-refresh — ignore */ }
        }

        if (rsiSeries && points.length >= RSI_DEFAULTS.period + 1) {
          const closes = points.map((p) => p.value);
          const rsiVals = computeRsi(closes, RSI_DEFAULTS.period);
          const rsiPoints: { time: UTCTimestamp; value: number }[] = [];
          for (let i = 0; i < rsiVals.length; i++) {
            const v = rsiVals[i];
            if (v == null) continue;
            rsiPoints.push({ time: points[i].time, value: v });
          }
          rsiSeries.setData(rsiPoints);

          for (const ds of divergenceSeriesRef.current) {
            try { chart.removeSeries(ds); } catch { /* already gone */ }
          }
          divergenceSeriesRef.current = [];

          const colors = themeColors();
          const divergences = detectDivergences(closes, rsiVals, RSI_DEFAULTS);
          for (const d of divergences) {
            const ds = chart.addSeries(LineSeries, {
              color: d.kind === 'bullish' ? colors.bullish : colors.bearish,
              lineWidth: 2,
              lineStyle: 2,
              priceLineVisible: false,
              lastValueVisible: false,
              crosshairMarkerVisible: false,
            }, 1);
            ds.setData([
              { time: points[d.fromIdx].time, value: rsiVals[d.fromIdx]! },
              { time: points[d.toIdx].time, value: rsiVals[d.toIdx]! },
            ]);
            divergenceSeriesRef.current.push(ds);
          }
        }

        // Auto support/resistance on the price pane. Detection scope =
        // chart's currently visible time range. Below the 90-min
        // minimum the helper no-ops (preserves existing lines so a
        // user zoom-in doesn't blank the chart).
        scheduleSrRecomputeRef.current?.();

        if (firstLoad) {
          firstLoad = false;
          // Guard against a thin first-fetch (only the in-progress
          // bar landed, history endpoint hadn't returned). Skip any
          // viewport override — the next 30s refresh will populate
          // the dataset and the standard path takes over. Without
          // this, ``setVisibleRange(now-90m..now)`` over a 1-bar
          // dataset can leave lightweight-charts showing a single
          // bar at the right edge with a flat Y range.
          if (fullBars.length < 5) {
            // intentionally no-op — let lightweight-charts auto-fit
            // to whatever sliver of data it has; refresh will fix it.
          } else {
          // Hard-refresh path. If the saved range carries bar-relative
          // values, restore using those — the operator's prior pan
          // and zoom both survive. Without them (older entries),
          // fall back to the width-preserve-anchor-to-now behavior.
          const sb = savedRange?.rightSpaceBars;
          const sw = savedRange?.widthBars;
          if (
            sb != null && sw != null
            && Number.isFinite(sb) && Number.isFinite(sw)
            && sw >= 10   // reject corrupted saves < 30 min wide
          ) {
            const barCount = fullBars.length;
            const newTo = (barCount - 1) + sb;
            const newFrom = newTo - sw;
            // Hydrate the in-memory ref too so the in-session
            // refresh path doesn't have to wait for a user gesture
            // to repopulate.
            userLogicalRangeRef.current = {
              rightSpaceBars: sb, widthBars: sw,
            };
            try {
              chart.timeScale().setVisibleLogicalRange(
                { from: newFrom, to: newTo },
              );
            } catch { /* range invalid for current data — skip */ }
          } else {
            // No usable saved range. Always land on the deterministic
            // default-width view anchored to now. Earlier the fallback
            // honored ``prevRange.to - prevRange.from`` which could
            // reproduce a corrupted narrow width on reload — make it
            // a fixed ``visibleMinutes`` instead so hard-refresh is
            // predictable.
            const nowSec = localUtcSeconds(new Date());
            const widthSec = visibleMinutes * 60;
            chart.timeScale().setVisibleRange({
              from: (nowSec - widthSec) as UTCTimestamp,
              to: nowSec,
            });
          }
          } // end fullBars.length >= 5 branch
        } else if (userLogicalRangeRef.current) {
          // Re-anchor the saved bar-relative viewport to the freshly-
          // loaded dataset. ``rightSpaceBars`` keeps the last-bar →
          // y-axis gap constant; ``widthBars`` keeps the zoom level
          // constant. New bars therefore push the chart left while
          // the right edge stays a fixed distance from the y axis.
          const barCount = fullBars.length;
          const lr = userLogicalRangeRef.current;
          const newTo = (barCount - 1) + lr.rightSpaceBars;
          const newFrom = newTo - lr.widthBars;
          try {
            chart.timeScale().setVisibleLogicalRange(
              { from: newFrom, to: newTo },
            );
          } catch { /* range invalid for current data — skip */ }
        } else if (prevRange) {
          chart.timeScale().setVisibleRange({
            from: prevRange.from as UTCTimestamp,
            to: prevRange.to as UTCTimestamp,
          });
        }
        // Respect the user's Y zoom across refreshes. If a saved
        // (or in-session captured) price range exists, restore it
        // directly via the price-scale API; otherwise let auto-scale
        // re-fit the polyline into the middle third (scaleMargins).
        // Only Reset-Zoom now clears userPriceRangeRef.
        const yToRestore = userPriceRangeRef.current;
        if (yToRestore) {
          try { priceScale.setVisibleRange(yToRestore); }
          catch { /* range invalid for current data — fall back to auto */
            try { priceScale.setAutoScale(true); } catch { /* ignore */ }
          }
        } else {
          try { priceScale.setAutoScale(true); } catch { /* ignore */ }
        }
        onError?.(null);
      } catch (e: any) {
        if (cancelled) return;
        const msg = e?.message || 'Failed to load history';
        onError?.(msg);
        const isTransient = /\bAPI\s+5\d{2}\b/.test(msg);
        if (isTransient) {
          retryTimer = setTimeout(load, retryDelayMs);
          retryDelayMs = Math.min(retryDelayMs * 2, RETRY_MAX_MS);
        }
      } finally {
        if (!cancelled) onLoadingChange?.(false);
      }
    };

    if (!seriesRef.current || !chartRef.current) return;

    load();
    const id = setInterval(load, REFRESH_INTERVAL_MS);
    const wake = () => { if (!cancelled) load(); };
    wakeSubs.add(wake);
    return () => {
      cancelled = true;
      wakeSubs.delete(wake);
      clearInterval(id);
      if (retryTimer) clearTimeout(retryTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.conId, target?.symbol, target?.secType, chartVersion, visibleMinutes, resyncToken]);

  // Set a sensible default visible range as soon as a target is chosen,
  // BEFORE the historical fetch returns. Otherwise lightweight-charts
  // auto-fits to whatever the first live tick puts in the series, and
  // the chart looks zoomed-in until load() finishes. Saved zoom (if
  // any) takes precedence; the data-fetch path will re-apply it again
  // after data lands, which is a no-op.
  useEffect(() => {
    if (!target) return;
    const chart = chartRef.current;
    if (!chart) return;
    const saved = loadSavedRange(targetKey(target));
    const nowSec = localUtcSeconds(new Date());
    // Always anchor the right edge to "now" on initial load. We retain
    // the *width* of the saved zoom — i.e. how far back the user was
    // looking — but slide the window forward so the live tail is in
    // view. This matches user expectation: "remember zoom level,
    // don't strand me where I left off."
    const widthSec = saved
      ? Math.max(60, saved.to - saved.from)
      : visibleMinutes * 60;
    const range = { from: nowSec - widthSec, to: nowSec };
    try {
      chart.timeScale().setVisibleRange({
        from: range.from as UTCTimestamp,
        to: range.to as UTCTimestamp,
      });
    } catch {
      // setVisibleRange can throw on a chart that has no data yet in
      // some lightweight-charts builds. The data-fetch effect re-applies
      // the same range after series.setData() which always succeeds.
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.conId, target?.symbol, target?.secType, chartVersion, visibleMinutes, resyncToken]);

  // Canonical SR fetch — populates the SINGLE source of truth that
  // drives line drawing, wedge shading, and the apex badge. Fetched
  // over PRELOAD_HOURS (24h by default) so the chart sees every
  // structure the operator might zoom out to inspect, not just the
  // bot's 2h window. Refreshed every 15s + whenever the target
  // changes; chart pan/zoom does NOT trigger a re-fetch (renderer
  // uses cached data + ``allBars`` to clip to visible).
  useEffect(() => {
    if (!target) return;
    let cancelled = false;
    const doFetch = async () => {
      // Pass the operator's broken-minutes setting + show-broken
      // wedges toggle so the backend's line detection and wedge set
      // align with what the chart will actually render (otherwise
      // the broken-minutes slider was capped at the Python default).
      const minBroken = brokenMinutesRef.current ?? 30;
      const breakStaleBars = Math.max(
        1, Math.ceil(minBroken * 60 / BAR_SECONDS),
      );
      const payload = await fetchBackendSr(
        // SR detection lookback intentionally shorter than the chart's
        // visible-history (PRELOAD_HOURS) — see SR_LOOKBACK_HOURS
        // docstring for why.
        target, SR_LOOKBACK_HOURS, BAR_SIZE,
        {
          breakStaleBars,
          includeBrokenWedges: showBrokenSrRef.current,
        },
      );
      if (cancelled) return;
      // Preserve last-good data on a transient fetch failure (engine
      // restart, network blip). Wiping to null would blank every SR
      // line until the next 15s tick — annoying for the operator.
      if (payload !== null) {
        srBackendDataRef.current = payload;
      }
      scheduleSrRecomputeRef.current?.(true);
    };
    doFetch();
    const id = window.setInterval(doFetch, 15000);
    const wake = () => { if (!cancelled) doFetch(); };
    wakeSubs.add(wake);
    return () => {
      cancelled = true;
      wakeSubs.delete(wake);
      window.clearInterval(id);
    };
  }, [target?.conId, target?.symbol, target?.secType, resyncToken]);

  // Regime fetch removed 2026-06-02 along with the badge it drove.
  // No /api/regime calls are made from the chart any more; the
  // backend endpoint stays around in case a future surface needs it.

  // One-time DOM sweep on mount: any badge element painted by a
  // pre-restart bundle (still hanging in the user's stale tab) is
  // removed. Identifies the badge by its distinctive monospace font
  // + position-absolute top:6px;left:8px. Cheap; only runs once per
  // chart mount.
  useEffect(() => {
    const containerEl = containerRef.current;
    if (!containerEl) return;
    const stragglers = containerEl.querySelectorAll<HTMLDivElement>(
      'div[style*="font-family:ui-monospace"]',
    );
    stragglers.forEach((node) => {
      const txt = node.textContent || '';
      if (
        txt.includes('V·') || txt.includes('ADX·')
        || txt.includes('BOS:') || txt.includes('V: …')
      ) {
        node.parentNode?.removeChild(node);
      }
    });
  }, [target?.conId, target?.symbol, target?.secType, resyncToken]);

  // Live tick subscription. Engine publishes STK ticks keyed on
  // ``symbol`` and FUT ticks keyed on ``localSymbol`` (= the IB-paste
  // form like MESM6 — same string we hold as ``target.symbol`` for
  // futures rows). OPT is skipped server-side because option tickers
  // share the underlying's symbol; clients route to the underlying STK
  // anyway via the OPT→STK substitution in the store.
  useEffect(() => {
    if (!target || !target.symbol) return;
    if (target.secType !== 'STK' && target.secType !== 'FUT') return;
    if (!seriesRef.current) return;

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${proto}//${window.location.host}/ws`;
    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;
    // Frame-arrival watchdog. The chart opens a RAW WebSocket here
    // (not the central wsManager that PositionsPanel / WatchlistPanel
    // use), so there's no ping/pong heartbeat. After a laptop-sleep
    // or NAT/proxy timeout the socket can stay in readyState=OPEN
    // while no frames arrive — ``onclose`` never fires, so the
    // existing setTimeout-on-close reconnect path never triggers.
    // useVisibilityWake catches the hidden→visible case but does
    // NOT fire when the tab stays visible across a laptop sleep.
    // This watchdog: if no frame for STALE_THRESHOLD_MS, force-close
    // so onclose → setTimeout(open, 2000) reconnects. Pure additive;
    // no behaviour change when the WS is healthy.
    let lastFrameAt: number = Date.now();
    const STALE_THRESHOLD_MS = 60_000;
    const WATCHDOG_TICK_MS = 30_000;

    const open = () => {
      if (closed) return;
      ws = new WebSocket(url);
      lastFrameAt = Date.now();
      ws.onopen = () => {
        lastFrameAt = Date.now();
        ws?.send(JSON.stringify({ type: 'subscribe_quote', symbol: target.symbol }));
      };
      ws.onmessage = (ev) => {
        lastFrameAt = Date.now();
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type !== 'quote' || msg.symbol !== target.symbol) return;
          // Drop ticks until the historical setData has populated the
          // series. lightweight-charts auto-fits the time scale to the
          // first few points if it gets data in an empty series, and
          // ticks landing during the ~1-3s historical-fetch window
          // would create a 1-second visible window that the later
          // setVisibleRange can't always reclaim. After historical
          // data is in, normal ticks just shift the existing window
          // forward keeping its width.
          if (!historicalLoadedRef.current) return;
          const series = seriesRef.current;
          if (!series) return;
          const lastStr = msg.data?.last;
          if (lastStr == null) return;
          const last = parseFloat(String(lastStr));
          if (!Number.isFinite(last)) return;
          // Round down to the bar boundary (BAR_SECONDS = 3 min) so
          // ticks within the same bar update the existing bar in
          // place. The visible window then only shifts when a real
          // bar boundary crosses (every 3 min) — matching the user's
          // mental model "this is a 3-min chart, advance one bar at
          // a time."  +BAR_SECONDS for slot-END labelling so the
          // in-progress bar lands at the next 3-min wallclock mark
          // (e.g. ticks in [15:27, 15:30) plot at x=15:30, matching
          // the bar that WILL close at that mark).
          const nowSec = localUtcSeconds(new Date());
          const barSec = Math.floor(nowSec / BAR_SECONDS) * BAR_SECONDS
            + BAR_SECONDS;
          // Stall-detection (2026-06-09): if a tick is landing for a
          // bar more than 90 s past the previously-pushed bar's slot,
          // the chart's bar materialisation has stalled (the "graph
          // hasn't drawn for 3 min" symptom). Surface it as a small
          // amber pill on the chart canvas — no DevTools required.
          // Clears itself when normal cadence resumes.
          {
            const prevBar = lastTickBarSecRef.current ?? 0;
            const gapSec = barSec - prevBar;
            if (prevBar > 0 && gapSec > 90) {
              setBarStall({
                gapSec,
                sinceMs: Date.now(),
                prevBarIso: new Date(prevBar * 1000).toISOString(),
                newBarIso: new Date(barSec * 1000).toISOString(),
              });
            } else if (prevBar > 0 && gapSec <= 60 && barStall) {
              // Cadence is back to ≤1 min — drop the badge so the
              // operator knows the chart has recovered.
              setBarStall(null);
            }
          }
          series.update({ time: barSec as UTCTimestamp, value: last });
          lastTickBarSecRef.current = barSec;
          // Mirror the update into the OHLC bars ref so SR pivot
          // detection (which reads bar.high / bar.low) sees the live
          // wick. Within the same 3-min bar: extend high/low and
          // overwrite close. New bar boundary: append a fresh bar
          // with all four fields = current tick.
          const allBars = barsRef.current;
          const lastBar = allBars[allBars.length - 1];
          if (lastBar && lastBar.time === barSec) {
            lastBar.close = last;
            if (last > lastBar.high) lastBar.high = last;
            if (last < lastBar.low) lastBar.low = last;
          } else {
            allBars.push({
              time: barSec as UTCTimestamp,
              open: last, high: last, low: last, close: last,
            });
          }
          // Trigger SR re-evaluation on every live tick. The throttle
          // inside scheduleSrRecomputeRef caps actual recomputes at
          // 1/sec so fast-tape charts don't churn. This is what
          // catches a clear break the moment it happens — without
          // it, the line would visually lag up to 3 min until the
          // next bar-close trigger.
          scheduleSrRecomputeRef.current?.();
          // Reposition the SR signal badges directly. ``subscribeVisible
          // TimeRangeChange`` does NOT fire when lightweight-charts
          // auto-extends the right edge for new data — so without this,
          // the chart pans forward but the B/S badges stay pinned at
          // their old pixel coordinates and "fall off" the price
          // polyline. The SR recompute itself is debounced (1/sec) and
          // sometimes early-exits, so it can't be relied on to
          // reposition every tick.
          repositionSrSignalsRef.current?.();
        } catch { /* malformed frame — ignore */ }
      };
      ws.onclose = () => { if (!closed) retry = setTimeout(open, 2000); };
      ws.onerror = () => { /* onclose handles reconnect */ };
    };
    open();

    const wake = () => {
      if (closed) return;
      if (retry) { clearTimeout(retry); retry = null; }
      if (ws) {
        ws.onclose = null;
        try { ws.close(); } catch { /* already closed */ }
        ws = null;
      }
      open();
    };
    wakeSubs.add(wake);

    // Periodic staleness check (see lastFrameAt declaration above).
    const watchdog = window.setInterval(() => {
      if (closed || !ws) return;
      if (ws.readyState !== WebSocket.OPEN) return;
      const sinceFrame = Date.now() - lastFrameAt;
      if (sinceFrame > STALE_THRESHOLD_MS) {
        try { ws.close(); } catch { /* already closed */ }
        // ws.onclose schedules a setTimeout-based reopen, so we
        // don't call open() directly here. Reset lastFrameAt so
        // the watchdog doesn't immediately trip again on the new
        // socket's startup gap.
        lastFrameAt = Date.now();
      }
    }, WATCHDOG_TICK_MS);

    return () => {
      closed = true;
      wakeSubs.delete(wake);
      window.clearInterval(watchdog);
      if (retry) clearTimeout(retry);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    };
  }, [target?.symbol, target?.secType, chartVersion, resyncToken]);

  useImperativeHandle(ref, () => ({
    resetZoom: () => {
      const chart = chartRef.current;
      const series = seriesRef.current;
      if (!chart || !series) return;
      const nowSec = localUtcSeconds(new Date());
      const range: SavedRange = {
        from: nowSec - visibleMinutes * 60,
        to: nowSec,
      };
      chart.timeScale().setVisibleRange({
        from: range.from as UTCTimestamp,
        to: range.to as UTCTimestamp,
      });
      userRangeRef.current = range;
      userLogicalRangeRef.current = null;
      const tgt = targetRef.current;
      if (tgt) saveRange(targetKey(tgt), range);
      // Reset Y to the "polyline fills the middle third, centered"
      // rule. scaleMargins is reapplied here defensively so an
      // HMR-preserved chart instance from before this change picks
      // up the 1/3 + 1/3 margins; setAutoScale(true) lets the chart
      // auto-fit the polyline into the remaining middle third.
      const priceScale = chart.priceScale('right');
      try {
        priceScale.applyOptions({
          scaleMargins: { top: 0.05, bottom: 0.15 },
        });
      } catch { /* ignore */ }
      try { priceScale.setAutoScale(true); } catch { /* ignore */ }
      userPriceRangeRef.current = null;
    },
    clearSupportResistance: () => {
      // Drop the currently-rendered SR overlay series and set the gate
      // so the next refresh tick (and subsequent ones for this target)
      // skip drawing them. Switching to a different target resets the
      // gate so SR re-shows automatically.
      const chart = chartRef.current;
      if (chart) {
        for (const ds of srSeriesRef.current) {
          try { chart.removeSeries(ds); } catch { /* already gone */ }
        }
      }
      srSeriesRef.current = [];
      // Wipe sticky buy/sell signals from the bounded queue.
      // ``botPinnedBadgeRef`` is intentionally preserved — Clear-SR
      // is for decluttering the detector overlay; the bot's active
      // entry marker is operational, not noise. On chart-bot panes
      // there's no easy way to re-summon it once cleared, so losing
      // the entry marker would leave the operator visually blind to
      // their open position.
      srSignalsRef.current = [];
      repositionSrSignalsRef.current?.();
      srHiddenRef.current = true;
      setSrHiddenTick((n) => n + 1);
    },
    showSupportResistance: () => {
      // Re-enable detection + redraw immediately. The next scheduled
      // recompute tick (within ~1s) will repopulate the overlay.
      srHiddenRef.current = false;
      setSrHiddenTick((n) => n + 1);
      scheduleSrRecomputeRef.current?.(true);
    },
    isSupportResistanceHidden: () => srHiddenRef.current,
    toggleSrSourceMode: () => {
      srSourceModeRef.current =
        srSourceModeRef.current === 'be' ? 'fe' : 'be';
      // Drop the cached backend payload + apex so a stale snapshot
      // doesn't bleed through after the toggle. The next BE turn
      // refetches on the 15s interval (or sooner if anything else
      // triggers the effect).
      srBackendDataRef.current = null;
      srBackendApexRef.current = null;
      srArrowsRef.current = [];
      scheduleSrRecomputeRef.current?.(true);
      setSrSourceModeTick((n) => n + 1);
      return srSourceModeRef.current;
    },
    getSrSourceMode: () => srSourceModeRef.current,
    snapshotZoom: () => {
      const chart = chartRef.current;
      if (!chart) return null;
      const snap: ZoomSnapshot = {};
      try {
        const lr = chart.timeScale().getVisibleLogicalRange();
        if (lr) snap.logical = { from: lr.from, to: lr.to };
      } catch { /* ignore */ }
      try {
        const pr = chart.priceScale('right').getVisibleRange();
        if (pr) snap.price = { from: pr.from, to: pr.to };
      } catch { /* ignore */ }
      return snap;
    },
    restoreZoomSnapshot: (snap) => {
      const chart = chartRef.current;
      if (!chart || !snap) return;
      // Defer one frame so we run after any layout reflow caused by
      // the caller (e.g. html2canvas cloning + resize observers).
      requestAnimationFrame(() => {
        try {
          if (snap.logical) chart.timeScale().setVisibleLogicalRange(snap.logical);
        } catch { /* ignore */ }
        try {
          if (snap.price) chart.priceScale('right').setVisibleRange(snap.price);
        } catch { /* ignore */ }
      });
    },
    withFrozenViewport: async <T,>(fn: () => Promise<T>): Promise<T> => {
      const chart = chartRef.current;
      if (!chart) return fn();
      const ts = chart.timeScale();
      const priceScale = chart.priceScale('right');
      const lr = (() => { try { return ts.getVisibleLogicalRange(); } catch { return null; } })();
      const pr = (() => { try { return priceScale.getVisibleRange(); } catch { return null; } })();
      // Tell the custom ResizeObserver to skip its setAutoScale
      // re-fit — html2canvas's DOM clone trips that observer mid-
      // capture and that's what was popping Y back to autoscale.
      viewportFrozenRef.current = true;
      // Lock every input + auto-shift path. handleScroll covers
      // wheel-pan + drag; handleScale covers wheel-zoom + pinch.
      // shiftVisibleRangeOnNewBar=false freezes the right-edge for
      // the duration. setAutoScale(false) on the price axis pins Y.
      try {
        chart.applyOptions({
          handleScroll: false,
          handleScale: false,
          timeScale: { shiftVisibleRangeOnNewBar: false },
        });
      } catch { /* ignore */ }
      try { priceScale.setAutoScale(false); } catch { /* ignore */ }
      // Pin Y BEFORE the capture too — setAutoScale(false) alone
      // doesn't anchor the range; an explicit setVisibleRange does.
      try { if (pr) priceScale.setVisibleRange(pr); } catch { /* ignore */ }
      try {
        return await fn();
      } finally {
        // Re-pin the snapshot ranges before re-enabling inputs.
        // Without this, html2canvas may have triggered a re-fit
        // mid-capture; restoring the snapshot here closes the gap
        // before the user sees anything.
        try { if (lr) ts.setVisibleLogicalRange(lr); } catch { /* ignore */ }
        try { if (pr) priceScale.setVisibleRange(pr); } catch { /* ignore */ }
        try {
          chart.applyOptions({
            handleScroll: true,
            handleScale: true,
            timeScale: { shiftVisibleRangeOnNewBar: true },
          });
        } catch { /* ignore */ }
        // Defer clearing the freeze flag one frame so the trailing
        // ResizeObserver callback from html2canvas's iframe teardown
        // (fires AFTER finally on some browsers) still no-ops.
        requestAnimationFrame(() => {
          viewportFrozenRef.current = false;
        });
      }
    },
  }), [visibleMinutes, srHiddenTick]);

  return (
    <div className="relative" style={{ width: '100%', height: '100%', minHeight: 0 }}>
      <div ref={containerRef} style={{ position: 'absolute', inset: 0 }} />
      {barStall && (
        <div
          style={{
            position: 'absolute',
            top: 6,
            left: '50%',
            transform: 'translateX(-50%)',
            zIndex: 25,
            pointerEvents: 'none',
            padding: '2px 10px',
            borderRadius: 3,
            background: 'rgba(247,189,92,0.92)',
            color: '#1a1a1a',
            fontFamily: 'ui-monospace, monospace',
            fontSize: 10,
            fontWeight: 700,
            letterSpacing: '0.05em',
            whiteSpace: 'nowrap',
          }}
          // Hover tooltip carries the precise bar timestamps for the
          // operator to read without DevTools, in case the bridged
          // bar-slot matters (e.g. always at 1-min boundary vs random).
          title={
            `gap ${barStall.gapSec}s · prev bar ${barStall.prevBarIso}`
            + ` · jumped to ${barStall.newBarIso}`
          }
        >
          ⚠ BARS STALLED · gap {(barStall.gapSec / 60).toFixed(1)} min
        </div>
      )}
      {!target && placeholder && (
        <div
          className="flex items-center justify-center text-xs"
          style={{
            position: 'absolute', inset: 0,
            color: 'var(--text-muted)',
            background: 'var(--panel-bg, transparent)',
            pointerEvents: 'none',
          }}
        >
          {placeholder}
        </div>
      )}
    </div>
  );
});

