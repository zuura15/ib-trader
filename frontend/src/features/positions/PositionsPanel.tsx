import { useEffect, useState } from 'react';
import { useStore } from '../../data/store';
import { formatPrice, formatCurrency, pnlClass } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';

interface BrokerPosition {
  account_id: string;
  symbol: string;
  sec_type: string;
  quantity: string;
  avg_cost: string;
  broker: string;
}

export function PositionsPanel({ compact = false }: { compact?: boolean }) {
  const dataMode = useStore((s) => s.dataMode);
  const mockPositions = useStore((s) => s.positions);
  const refreshTick = useStore((s) => s.positionRefreshTick);
  const [livePositions, setLivePositions] = useState<BrokerPosition[]>([]);

  // In live mode, poll the positions API every 30 seconds + on command completion
  useEffect(() => {
    if (dataMode !== 'live') return;

    const fetchPositions = () => {
      fetch('/api/positions')
        .then(r => r.ok ? r.json() : [])
        .then(setLivePositions)
        .catch(() => {});
    };

    fetchPositions();
    const interval = setInterval(fetchPositions, 30000);
    return () => clearInterval(interval);
  }, [dataMode, refreshTick]);

  // Mock mode — use store positions
  if (dataMode === 'mock') {
    const totalUnrealized = mockPositions.reduce((s, p) => s + p.unrealizedPnl, 0);
    const totalRealized = mockPositions.reduce((s, p) => s + p.realizedPnl, 0);

    return (
      <PanelShell title="Positions" accent="blue" right={
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{mockPositions.length} open</span>
      }>
        <div className="h-full overflow-auto">
          <table className="data-table">
            <thead>
              <tr>
                <th>Symbol</th>
                <th className="text-right">Qty</th>
                <th className="text-right">Avg Cost</th>
                <th className="text-right">Mark</th>
                <th className="text-right">Unrl P&L</th>
                {!compact && <th className="text-right">Rlzd P&L</th>}
              </tr>
            </thead>
            <tbody>
              {mockPositions.map((pos) => (
                <tr key={pos.symbol}>
                  <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>{pos.symbol}</td>
                  <td className="text-right font-mono" style={{ color: pos.quantity >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                    {pos.quantity > 0 ? '+' : ''}{pos.quantity}
                  </td>
                  <td className="text-right font-mono" style={{ color: 'var(--text-secondary)' }}>{formatPrice(pos.avgCost)}</td>
                  <td className="text-right font-mono" style={{ color: 'var(--text-primary)' }}>{formatPrice(pos.markPrice)}</td>
                  <td className={`text-right font-mono ${pnlClass(pos.unrealizedPnl)}`}>{formatCurrency(pos.unrealizedPnl)}</td>
                  {!compact && (
                    <td className={`text-right font-mono ${pnlClass(pos.realizedPnl)}`}>
                      {pos.realizedPnl !== 0 ? formatCurrency(pos.realizedPnl) : <span className="value-na">—</span>}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
            <tfoot>
              <tr>
                <td colSpan={3} className="text-right font-semibold text-[10px]"
                  style={{ color: 'var(--text-secondary)', borderTop: '1px solid var(--border-default)' }}>
                  TOTAL
                </td>
                <td style={{ borderTop: '1px solid var(--border-default)' }} />
                <td className={`text-right font-semibold font-mono ${pnlClass(totalUnrealized)}`}
                  style={{ borderTop: '1px solid var(--border-default)' }}>
                  {formatCurrency(totalUnrealized)}
                </td>
                {!compact && (
                  <td className={`text-right font-semibold font-mono ${pnlClass(totalRealized)}`}
                    style={{ borderTop: '1px solid var(--border-default)' }}>
                    {formatCurrency(totalRealized)}
                  </td>
                )}
              </tr>
            </tfoot>
          </table>
        </div>
      </PanelShell>
    );
  }

  // Live mode — show broker positions from cache
  const [showStocks, setShowStocks] = useState(true);
  const [showOptions, setShowOptions] = useState(false);
  const [showOther, setShowOther] = useState(false);

  const filtered = livePositions.filter(p => {
    const st = (p.sec_type || 'STK').toUpperCase();
    if (st === 'STK' || st === 'ETF' || st === '') return showStocks;
    if (st === 'OPT') return showOptions;
    return showOther;
  });

  return (
    <PanelShell title="Positions" accent="blue" right={
      <div className="flex items-center gap-2">
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{filtered.length}/{livePositions.length}</span>
        <div className="flex gap-1">
          {([
            { key: 'stocks', label: 'STK', active: showStocks, toggle: () => setShowStocks(!showStocks) },
            { key: 'options', label: 'OPT', active: showOptions, toggle: () => setShowOptions(!showOptions) },
            { key: 'other', label: 'Other', active: showOther, toggle: () => setShowOther(!showOther) },
          ] as const).map(f => (
            <button key={f.key} onClick={f.toggle}
              className="text-[10px] px-1.5 py-0.5 rounded cursor-pointer border-none"
              style={{
                background: f.active ? 'var(--badge-blue-bg)' : 'transparent',
                color: f.active ? 'var(--accent-blue)' : 'var(--text-muted)',
              }}>
              {f.label}
            </button>
          ))}
        </div>
      </div>
    }>
      <div className="h-full overflow-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th className="text-right">Qty</th>
              <th className="text-right">Avg Cost</th>
              {!compact && <th>Account</th>}
            </tr>
          </thead>
          <tbody>
            {filtered.length === 0 ? (
              <tr>
                <td colSpan={compact ? 3 : 4} style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 20 }}>
                  No positions (engine updates every 30s)
                </td>
              </tr>
            ) : filtered.map((pos, i) => {
              const qty = parseFloat(pos.quantity);
              return (
                <tr key={`${pos.symbol}-${pos.account_id}-${i}`}>
                  <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>{pos.symbol}</td>
                  <td className="text-right font-mono" style={{ color: qty >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                    {qty > 0 ? '+' : ''}{qty}
                  </td>
                  <td className="text-right font-mono" style={{ color: 'var(--text-secondary)' }}>
                    ${parseFloat(pos.avg_cost).toFixed(2)}
                  </td>
                  {!compact && (
                    <td style={{ color: 'var(--text-muted)', fontSize: 11 }}>{pos.account_id}</td>
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
