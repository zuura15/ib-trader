import { useEffect, useState } from 'react';
import { getChartPnlRollup } from '../../api/client';
import { formatCurrency } from '../../utils/format';

const REFRESH_MS = 30_000;

/**
 * Top-bar 24h realized-P&L card for ALL futures trading, IB-authoritative.
 *
 * Sums ``pnl_24h`` across every ``FUT`` contract in the engine's
 * ``/api/chart/pnl-rollup`` sweep (periodic ``reqExecutions``), so it
 * reflects trades placed directly in TWS — not just orders this app sent,
 * and not just bot closes. Options/stock rows are intentionally excluded;
 * this card is futures-only. Label + value render on a single line.
 */
export function FuturesPnl24hCard() {
  const [pnl, setPnl] = useState<number | null>(null);

  useEffect(() => {
    let cancelled = false;
    const fetchNow = async () => {
      try {
        const rollup = await getChartPnlRollup();
        if (cancelled) return;
        let sum = 0;
        for (const entry of Object.values(rollup)) {
          if (entry.sec_type === 'FUT' && Number.isFinite(entry.pnl_24h)) {
            sum += entry.pnl_24h;
          }
        }
        setPnl(sum);
      } catch {
        // Leave the last good value visible; muted until the next success.
      }
    };
    fetchNow();
    const t = setInterval(fetchNow, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, []);

  const loaded = pnl !== null;
  const color = !loaded
    ? 'var(--text-muted)'
    : pnl >= 0
      ? 'var(--accent-green)'
      : 'var(--accent-red)';

  return (
    <div
      className="rounded border px-3 py-1.5"
      style={{ borderColor: 'var(--border-default)', background: 'var(--bg-primary)' }}
      title="Realized P&L across ALL futures trades in the last 24h, summed from IB executions — includes trades placed directly in TWS, not just this app. Options and stock trades are excluded."
    >
      <span style={{ display: 'inline-flex', alignItems: 'baseline', gap: 6, whiteSpace: 'nowrap' }}>
        <span style={{ fontSize: 10, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
          24h FUT P&L
        </span>
        <span className="font-mono" style={{ fontSize: 13, fontWeight: 600, color }}>
          {loaded ? formatCurrency(pnl) : '—'}
        </span>
      </span>
    </div>
  );
}
