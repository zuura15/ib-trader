import {
  forwardRef, useEffect, useImperativeHandle, useRef, useState,
} from 'react';
import {
  createChart, ColorType, LineSeries,
  type IChartApi, type ISeriesApi, type UTCTimestamp,
} from 'lightweight-charts';
import { getHistory } from '../../api/client';
import {
  type SavedRange, type Bar,
  VISIBLE_MINUTES, PRELOAD_HOURS, REFRESH_INTERVAL_MS, BAR_SIZE, BAR_SECONDS,
  targetKey, loadSavedRange, saveRange,
  toBars, themeColors, localUtcSeconds,
} from './chartUtils';
import { computeRsi, detectDivergences, RSI_DEFAULTS } from './rsiDivergence';
import { detectSupportResistance } from './supportResistance';
import type { ChartTarget } from '../../data/store';

export interface SymbolChartHandle {
  resetZoom: () => void;
  /** Hide the auto-drawn support/resistance lines for the current
   *  target. They re-detect (and re-show) on the next refresh tick or
   *  when the user changes target. */
  clearSupportResistance: () => void;
}

interface Props {
  target: ChartTarget | null;
  /** Visible minutes default. Defaults to chartUtils VISIBLE_MINUTES (90). */
  visibleMinutes?: number;
  /** Show the RSI sub-pane. Default true. */
  showRsi?: boolean;
  /** Render the placeholder "Click a row…" message when target is null. */
  placeholder?: string | null;
  /** Show broken/archived S/R lines (amber dashed). Off by default;
   *  the line stays in the SR engine state for ``breakStaleBars`` but
   *  is filtered out of render unless this is true. */
  showBrokenSr?: boolean;
  /** Optional callback invoked whenever loading state changes. */
  onLoadingChange?: (loading: boolean) => void;
  /** Optional callback for errors. */
  onError?: (msg: string | null) => void;
}

export const SymbolChart = forwardRef<SymbolChartHandle, Props>(function SymbolChart(
  {
    target,
    visibleMinutes = VISIBLE_MINUTES,
    showRsi = true,
    placeholder = 'Click a row in Positions or Watchlist to chart it.',
    showBrokenSr = false,
    onLoadingChange,
    onError,
  }: Props,
  ref,
) {
  const targetRef = useRef(target);
  useEffect(() => { targetRef.current = target; }, [target]);

  const containerRef = useRef<HTMLDivElement | null>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const rsiSeriesRef = useRef<ISeriesApi<'Line'> | null>(null);
  const divergenceSeriesRef = useRef<ISeriesApi<'Line'>[]>([]);
  // Auto-detected support/resistance lines on the price pane. Recreated
  // every refresh; `srHiddenRef` lets the user dismiss them for the
  // current target via the Clear-SR button (re-shows on target switch).
  const srSeriesRef = useRef<ISeriesApi<'Line'>[]>([]);
  const srHiddenRef = useRef(false);
  // Track ``showBrokenSr`` via ref so the throttled recompute closure
  // sees fresh values without re-subscribing on every prop change.
  const showBrokenSrRef = useRef(showBrokenSr);
  useEffect(() => {
    showBrokenSrRef.current = showBrokenSr;
    // Re-render lines on toggle so the user sees the change immediately
    // rather than waiting for the next zoom/tick.
    scheduleSrRecomputeRef.current?.();
  }, [showBrokenSr]);
  // Debounced SR recompute. Set inside the chart-create effect (since
  // it captures the chart instance); the visible-range subscription
  // calls into it via this ref so we don't have to re-subscribe each
  // time the closure changes.
  const scheduleSrRecomputeRef = useRef<(() => void) | null>(null);
  // Below this many bars, SR detection is skipped entirely — the
  // existing lines stay on screen. 30 bars × 3 min = 90 min.
  const SR_MIN_BARS = 30;
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
  const [chartVersion, setChartVersion] = useState(0);

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
        // When live ticks land past the current visible range, slide
        // the window forward (preserving its width) so the price tail
        // stays in view. Default is true in v5; set explicitly so it
        // can't regress to false on a future lib update.
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
      rightPriceScale: { borderColor: colors.grid },
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

    chartRef.current = chart;
    seriesRef.current = series;
    rsiSeriesRef.current = rsi;
    divergenceSeriesRef.current = [];
    setChartVersion((v) => v + 1);

    // SR detection helper, scoped to this chart instance. Reads the
    // current visible time range, slices the price series to the
    // bars in view, runs detection, draws lines. Skipped when the
    // visible window is below SR_MIN_BARS (90 min on a 3-min chart).
    let srTimer: ReturnType<typeof setTimeout> | null = null;
    const recomputeSr = (): void => {
      const ch = chartRef.current;
      if (!ch) return;
      const allBars = barsRef.current;
      if (allBars.length === 0) return;
      const range = ch.timeScale().getVisibleRange();
      if (!range || range.from == null || range.to == null) return;
      const fromTime = Number(range.from);
      const toTime = Number(range.to);
      // Find first/last bar indices within the visible time range.
      let fromIdx = -1;
      let toIdx = -1;
      for (let i = 0; i < allBars.length; i++) {
        if (allBars[i].time >= fromTime) { fromIdx = i; break; }
      }
      for (let i = allBars.length - 1; i >= 0; i--) {
        if (allBars[i].time <= toTime) { toIdx = i; break; }
      }
      if (fromIdx < 0 || toIdx < 0 || toIdx <= fromIdx) return;
      const widthBars = toIdx - fromIdx + 1;
      // Below 90 min: leave the existing lines alone. Zoom-ins below
      // default don't disturb the chart per user spec.
      if (widthBars < SR_MIN_BARS) return;

      // Wipe + redraw.
      for (const ds of srSeriesRef.current) {
        try { ch.removeSeries(ds); } catch { /* already gone */ }
      }
      srSeriesRef.current = [];
      if (srHiddenRef.current) return;

      const slice = allBars.slice(fromIdx, toIdx + 1);
      const lines = detectSupportResistance(slice);
      const colors = themeColors();
      // Counter for human-readable line labels rendered on the right
      // axis. Numbered in detection order; lets the user point at a
      // specific line in conversation ("L3 looks wrong"). Logged to
      // console alongside so the data is one F12 away.
      let labelCounter = 0;
      const lineLog: Record<string, unknown>[] = [];
      for (const line of lines) {
        const isBroken = line.breakIdx != null;
        // Broken lines are noisy at zoom-out and clutter the active
        // structure the user is reading. The header toggle decides
        // whether they render; the engine still tracks them so a
        // toggle-on shows them without a recompute lag.
        if (isBroken && !showBrokenSrRef.current) continue;
        labelCounter += 1;
        const label = `L${labelCounter}`;
        // Color hierarchy:
        //   • broken (any touches) → amber/yellow, dashed — "archived"
        //     line that recently gave way; kept on screen briefly so
        //     the user sees what just broke
        //   • confirmed (3+ touches) → green (support) / red (resistance)
        //   • tentative (2 touches, anchor pair only) → blue
        // The blue → green/red transition gives a clear visual cue
        // when a forming trend earns its third confirmation touch.
        const confirmed = line.touches >= 3;
        const color = isBroken
          ? colors.archived
          : confirmed
            ? line.type === 'support' ? colors.bullish : colors.bearish
            : colors.line;
        const ds = ch.addSeries(LineSeries, {
          color,
          lineWidth: 1,
          // lightweight-charts LineStyle: 0=solid, 1=dotted, 2=dashed.
          // Visual hierarchy: confirmed = solid (most prominent),
          // tentative 2-touch = dotted (forming, less committed),
          // broken/archived = dashed (gave way; kept on screen briefly).
          lineStyle: isBroken ? 2 : confirmed ? 0 : 1,
          priceLineVisible: false,
          // Show the label + line price on the right axis. Tiny visual
          // cost; lets the user identify a specific line by name.
          lastValueVisible: true,
          title: label,
          crosshairMarkerVisible: false,
        });
        // line.fromIdx / toIdx are local to the slice; map back via
        // ``slice[i].time`` for the actual chart time. Price comes
        // from the line algebra so it draws straight regardless of
        // the close at the endpoint bar.
        const startTime = slice[line.fromIdx].time;
        const endTime = slice[line.toIdx].time;
        const startPrice = line.slope * line.fromIdx + line.intercept;
        const endPrice = line.slope * line.toIdx + line.intercept;
        ds.setData([
          { time: startTime, value: startPrice },
          { time: endTime, value: endPrice },
        ]);
        srSeriesRef.current.push(ds);
        // Companion debug log — same number as on the chart.
        const startBarTime = new Date((startTime as number) * 1000).toISOString();
        const endBarTime = new Date((endTime as number) * 1000).toISOString();
        const anchorBTime = new Date(
          (slice[line.anchorBIdx].time as number) * 1000,
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
            ? new Date((slice[line.breakIdx].time as number) * 1000).toISOString()
            : null,
          isBroken, confirmed,
        });
      }
      if (lineLog.length > 0) {
        // eslint-disable-next-line no-console
        console.table(lineLog);
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
    scheduleSrRecomputeRef.current = () => {
      const now = Date.now();
      const elapsed = now - lastSrFireMs;
      if (elapsed >= SR_THROTTLE_MS) {
        lastSrFireMs = now;
        recomputeSr();
        return;
      }
      if (srTimer) return;  // trailing fire already scheduled
      srTimer = setTimeout(() => {
        srTimer = null;
        lastSrFireMs = Date.now();
        recomputeSr();
      }, SR_THROTTLE_MS - elapsed);
    };

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
      const r: SavedRange = { from, to };
      userRangeRef.current = r;
      // Re-run SR detection over the new visible range (debounced —
      // zoom emits many events). Detection self-skips when width is
      // below the 90 min minimum, so zoom-ins below default leave
      // the existing lines alone.
      scheduleSrRecomputeRef.current?.();
      const tgt = targetRef.current;
      if (tgt) saveRange(targetKey(tgt), r);
    });

    return () => {
      if (srTimer) clearTimeout(srTimer);
      scheduleSrRecomputeRef.current = null;
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      rsiSeriesRef.current = null;
      divergenceSeriesRef.current = [];
      srSeriesRef.current = [];
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
      barsRef.current = [];
      onError?.(null);
      userRangeRef.current = null;
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
        series.setData(points);
        // Historical bars are now in the series — safe to let live
        // ticks update past this point without auto-fit-to-one-point
        // pathology.
        historicalLoadedRef.current = true;

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
          // First load anchors `to` to wall-clock "now" regardless of
          // what's persisted. We retain the *width* of the saved zoom
          // (so users keep their preferred lookback) but always show
          // the live tail. Subsequent loads in the same session
          // respect the user's panning/zooming via prevRange below.
          const nowSec = localUtcSeconds(new Date());
          const widthSec = prevRange
            ? Math.max(60, prevRange.to - prevRange.from)
            : visibleMinutes * 60;
          chart.timeScale().setVisibleRange({
            from: (nowSec - widthSec) as UTCTimestamp,
            to: nowSec,
          });
        } else if (prevRange) {
          chart.timeScale().setVisibleRange({
            from: prevRange.from as UTCTimestamp,
            to: prevRange.to as UTCTimestamp,
          });
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
    return () => {
      cancelled = true;
      clearInterval(id);
      if (retryTimer) clearTimeout(retryTimer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.conId, target?.symbol, target?.secType, chartVersion, visibleMinutes]);

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
  }, [target?.conId, target?.symbol, target?.secType, chartVersion, visibleMinutes]);

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

    const open = () => {
      if (closed) return;
      ws = new WebSocket(url);
      ws.onopen = () => {
        ws?.send(JSON.stringify({ type: 'subscribe_quote', symbol: target.symbol }));
      };
      ws.onmessage = (ev) => {
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
          // a time."
          const nowSec = localUtcSeconds(new Date());
          const barSec = Math.floor(nowSec / BAR_SECONDS) * BAR_SECONDS;
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
        } catch { /* malformed frame — ignore */ }
      };
      ws.onclose = () => { if (!closed) retry = setTimeout(open, 2000); };
      ws.onerror = () => { /* onclose handles reconnect */ };
    };
    open();

    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      if (ws) {
        ws.onclose = null;
        ws.close();
      }
    };
  }, [target?.symbol, target?.secType, chartVersion]);

  // Imperative reset-zoom handle for the parent toolbar. Anchors `to`
  // at wall-clock "now" so the live tail stays in view (same reasoning
  // as the firstLoad default-range branch above).
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
      const tgt = targetRef.current;
      if (tgt) saveRange(targetKey(tgt), range);
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
      srHiddenRef.current = true;
    },
  }), [visibleMinutes]);

  return (
    <div className="relative" style={{ width: '100%', height: '100%', minHeight: 0 }}>
      <div ref={containerRef} style={{ position: 'absolute', inset: 0 }} />
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

