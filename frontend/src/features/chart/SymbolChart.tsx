import {
  forwardRef, useEffect, useImperativeHandle, useRef, useState,
} from 'react';
import {
  createChart, ColorType, LineSeries,
  type IChartApi, type ISeriesApi, type UTCTimestamp,
} from 'lightweight-charts';
import { getHistory } from '../../api/client';
import {
  type SavedRange,
  VISIBLE_MINUTES, PRELOAD_HOURS, REFRESH_INTERVAL_MS, BAR_SIZE,
  targetKey, loadSavedRange, saveRange,
  toPoints, themeColors, localUtcSeconds,
} from './chartUtils';
import { computeRsi, detectDivergences, RSI_DEFAULTS } from './rsiDivergence';
import type { ChartTarget } from '../../data/store';

export interface SymbolChartHandle {
  resetZoom: () => void;
}

interface Props {
  target: ChartTarget | null;
  /** Visible minutes default. Defaults to chartUtils VISIBLE_MINUTES (90). */
  visibleMinutes?: number;
  /** Show the RSI sub-pane. Default true. */
  showRsi?: boolean;
  /** Render the placeholder "Click a row…" message when target is null. */
  placeholder?: string | null;
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
  const userRangeRef = useRef<SavedRange | null>(null);

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
    const chart = createChart(el, {
      width: el.clientWidth,
      height: el.clientHeight,
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
      },
      rightPriceScale: { borderColor: colors.grid },
      crosshair: { mode: 1 },
    });
    const series = chart.addSeries(LineSeries, {
      color: colors.line,
      lineWidth: 2,
      priceLineVisible: false,
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
      const tgt = targetRef.current;
      if (tgt) saveRange(targetKey(tgt), r);
    });

    const ro = new ResizeObserver(() => {
      if (!containerRef.current || !chartRef.current) return;
      chartRef.current.applyOptions({
        width: containerRef.current.clientWidth,
        height: containerRef.current.clientHeight,
      });
    });
    ro.observe(el);

    return () => {
      ro.disconnect();
      chart.remove();
      chartRef.current = null;
      seriesRef.current = null;
      rsiSeriesRef.current = null;
      divergenceSeriesRef.current = [];
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
      }
      divergenceSeriesRef.current = [];
      onError?.(null);
      userRangeRef.current = null;
      return;
    }

    let cancelled = false;
    let firstLoad = true;
    userRangeRef.current = null;
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
        const points = toPoints(bars);
        const series = seriesRef.current;
        const rsiSeries = rsiSeriesRef.current;
        const chart = chartRef.current;
        if (!series || !chart) return;

        const savedRange = firstLoad ? loadSavedRange(targetKey(target)) : null;
        const prevRange = userRangeRef.current ?? savedRange;
        series.setData(points);

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

        if (firstLoad) {
          firstLoad = false;
          if (prevRange) {
            chart.timeScale().setVisibleRange({
              from: prevRange.from as UTCTimestamp,
              to: prevRange.to as UTCTimestamp,
            });
          } else {
            const total = points.length;
            if (total > 0) {
              // Anchor `to` at "now" (wall-clock), not the last historical
              // bar's timestamp. IB bars often lag by a couple of minutes,
              // and live WS ticks append at the current wall clock — so
              // anchoring on the last bar leaves the live tail off-screen
              // until the user pans right. `from` is "now − visibleMinutes"
              // so the window width is honored regardless of where the
              // bar tail lands.
              const nowSec = localUtcSeconds(new Date());
              chart.timeScale().setVisibleRange({
                from: (nowSec - visibleMinutes * 60) as UTCTimestamp,
                to: nowSec,
              });
            }
          }
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
    const range = saved ?? { from: nowSec - visibleMinutes * 60, to: nowSec };
    chart.timeScale().setVisibleRange({
      from: range.from as UTCTimestamp,
      to: range.to as UTCTimestamp,
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target?.conId, target?.symbol, target?.secType, chartVersion, visibleMinutes]);

  // Live tick subscription — engine publishes only STK quotes.
  useEffect(() => {
    if (!target || target.secType !== 'STK' || !target.symbol) return;
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
          const series = seriesRef.current;
          if (!series) return;
          const lastStr = msg.data?.last;
          if (lastStr == null) return;
          const last = parseFloat(String(lastStr));
          if (!Number.isFinite(last)) return;
          series.update({ time: localUtcSeconds(new Date()), value: last });
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
