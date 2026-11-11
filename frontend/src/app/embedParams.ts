/** URL / bridge parameters for the chart-only embed page (#105). */
import { isChartTimeframe, type ChartTimeframe } from '../features/chart/chartUtils';
import type { ChartTarget } from '../data/store';

export interface EmbedChartParams {
  slot: number | null;
  symbol: string | null;
  secType: ChartTarget['secType'];
  botRef: string | null;
  timeframe: ChartTimeframe;
  theme: 'dark' | 'light';
}

const SEC_TYPES: ReadonlyArray<ChartTarget['secType']> = ['STK', 'FUT', 'OPT'];

/** True when the URL asks for the chart-only page. */
export function isEmbedChartUrl(search: string): boolean {
  return new URLSearchParams(search).get('embed') === 'chart';
}

/** Parse and sanitise embed parameters; unknown values fall back to defaults. */
export function parseEmbedParams(input: string | Record<string, unknown>): EmbedChartParams {
  const get = (k: string): string | null => {
    if (typeof input === 'string') return new URLSearchParams(input).get(k);
    const v = input[k];
    return v == null ? null : String(v);
  };
  const slotRaw = Number(get('slot'));
  const symbol = (get('symbol') ?? '').trim().toUpperCase();
  const secType = (get('secType') ?? 'FUT').toUpperCase() as ChartTarget['secType'];
  const tf = get('tf');
  return {
    slot: Number.isInteger(slotRaw) && slotRaw > 0 ? slotRaw : null,
    symbol: /^[A-Z0-9 .]{1,24}$/.test(symbol) ? symbol : null,
    secType: SEC_TYPES.includes(secType) ? secType : 'FUT',
    botRef: get('ref'),
    timeframe: isChartTimeframe(tf) ? tf : '1m',
    theme: get('theme') === 'light' ? 'light' : 'dark',
  };
}
