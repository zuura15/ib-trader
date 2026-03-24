import { useStore } from '../../data/store';
import { formatCurrency, formatDuration } from '../../utils/format';
import type { LayoutVariant, ThemeMode } from '../../types';

const THEMES: { id: ThemeMode; label: string; icon: string }[] = [
  { id: 'dark',     label: 'Midnight', icon: '🌑' },
  { id: 'charcoal', label: 'Charcoal', icon: '◼' },
  { id: 'navy',     label: 'Navy',     icon: '🔵' },
  { id: 'mocha',    label: 'Mocha',    icon: '☕' },
  { id: 'light',    label: 'Light',    icon: '☀' },
];

const variantLabels: Record<LayoutVariant, string> = {
  A: 'Classic',
  B: 'Modern',
  C: 'Command',
  D: 'Bots',
};

export function GlobalHeader() {
  const { global, activeVariant, setVariant, theme, setTheme, dataMode, wsConnected } = useStore();
  const { connectionStatus, accountMode, serviceHealth, realizedPnl, sessionUptime } = global;

  const healthyCount = Object.values(serviceHealth).filter(Boolean).length;
  const totalServices = Object.keys(serviceHealth).length;

  const connColor = connectionStatus === 'connected' ? 'var(--accent-green)' : connectionStatus === 'reconnecting' ? 'var(--accent-yellow)' : 'var(--accent-red)';
  const healthColor = healthyCount === totalServices && totalServices > 0 ? 'var(--accent-green)' : healthyCount > 0 ? 'var(--accent-yellow)' : 'var(--accent-red)';

  // In live mode, show WS connection status as data freshness indicator
  const dataFresh = dataMode === 'mock' ? true : wsConnected;

  return (
    <header className="flex flex-wrap items-start justify-between gap-3 border-b px-4 py-3 shrink-0"
      style={{ background: 'var(--bg-root)', borderColor: 'var(--border-default)' }}>

      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-4">
        {/* Title */}
        <div>
          <div style={{ fontSize: 11, letterSpacing: '0.24em', textTransform: 'uppercase', color: 'var(--text-muted)' }}>
            IB Control Center
          </div>
          <div style={{ fontSize: 14, fontWeight: 600, color: 'var(--text-primary)' }}>
            Trading Workstation {dataMode === 'mock' ? '(Demo)' : ''}
          </div>
        </div>

        {/* Status chips */}
        <div className="flex flex-wrap gap-2">
          {/* Engine Connection */}
          <div className="rounded border px-2.5 py-1" style={{ borderColor: 'var(--border-default)', background: 'var(--bg-primary)' }}>
            <span style={{ fontSize: 10, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginRight: 8 }}>Engine</span>
            <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, fontWeight: 500, color: connColor, textTransform: 'capitalize' }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: connColor, display: 'inline-block' }} />
              {connectionStatus}
            </span>
          </div>

          {/* Account */}
          <div className="rounded border px-2.5 py-1" style={{ borderColor: 'var(--border-default)', background: 'var(--bg-primary)' }}>
            <span style={{ fontSize: 10, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginRight: 8 }}>Account</span>
            <span style={{ fontSize: 12, fontWeight: 500, color: accountMode === 'live' ? 'var(--accent-red)' : accountMode === 'paper' ? 'var(--accent-blue)' : 'var(--text-muted)', textTransform: 'capitalize' }}>
              {accountMode}
            </span>
          </div>

          {/* Services */}
          <div className="rounded border px-2.5 py-1" style={{ borderColor: 'var(--border-default)', background: 'var(--bg-primary)' }}>
            <span style={{ fontSize: 10, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginRight: 8 }}>Services</span>
            <span style={{ fontSize: 12, fontWeight: 500, color: healthColor }}>
              {totalServices > 0 ? `${healthyCount}/${totalServices}` : '—'}
            </span>
          </div>

          {/* Data freshness */}
          <div className="rounded border px-2.5 py-1" style={{
            borderColor: !dataFresh ? 'var(--accent-yellow)' : 'var(--border-default)',
            background: !dataFresh ? 'var(--badge-yellow-bg)' : 'var(--bg-primary)'
          }}>
            <span style={{ fontSize: 10, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginRight: 8 }}>Data</span>
            <span style={{ fontSize: 12, fontWeight: 500, color: dataFresh ? 'var(--accent-green)' : 'var(--accent-yellow)' }}>
              {dataFresh ? 'live' : 'stale'}
            </span>
          </div>
        </div>
      </div>

      {/* Right side: P&L + Uptime + Theme + Variant */}
      <div className="flex shrink-0 flex-wrap items-center justify-end gap-3">
        {/* P&L */}
        <div className="rounded border px-3 py-1.5" style={{ borderColor: 'var(--border-default)', background: 'var(--bg-primary)' }}>
          <div style={{ fontSize: 10, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 2 }}>Realized P&L</div>
          <div className="font-mono" style={{ fontSize: 13, fontWeight: 600, color: realizedPnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
            {formatCurrency(realizedPnl)}
          </div>
        </div>

        {/* Uptime */}
        {sessionUptime > 0 && (
          <div style={{ fontSize: 11, color: 'var(--text-muted)' }}>
            UP {formatDuration(sessionUptime * 1000)}
          </div>
        )}

        {/* Theme picker */}
        <div className="rounded border p-1" style={{ borderColor: 'var(--border-default)', background: 'var(--bg-primary)' }}>
          <div style={{ fontSize: 10, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 4, paddingLeft: 8 }}>
            Theme
          </div>
          <div className="flex gap-1">
            {THEMES.map((t) => (
              <button
                key={t.id}
                onClick={() => setTheme(t.id)}
                title={t.label}
                className="rounded px-2 py-1.5 text-xs font-medium cursor-pointer transition-colors"
                style={{
                  background: theme === t.id ? 'var(--accent-blue)' : 'var(--bg-secondary)',
                  color: theme === t.id ? (theme === 'light' ? '#fff' : '#090b0f') : 'var(--text-secondary)',
                  border: 'none',
                  fontSize: 11,
                }}
              >
                <span style={{ marginRight: 3 }}>{t.icon}</span>{t.label}
              </button>
            ))}
          </div>
        </div>

        {/* Variant switcher */}
        <div className="rounded border p-1" style={{ borderColor: 'var(--border-default)', background: 'var(--bg-primary)' }}>
          <div style={{ fontSize: 10, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 4, paddingLeft: 8 }}>
            Layout
          </div>
          <div className="flex gap-1">
            {(['A', 'B', 'C', 'D'] as LayoutVariant[]).map((v) => (
              <button
                key={v}
                onClick={() => setVariant(v)}
                className="rounded px-2.5 py-1.5 text-xs font-medium cursor-pointer transition-colors"
                style={{
                  background: activeVariant === v ? 'var(--accent-blue)' : 'var(--bg-secondary)',
                  color: activeVariant === v ? (theme === 'dark' ? '#090b0f' : '#ffffff') : 'var(--text-secondary)',
                  border: 'none',
                }}
              >
                {v} · {variantLabels[v]}
              </button>
            ))}
          </div>
        </div>
      </div>
    </header>
  );
}
