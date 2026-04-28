import { useEffect, useState } from 'react';
import { PanelShell } from '../../components/PanelShell';
import { SymbolChart } from './SymbolChart';
import type { ChartTarget } from '../../data/store';

interface BrokerPosition {
  id: string;
  symbol: string;
  sec_type: string;
  quantity: string;
  con_id?: number | null;
}

const MAX_ROWS = 6;

export function StackedChartsPane() {
  const [positions, setPositions] = useState<BrokerPosition[]>([]);
  const [wsLive, setWsLive] = useState(false);

  // Mirror PositionsPanel's WS subscription so the stack tracks the
  // same live position list. Keeping a private subscription here (rather
  // than lifting positions into the store) is the smallest change that
  // preserves PositionsPanel's existing wiring.
  useEffect(() => {
    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const base = import.meta.env.VITE_WS_URL || `${proto}//${window.location.host}/ws`;
    const token = import.meta.env.VITE_API_TOKEN || '';
    const url = token ? `${base}?token=${token}` : base;

    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const open = () => {
      if (closed) return;
      ws = new WebSocket(url);
      ws.onopen = () => {
        setWsLive(true);
        ws?.send(JSON.stringify({ type: 'subscribe_positions' }));
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'positions' && Array.isArray(msg.data)) {
            setPositions(msg.data as BrokerPosition[]);
          }
        } catch { /* ignore malformed */ }
      };
      ws.onclose = () => {
        setWsLive(false);
        if (!closed) retry = setTimeout(open, 2000);
      };
      ws.onerror = () => { /* onclose handles reconnect */ };
    };

    open();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      if (ws) { ws.onclose = null; ws.close(); }
    };
  }, []);

  // Same stocks/futures-only filter the user actually charts. Drop OPT
  // because we'd just chart the underlying stock — better to surface
  // those once via the explicit chart pane than duplicate them here.
  const allCharted = positions.filter((p) => {
    const st = (p.sec_type || 'STK').toUpperCase();
    if (st !== 'STK' && st !== 'ETF' && st !== 'FUT') return false;
    const qty = parseFloat(p.quantity || '0');
    return qty !== 0;
  });
  // Hard cap at MAX_ROWS so the panel can split height evenly without
  // needing to scroll. Anything past the cap is hidden — a count badge
  // surfaces overflow so the user knows there's more.
  const charted = allCharted.slice(0, MAX_ROWS);
  const overflow = allCharted.length - charted.length;

  const right = (
    <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
      {wsLive ? '● live' : '○ reconnecting'} · {charted.length}
      {overflow > 0 ? ` (+${overflow} hidden)` : ''}
    </span>
  );

  return (
    <PanelShell title="Stacked Charts" accent="blue" right={right}>
      {charted.length === 0 ? (
        <div
          className="flex items-center justify-center text-xs h-full"
          style={{ color: 'var(--text-muted)' }}
        >
          No positions to chart.
        </div>
      ) : (
        // Flex column with `flex: 1` rows distributes the panel's height
        // evenly across all visible charts. With the MAX_ROWS cap there's
        // no scroll — every chart gets exactly 1/Nth of the available
        // vertical space.
        <div
          className="h-full flex flex-col"
          style={{ minHeight: 0 }}
        >
          {charted.map((pos) => {
            const secType = ((pos.sec_type || 'STK').toUpperCase() === 'FUT' ? 'FUT' : 'STK') as ChartTarget['secType'];
            const target: ChartTarget = {
              symbol: pos.symbol,
              secType,
              conId: pos.con_id ?? null,
            };
            const qty = parseFloat(pos.quantity || '0');
            return (
              <div
                key={pos.id}
                className="flex flex-col"
                style={{
                  flex: 1,
                  minHeight: 0,
                  borderBottom: '1px solid var(--border-default)',
                }}
              >
                <div
                  style={{
                    display: 'flex', justifyContent: 'space-between',
                    alignItems: 'center',
                    padding: '2px 8px',
                    fontSize: 11,
                    flexShrink: 0,
                  }}
                >
                  <span style={{ fontWeight: 600, color: 'var(--text-primary)' }}>
                    {pos.symbol}{' '}
                    <span style={{ color: 'var(--text-muted)', fontWeight: 400 }}>
                      · {secType}
                    </span>
                  </span>
                  <span
                    className="font-mono"
                    style={{
                      color: qty >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                    }}
                  >
                    {qty > 0 ? '+' : ''}{qty}
                  </span>
                </div>
                <div style={{ flex: 1, minHeight: 0 }}>
                  <SymbolChart
                    target={target}
                    placeholder={null}
                  />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </PanelShell>
  );
}
