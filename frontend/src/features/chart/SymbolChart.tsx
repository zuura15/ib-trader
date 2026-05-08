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
import { useUserSetting } from '../../data/userSettings';

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
}

export const SymbolChart = forwardRef<SymbolChartHandle, Props>(function SymbolChart(
  {
    target,
    visibleMinutes = VISIBLE_MINUTES,
    showRsi = true,
    placeholder = 'Click a row in Positions or Watchlist to chart it.',
    showBrokenSr = false,
    brokenMinutes = 30,
    showCounterSupport = false,
    showCounterResistance = false,
    onLoadingChange,
    onError,
  }: Props,
  ref,
) {
  const targetRef = useRef(target);
  useEffect(() => { targetRef.current = target; }, [target]);

  // Wipe sticky buy/sell signals when the user switches to a different
  // symbol. Without this, MGCM6's historical badges would render on
  // ESM6's chart at the same wallclock pivot times — confusing.
  useEffect(() => {
    srSignalsRef.current = [];
    repositionSrSignalsRef.current?.();
  }, [target?.conId, target?.symbol, target?.secType]);

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
  const srOverlayRef = useRef<SVGSVGElement | null>(null);
  const srHiddenRef = useRef(false);
  // Track filter toggles via refs so the throttled recompute closure
  // sees fresh values without re-subscribing on every prop change.
  const showBrokenSrRef = useRef(showBrokenSr);
  const brokenMinutesRef = useRef(brokenMinutes);
  const showCounterSupportRef = useRef(showCounterSupport);
  const showCounterResistanceRef = useRef(showCounterResistance);
  useEffect(() => {
    showBrokenSrRef.current = showBrokenSr;
    brokenMinutesRef.current = brokenMinutes;
    showCounterSupportRef.current = showCounterSupport;
    showCounterResistanceRef.current = showCounterResistance;
    // Re-render lines on toggle. ``force=true`` bypasses the SR_MIN_BARS
    // early-exit so the toggle takes effect even when zoomed in below
    // 90 min — passive zoom/pan triggers still respect that guard.
    scheduleSrRecomputeRef.current?.(true);
  }, [showBrokenSr, brokenMinutes, showCounterSupport, showCounterResistance]);
  // Debounced SR recompute. Set inside the chart-create effect (since
  // it captures the chart instance); the visible-range subscription
  // calls into it via this ref so we don't have to re-subscribe each
  // time the closure changes.
  const scheduleSrRecomputeRef = useRef<((force?: boolean) => void) | null>(null);
  // Repaints the SVG overlay with current ``srSignalsRef`` content.
  // Set inside the chart-create effect (captures chart + container).
  // Called from the SR recompute and from pan/zoom/resize events.
  const repositionSrSignalsRef = useRef<(() => void) | null>(null);
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
  // Logical-range mirror of the user's current viewport. Captured
  // alongside ``userRangeRef`` on every pan/zoom so we can restore
  // pan position in *bar-index* space across periodic ``load()``
  // refreshes. Time-domain restoration loses the user's "empty space
  // past the last bar" because lightweight-charts re-clamps `to` to
  // data bounds when ``setData`` runs; the logical-domain API
  // survives this because it stores positions as bar indices that
  // re-anchor on the new data set.
  const userLogicalRangeRef = useRef<{ from: number; to: number } | null>(null);
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
        // Don't auto-shift the viewport when a new bar arrives. The
        // user's current pan/zoom is sticky — only the "Reset Zoom"
        // button (or a fresh symbol load) snaps back to the live
        // edge. This is the right tradeoff for analysis: examining
        // an older window shouldn't get yanked forward every 3 min
        // just because a new bar formed.
        shiftVisibleRangeOnNewBar: false,
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
        scaleMargins: { top: 0.25, bottom: 0.25 },
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

    chartRef.current = chart;
    seriesRef.current = series;
    rsiSeriesRef.current = rsi;
    divergenceSeriesRef.current = [];

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

    repositionSrSignalsRef.current = () => {
      const svg = srOverlayRef.current;
      const ch = chartRef.current;
      const ser = seriesRef.current;
      if (!svg || !ch || !ser) return;
      // Empty + repaint. Cheap (handful of children); keeps the
      // logic linear instead of diffing.
      while (svg.firstChild) svg.removeChild(svg.firstChild);
      // Skip on tiny chart panes — the stacked-charts sparklines are
      // ~46px tall, so a letter 28px above the bar lands outside the
      // pane and looks broken. Buy/sell signals are an analyst tool;
      // they belong on the main chart, not on overview thumbnails.
      const svgRect = svg.getBoundingClientRect();
      if (svgRect.height < 120) return;
      const ts = ch.timeScale();
      const LETTER_OFFSET_PX = 36;   // distance from bar to letter
      // Active window is ``signalActiveBars`` bars from the anchor.
      // Computed once per repaint against ``Date.now()`` so badges
      // dim naturally as bars roll over without needing an SR
      // recompute to flip them.
      const nowSec = Math.floor(Date.now() / 1000);
      const activeWindowSec = signalActiveBarsRef.current * BAR_SECONDS;
      for (const sig of srSignalsRef.current) {
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
        const bg = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
        bg.setAttribute('cx', String(x));
        bg.setAttribute('cy', String(yLetter));
        bg.setAttribute('r', String(RADIUS));
        bg.setAttribute('fill', badgeColor);
        bg.setAttribute('stroke', themeNow.background);
        bg.setAttribute('stroke-width', '2');
        bg.setAttribute('opacity', opacity);
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
    };

    setChartVersion((v) => v + 1);

    // SR detection helper, scoped to this chart instance. Reads the
    // current visible time range, slices the price series to the
    // bars in view, runs detection, draws lines. Skipped when the
    // visible window is below SR_MIN_BARS (90 min on a 3-min chart).
    let srTimer: ReturnType<typeof setTimeout> | null = null;
    const recomputeSr = (forceRecompute = false): void => {
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
      // Below 90 min on a passive trigger (zoom/pan/tick): leave the
      // existing lines alone — recomputing on a small slice would
      // produce short-span lines that thrash on each zoom. A FORCED
      // trigger (broken-toggle) must redraw regardless so the
      // toggle takes effect at any zoom.
      if (widthBars < SR_MIN_BARS && !forceRecompute) return;

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

      const slice = allBars.slice(fromIdx, toIdx + 1);
      // Map "show broken for N minutes" to bar count. BAR_SECONDS=180
      // (3 min). When the broken toggle is off the value is irrelevant
      // — broken lines get filtered out at render anyway — but we
      // still keep ``breakStaleBars`` permissive enough that the
      // engine retains them for an immediate toggle-on.
      const breakStaleBars = Math.max(
        1, Math.ceil((brokenMinutesRef.current ?? 30) * 60 / BAR_SECONDS),
      );
      const lines = detectSupportResistance(slice, { breakStaleBars });
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
        const ds = ch.addSeries(LineSeries, {
          color,
          lineWidth: 1,
          // lightweight-charts LineStyle: 0=solid, 1=dotted, 2=dashed.
          // Visual hierarchy: confirmed = solid (most prominent),
          // tentative 2-touch = dotted (forming, less committed),
          // broken/archived = dashed (gave way; kept on screen briefly).
          lineStyle: isBroken ? 2 : confirmed ? 0 : 1,
          priceLineVisible: false,
          lastValueVisible: false,
          crosshairMarkerVisible: false,
          // Exclude SR lines from the right price scale's auto-fit.
          // Otherwise a deeply-anchored support that extends below
          // the visible price range pulls the auto-scaled Y window
          // wide, squashing the actual price polyline to a sliver.
          autoscaleInfoProvider: () => null,
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

        // Signal: uptrending support or downtrending resistance with
        // 3+ confirming pivot touches → buy/sell indicator. Anchored
        // at the line's anchor-B (most-recent construction pivot).
        // Broken lines already filtered above. Price comes from the
        // line algebra (not the close at that bar) so the leader
        // line lands on the exact pivot the line is constructed on.
        if (confirmed) {
          const isBuy = line.type === 'support' && line.slope > 0;
          const isSell = line.type === 'resistance' && line.slope < 0;
          if (isBuy || isSell) {
            const anchorIdx = line.anchorBIdx;
            // Dedup against existing fired signals AND new ones in
            // this recompute. Bar-time is the canonical axis (idx is
            // slice-local and shifts as the visible window slides;
            // bar-time is stable).
            const anchorTime = slice[anchorIdx].time as number;
            const dedupKey = (anchorTime << 1) | (isBuy ? 1 : 0);
            // Skip if any existing signal already covers this key.
            const alreadyKnown = srSignalsRef.current.some((s) =>
              ((s.time as number) << 1 | (s.side === 'B' ? 1 : 0)) === dedupKey,
            );
            if (!alreadyKnown) {
              newSignals.push({
                time: slice[anchorIdx].time as UTCTimestamp,
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

      // Append newly-fired signals (existing ones already kept).
      srSignalsRef.current = [...srSignalsRef.current, ...newSignals];
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
      };
      void fetch('/api/debug/log/sr-debug', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(snapshot),
      }).catch(() => {
        // server-side debug log is best-effort; ignore network errors
      });
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
        const pr = chart.priceScale('right').getVisibleRange();
        // Repaint signals on any Y change (wheel/pinch/axis drag).
        repositionSrSignalsRef.current?.();
        if (!pr) return;
        const cur = userPriceRangeRef.current;
        if (cur && cur.from === pr.from && cur.to === pr.to) return;
        userPriceRangeRef.current = { from: pr.from, to: pr.to };
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
    if (containerEl) {
      containerEl.addEventListener('wheel', captureYRange, { passive: true });
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
        userLogicalRangeRef.current = { from: lr.from, to: lr.to };
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
        try { chart.priceScale('right').setAutoScale(true); } catch { /* ignore */ }
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
        containerEl.removeEventListener('wheel', captureYRange);
        containerEl.removeEventListener('mouseup', captureYRange);
        containerEl.removeEventListener('touchend', captureYRange);
      }
      if (srOverlayRef.current && srOverlayRef.current.parentNode) {
        srOverlayRef.current.parentNode.removeChild(srOverlayRef.current);
      }
      srOverlayRef.current = null;
      srSignalsRef.current = [];
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
          if (
            savedRange?.priceFrom != null && savedRange?.priceTo != null
          ) {
            userPriceRangeRef.current = {
              from: savedRange.priceFrom,
              to: savedRange.priceTo,
            };
          }
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
        } else if (userLogicalRangeRef.current) {
          // Prefer logical range so any empty space the user kept past
          // the last bar is preserved when new bars arrive — see ref
          // declaration for the rationale.
          try {
            chart.timeScale().setVisibleLogicalRange(
              userLogicalRangeRef.current,
            );
          } catch { /* range invalid for current data — skip */ }
        } else if (prevRange) {
          chart.timeScale().setVisibleRange({
            from: prevRange.from as UTCTimestamp,
            to: prevRange.to as UTCTimestamp,
          });
        }
        // Periodic refresh re-fits Y to the polyline's current
        // amplitude (rule: amp = 1/3 of chart, centered, via
        // scaleMargins). Drops any manual Y the user had — by
        // design: refresh, reset, and resize all snap Y back; only
        // X-only pan/zoom respects an in-progress manual Y.
        try { priceScale.setAutoScale(true); } catch { /* ignore */ }
        userPriceRangeRef.current = null;
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
          scaleMargins: { top: 0.25, bottom: 0.25 },
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
      // Wipe sticky buy/sell signals too — Clear-SR is the explicit
      // "I want a clean slate" gesture, so historical badges go too.
      srSignalsRef.current = [];
      repositionSrSignalsRef.current?.();
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

