import { useEffect, useMemo, useState } from 'react';
import { PanelShell } from '../../components/PanelShell';
import { getBotTrades, type BotTradeResponse } from '../../api/client';
import { formatDateTime } from '../../utils/format';
import { useStore } from '../../data/store';

/**
 * Bot Trades panel — one synthesized row per bot entry-to-exit round-trip.
 *
 * Collapsed row: Symbol | Dir | Duration | P&L | Closed
 * Expanded row (click any row to toggle): Entry/Exit price + time,
 *   Trail resets, Commission, Bot name, Serials.
 *
 * Columns are sortable — click a header to sort by that column. Default
 * is Closed desc (most-recent first). Click the same header again to
 * flip direction.
 *
 * Data from /api/bot-trades, populated by the bot runner's
 * _handle_record_trade_closed hook + the scripts/backfill_bot_trades.py
 * one-shot for pre-existing history.
 */

type SortKey = 'symbol' | 'direction' | 'duration_seconds' | 'realized_pnl' | 'exit_time';
type SortDir = 'asc' | 'desc';

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
  return {
    text: `${sign}$${Math.abs(v).toFixed(2)}`,
    color: v >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
  };
}

function fmtPrice(s: string | null): string {
  if (!s) return '—';
  const v = parseFloat(s);
  return isNaN(v) ? '—' : `$${v.toFixed(2)}`;
}

function fmtQty(s: string | null): string {
  if (!s) return '—';
  const v = parseFloat(s);
  if (isNaN(v)) return '—';
  return Number.isInteger(v) ? String(v) : v.toFixed(4);
}

function fmtTime(s: string | null): string {
  if (!s) return '—';
  return formatDateTime(s);
}

function compareValues(a: BotTradeResponse, b: BotTradeResponse, key: SortKey): number {
  switch (key) {
    case 'symbol':
      return a.symbol.localeCompare(b.symbol);
    case 'direction':
      return a.direction.localeCompare(b.direction);
    case 'duration_seconds':
      return (a.duration_seconds ?? 0) - (b.duration_seconds ?? 0);
    case 'realized_pnl': {
      const av = a.realized_pnl !== null ? parseFloat(a.realized_pnl) : 0;
      const bv = b.realized_pnl !== null ? parseFloat(b.realized_pnl) : 0;
      return av - bv;
    }
    case 'exit_time': {
      const at = a.exit_time ? new Date(a.exit_time).getTime() : 0;
      const bt = b.exit_time ? new Date(b.exit_time).getTime() : 0;
      return at - bt;
    }
  }
}

function SortHeader({
  label, myKey, sortKey, sortDir, onSort,
}: {
  label: string;
  myKey: SortKey;
  sortKey: SortKey;
  sortDir: SortDir;
  onSort: (k: SortKey) => void;
}) {
  const active = sortKey === myKey;
  const arrow = !active ? '⇅' : sortDir === 'asc' ? '▲' : '▼';
  return (
    <th
      onClick={() => onSort(myKey)}
      style={{ cursor: 'pointer', userSelect: 'none' }}
      title={`Sort by ${label}`}
    >
      {label}{' '}
      <span style={{ color: active ? 'var(--accent-blue)' : 'var(--text-muted)', fontSize: 10 }}>
        {arrow}
      </span>
    </th>
  );
}

export function BotTradesPanel({ compact = false }: { compact?: boolean }) {
  const dataMode = useStore((s) => s.dataMode);
  const refreshTick = useStore((s) => s.positionRefreshTick);
  const [trades, setTrades] = useState<BotTradeResponse[]>([]);
  const [expandedId, setExpandedId] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>('exit_time');
  const [sortDir, setSortDir] = useState<SortDir>('desc');

  useEffect(() => {
    if (dataMode !== 'live') return;
    let cancelled = false;
    const fetchTrades = () => {
      getBotTrades()
        .then((rows) => { if (!cancelled) setTrades(rows); })
        .catch(() => {});
    };
    fetchTrades();
    const interval = setInterval(fetchTrades, 10_000);
    return () => { cancelled = true; clearInterval(interval); };
  }, [dataMode, refreshTick]);

  const handleSort = (key: SortKey) => {
    if (key === sortKey) {
      setSortDir(sortDir === 'asc' ? 'desc' : 'asc');
    } else {
      setSortKey(key);
      // Timestamps + P&L default to desc (newest / highest first);
      // text columns default to asc (alphabetical).
      setSortDir(key === 'symbol' || key === 'direction' ? 'asc' : 'desc');
    }
  };

  const sortedTrades = useMemo(() => {
    const copy = [...trades];
    copy.sort((a, b) => {
      const cmp = compareValues(a, b, sortKey);
      return sortDir === 'asc' ? cmp : -cmp;
    });
    return copy;
  }, [trades, sortKey, sortDir]);

  return (
    <PanelShell title="Bot Trades" accent="purple" right={
      <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>{trades.length} trades</span>
    }>
      <div className="h-full overflow-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th style={{ width: 28 }}></th>
              <SortHeader label="Symbol" myKey="symbol" sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
              <SortHeader label="Dir" myKey="direction" sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
              <SortHeader label="Duration" myKey="duration_seconds" sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
              <SortHeader label="P&L" myKey="realized_pnl" sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
              {!compact && (
                <SortHeader label="Closed" myKey="exit_time" sortKey={sortKey} sortDir={sortDir} onSort={handleSort} />
              )}
            </tr>
          </thead>
          <tbody>
            {sortedTrades.length === 0 ? (
              <tr>
                <td colSpan={compact ? 5 : 6}
                  style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 20 }}>
                  No bot trades yet
                </td>
              </tr>
            ) : (
              sortedTrades.flatMap((t) => {
                const pnl = formatPnl(t.realized_pnl);
                const dur = formatDuration(t.duration_seconds);
                const isExpanded = expandedId === t.id;
                const rows: JSX.Element[] = [
                  <tr
                    key={t.id}
                    onClick={() => setExpandedId(isExpanded ? null : t.id)}
                    style={{ cursor: 'pointer' }}
                    data-testid={`bot-trade-row-${t.id}`}
                    data-symbol={t.symbol}
                    data-pnl={t.realized_pnl ?? ''}
                    data-expanded={isExpanded}
                  >
                    <td style={{ color: 'var(--text-muted)', fontSize: 12 }}>
                      {isExpanded ? '▾' : '▸'}
                    </td>
                    <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>
                      {t.symbol}
                    </td>
                    <td style={{ color: t.direction === 'LONG' ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                      {t.direction === 'LONG' ? 'L' : 'S'}
                    </td>
                    <td className="font-mono" style={{ color: 'var(--text-primary)' }}>{dur}</td>
                    <td className="font-mono" style={{ color: pnl.color, fontWeight: 600 }}>
                      {pnl.text}
                    </td>
                    {!compact && (
                      <td style={{ color: 'var(--text-muted)', fontSize: 11 }}>
                        {fmtTime(t.exit_time)}
                      </td>
                    )}
                  </tr>,
                ];
                if (isExpanded) {
                  rows.push(
                    <tr key={`${t.id}-detail`} data-testid={`bot-trade-detail-${t.id}`}>
                      <td
                        colSpan={compact ? 5 : 6}
                        style={{ background: 'var(--bg-root)', padding: 0, borderTop: 'none' }}
                      >
                        <div style={{
                          padding: '10px 16px',
                          display: 'grid',
                          gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))',
                          gap: '8px 20px',
                          fontSize: 12,
                        }}>
                          <div>
                            <div style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Entry</div>
                            <div className="font-mono">
                              {fmtPrice(t.entry_price)} × {fmtQty(t.entry_qty)}
                            </div>
                            <div className="font-mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                              {fmtTime(t.entry_time)}
                            </div>
                          </div>
                          <div>
                            <div style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Exit</div>
                            <div className="font-mono">
                              {fmtPrice(t.exit_price)} × {fmtQty(t.exit_qty)}
                            </div>
                            <div className="font-mono" style={{ fontSize: 11, color: 'var(--text-muted)' }}>
                              {fmtTime(t.exit_time)}
                            </div>
                          </div>
                          <div>
                            <div style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Trail resets</div>
                            <div className="font-mono">{t.trail_reset_count}</div>
                          </div>
                          <div>
                            <div style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Commission</div>
                            <div className="font-mono">
                              {t.commission ? `$${parseFloat(t.commission).toFixed(2)}` : '$0.00'}
                            </div>
                          </div>
                          {t.bot_name && (
                            <div>
                              <div style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Bot</div>
                              <div className="font-mono">{t.bot_name}</div>
                            </div>
                          )}
                          {(t.entry_serial || t.exit_serial) && (
                            <div>
                              <div style={{ color: 'var(--text-muted)', fontSize: 10, textTransform: 'uppercase', letterSpacing: '0.05em' }}>Serials</div>
                              <div className="font-mono">
                                #{t.entry_serial ?? '—'} → #{t.exit_serial ?? '—'}
                              </div>
                            </div>
                          )}
                        </div>
                      </td>
                    </tr>
                  );
                }
                return rows;
              })
            )}
          </tbody>
        </table>
      </div>
    </PanelShell>
  );
}
