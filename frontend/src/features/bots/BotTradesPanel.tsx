import { useEffect, useState } from 'react';
import { PanelShell } from '../../components/PanelShell';
import { getBotTrades, type BotTradeResponse } from '../../api/client';
import { formatDateTime } from '../../utils/format';
import { useStore } from '../../data/store';

/**
 * Bot Trades panel — one synthesized row per bot entry-to-exit round-trip.
 *
 * Collapsed row:  Symbol | Duration | P&L
 * Expanded row:   Entry price / time, Exit price / time, Trail resets
 *
 * Click a row to toggle its expansion. Data comes from the
 * /api/bot-trades endpoint which is populated by the bot runner's
 * _handle_record_trade_closed hook.
 */
function formatDuration(seconds: number | null): string {
  if (seconds === null || seconds === undefined) return '—';
  if (seconds < 60) return `${seconds}s`;
  if (seconds < 3600) {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}m ${s}s`;
  }
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  return `${h}h ${m}m`;
}

function formatPnl(pnl: string | null): { text: string; color: string } {
  if (pnl === null || pnl === undefined) return { text: '—', color: 'var(--text-muted)' };
  const v = parseFloat(pnl);
  if (isNaN(v)) return { text: '—', color: 'var(--text-muted)' };
  const sign = v >= 0 ? '+' : '-';
  const text = `${sign}$${Math.abs(v).toFixed(2)}`;
  return {
    text,
    color: v >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
  };
}

export function BotTradesPanel({ compact = false }: { compact?: boolean }) {
  const dataMode = useStore((s) => s.dataMode);
  const refreshTick = useStore((s) => s.positionRefreshTick);
  const [trades, setTrades] = useState<BotTradeResponse[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);

  useEffect(() => {
    if (dataMode !== 'live') return;
    let cancelled = false;
    const fetchTrades = () => {
      getBotTrades()
        .then((rows) => { if (!cancelled) setTrades(rows); })
        .catch(() => {});
    };
    fetchTrades();
    // Poll every 10 s to pick up new completed round-trips. The
    // bot_trades table is append-only; a simple refetch is enough.
    const interval = setInterval(fetchTrades, 10_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [dataMode, refreshTick]);

  return (
    <PanelShell title="Bot Trades" accent="purple" right={
      <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>{trades.length} trades</span>
    }>
      <div className="h-full overflow-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th style={{ width: 28 }}></th>
              <th>Symbol</th>
              <th>Dir</th>
              <th>Duration</th>
              <th>P&L</th>
              {!compact && <th>Closed</th>}
            </tr>
          </thead>
          <tbody>
            {trades.length === 0 ? (
              <tr>
                <td colSpan={compact ? 5 : 6} style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 20 }}>
                  No bot trades yet
                </td>
              </tr>
            ) : trades.map((t) => {
              const pnl = formatPnl(t.realized_pnl);
              const dur = formatDuration(t.duration_seconds);
              const isExpanded = expandedId === t.id;
              return [
                <tr
                  key={t.id}
                  onClick={() => setExpandedId(isExpanded ? null : t.id)}
                  style={{ cursor: 'pointer' }}
                  data-testid={`bot-trade-row-${t.id}`}
                  data-symbol={t.symbol}
                  data-pnl={t.realized_pnl ?? ''}
                  onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--row-hover)')}
                  onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
                >
                  <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>{isExpanded ? '▾' : '▸'}</td>
                  <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>{t.symbol}</td>
                  <td style={{ color: t.direction === 'LONG' ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                    {t.direction === 'LONG' ? 'L' : 'S'}
                  </td>
                  <td className="font-mono" style={{ color: 'var(--text-primary)' }}>{dur}</td>
                  <td className="font-mono" style={{ color: pnl.color, fontWeight: 600 }}>{pnl.text}</td>
                  {!compact && (
                    <td style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                      {t.exit_time ? formatDateTime(t.exit_time) : '—'}
                    </td>
                  )}
                </tr>,
                isExpanded ? (
                  <tr
                    key={`${t.id}-detail`}
                    data-testid={`bot-trade-detail-${t.id}`}
                  >
                    <td colSpan={compact ? 5 : 6}
                      style={{ background: 'var(--bg-root)', padding: '8px 12px', borderTop: 'none' }}>
                      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))', gap: 8, fontSize: 12 }}>
                        <div>
                          <span style={{ color: 'var(--text-muted)' }}>Entry:</span>{' '}
                          <span className="font-mono">${parseFloat(t.entry_price).toFixed(2)}</span>{' '}
                          <span style={{ color: 'var(--text-muted)' }}>×</span>{' '}
                          <span className="font-mono">{parseFloat(t.entry_qty)}</span>
                        </div>
                        <div>
                          <span style={{ color: 'var(--text-muted)' }}>Entry time:</span>{' '}
                          <span className="font-mono" style={{ fontSize: 11 }}>
                            {formatDateTime(t.entry_time)}
                          </span>
                        </div>
                        <div>
                          <span style={{ color: 'var(--text-muted)' }}>Exit:</span>{' '}
                          <span className="font-mono">
                            {t.exit_price ? `$${parseFloat(t.exit_price).toFixed(2)}` : '—'}
                          </span>{' '}
                          {t.exit_qty && (
                            <>
                              <span style={{ color: 'var(--text-muted)' }}>×</span>{' '}
                              <span className="font-mono">{parseFloat(t.exit_qty)}</span>
                            </>
                          )}
                        </div>
                        <div>
                          <span style={{ color: 'var(--text-muted)' }}>Exit time:</span>{' '}
                          <span className="font-mono" style={{ fontSize: 11 }}>
                            {t.exit_time ? formatDateTime(t.exit_time) : '—'}
                          </span>
                        </div>
                        <div>
                          <span style={{ color: 'var(--text-muted)' }}>Trail resets:</span>{' '}
                          <span className="font-mono">{t.trail_reset_count}</span>
                        </div>
                        <div>
                          <span style={{ color: 'var(--text-muted)' }}>Commission:</span>{' '}
                          <span className="font-mono">
                            {t.commission ? `$${parseFloat(t.commission).toFixed(2)}` : '$0.00'}
                          </span>
                        </div>
                        {t.bot_name && (
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Bot:</span>{' '}
                            <span className="font-mono">{t.bot_name}</span>
                          </div>
                        )}
                        {(t.entry_serial || t.exit_serial) && (
                          <div>
                            <span style={{ color: 'var(--text-muted)' }}>Serials:</span>{' '}
                            <span className="font-mono">
                              #{t.entry_serial ?? '—'} → #{t.exit_serial ?? '—'}
                            </span>
                          </div>
                        )}
                      </div>
                    </td>
                  </tr>
                ) : null,
              ];
            }).flat()}
          </tbody>
        </table>
      </div>
    </PanelShell>
  );
}
