import { useState } from 'react';
import { useStore } from '../../data/store';
import { formatCurrency } from '../../utils/format';
import { SettingsModal } from '../settings/SettingsModal';
import { ResyncButton } from './ResyncButton';

export function GlobalHeader() {
  const { global, dataMode, wsConnected } = useStore();
  const { connectionStatus, accountMode, accountId, serviceHealth, realizedPnl, engineStartedAt } = global;
  // Format "up since" with the engine's start timestamp. The backend
  // writes it as a timezone-aware ISO (server-local PT); ``new Date``
  // honors the offset, ``toLocaleTimeString`` then renders in the
  // browser's locale time.
  const upSinceLabel = (() => {
    if (!engineStartedAt) return null;
    const d = new Date(engineStartedAt);
    if (!Number.isFinite(d.getTime())) return null;
    // Drop the year/seconds — the chip is tight.
    const date = d.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
    const time = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
    return `${date}, ${time}`;
  })();
  const [settingsOpen, setSettingsOpen] = useState(false);

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
            {accountId && (
              <span style={{ fontSize: 11, fontWeight: 400, color: 'var(--text-muted)', marginLeft: 6 }}>
                {accountId}
              </span>
            )}
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
          <div
            style={{ fontSize: 10, letterSpacing: '0.2em', textTransform: 'uppercase', color: 'var(--text-muted)', marginBottom: 2 }}
            title="Sum of NET realized P&L on closed bot trades in the last 24 hours (rolling window) — realized_pnl minus commission per trade. Commission backfills asynchronously after IB's commissionReport, so this can briefly inflate until the report lands."
          >
            24h P&L
          </div>
          <div className="font-mono" style={{ fontSize: 13, fontWeight: 600, color: realizedPnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)' }}>
            {formatCurrency(realizedPnl)}
          </div>
        </div>

        {/* Uptime — static "up since …" only. The running counter was
            removed (2026-05-10) because ``formatDuration(sessionUptime)``
            recomputed on every poll tick, forcing a header re-render
            every ~2s and keeping the tab hot when nothing was changing.
            The hover tooltip carries the full datetime for precision. */}
        {upSinceLabel && (
          <div
            style={{ fontSize: 11, color: 'var(--text-muted)' }}
            title={engineStartedAt
              ? `Engine started ${new Date(engineStartedAt).toLocaleString()}`
              : undefined}
          >
            up since {upSinceLabel}
          </div>
        )}

        {/* Resync — operator override for stuck subscriptions. See
            ResyncButton for the click vs shift+click semantics. */}
        <ResyncButton />

        {/* Settings (theme + layout selectors moved here so the header
            has room for the resync button). */}
        <button
          onClick={() => setSettingsOpen(true)}
          title="Settings"
          aria-label="Open settings"
          className="rounded border cursor-pointer"
          style={{
            borderColor: 'var(--border-default)', background: 'var(--bg-primary)',
            color: 'var(--text-secondary)',
            width: 32, height: 32, fontSize: 16,
            display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          ⚙
        </button>
      </div>
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
    </header>
  );
}
