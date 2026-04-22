import React, { useEffect, useState } from 'react';
import { useStore } from '../../data/store';
import { formatAge } from '../../utils/format';
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

function resetBot(botId: string) {
  if (!window.confirm('Reset this bot from ERRORED back to OFF? You will still need to Start it afterward.')) return;
  setPending(botId, 'Resetting...');
  // The runner requires a stop-before-reset when the task is still
  // registered; call stop first, then reset. Both are idempotent.
  fetch(`/api/bots/${botId}/stop`, { method: 'POST' })
    .catch(() => {})
    .then(() => fetch(`/api/bots/${botId}/reset`, { method: 'POST' }))
    .then((r) => {
      if (r && !r.ok) r.json().then((d) => alert(d.detail || 'Reset failed'));
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

function forceSell(botId: string) {
  setPending(botId, 'Selling...');
  fetch(`/api/bots/${botId}/force-sell`, { method: 'POST' })
    .then((r) => {
      if (!r.ok) r.json().then((d) => alert(d.detail || 'Force sell failed'));
    })
    .catch(() => {})
    .finally(() => setTimeout(() => setPending(botId, null), 2000));
}

/**
 * Renders the FORCE SELL button only when the bot currently holds a position.
 * Polls /api/bots/{id}/state and subscribes to bot_state WS updates so the
 * button appears the moment the entry fills and disappears on exit.
 */
function ForceSellButton({ botId, symbol, botRef, compact = false }: {
  botId: string; symbol: string; botRef?: string; compact?: boolean;
}) {
  const [hasPosition, setHasPosition] = useState(false);

  useEffect(() => {
    const applyState = (s: BotPositionState) => {
      const qty = s.qty ? parseFloat(s.qty) : 0;
      const pos = s.position_state || s.state || 'FLAT';
      setHasPosition(
        qty > 0 && pos !== 'FLAT' && pos !== 'OFF' && pos !== 'AWAITING_ENTRY_TRIGGER',
      );
    };

    fetch(`/api/bots/${botId}/state`)
      .then((r) => (r.ok ? r.json() : {}))
      .then(applyState)
      .catch(() => {});

    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;
    const ws = new WebSocket(wsUrl);
    ws.onopen = () => {
      if (botRef) ws.send(JSON.stringify({ type: 'subscribe_bot', bot_ref: botRef, symbol }));
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'bot_state' && msg.symbol === symbol && msg.strategy) {
          applyState(msg.strategy);
        }
      } catch {}
    };
    return () => ws.close();
  }, [botId, symbol, botRef]);

  if (!hasPosition) return null;

  const baseClass = compact
    ? 'text-[13px] px-2 py-0.5 rounded font-semibold ml-1'
    : 'text-[13px] px-2 py-0.5 rounded font-semibold';

  return (
    <button
      onClick={() => forceSell(botId)}
      className={baseClass}
      style={{
        background: 'var(--badge-red-bg)',
        color: 'var(--accent-red)',
        border: '1px solid var(--accent-red)',
        cursor: 'pointer',
      }}
      data-testid={`bot-force-sell-${botId}`}
      title="Close the bot's position immediately via the same exit path the strategy uses"
    >
      {compact ? 'SELL' : 'FORCE SELL'}
    </button>
  );
}

/**
 * Renders the current # of shares held by the bot. Mirrors
 * ForceSellButton's subscription pattern — initial fetch from
 * /api/bots/{id}/state, then a WebSocket subscription so the
 * cell updates on every fill without polling.
 */
function SharesCell({ botId, symbol, botRef }: {
  botId: string; symbol: string; botRef?: string;
}) {
  const [qty, setQty] = useState<number>(0);

  useEffect(() => {
    const apply = (s: BotPositionState) => {
      const n = s.qty ? parseFloat(s.qty) : 0;
      const pos = s.position_state || s.state || 'FLAT';
      setQty(pos === 'FLAT' || pos === 'OFF' ? 0 : n);
    };

    fetch(`/api/bots/${botId}/state`)
      .then((r) => (r.ok ? r.json() : {}))
      .then(apply)
      .catch(() => {});

    const wsUrl = `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;
    const ws = new WebSocket(wsUrl);
    ws.onopen = () => {
      if (botRef) ws.send(JSON.stringify({ type: 'subscribe_bot', bot_ref: botRef, symbol }));
    };
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        if (msg.type === 'bot_state' && msg.symbol === symbol && msg.strategy) {
          apply(msg.strategy);
        }
      } catch {}
    };
    return () => ws.close();
  }, [botId, symbol, botRef]);

  return (
    <span
      className="font-mono"
      data-testid={`bot-shares-${botId}`}
      data-qty={qty}
    >
      {qty > 0 ? qty : '—'}
    </span>
  );
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
  const qty = state.qty ? parseFloat(state.qty) : 0;
  // Don't render a position headline when there isn't a position to show.
  // ENTRY_ORDER_PLACED has `qty=0` for 3-4s while the entry order is in flight;
  // the `qty === 0` check also defends against any future state that could
  // transiently report zero.
  if (
    qty === 0 ||
    posState === 'FLAT' ||
    posState === 'OFF' ||
    posState === 'AWAITING_ENTRY_TRIGGER' ||
    posState === 'ENTRY_ORDER_PLACED'
  ) return null;

  const entry = state.entry_price ? parseFloat(state.entry_price) : 0;
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
    <div className="mt-1 px-2 py-2 rounded text-[13px] font-mono"
      style={{ background: 'var(--bg-root)', border: '1px solid var(--border-default)', overflowWrap: 'break-word' }}
      data-testid={`position-line-${botId}`}
      data-symbol={symbol}
      data-qty={qty}
      data-entry={entry.toFixed(2)}
      data-position-state={posState}>
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
        <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>{bots.length} registered</span>
      }>
        <div className="h-full overflow-auto p-2 grid grid-cols-1 gap-2">
          {bots.map((bot) => {
            const cfg = statusConfig[bot.status];
            const symbol = bot.symbols[0] || '—';
            return (
              <div
                key={bot.id}
                className="rounded border p-2"
                style={{ background: 'var(--bg-secondary)', borderColor: 'var(--border-default)' }}
                data-testid={`bot-row-${bot.id}`}
                data-bot-id={bot.id}
                data-bot-name={bot.name}
                data-bot-status={bot.status}
              >
                <div className="flex items-center gap-3 flex-wrap">
                <span style={{ color: cfg.var }} className="text-sm" title={cfg.label}>{cfg.dot}</span>
                <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{bot.name}</span>
                <span className="font-mono text-[13px]" style={{ color: 'var(--text-secondary)' }}>{symbol}</span>
                {bot.symbols[0] && (
                  <SharesCell botId={bot.id} symbol={bot.symbols[0]} botRef={bot.refId} />
                )}
                <span
                  className="font-mono text-[13px]"
                  style={{ color: bot.status === 'error' ? 'var(--accent-red)' : 'var(--text-muted)' }}
                  title="Heartbeat age"
                >
                  {formatAge(bot.lastHeartbeat)} ago
                </span>
                <div className="flex items-center gap-2 ml-auto">
                  <button
                    onClick={() => !getPending(bot.id) && toggleBot(bot.id, bot.status)}
                    className="text-[13px] px-2 py-0.5 rounded font-semibold"
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
                      className="text-[13px] px-2 py-0.5 rounded font-semibold"
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
                  {bot.status === 'running' && bot.symbols[0] && (
                    <ForceSellButton
                      botId={bot.id}
                      symbol={bot.symbols[0]}
                      botRef={bot.refId}
                    />
                  )}
                  {bot.status === 'running' && (
                    <button
                      onClick={() => forceStop(bot.id)}
                      className="text-[13px] px-2 py-0.5 rounded font-semibold"
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
                  <button
                    onClick={() => resetBot(bot.id)}
                    className="text-[13px] px-2 py-0.5 rounded font-semibold"
                    style={{
                      background: 'var(--badge-blue-bg)',
                      color: 'var(--accent-blue)',
                      border: '1px solid var(--accent-blue)',
                      cursor: 'pointer',
                    }}
                    data-testid={`bot-reset-${bot.id}`}
                    title="Reset bot state to OFF"
                  >
                    RESET
                  </button>
                </div>
                </div>
                {/* Position details — PositionLine self-gates to null when
                    the bot is flat, so this only materializes while a
                    position is open. Top row stays uncluttered in the
                    common case; detail appears exactly when useful. */}
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
      <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>{bots.length} registered</span>
    }>
      <div className="h-full overflow-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th></th>
              <th>Bot</th>
              <th>Symbol</th>
              <th>Shares</th>
              <th>Heartbeat</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {bots.map((bot) => {
              const cfg = statusConfig[bot.status];
              const symbol = bot.symbols[0] || '—';
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
                  <td className="font-mono" style={{ color: 'var(--text-secondary)' }}>{symbol}</td>
                  <td>
                    {bot.symbols[0] ? (
                      <SharesCell botId={bot.id} symbol={bot.symbols[0]} botRef={bot.refId} />
                    ) : (
                      <span className="font-mono">—</span>
                    )}
                  </td>
                  <td className="font-mono" style={{ color: bot.status === 'error' ? 'var(--accent-red)' : 'var(--text-secondary)' }}>
                    {formatAge(bot.lastHeartbeat)}
                  </td>
                  <td>
                    <button
                      onClick={() => !getPending(bot.id) && toggleBot(bot.id, bot.status)}
                      className="text-[13px] px-2 py-0.5 rounded font-semibold"
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
                        className="text-[13px] px-2 py-0.5 rounded font-semibold ml-1"
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
                    {bot.status === 'running' && bot.symbols[0] && (
                      <ForceSellButton
                        botId={bot.id}
                        symbol={bot.symbols[0]}
                        botRef={bot.refId}
                        compact
                      />
                    )}
                    {bot.status === 'running' && (
                      <button
                        onClick={() => forceStop(bot.id)}
                        className="text-[13px] px-2 py-0.5 rounded font-semibold ml-1"
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
                    <button
                      onClick={() => resetBot(bot.id)}
                      className="text-[13px] px-2 py-0.5 rounded font-semibold ml-1"
                      style={{
                        background: 'var(--badge-blue-bg)',
                        color: 'var(--accent-blue)',
                        border: '1px solid var(--accent-blue)',
                        cursor: 'pointer',
                      }}
                      data-testid={`bot-reset-${bot.id}`}
                      title="Reset bot state to OFF"
                    >
                      RESET
                    </button>
                  </td>
                </tr>
                {bot.symbols[0] && (
                  <tr>
                    <td colSpan={6} style={{ padding: 0 }}>
                      <PositionLine botId={bot.id} symbol={bot.symbols[0]} botRef={bot.refId} />
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
