import { useState } from 'react';
import { useStore } from '../../data/store';
import { FuturesPnl24hCard } from './FuturesPnl24hCard';
import { ResyncButton } from './ResyncButton';

type SheetId = 'trades' | 'bot-trades' | 'alerts' | 'logs';

interface Props {
  onOpenSheet: (s: SheetId) => void;
  onOpenSettings: () => void;
  onOpenRules: () => void;
  /** Tab-specific header action (Graphs: expand / collapse all). */
  action?: { label: string; icon: string; onClick: () => void } | null;
}

/**
 * Condensed header for the mobile layout.
 *
 * Shows the minimum critical information a trader needs at a glance:
 * connection status, account mode (paper vs live), and data freshness.
 */
export function MobileHeader({ onOpenSheet, onOpenSettings, onOpenRules, action }: Props) {
  const { global, dataMode, wsConnected, alerts } = useStore();
  const { connectionStatus, accountMode } = global;
  const [menuOpen, setMenuOpen] = useState(false);
  const activeAlerts = alerts.filter((a) => !a.dismissed).length;

  const dataFresh = dataMode === 'mock' ? true : wsConnected;

  const connColor =
    connectionStatus === 'connected'
      ? 'var(--accent-green)'
      : connectionStatus === 'reconnecting'
        ? 'var(--accent-yellow)'
        : 'var(--accent-red)';

  return (
    <div
      className="flex items-center justify-between px-2 py-0.5 shrink-0 border-b"
      style={{ background: 'var(--bg-root)', borderColor: 'var(--border-default)' }}
    >
      {/* Left: connection + account */}
      <div className="flex items-center gap-3">
        {/* Connection status */}
        <div className="flex items-center gap-1.5">
          <span
            style={{
              width: 10,
              height: 10,
              borderRadius: '50%',
              background: connColor,
              display: 'inline-block',
            }}
          />
        </div>

        {/* Account mode */}
        <span
          style={{
            fontSize: 14,
            fontWeight: 600,
            letterSpacing: '0.1em',
            textTransform: 'uppercase',
            color:
              accountMode === 'live'
                ? 'var(--accent-red)'
                : accountMode === 'paper'
                  ? 'var(--accent-blue)'
                  : 'var(--text-muted)',
          }}
        >
          {accountMode}
        </span>

        {/* Data freshness */}
        <span style={{ fontSize: 14, color: dataFresh ? 'var(--accent-green)' : 'var(--accent-yellow)' }}>
          {dataFresh ? 'live' : 'stale'}
        </span>
      </div>

      {/* Center: single account-wide 24h futures realized P&L */}
      <FuturesPnl24hCard compact />

      {/* Right: alerts + menu (signals, settings, resync, sheets) */}
      <div className="flex items-center gap-1" style={{ position: 'relative' }}>
        {action && (
          <button
            onClick={action.onClick}
            aria-label={action.label}
            title={action.label}
            data-testid="mobile-header-action"
            style={iconBtn}
          >
            {action.icon}
          </button>
        )}
        <button
          onClick={() => onOpenSheet('alerts')}
          aria-label="Alerts"
          style={iconBtn}
        >
          <span>🔔</span>
          {activeAlerts > 0 && (
            <span style={{
              position: 'absolute', top: 6, right: 4, minWidth: 16, height: 16,
              borderRadius: 8, background: 'var(--accent-red)', color: '#fff',
              fontSize: 10, fontWeight: 700, lineHeight: '16px', textAlign: 'center',
              padding: '0 3px',
            }}>{activeAlerts}</span>
          )}
        </button>
        <button
          onClick={() => setMenuOpen((v) => !v)}
          aria-label="Menu"
          aria-expanded={menuOpen}
          data-testid="mobile-menu"
          style={iconBtn}
        >
          ⋯
        </button>
        {menuOpen && (
          <>
            <div onClick={() => setMenuOpen(false)} style={{ position: 'fixed', inset: 0, zIndex: 70 }} />
            <div style={{
              position: 'absolute', top: '100%', right: 0, zIndex: 71, minWidth: 200,
              background: 'var(--bg-primary)', border: '1px solid var(--border-default)',
              borderRadius: 6, boxShadow: '0 8px 24px rgba(0,0,0,0.35)', padding: 4,
            }}>
              {([
                ['⇅  Signal rules', onOpenRules],
                ['⚙  Settings', onOpenSettings],
                ['☰  Trades', () => onOpenSheet('trades')],
                ['🤖  Bot trades', () => onOpenSheet('bot-trades')],
                ['📜  Logs', () => onOpenSheet('logs')],
              ] as const).map(([label, fn]) => (
                <button
                  key={label}
                  onClick={() => { setMenuOpen(false); fn(); }}
                  style={menuItem}
                >
                  {label}
                </button>
              ))}
              <div style={{ padding: '6px 8px' }}><ResyncButton /></div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

const iconBtn: React.CSSProperties = {
  position: 'relative', background: 'transparent', border: 'none', cursor: 'pointer',
  color: 'var(--text-secondary)', fontSize: 18, minWidth: 40, minHeight: 40,
  display: 'flex', alignItems: 'center', justifyContent: 'center',
};
const menuItem: React.CSSProperties = {
  display: 'block', width: '100%', textAlign: 'left', minHeight: 44,
  padding: '0 12px', border: 'none', background: 'transparent', cursor: 'pointer',
  fontSize: 14, color: 'var(--text-primary)', borderRadius: 4,
};
