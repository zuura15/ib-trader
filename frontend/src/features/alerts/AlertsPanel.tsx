import { useState } from 'react';
import { useStore } from '../../data/store';
import { formatTime } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';

function getSeverityConfig(severity: string) {
  const s = (severity || '').toLowerCase();
  if (s === 'catastrophic' || s === 'error' || s === 'critical') {
    return { badge: 'badge-red', label: 'ERROR', color: 'var(--accent-red)', bg: 'var(--badge-red-bg)' };
  }
  return { badge: 'badge-yellow', label: 'WARN', color: 'var(--accent-yellow)', bg: 'var(--badge-yellow-bg)' };
}

function sortByTime(a: any, b: any) {
  const ta = a.timestamp instanceof Date ? a.timestamp.getTime() : new Date(a.timestamp).getTime();
  const tb = b.timestamp instanceof Date ? b.timestamp.getTime() : new Date(b.timestamp).getTime();
  return tb - ta;
}

export function AlertsPanel() {
  const { alerts, dismissAlert } = useStore();
  const [tab, setTab] = useState<'active' | 'dismissed'>('active');

  const activeAlerts = alerts.filter(a => !a.dismissed).sort(sortByTime);
  const dismissedAlerts = alerts.filter(a => a.dismissed).sort(sortByTime);
  const visibleAlerts = tab === 'active' ? activeAlerts : dismissedAlerts;

  return (
    <PanelShell title="Alerts" accent="red" right={
      <div className="flex items-center gap-2">
        {/* Tabs */}
        <div className="flex gap-1">
          <button onClick={() => setTab('active')}
            className="text-[10px] px-2 py-0.5 rounded border-none"
            style={{
              background: tab === 'active' ? 'var(--badge-red-bg)' : 'transparent',
              color: tab === 'active' ? 'var(--accent-red)' : 'var(--text-muted)',
            }}>
            Active ({activeAlerts.length})
          </button>
          <button onClick={() => setTab('dismissed')}
            className="text-[10px] px-2 py-0.5 rounded border-none"
            style={{
              background: tab === 'dismissed' ? 'var(--badge-blue-bg)' : 'transparent',
              color: tab === 'dismissed' ? 'var(--accent-blue)' : 'var(--text-muted)',
            }}>
            Dismissed ({dismissedAlerts.length})
          </button>
        </div>
      </div>
    }>
      <div className="h-full overflow-auto p-2">
        {visibleAlerts.length === 0 && (
          <div className="text-center py-6 text-xs" style={{ color: 'var(--text-muted)' }}>
            {tab === 'active' ? 'No active alerts' : 'No dismissed alerts'}
          </div>
        )}

        {visibleAlerts.map((alert) => {
          const sev = getSeverityConfig(alert.severity);
          return (
            <div
              key={alert.id}
              className="mb-2 rounded border text-sm"
              style={{
                background: alert.dismissed ? 'var(--bg-secondary)' : sev.bg,
                borderColor: alert.dismissed ? 'var(--border-default)' : sev.color,
                opacity: alert.dismissed ? 0.7 : 1,
              }}
            >
              <div className="p-2.5">
                <div className="flex items-center gap-2 mb-1">
                  <span className={`badge ${sev.badge} shrink-0`}>{sev.label}</span>
                  <span className="font-semibold" style={{ color: alert.dismissed ? 'var(--text-secondary)' : sev.color }}>
                    {alert.title}
                  </span>
                  <span style={{ color: 'var(--text-muted)', fontSize: 11, marginLeft: 'auto', flexShrink: 0 }}>
                    {formatTime(alert.timestamp)}
                  </span>
                </div>

                <div style={{ color: 'var(--text-secondary)', lineHeight: 1.4 }}>
                  {alert.message}
                </div>

                {alert.details && (
                  <pre className="mt-1.5 p-1.5 rounded text-[11px] font-mono whitespace-pre-wrap"
                    style={{ background: 'var(--bg-tertiary)', color: 'var(--text-secondary)' }}>
                    {alert.details}
                  </pre>
                )}

                {/* Dismiss button — only on active tab */}
                {!alert.dismissed && (
                  <div className="mt-2">
                    <button
                      onClick={() => dismissAlert(alert.id)}
                      className="rounded px-3 py-1 text-xs transition-colors"
                      style={{
                        background: 'var(--bg-tertiary)',
                        color: 'var(--text-secondary)',
                        border: '1px solid var(--border-default)',
                      }}
                      onMouseEnter={(e) => {
                        e.currentTarget.style.background = 'var(--bg-hover)';
                        e.currentTarget.style.color = 'var(--text-primary)';
                      }}
                      onMouseLeave={(e) => {
                        e.currentTarget.style.background = 'var(--bg-tertiary)';
                        e.currentTarget.style.color = 'var(--text-secondary)';
                      }}
                    >
                      Dismiss
                    </button>
                  </div>
                )}
              </div>
            </div>
          );
        })}
      </div>
    </PanelShell>
  );
}
