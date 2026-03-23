import { useState, useEffect, useCallback } from 'react';
import { useStore } from '../../data/store';
import { formatPrice, formatCurrency, pnlClass } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';

interface BrokerPosition {
  id: string;
  account_id: string;
  symbol: string;
  sec_type: string;
  quantity: string;
  avg_cost: string;
  market_price: string | null;
  broker: string;
}

type SortKey = 'symbol' | 'quantity' | 'price' | 'avg_cost' | 'pnl';
type SortDir = 'asc' | 'desc';

function parseNum(v: string | null | undefined): number {
  if (v == null) return 0;
  const n = parseFloat(v);
  return isNaN(n) ? 0 : n;
}

function computePnl(pos: BrokerPosition): number | null {
  if (!pos.market_price) return null;
  return (parseNum(pos.market_price) - parseNum(pos.avg_cost)) * parseNum(pos.quantity);
}

function sortPositions(positions: BrokerPosition[], key: SortKey, dir: SortDir): BrokerPosition[] {
  const sorted = [...positions];
  const mult = dir === 'asc' ? 1 : -1;

  sorted.sort((a, b) => {
    let cmp = 0;
    switch (key) {
      case 'symbol':
        cmp = a.symbol.localeCompare(b.symbol);
        break;
      case 'quantity':
        cmp = parseNum(a.quantity) - parseNum(b.quantity);
        break;
      case 'price':
        cmp = parseNum(a.market_price) - parseNum(b.market_price);
        break;
      case 'avg_cost':
        cmp = parseNum(a.avg_cost) - parseNum(b.avg_cost);
        break;
      case 'pnl':
        cmp = (computePnl(a) ?? -Infinity) - (computePnl(b) ?? -Infinity);
        break;
    }
    return cmp * mult;
  });

  return sorted;
}

export function PositionsPanel({ compact = false }: { compact?: boolean }) {
  const dataMode = useStore((s) => s.dataMode);
  const mockPositions = useStore((s) => s.positions);

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

  // --------------- Live mode ---------------
  const REFRESH_INTERVAL = 2;
  const [positions, setPositions] = useState<BrokerPosition[]>([]);
  const [showStocks, setShowStocks] = useState(true);
  const [showOptions, setShowOptions] = useState(false);
  const [showOther, setShowOther] = useState(false);
  const [sortKey, setSortKey] = useState<SortKey>('symbol');
  const [sortDir, setSortDir] = useState<SortDir>('asc');

  const fetchPositions = useCallback(() => {
    fetch(`/api/positions?_t=${Date.now()}`, { cache: 'no-store' })
      .then(r => r.ok ? r.json() : [])
      .then((data: BrokerPosition[]) => {
        setPositions(data);
      })
      .catch(() => {});
  }, []);

  // Fetch on mount + every REFRESH_INTERVAL seconds, pause when tab is hidden
  useEffect(() => {
    fetchPositions();
    let timer = setInterval(fetchPositions, REFRESH_INTERVAL * 1000);

    const onVisibility = () => {
      clearInterval(timer);
      if (!document.hidden) {
        fetchPositions();
        timer = setInterval(fetchPositions, REFRESH_INTERVAL * 1000);
      }
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisibility);
    };
  }, [fetchPositions]);

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      setSortDir('asc');
    }
  };

  const sortIndicator = (key: SortKey) => {
    if (key !== sortKey) return '';
    return sortDir === 'asc' ? ' ▲' : ' ▼';
  };

  const thStyle = (key: SortKey): React.CSSProperties => ({
    cursor: 'pointer',
    userSelect: 'none',
    color: key === sortKey ? 'var(--text-primary)' : undefined,
  });

  const filtered = positions.filter(p => {
    const st = (p.sec_type || 'STK').toUpperCase();
    if (st === 'STK' || st === 'ETF' || st === '') return showStocks;
    if (st === 'OPT') return showOptions;
    return showOther;
  });

  const sorted = sortPositions(filtered, sortKey, sortDir);

  // Compute total P&L across filtered positions
  let totalPnl = 0;
  for (const pos of filtered) {
    const pnl = computePnl(pos);
    if (pnl !== null) totalPnl += pnl;
  }

  return (
    <PanelShell title="Positions" accent="blue" right={
      <div className="flex items-center gap-2">
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{filtered.length}/{positions.length}</span>
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
              <th style={thStyle('symbol')} onClick={() => handleSort('symbol')}>
                Symbol{sortIndicator('symbol')}
              </th>
              <th className="text-right" style={thStyle('quantity')} onClick={() => handleSort('quantity')}>
                Qty{sortIndicator('quantity')}
              </th>
              <th className="text-right" style={thStyle('price')} onClick={() => handleSort('price')}>
                Price{sortIndicator('price')}
              </th>
              <th className="text-right" style={thStyle('avg_cost')} onClick={() => handleSort('avg_cost')}>
                Avg Cost{sortIndicator('avg_cost')}
              </th>
              {!compact && (
                <th className="text-right" style={thStyle('pnl')} onClick={() => handleSort('pnl')}>
                  P&L{sortIndicator('pnl')}
                </th>
              )}
            </tr>
          </thead>
          <tbody>
            {sorted.length === 0 ? (
              <tr>
                <td colSpan={compact ? 4 : 5} style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 20 }}>
                  No positions
                </td>
              </tr>
            ) : sorted.map((pos) => {
              const qty = parseNum(pos.quantity);
              const avg = parseNum(pos.avg_cost);
              const mkt = pos.market_price ? parseNum(pos.market_price) : null;
              const pnl = computePnl(pos);
              return (
                <tr key={pos.id}>
                  <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>{pos.symbol}</td>
                  <td className="text-right font-mono" style={{ color: qty >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                    {qty > 0 ? '+' : ''}{qty}
                  </td>
                  <td className="text-right font-mono" style={{ color: 'var(--text-primary)' }}>
                    {mkt !== null ? `$${mkt.toFixed(2)}` : <span className="value-na">—</span>}
                  </td>
                  <td className="text-right font-mono" style={{ color: 'var(--text-secondary)' }}>
                    ${avg.toFixed(2)}
                  </td>
                  {!compact && (
                    <td className={`text-right font-mono ${pnl !== null ? pnlClass(pnl) : ''}`}>
                      {pnl !== null ? formatCurrency(pnl) : <span className="value-na">—</span>}
                    </td>
                  )}
                </tr>
              );
            })}
          </tbody>
          {sorted.length > 0 && !compact && (
            <tfoot>
              <tr>
                <td colSpan={4} className="text-right font-semibold text-[10px]"
                  style={{ color: 'var(--text-secondary)', borderTop: '1px solid var(--border-default)' }}>
                  TOTAL
                </td>
                <td className={`text-right font-semibold font-mono ${pnlClass(totalPnl)}`}
                  style={{ borderTop: '1px solid var(--border-default)' }}>
                  {formatCurrency(totalPnl)}
                </td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    </PanelShell>
  );
}
