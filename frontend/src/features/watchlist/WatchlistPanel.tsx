import { useState, useEffect, useRef } from 'react';
import { useStore } from '../../data/store';
import { PanelShell } from '../../components/PanelShell';
import { WatchlistConfig } from './WatchlistConfig';

function fmtVol(v: string | null): string {
  if (!v) return '—';
  const n = parseFloat(v);
  if (isNaN(n)) return '—';
  if (n >= 1_000_000_000) return `${(n / 1_000_000_000).toFixed(1)}B`;
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return v;
}

function fmtPrice(v: string | null): string {
  if (!v) return '—';
  const n = parseFloat(v);
  return isNaN(n) ? '—' : n.toFixed(2);
}

function changeColor(v: string | null): string {
  if (!v) return 'var(--text-muted)';
  const n = parseFloat(v);
  if (n > 0) return 'var(--accent-green)';
  if (n < 0) return 'var(--accent-red)';
  return 'var(--text-secondary)';
}

function RefreshCountdown({ interval }: { interval: number }) {
  const [sec, setSec] = useState(interval);
  const ref = useRef(0);

  useEffect(() => {
    ref.current = interval;
    setSec(interval);
    const timer = setInterval(() => {
      ref.current -= 1;
      if (ref.current <= 0) ref.current = interval;
      setSec(ref.current);
    }, 1000);
    return () => clearInterval(timer);
  }, [interval]);

  return (
    <span style={{ fontSize: 10, color: 'var(--text-muted)', fontVariantNumeric: 'tabular-nums' }}>
      ↻ {sec}s
    </span>
  );
}

export function WatchlistPanel({ compact = false }: { compact?: boolean }) {
  const watchlist = useStore((s) => s.watchlist);
  const selectedChartTarget = useStore((s) => s.selectedChartTarget);
  const setSelectedChartTarget = useStore((s) => s.setSelectedChartTarget);
  const [configOpen, setConfigOpen] = useState(false);

  return (
    <PanelShell title="Watchlist" accent="green" right={
      <div className="flex items-center gap-2">
        <RefreshCountdown interval={5} />
        <button
          onClick={() => setConfigOpen(true)}
          style={{
            background: 'none', border: 'none',
            color: 'var(--text-muted)', fontSize: 14,
            cursor: 'pointer', padding: '0 4px',
          }}
          title="Configure watchlist"
        >
          ⚙
        </button>
      </div>
    }>
      <div className="h-full overflow-auto">
        {watchlist.length === 0 ? (
          <div style={{
            padding: 24, textAlign: 'center',
            color: 'var(--text-muted)', fontSize: 12,
          }}>
            No symbols configured — tap ⚙ to add
          </div>
        ) : (
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="text-right">Last</th>
                <th className="text-right">Chg</th>
                <th className="text-right">Chg%</th>
                {!compact && <>
                  <th className="text-right">Vol</th>
                  <th className="text-right">Avg Vol</th>
                  <th className="text-right">High</th>
                  <th className="text-right">Low</th>
                  <th className="text-right">52W H</th>
                  <th className="text-right">52W L</th>
                </>}
              </tr>
            </thead>
            <tbody>
              {watchlist.map((item) => {
                const isSelected =
                  selectedChartTarget != null &&
                  selectedChartTarget.conId == null &&
                  selectedChartTarget.symbol === item.symbol;
                return (
                <tr
                  key={item.symbol}
                  onClick={() => setSelectedChartTarget({
                    symbol: item.symbol, secType: 'STK', conId: null,
                  })}
                  style={{
                    cursor: 'pointer',
                    background: isSelected ? 'var(--row-selected-bg, var(--badge-blue-bg))' : undefined,
                  }}
                >
                  <td className="font-semibold" style={{
                    color: item.error ? 'var(--accent-yellow)' : 'var(--text-primary)',
                  }}>
                    {item.symbol}
                    {item.error && <span style={{ fontSize: 10, marginLeft: 4 }}>⚠</span>}
                  </td>
                  <td className="text-right font-mono" style={{ color: 'var(--text-primary)' }}>
                    {fmtPrice(item.last)}
                  </td>
                  <td className="text-right font-mono" style={{ color: changeColor(item.change) }}>
                    {item.change ? (parseFloat(item.change) > 0 ? '+' : '') + fmtPrice(item.change) : '—'}
                  </td>
                  <td className="text-right font-mono" style={{ color: changeColor(item.change_pct) }}>
                    {item.change_pct ? (parseFloat(item.change_pct) > 0 ? '+' : '') + parseFloat(item.change_pct).toFixed(2) + '%' : '—'}
                  </td>
                  {!compact && <>
                    <td className="text-right font-mono" style={{ color: 'var(--text-secondary)' }}>
                      {fmtVol(item.volume)}
                    </td>
                    <td className="text-right font-mono" style={{ color: 'var(--text-muted)' }}>
                      {fmtVol(item.avg_volume)}
                    </td>
                    <td className="text-right font-mono" style={{ color: 'var(--text-secondary)' }}>
                      {fmtPrice(item.high)}
                    </td>
                    <td className="text-right font-mono" style={{ color: 'var(--text-secondary)' }}>
                      {fmtPrice(item.low)}
                    </td>
                    <td className="text-right font-mono" style={{ color: 'var(--text-muted)' }}>
                      {fmtPrice(item.high_52w)}
                    </td>
                    <td className="text-right font-mono" style={{ color: 'var(--text-muted)' }}>
                      {fmtPrice(item.low_52w)}
                    </td>
                  </>}
                </tr>
                );
              })}
            </tbody>
          </table>
        )}
      </div>
      <WatchlistConfig open={configOpen} onClose={() => setConfigOpen(false)} />
    </PanelShell>
  );
}
