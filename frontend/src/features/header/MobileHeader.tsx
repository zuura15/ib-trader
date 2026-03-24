import { useStore } from '../../data/store';
import type { ThemeMode } from '../../types';

const THEME_CYCLE: ThemeMode[] = ['dark', 'charcoal', 'navy', 'mocha', 'light'];
const THEME_ICONS: Record<ThemeMode, string> = {
  dark: '🌑', charcoal: '◼', navy: '🔵', mocha: '☕', light: '☀',
};

/**
 * Condensed header for the mobile layout.
 *
 * Shows the minimum critical information a trader needs at a glance:
 * connection status, account mode (paper vs live), and data freshness.
 */
export function MobileHeader() {
  const { global, dataMode, wsConnected, theme, setTheme } = useStore();
  const { connectionStatus, accountMode } = global;

  const dataFresh = dataMode === 'mock' ? true : wsConnected;

  const connColor =
    connectionStatus === 'connected'
      ? 'var(--accent-green)'
      : connectionStatus === 'reconnecting'
        ? 'var(--accent-yellow)'
        : 'var(--accent-red)';

  return (
    <div
      className="flex items-center justify-between px-3 py-1.5 shrink-0 border-b"
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
          <span style={{ fontSize: 14, color: 'var(--text-secondary)', textTransform: 'capitalize' }}>
            {connectionStatus}
          </span>
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

      {/* Right: theme cycler */}
      <button
        onClick={() => {
          const idx = THEME_CYCLE.indexOf(theme);
          setTheme(THEME_CYCLE[(idx + 1) % THEME_CYCLE.length]);
        }}
        className="border-none cursor-pointer rounded"
        style={{ background: 'transparent', color: 'var(--text-muted)', fontSize: 16, minWidth: 44, minHeight: 44, display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 4 }}
        aria-label="Cycle theme"
      >
        <span>{THEME_ICONS[theme]}</span>
      </button>
    </div>
  );
}
