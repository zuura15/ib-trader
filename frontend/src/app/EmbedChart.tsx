/**
 * Chart-only page for the Burrow Android app's native screens (#105).
 *
 * ``/?embed=chart&slot=1&symbol=MGCZ6&secType=FUT&tf=1m&theme=dark``
 * renders one chart-bot chart (price line + RSI, order lines, fills)
 * full-bleed with no app shell. The native app keeps a single WebView
 * on this page and switches symbol / timeframe / theme in place by
 * calling ``window.burrowChart.set({...})`` instead of reloading.
 */
import { useEffect, useState } from 'react';
import { BotChart } from '../features/chart-bot/BotChart';
import { parseEmbedParams, type EmbedChartParams } from './embedParams';
import { tickSizeFor } from '../features/chart/chartUtils';

declare global {
  interface Window {
    burrowChart?: { set: (next: Record<string, unknown>) => void };
    /** Burrow's native bridge (absent in a plain browser). A price pick
     *  only asks the app to show its limit-order sheet; the app confirms
     *  with the operator before anything is sent. */
    BurrowNative?: { limitPick: (price: string) => void };
  }
}

function applyTheme(theme: 'dark' | 'light') {
  // Not persisted: the embed must never change the desktop app's theme.
  document.documentElement.setAttribute('data-theme', `burrow-${theme}`);
}

export function EmbedChart() {
  const [params, setParams] = useState<EmbedChartParams>(
    () => parseEmbedParams(window.location.search),
  );
  // Remount the chart when the theme changes: it reads its colours
  // from CSS variables once, at creation.
  const [themeEpoch, setThemeEpoch] = useState(0);

  useEffect(() => {
    applyTheme(params.theme);
  }, [params.theme]);

  useEffect(() => {
    window.burrowChart = {
      set: (next) => {
        setParams((prev) => {
          const merged = parseEmbedParams({
            slot: prev.slot, symbol: prev.symbol, secType: prev.secType,
            ref: prev.botRef, tf: prev.timeframe, theme: prev.theme,
            ...next,
          });
          if (merged.theme !== prev.theme) {
            applyTheme(merged.theme);
            setThemeEpoch((n) => n + 1);
          }
          return merged;
        });
      },
    };
    return () => { delete window.burrowChart; };
  }, []);

  const botId = params.slot != null ? `chart-bot-${params.slot}` : 'embed';
  return (
    <div className="embed-chart-shell">
      {params.symbol ? (
        <BotChart
          key={`${params.symbol}:${themeEpoch}`}
          botId={botId}
          botRef={params.botRef ?? undefined}
          symbol={params.symbol}
          secType={params.secType}
          showHeader={false}
          renderHeader={() => null}
          timeframe={params.timeframe}
          pickTickSize={tickSizeFor(params.symbol)}
          onPricePick={window.BurrowNative
            ? (price) => window.BurrowNative?.limitPick(String(price))
            : undefined}
        />
      ) : null}
    </div>
  );
}
