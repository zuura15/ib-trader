import { useEffect } from 'react';
import { useStore } from '../../data/store';
import { formatPrice, formatAge, parseUTC, formatInstrument } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';

const statusBadge: Record<string, { cls: string; label: string }> = {
  submitted: { cls: 'badge-blue', label: 'SUBMITTED' },
  pending: { cls: 'badge-gray', label: 'PENDING' },
  partial: { cls: 'badge-yellow', label: 'PARTIAL' },
  filled: { cls: 'badge-green', label: 'FILLED' },
  cancelled: { cls: 'badge-gray', label: 'CANCELLED' },
  canceled: { cls: 'badge-gray', label: 'CANCELLED' },
  rejected: { cls: 'badge-red', label: 'REJECTED' },
  error: { cls: 'badge-red', label: 'ERROR' },
  open: { cls: 'badge-blue', label: 'OPEN' },
  repricing: { cls: 'badge-blue', label: 'REPRICING' },
  amending: { cls: 'badge-blue', label: 'AMENDING' },
  abandoned: { cls: 'badge-gray', label: 'ABANDONED' },
};

const defaultBadge = { cls: 'badge-gray', label: '?' };

export function OrdersPanel({ compact = false }: { compact?: boolean }) {
  const dataMode = useStore((s) => s.dataMode);
  const refreshTick = useStore((s) => s.positionRefreshTick);
  const setOrders = useStore((s) => s.setOrders);

  // Re-fetch orders from API after command completion
  useEffect(() => {
    if (dataMode !== 'live') return;
    fetch('/api/orders')
      .then(r => r.ok ? r.json() : [])
      .then(apiOrders => {
        setOrders(apiOrders.map((o: any) => ({
          id: o.id,
          symbol: o.symbol,
          side: o.side,
          quantity: Number(o.qty_requested),
          filledQty: Number(o.qty_filled),
          orderType: o.order_type,
          status: o.status?.toLowerCase(),
          source: 'system' as const,
          submittedAt: parseUTC(o.placed_at),
          lastUpdate: parseUTC(o.placed_at),
          limitPrice: o.price_placed ? Number(o.price_placed) : undefined,
          avgFillPrice: o.avg_fill_price ? Number(o.avg_fill_price) : undefined,
          sec_type: o.sec_type,
          expiry: o.expiry,
          trading_class: o.trading_class,
          multiplier: o.multiplier,
          display_symbol: o.display_symbol,
          con_id: o.con_id,
        })));
      })
      .catch(() => {});
  }, [dataMode, refreshTick, setOrders]);

  const allOrders = useStore((s) => s.orders);
  const orders = allOrders.filter(o => {
    const s = (o.status || '').toLowerCase();
    return s !== 'filled' && s !== 'cancelled' && s !== 'canceled' && s !== 'abandoned';
  });

  return (
    <PanelShell title="Orders" accent="blue" right={
      <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{orders.length} open</span>
    }>
      <div className="h-full overflow-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th>Symbol</th>
              <th>Side</th>
              <th>Type</th>
              <th>Qty</th>
              {!compact && <th>Fill</th>}
              <th>Price</th>
              <th>Status</th>
              <th>Age</th>
              {!compact && <th>Avg Fill</th>}
            </tr>
          </thead>
          <tbody>
            {orders.length === 0 ? (
              <tr>
                <td colSpan={compact ? 6 : 8} style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 20 }}>
                  No open orders
                </td>
              </tr>
            ) : orders.map((order) => {
              const badge = statusBadge[(order.status || '').toLowerCase()] || defaultBadge;
              const submitted = order.submittedAt instanceof Date
                ? order.submittedAt
                : new Date(order.submittedAt || Date.now());
              return (
                <tr key={order.id}>
                  <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>{formatInstrument(order)}</td>
                  <td>
                    <span style={{ color: order.side === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                      {order.side}
                    </span>
                  </td>
                  <td style={{ color: 'var(--text-secondary)' }}>{order.orderType}</td>
                  <td className="font-mono">{order.quantity}</td>
                  {!compact && (
                    <td className="font-mono">
                      {order.filledQty > 0 ? (
                        <span style={{ color: order.filledQty === order.quantity ? 'var(--accent-green)' : 'var(--accent-yellow)' }}>
                          {order.filledQty}/{order.quantity}
                        </span>
                      ) : (
                        <span style={{ color: 'var(--text-muted)' }}>—</span>
                      )}
                    </td>
                  )}
                  <td className="font-mono">
                    {order.limitPrice ? formatPrice(order.limitPrice) :
                     order.stopPrice ? formatPrice(order.stopPrice) :
                     <span style={{ color: 'var(--text-muted)' }}>MKT</span>}
                  </td>
                  <td>
                    <span className={`badge ${badge.cls}`}>{badge.label}</span>
                  </td>
                  <td style={{ color: 'var(--text-muted)' }}>{formatAge(submitted)}</td>
                  {!compact && (
                    <td className="font-mono">
                      {order.avgFillPrice ? formatPrice(order.avgFillPrice) :
                       <span style={{ color: 'var(--text-muted)' }}>—</span>}
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
