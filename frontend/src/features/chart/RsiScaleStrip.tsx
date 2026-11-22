/** Multi-timeframe RSI readout: one 0–100 scale with a marker per timeframe. */
import { useEffect, useRef, useState } from 'react';
import { getHistory } from '../../api/client';
import { TIMEFRAMES, type ChartTimeframe } from './chartUtils';
import { computeRsi, RSI_DEFAULTS } from './rsiDivergence';

/** Timeframes always shown on the scale. The chart's own timeframe is
 *  added when it isn't one of these (1m), and its marker comes from the
 *  chart itself so it matches the RSI pane exactly. */
const SCALE_TIMEFRAMES: ChartTimeframe[] = ['3m', '1h', '4h', '1D'];

/** How often each background timeframe re-fetches its bars. Between
 *  fetches the live price is folded into the last bar, so the marker
 *  still moves with the tape. */
const REFRESH_MS: Record<ChartTimeframe, number> = {
  '1m': 60_000,
  '3m': 60_000,
  '1h': 30 * 60_000,
  '4h': 2 * 3600_000,
  '1D': 12 * 3600_000,
};

const OVERSOLD = 30;
const OVERBOUGHT = 70;
const PAD = 12;
const HEIGHT = 34;

type Target = { conId?: number | null; symbol: string; secType?: string };

interface Props {
  target: Target;
  /** Timeframe the chart is displaying. */
  displayed: ChartTimeframe;
  /** Latest RSI of the displayed bars (SymbolChart ``onRsi``). */
  displayedRsi: number | null;
  /** Live last price, folded into each background timeframe's last bar. */
  lastPrice: number | null;
}

/** Closes per background timeframe, fetched on that timeframe's cadence. */
function useTimeframeCloses(target: Target, timeframes: ChartTimeframe[]) {
  const { conId, symbol, secType } = target;
  const tfList = timeframes.join(',');
  const key = `${conId ?? ''}|${symbol}|${secType ?? ''}|${tfList}`;
  // Tagged with the key they were fetched for, so a symbol switch never
  // shows the previous symbol's readings.
  const [state, setState] = useState<{ key: string; closes: Partial<Record<ChartTimeframe, number[]>> }>(
    { key, closes: {} },
  );

  useEffect(() => {
    let cancelled = false;
    const timers: ReturnType<typeof setTimeout>[] = [];
    const load = async (tf: ChartTimeframe) => {
      const spec = TIMEFRAMES[tf];
      try {
        const bars = await getHistory({
          conId: conId ?? undefined,
          symbol: conId == null ? symbol : undefined,
          secType: conId == null ? secType : undefined,
          hours: spec.preloadHours,
          barSize: spec.barSize,
        });
        if (cancelled) return;
        const closes = bars.map((b) => b.close);
        setState((prev) => ({
          key,
          closes: { ...(prev.key === key ? prev.closes : {}), [tf]: closes },
        }));
      } catch (err) {
        // Keep the last good closes; the next cadence tick retries.
        console.warn('RSI_SCALE_HISTORY_FAILED', tf, err);
      }
      if (!cancelled) timers.push(setTimeout(() => { void load(tf); }, REFRESH_MS[tf]));
    };
    // Staggered first fetch: every chart pane mounts at once, and IB
    // paces historical requests (same contract within 2 s, ~60 / 10 min).
    // The engine's 5-min history cache absorbs the steady-state refreshes.
    (tfList ? tfList.split(',') as ChartTimeframe[] : []).forEach((tf, i) => {
      timers.push(setTimeout(() => { void load(tf); }, 2500 * (i + 1) + Math.random() * 4000));
    });
    return () => { cancelled = true; timers.forEach(clearTimeout); };
  }, [key, conId, symbol, secType, tfList]);

  return state.key === key ? state.closes : {};
}

function latestRsi(closes: number[] | undefined, lastPrice: number | null): number | null {
  if (!closes || closes.length < RSI_DEFAULTS.period + 1) return null;
  const series = lastPrice != null ? [...closes.slice(0, -1), lastPrice] : closes;
  const vals = computeRsi(series, RSI_DEFAULTS.period);
  return vals[vals.length - 1] ?? null;
}

export function RsiScaleStrip({ target, displayed, displayedRsi, lastPrice }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(0);
  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const background = SCALE_TIMEFRAMES.filter((tf) => tf !== displayed);
  const closes = useTimeframeCloses(target, background);

  const shown: ChartTimeframe[] = SCALE_TIMEFRAMES.includes(displayed)
    ? SCALE_TIMEFRAMES : [displayed, ...SCALE_TIMEFRAMES];
  const markers = shown
    .map((tf) => ({
      tf,
      value: tf === displayed ? displayedRsi : latestRsi(closes[tf], lastPrice),
      current: tf === displayed,
    }))
    .filter((m): m is { tf: ChartTimeframe; value: number; current: boolean } => m.value != null)
    // Labels alternate above/below in value order so neighbours don't collide.
    .sort((a, b) => a.value - b.value);

  const span = Math.max(width - PAD * 2, 0);
  const x = (v: number) => PAD + (Math.min(Math.max(v, 0), 100) / 100) * span;
  const tone = (v: number) => (v >= OVERBOUGHT ? 'var(--accent-red)'
    : v <= OVERSOLD ? 'var(--accent-green)' : 'var(--accent-purple)');

  return (
    <div ref={wrapRef} style={{ height: HEIGHT, flexShrink: 0 }} data-testid="rsi-scale-strip">
      {width > 0 && (
        <svg width={width} height={HEIGHT} style={{ display: 'block' }} aria-label="RSI by timeframe">
          <rect x={PAD} y={18} width={x(OVERSOLD) - PAD} height={4} rx={2}
            fill="var(--accent-green)" opacity={0.3} />
          <rect x={x(OVERSOLD)} y={18} width={x(OVERBOUGHT) - x(OVERSOLD)} height={4}
            fill="var(--text-muted)" opacity={0.35} />
          <rect x={x(OVERBOUGHT)} y={18} width={PAD + span - x(OVERBOUGHT)} height={4} rx={2}
            fill="var(--accent-red)" opacity={0.3} />
          {markers.map((m, i) => {
            const cx = x(m.value);
            const above = i % 2 === 1;
            const anchor = cx < PAD + 20 ? 'start' : cx > PAD + span - 20 ? 'end' : 'middle';
            const extreme = m.value >= OVERBOUGHT || m.value <= OVERSOLD;
            return (
              <g key={m.tf}>
                <circle cx={cx} cy={20} r={5} fill={tone(m.value)}
                  stroke={m.current ? 'var(--text-primary)' : 'none'} strokeWidth={1.5} />
                <text x={cx} y={above ? 10 : 32} fontSize={10} textAnchor={anchor}
                  fill="var(--text-muted)" fontFamily="ui-monospace, monospace">
                  {m.tf}{' '}
                  <tspan fontWeight={600} fill={extreme ? tone(m.value) : 'var(--text-primary)'}>
                    {Math.round(m.value)}
                  </tspan>
                </text>
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
}
