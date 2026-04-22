import { useState, useEffect } from 'react';
import { useStore } from '../../data/store';
import { formatDateTime } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';
import { getTrades } from '../../api/client';

const statusBadge: Record<string, { cls: string; label: string }> = {
  OPEN: { cls: 'badge-blue', label: 'OPEN' },
  CLOSED: { cls: 'badge-green', label: 'CLOSED' },
  PARTIAL: { cls: 'badge-yellow', label: 'PARTIAL' },
};

const defaultBadge = { cls: 'badge-gray', label: '?' };

export function TradesPanel({ compact = false }: { compact?: boolean }) {
  const dataMode = useStore((s) => s.dataMode);
  const tradeGroups = useStore((s) => s.tradeGroups);
  const setTradeGroups = useStore((s) => s.setTradeGroups);
  const refreshTick = useStore((s) => s.positionRefreshTick);
  const orders = useStore((s) => s.orders);

  // Re-fetch trades from API after command completion
  useEffect(() => {
    if (dataMode !== 'live') return;
    getTrades().then(trades => {
      setTradeGroups(trades.map(t => ({
        id: t.id,
        serialNumber: t.serial_number,
        symbol: t.symbol,
        direction: t.direction,
        status: t.status,
        realizedPnl: t.realized_pnl,
        totalCommission: t.total_commission,
        openedAt: t.opened_at,
        closedAt: t.closed_at,
        entryQty: t.entry_qty,
        entryPrice: t.entry_price,
        exitQty: t.exit_qty,
        exitPrice: t.exit_price,
        orderType: t.order_type,
      })));
    }).catch(() => {});
  }, [dataMode, refreshTick, setTradeGroups]);

  // In mock mode, derive trades from filled orders (legacy behavior)
  if (dataMode === 'mock') {
    const filledOrders = orders.filter(o => o.status === 'filled');
    return (
      <PanelShell title="Trades" accent="green" right={
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{filledOrders.length} executed</span>
      }>
        <div className="h-full overflow-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th>Side</th>
                <th>Qty</th>
                <th>Fill Price</th>
                <th>Time</th>
              </tr>
            </thead>
            <tbody>
              {filledOrders.length === 0 ? (
                <tr>
                  <td colSpan={5} style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 20 }}>
                    No executed trades
                  </td>
                </tr>
              ) : filledOrders.map((t) => (
                <tr key={t.id}>
                  <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>{t.symbol}</td>
                  <td style={{ color: t.side === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)' }}>{t.side}</td>
                  <td className="font-mono">{t.filledQty}</td>
                  <td className="font-mono">{t.avgFillPrice ?? '—'}</td>
                  <td style={{ color: 'var(--text-muted)' }}>
                    {t.lastUpdate instanceof Date ? t.lastUpdate.toLocaleTimeString() : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </PanelShell>
    );
  }

  // Live mode — show trade groups from API
  const [filter, setFilter] = useState<'all' | 'open' | 'closed'>('all');
  const filtered = tradeGroups.filter(t => {
    if (filter === 'open') return t.status === 'OPEN' || t.status === 'PARTIAL';
    if (filter === 'closed') return t.status === 'CLOSED';
    return true;
  });

  return (
    <PanelShell title="Trades" accent="green" right={
      <div className="flex items-center gap-2">
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{filtered.length} trades</span>
        <div className="flex gap-1">
          {(['all', 'open', 'closed'] as const).map(f => (
            <button key={f} onClick={() => setFilter(f)}
              className="text-[10px] px-1.5 py-0.5 rounded cursor-pointer border-none"
              style={{
                background: filter === f ? 'var(--badge-blue-bg)' : 'transparent',
                color: filter === f ? 'var(--accent-blue)' : 'var(--text-muted)',
              }}>
              {f}
            </button>
          ))}
        </div>
      </div>
    }>
      <div className="h-full overflow-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th>#</th>
              <th>Symbol</th>
              <th>Dir</th>
              <th>Status</th>
              {!compact && <th>Type</th>}
              <th>Qty</th>
              <th>Entry</th>
              {!compact && <th>Exit</th>}
              <th>P&L</th>
              <th>Opened</th>
              {!compact && <th>Closed</th>}
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={compact ? 7 : 11} style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 20 }}>
                  No trades
                </td>
              </tr>
            ) : filtered.map((t) => {
              const badge = statusBadge[t.status] || defaultBadge;
              const pnl = t.realizedPnl ? parseFloat(t.realizedPnl) : null;
              const qty = t.entryQty ? parseFloat(t.entryQty) : null;
              const entryPx = t.entryPrice ? parseFloat(t.entryPrice) : null;
              const exitPx = t.exitPrice ? parseFloat(t.exitPrice) : null;
              // % P&L derived from realized $ and cost basis — only
              // meaningful when the close has a price and we know qty.
              const pnlPct = pnl !== null && entryPx && qty && entryPx > 0
                ? (pnl / (entryPx * qty)) * 100
                : null;
              return (
                <tr
                  key={t.id}
                  data-testid={`trade-row-${t.serialNumber}`}
                  data-serial={t.serialNumber}
                  data-symbol={t.symbol}
                  data-status={t.status}
                >
                  <td className="font-mono" style={{ color: 'var(--text-muted)' }} data-testid={`trade-serial-${t.serialNumber}`}>#{t.serialNumber}</td>
                  <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>{t.symbol}</td>
                  <td style={{ color: t.direction === 'LONG' ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                    {t.direction === 'LONG' ? 'L' : 'S'}
                  </td>
                  <td><span className={`badge ${badge.cls}`}>{badge.label}</span></td>
                  {!compact && (
                    <td className="font-mono" style={{ color: 'var(--text-muted)' }}>
                      {t.orderType || '—'}
                    </td>
                  )}
                  <td className="font-mono" style={{ color: 'var(--text-primary)' }}>
                    {qty !== null ? (qty === Math.floor(qty) ? qty.toString() : qty.toFixed(4)) : '—'}
                  </td>
                  <td className="font-mono" style={{ color: 'var(--text-primary)' }}>
                    {entryPx !== null ? `$${entryPx.toFixed(2)}` : '—'}
                  </td>
                  {!compact && (
                    <td className="font-mono" style={{ color: 'var(--text-primary)' }}>
                      {exitPx !== null ? `$${exitPx.toFixed(2)}` : '—'}
                    </td>
                  )}
                  <td className="font-mono" style={{
                    color: pnl === null ? 'var(--text-muted)'
                      : pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                  }}>
                    {pnl !== null ? (
                      <>
                        {(pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2)}
                        {pnlPct !== null && (
                          <span style={{ opacity: 0.75, marginLeft: 4 }}>
                            ({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(2)}%)
                          </span>
                        )}
                      </>
                    ) : '—'}
                  </td>
                  <td style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                    {formatDateTime(t.openedAt)}
                  </td>
                  {!compact && (
                    <td style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                      {formatDateTime(t.closedAt)}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </PanelShell>
  );
}
