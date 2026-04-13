import React, { useEffect, useState } from 'react';
import { useStore } from '../../data/store';
import { formatAge, formatCurrency, formatDuration, pnlClass } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';
import type { Bot, BotStatus } from '../../types';

interface BotPositionState {
  position_state?: string;
  entry_price?: string;
  trade_serial?: number;
  qty?: string;
  last_price?: string;
  high_water_mark?: string;
  current_stop?: string;
  trail_activated?: boolean;
}

const statusConfig: Record<BotStatus, { var: string; label: string; dot: string }> = {
  running: { var: 'var(--accent-green)', label: 'RUNNING', dot: '●' },
  stopped: { var: 'var(--text-muted)', label: 'STOPPED', dot: '○' },
  error: { var: 'var(--accent-red)', label: 'ERROR', dot: '✗' },
  paused: { var: 'var(--accent-yellow)', label: 'PAUSED', dot: '◑' },
};

function mapApiBot(b: any): Bot {
  return {
    id: b.id,
    name: b.name,
    strategy: b.strategy,
    status: b.status.toLowerCase() as BotStatus,
    lastHeartbeat: b.last_heartbeat ? new Date(b.last_heartbeat) : new Date(),
    lastSignal: b.last_signal || undefined,
    lastAction: b.last_action || undefined,
    lastActionTime: b.last_action_at ? new Date(b.last_action_at) : undefined,
    errorMessage: b.error_message || undefined,
    tradesTotal: b.trades_total || 0,
    tradesToday: b.trades_today || 0,
    pnlToday: parseFloat(b.pnl_today) || 0,
    symbols: b.symbols_json ? JSON.parse(b.symbols_json) : [],
    refId: b.ref_id,
    uptime: 0,
  };
}

function toggleBot(botId: string, currentStatus: BotStatus, setBots: (bots: Bot[]) => void) {
  const action = currentStatus === 'running' ? 'stop' : 'start';
  fetch(`/api/bots/${botId}/${action}`, { method: 'POST' })
    .then((r) => r.json())
    .then(() => {
      fetch('/api/bots')
        .then((r) => r.json())
        .then((data: any[]) => setBots(data.map(mapApiBot)))
        .catch(() => {});
    })
    .catch(() => {});
}

function forceBuy(botId: string) {
  fetch(`/api/bots/${botId}/force-buy`, { method: 'POST' })
    .then((r) => {
      if (!r.ok) r.json().then((d) => alert(d.detail || 'Force buy failed'));
    })
    .catch(() => {});
}

function PositionLine({ botId, symbol, botRef }: { botId: string; symbol: string; botRef?: string }) {
  const [state, setState] = useState<BotPositionState>({});
  const [livePrice, setLivePrice] = useState<number>(0);

  useEffect(() => {
    // Initial fetch for snapshot
    fetch(`/api/bots/${botId}/state`)
      .then((r) => r.ok ? r.json() : {})
      .then(setState)
      .catch(() => {});

    // WebSocket for live updates — quote pushes on every IB tick,
    // bot_state pushes on every fill. No polling.
    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;
    const ws = new WebSocket(wsUrl);

    ws.onopen = () => {
      ws.send(JSON.stringify({ type: 'subscribe_quote', symbol }));
      if (botRef) {
        ws.send(JSON.stringify({ type: 'subscribe_bot', bot_ref: botRef, symbol }));
      }
    };

    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'quote' && msg.symbol === symbol) {
          const last = parseFloat(msg.data.last) || parseFloat(msg.data.bid) || 0;
          if (last > 0) setLivePrice(last);
        } else if (msg.type === 'bot_state' && msg.symbol === symbol) {
          if (msg.strategy) setState(msg.strategy);
        }
      } catch {}
    };

    return () => ws.close();
  }, [botId, symbol, botRef]);

  if (!state.position_state || state.position_state === 'FLAT') return null;

  const entry = state.entry_price ? parseFloat(state.entry_price) : 0;
  const qty = state.qty ? parseFloat(state.qty) : 0;
  const hwm = state.high_water_mark ? parseFloat(state.high_water_mark) : 0;
  const stop = state.current_stop ? parseFloat(state.current_stop) : 0;
  const botLastPrice = state.last_price ? parseFloat(state.last_price) : 0;
  // Prefer live quote (always fresh); fall back to bot's tracked price.
  const price = livePrice > 0 ? livePrice : (botLastPrice > 0 ? botLastPrice : (hwm > 0 ? hwm : entry));
  // P&L: long position = (price - entry) * qty; short position = (entry - price) * |qty|
  const pnl = entry > 0 && price > 0 ? (price - entry) * qty : 0;
  const pnlPct = entry > 0 ? ((price - entry) / entry) * 100 * (qty >= 0 ? 1 : -1) : 0;

  return (
    <div className="ml-4 mt-1 px-2 py-1 rounded text-[11px] font-mono"
      style={{ background: 'var(--bg-root)', border: '1px solid var(--border-default)' }}>
      <div className="flex gap-4">
        <span style={{ color: 'var(--text-muted)' }}>
          {state.position_state === 'ENTERING' ? '⏳' : state.position_state === 'EXITING' ? '⏹' : '📈'}
          {' '}{state.position_state}
        </span>
        <span>
          <span style={{ color: 'var(--text-muted)' }}>qty:</span>{' '}
          <span style={{ color: 'var(--text-primary)' }}>{qty}</span>
        </span>
        <span>
          <span style={{ color: 'var(--text-muted)' }}>entry:</span>{' '}
          <span style={{ color: 'var(--text-primary)' }}>${entry.toFixed(2)}</span>
        </span>
        {price > 0 && price !== entry && (
          <span>
            <span style={{ color: 'var(--text-muted)' }}>now:</span>{' '}
            <span style={{ color: 'var(--text-primary)' }}>${price.toFixed(2)}</span>
          </span>
        )}
        <span style={{ color: pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 600 }}>
          {pnl >= 0 ? '+' : ''}{pnl.toFixed(2)} ({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(3)}%)
        </span>
        {state.trail_activated && (
          <span style={{ color: 'var(--accent-yellow)' }}>
            trail: ${stop.toFixed(2)}
          </span>
        )}
      </div>
    </div>
  );
}

export function BotsPanel({ large = false }: { large?: boolean }) {
  const bots = useStore((s) => s.bots);
  const setBots = useStore((s) => s.setBots);

  useEffect(() => {
    const fetchBots = () => {
      fetch('/api/bots')
        .then((r) => r.json())
        .then((data: any[]) => setBots(data.map(mapApiBot)))
        .catch(() => {});
    };
    fetchBots();
    const interval = setInterval(fetchBots, 5000);
    return () => clearInterval(interval);
  }, [setBots]);

  if (large) {
    return (
      <PanelShell title="Bots" accent="purple" right={
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{bots.length} registered</span>
      }>
        <div className="h-full overflow-auto p-2 grid grid-cols-1 gap-2">
          {bots.map((bot) => {
            const cfg = statusConfig[bot.status];
            return (
              <div key={bot.id} className="rounded border p-3"
                style={{ background: 'var(--bg-secondary)', borderColor: 'var(--border-default)' }}>
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span style={{ color: cfg.var }} className="text-sm">{cfg.dot}</span>
                    <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{bot.name}</span>
                    <span className={`badge ${bot.status === 'running' ? 'badge-green' : bot.status === 'error' ? 'badge-red' : bot.status === 'paused' ? 'badge-yellow' : 'badge-gray'}`}>
                      {cfg.label}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
                      {bot.strategy}
                    </span>
                    <button
                      onClick={() => toggleBot(bot.id, bot.status, setBots)}
                      className="text-[10px] px-2 py-0.5 rounded font-semibold"
                      style={{
                        background: bot.status === 'running' ? 'var(--badge-red-bg)' : 'var(--badge-green-bg)',
                        color: bot.status === 'running' ? 'var(--accent-red)' : 'var(--accent-green)',
                        border: `1px solid ${bot.status === 'running' ? 'var(--accent-red)' : 'var(--accent-green)'}`,
                        cursor: 'pointer',
                      }}
                    >
                      {bot.status === 'running' ? 'STOP' : 'START'}
                    </button>
                    {bot.status === 'running' && (
                      <button
                        onClick={() => forceBuy(bot.id)}
                        className="text-[10px] px-2 py-0.5 rounded font-semibold"
                        style={{
                          background: 'var(--badge-yellow-bg, rgba(234,179,8,0.1))',
                          color: 'var(--accent-yellow)',
                          border: '1px solid var(--accent-yellow)',
                          cursor: 'pointer',
                        }}
                      >
                        FORCE BUY
                      </button>
                    )}
                  </div>
                </div>

                <div className="grid grid-cols-4 gap-3 text-xs mb-2">
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>Heartbeat</div>
                    <div className="font-mono" style={{ color: bot.status === 'error' ? 'var(--accent-red)' : 'var(--text-primary)' }}>
                      {formatAge(bot.lastHeartbeat)} ago
                    </div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>Trades Today</div>
                    <div className="font-mono" style={{ color: 'var(--text-primary)' }}>{bot.tradesToday}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>P&L Today</div>
                    <div className={`font-mono ${pnlClass(bot.pnlToday)}`}>{formatCurrency(bot.pnlToday)}</div>
                  </div>
                  <div>
                    <div className="text-[10px] uppercase tracking-wider" style={{ color: 'var(--text-muted)' }}>Uptime</div>
                    <div className="font-mono" style={{ color: 'var(--text-primary)' }}>{bot.uptime > 0 ? formatDuration(bot.uptime) : '—'}</div>
                  </div>
                </div>

                <div className="text-xs">
                  <div className="flex gap-2">
                    <span style={{ color: 'var(--text-muted)' }}>Symbols:</span>
                    <span style={{ color: 'var(--text-secondary)' }}>{bot.symbols.join(', ')}</span>
                  </div>
                  {bot.lastSignal && (
                    <div className="flex gap-2 mt-0.5">
                      <span style={{ color: 'var(--text-muted)' }}>Signal:</span>
                      <span style={{ color: 'var(--text-secondary)' }}>{bot.lastSignal}</span>
                    </div>
                  )}
                  {bot.lastAction && (
                    <div className="flex gap-2 mt-0.5">
                      <span style={{ color: 'var(--text-muted)' }}>Action:</span>
                      <span style={{ color: 'var(--accent-blue)' }}>{bot.lastAction}</span>
                    </div>
                  )}
                  {bot.errorMessage && (
                    <div className="mt-1 p-1.5 rounded text-[11px]"
                      style={{ background: 'var(--badge-red-bg)', color: 'var(--accent-red)' }}>
                      {bot.errorMessage}
                    </div>
                  )}
                </div>
                {bot.status === 'running' && (
                  <PositionLine botId={bot.id} symbol={bot.symbols[0] || ''} botRef={bot.refId} />
                )}
              </div>
            );
          })}
        </div>
      </PanelShell>
    );
  }

  // Compact table view
  return (
    <PanelShell title="Bots" accent="purple" right={
      <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{bots.length} registered</span>
    }>
      <div className="h-full overflow-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th></th>
              <th>Bot</th>
              <th>Strategy</th>
              <th>Heartbeat</th>
              <th>Trades</th>
              <th>P&L</th>
              <th>Last Action</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {bots.map((bot) => {
              const cfg = statusConfig[bot.status];
              return (
                <React.Fragment key={bot.id}>
                <tr>
                  <td style={{ color: cfg.var }} title={cfg.label}>{cfg.dot}</td>
                  <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>{bot.name}</td>
                  <td style={{ color: 'var(--text-secondary)' }}>{bot.strategy}</td>
                  <td className="font-mono" style={{ color: bot.status === 'error' ? 'var(--accent-red)' : 'var(--text-secondary)' }}>
                    {formatAge(bot.lastHeartbeat)}
                  </td>
                  <td className="font-mono">{bot.tradesToday}</td>
                  <td className={`font-mono ${pnlClass(bot.pnlToday)}`}>{formatCurrency(bot.pnlToday)}</td>
                  <td className="max-w-[200px] truncate" style={{ color: 'var(--text-secondary)' }}>
                    {bot.lastAction || '—'}
                  </td>
                  <td>
                    <button
                      onClick={() => toggleBot(bot.id, bot.status, setBots)}
                      className="text-[10px] px-2 py-0.5 rounded font-semibold"
                      style={{
                        background: bot.status === 'running' ? 'var(--badge-red-bg)' : 'var(--badge-green-bg)',
                        color: bot.status === 'running' ? 'var(--accent-red)' : 'var(--accent-green)',
                        border: `1px solid ${bot.status === 'running' ? 'var(--accent-red)' : 'var(--accent-green)'}`,
                        cursor: 'pointer',
                      }}
                    >
                      {bot.status === 'running' ? 'STOP' : 'START'}
                    </button>
                    {bot.status === 'running' && (
                      <button
                        onClick={() => forceBuy(bot.id)}
                        className="text-[10px] px-2 py-0.5 rounded font-semibold ml-1"
                        style={{
                          background: 'var(--badge-yellow-bg, rgba(234,179,8,0.1))',
                          color: 'var(--accent-yellow)',
                          border: '1px solid var(--accent-yellow)',
                          cursor: 'pointer',
                        }}
                      >
                        FORCE
                      </button>
                    )}
                  </td>
                </tr>
                {bot.status === 'running' && (
                  <tr>
                    <td colSpan={8} style={{ padding: 0 }}>
                      <PositionLine botId={bot.id} symbol={bot.symbols[0] || ''} botRef={bot.refId} />
                    </td>
                  </tr>
                )}
                </React.Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
    </PanelShell>
  );
}
