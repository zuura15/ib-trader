import React, { useEffect, useState } from 'react';
import { useStore } from '../../data/store';
import { formatAge, formatCurrency, formatDuration, pnlClass } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';
import type { Bot, BotStatus } from '../../types';

interface BotPositionState {
  position_state?: string;
  state?: string;
  entry_price?: string;
  trade_serial?: number;
  qty?: string;
  last_price?: string;
  high_water_mark?: string;
  current_stop?: string;
  hard_stop?: string;
  trail_activation_price?: string;
  trail_width_pct?: string;
  trail_activated?: boolean;
  entry_time?: string;
  symbol?: string;
  [key: string]: unknown;  // allow extra fields from Redis
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

// Track pending actions per bot for optimistic UI feedback
const pendingActions: Record<string, string> = {};
function setPending(botId: string, action: string | null) {
  if (action) pendingActions[botId] = action;
  else delete pendingActions[botId];
  // Force re-render in React — dispatch a synthetic state change on the
  // nearest store. Lightweight: just bumps a counter.
  window.dispatchEvent(new Event('bot-pending-change'));
}
function getPending(botId: string): string | null {
  return pendingActions[botId] || null;
}

function toggleBot(botId: string, currentStatus: BotStatus) {
  const action = currentStatus === 'running' ? 'stop' : 'start';
  setPending(botId, action === 'start' ? 'Starting...' : 'Stopping...');
  fetch(`/api/bots/${botId}/${action}`, { method: 'POST' })
    .then((r) => {
      if (!r.ok) r.json().then((d) => alert(d.detail || `${action} failed`));
    })
    .catch(() => {})
    .finally(() => setTimeout(() => setPending(botId, null), 2000));
}

function forceStop(botId: string) {
  if (!window.confirm('Force-stop this bot? It will be parked in ERRORED and require Start to recover.')) return;
  setPending(botId, 'Force stopping...');
  fetch(`/api/bots/${botId}/force-stop`, { method: 'POST' })
    .then((r) => {
      if (!r.ok) r.json().then((d) => alert(d.detail || 'Force stop failed'));
    })
    .catch(() => {})
    .finally(() => setTimeout(() => setPending(botId, null), 2000));
}

function forceBuy(botId: string) {
  setPending(botId, 'Placing...');
  fetch(`/api/bots/${botId}/force-buy`, { method: 'POST' })
    .then((r) => {
      if (!r.ok) r.json().then((d) => alert(d.detail || 'Force buy failed'));
    })
    .catch(() => {})
    .finally(() => setTimeout(() => setPending(botId, null), 2000));
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
          // Use mid price (bid+ask)/2 — consistent with positions panel
          const bid = parseFloat(msg.data.bid) || 0;
          const ask = parseFloat(msg.data.ask) || 0;
          const mid = bid > 0 && ask > 0 ? (bid + ask) / 2 : (parseFloat(msg.data.last) || 0);
          if (mid > 0) setLivePrice(mid);
        } else if (msg.type === 'bot_state' && msg.symbol === symbol) {
          if (msg.strategy) setState(msg.strategy);
        }
      } catch {}
    };

    return () => ws.close();
  }, [botId, symbol, botRef]);

  const posState = state.position_state || state.state || 'FLAT';
  if (posState === 'FLAT' || posState === 'OFF' || posState === 'AWAITING_ENTRY_TRIGGER') return null;

  const entry = state.entry_price ? parseFloat(state.entry_price) : 0;
  const qty = state.qty ? parseFloat(state.qty) : 0;
  const hwm = state.high_water_mark ? parseFloat(state.high_water_mark) : 0;
  const hardStop = state.hard_stop ? parseFloat(state.hard_stop)
    : (entry > 0 ? entry * (1 - 0.003) : 0);
  const trailActPrice = state.trail_activation_price ? parseFloat(state.trail_activation_price)
    : (entry > 0 ? entry * (1 + 0.00005) : 0);
  const rawStop = state.current_stop ? parseFloat(state.current_stop) : 0;
  const stop = rawStop > 0 ? rawStop : hardStop;
  const botLastPrice = state.last_price ? parseFloat(state.last_price) : 0;
  const price = livePrice > 0 ? livePrice : (botLastPrice > 0 ? botLastPrice : (hwm > 0 ? hwm : entry));
  const pnl = entry > 0 && price > 0 ? (price - entry) * qty : 0;
  const pnlPct = entry > 0 ? ((price - entry) / entry) * 100 * (qty >= 0 ? 1 : -1) : 0;

  // Format duration since entry
  const entryTime = state.entry_time ? new Date(state.entry_time) : null;
  let elapsed = '';
  if (entryTime) {
    const secs = Math.floor((Date.now() - entryTime.getTime()) / 1000);
    if (secs < 60) elapsed = `${secs}s`;
    else if (secs < 3600) elapsed = `${Math.floor(secs / 60)}m ${secs % 60}s`;
    else elapsed = `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`;
  }

  const lbl = { color: 'var(--text-muted)' } as const;
  const val = (color?: string) => ({ color: color || 'var(--text-primary)' }) as const;

  return (
    <div className="mt-1 px-2 py-2 rounded text-[10px] font-mono"
      style={{ background: 'var(--bg-root)', border: '1px solid var(--border-default)', overflowWrap: 'break-word' }}>
      {/* Row 1: Position headline + P&L */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 items-center mb-1">
        <span style={val("var(--text-primary)")}>
          <span className="font-semibold">{symbol}</span>
          {' '}{qty > 0 ? '+' : ''}{qty} @ ${entry.toFixed(2)}
        </span>
        <span style={{ color: pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)', fontWeight: 600 }}>
          {pnl >= 0 ? '+' : ''}${pnl.toFixed(2)} ({pnlPct >= 0 ? '+' : ''}{pnlPct.toFixed(3)}%)
        </span>
        {elapsed && (
          <span style={lbl}>⏱ {elapsed}</span>
        )}
      </div>
      {/* Row 2: Price + Stops — wraps on mobile */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 items-center mb-1">
        {price > 0 && (
          <span><span style={lbl}>Mid:</span> <span style={val()}>${price.toFixed(2)}</span></span>
        )}
        {hardStop > 0 && (
          <span><span style={lbl}>Hard:</span> <span style={val("var(--accent-red)")}>${hardStop.toFixed(2)}</span></span>
        )}
        {stop > 0 && (
          <span><span style={lbl}>Stop:</span> <span style={val(state.trail_activated ? 'var(--accent-yellow)' : 'var(--accent-red)')}>${stop.toFixed(2)}</span></span>
        )}
      </div>
      {/* Row 3: Trail state + projected P&L at stop — wraps on mobile */}
      <div className="flex flex-wrap gap-x-4 gap-y-1 items-center">
        {hwm > 0 && (
          <span><span style={lbl}>HWM:</span> <span style={val()}>${hwm.toFixed(2)}</span></span>
        )}
        <span>
          <span style={lbl}>Trail:</span>{' '}
          <span style={val(state.trail_activated ? 'var(--accent-green)' : 'var(--text-muted)')}>
            {state.trail_activated ? 'ACTIVE' : 'INACTIVE'}
          </span>
        </span>
        {trailActPrice > 0 && !state.trail_activated && (
          <span><span style={lbl}>Arms @</span> <span style={val("var(--accent-yellow)")}>${trailActPrice.toFixed(2)}</span></span>
        )}
        {stop > 0 && entry > 0 && qty !== 0 && (() => {
          const exitPnl = (stop - entry) * qty;
          return (
            <span>
              <span style={lbl}>If stopped:</span>{' '}
              <span style={val(exitPnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)')}>
                {exitPnl >= 0 ? '+' : ''}${exitPnl.toFixed(2)}
              </span>
            </span>
          );
        })()}
      </div>
    </div>
  );
}


export function BotsPanel({ large = false }: { large?: boolean }) {
  const bots = useStore((s) => s.bots);
  // Re-render when pending actions change (optimistic button labels)
  const [, forceUpdate] = useState(0);
  useEffect(() => {
    const handler = () => forceUpdate((n) => n + 1);
    window.addEventListener('bot-pending-change', handler);
    return () => window.removeEventListener('bot-pending-change', handler);
  }, []);

  if (large) {
    return (
      <PanelShell title="Bots" accent="purple" right={
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{bots.length} registered</span>
      }>
        <div className="h-full overflow-auto p-2 grid grid-cols-1 gap-2">
          {bots.map((bot) => {
            const cfg = statusConfig[bot.status];
            return (
              <div
                key={bot.id}
                className="rounded border p-3"
                style={{ background: 'var(--bg-secondary)', borderColor: 'var(--border-default)' }}
                data-testid={`bot-row-${bot.id}`}
                data-bot-id={bot.id}
                data-bot-name={bot.name}
                data-bot-status={bot.status}
              >
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span style={{ color: cfg.var }} className="text-sm">{cfg.dot}</span>
                    <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{bot.name}</span>
                    <span
                      className={`badge ${bot.status === 'running' ? 'badge-green' : bot.status === 'error' ? 'badge-red' : bot.status === 'paused' ? 'badge-yellow' : 'badge-gray'}`}
                      data-testid={`bot-status-${bot.id}`}
                      data-status={bot.status}
                    >
                      {cfg.label}
                    </span>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
                      {bot.strategy}
                    </span>
                    <button
                      onClick={() => !getPending(bot.id) && toggleBot(bot.id, bot.status)}
                      className="text-[10px] px-2 py-0.5 rounded font-semibold"
                      disabled={!!getPending(bot.id)}
                      style={{
                        background: getPending(bot.id) ? 'var(--badge-yellow-bg, rgba(234,179,8,0.1))' : bot.status === 'running' ? 'var(--badge-red-bg)' : 'var(--badge-green-bg)',
                        color: getPending(bot.id) ? 'var(--accent-yellow)' : bot.status === 'running' ? 'var(--accent-red)' : 'var(--accent-green)',
                        border: `1px solid ${getPending(bot.id) ? 'var(--accent-yellow)' : bot.status === 'running' ? 'var(--accent-red)' : 'var(--accent-green)'}`,
                        cursor: getPending(bot.id) ? 'wait' : 'pointer',
                        opacity: getPending(bot.id) ? 0.7 : 1,
                      }}
                      data-testid={`bot-toggle-${bot.id}`}
                    >
                      {getPending(bot.id) || (bot.status === 'running' ? 'STOP' : 'START')}
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
                        data-testid={`bot-force-buy-${bot.id}`}
                      >
                        FORCE BUY
                      </button>
                    )}
                    {bot.status === 'running' && (
                      <button
                        onClick={() => forceStop(bot.id)}
                        className="text-[10px] px-2 py-0.5 rounded font-semibold"
                        style={{
                          background: 'var(--badge-red-bg)',
                          color: 'var(--accent-red)',
                          border: '1px solid var(--accent-red)',
                          cursor: 'pointer',
                        }}
                        data-testid={`bot-force-stop-${bot.id}`}
                        title="Emergency stop — parks the bot in ERRORED"
                      >
                        FORCE STOP
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
                {/* Render PositionLine whenever the bot has a symbol — the
                    line renders FLAT state when there's no position, and
                    keeps showing a live position if the bot is stopped
                    while holding one. Gating on status hid real positions
                    during transitions. */}
                {bot.symbols[0] && (
                  <PositionLine botId={bot.id} symbol={bot.symbols[0]} botRef={bot.refId} />
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
                <tr
                  data-testid={`bot-row-${bot.id}`}
                  data-bot-id={bot.id}
                  data-bot-name={bot.name}
                  data-bot-status={bot.status}
                >
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
                      onClick={() => !getPending(bot.id) && toggleBot(bot.id, bot.status)}
                      className="text-[10px] px-2 py-0.5 rounded font-semibold"
                      disabled={!!getPending(bot.id)}
                      style={{
                        background: getPending(bot.id) ? 'var(--badge-yellow-bg, rgba(234,179,8,0.1))' : bot.status === 'running' ? 'var(--badge-red-bg)' : 'var(--badge-green-bg)',
                        color: getPending(bot.id) ? 'var(--accent-yellow)' : bot.status === 'running' ? 'var(--accent-red)' : 'var(--accent-green)',
                        border: `1px solid ${getPending(bot.id) ? 'var(--accent-yellow)' : bot.status === 'running' ? 'var(--accent-red)' : 'var(--accent-green)'}`,
                        cursor: getPending(bot.id) ? 'wait' : 'pointer',
                        opacity: getPending(bot.id) ? 0.7 : 1,
                      }}
                      data-testid={`bot-toggle-${bot.id}`}
                    >
                      {getPending(bot.id) || (bot.status === 'running' ? 'STOP' : 'START')}
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
                        data-testid={`bot-force-buy-${bot.id}`}
                      >
                        FORCE
                      </button>
                    )}
                    {bot.status === 'running' && (
                      <button
                        onClick={() => forceStop(bot.id)}
                        className="text-[10px] px-2 py-0.5 rounded font-semibold ml-1"
                        style={{
                          background: 'var(--badge-red-bg)',
                          color: 'var(--accent-red)',
                          border: '1px solid var(--accent-red)',
                          cursor: 'pointer',
                        }}
                        data-testid={`bot-force-stop-${bot.id}`}
                        title="Emergency stop"
                      >
                        ABORT
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
