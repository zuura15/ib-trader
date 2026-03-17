import { useStore } from '../../data/store';
import { PanelShell } from '../../components/PanelShell';
import type { ScenarioName } from '../../types';

const scenarios: Array<{ name: ScenarioName; label: string; group: string }> = [
  { name: 'healthy', label: 'Healthy / Normal', group: 'System' },
  { name: 'ib_disconnected', label: 'IB Disconnected', group: 'System' },
  { name: 'reconnecting', label: 'Reconnecting', group: 'System' },
  { name: 'stale_data', label: 'Stale Market Data', group: 'System' },
  { name: 'paper_mode', label: 'Paper Mode', group: 'Account' },
  { name: 'live_warning', label: 'Live Account Warning', group: 'Account' },
  { name: 'command_running', label: 'Command Running', group: 'Commands' },
  { name: 'command_failure', label: 'Command Failure', group: 'Commands' },
  { name: 'partial_fill', label: 'Partial Fill', group: 'Orders' },
  { name: 'order_rejection', label: 'Order Rejection', group: 'Orders' },
  { name: 'broker_burst', label: 'Broker Message Burst', group: 'Data' },
  { name: 'bot_heartbeat_missing', label: 'Bot Heartbeat Missing', group: 'Bots' },
  { name: 'reconciliation_mismatch', label: 'Reconciliation Mismatch', group: 'Data' },
];

const groups = [...new Set(scenarios.map(s => s.group))];

export function ScenarioPanel() {
  const { activeScenario, applyScenario } = useStore();

  return (
    <PanelShell title="Scenarios" accent="amber" right={
      <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>simulation controls</span>
    }>
      <div className="h-full overflow-auto p-2">
        <div className="text-[10px] mb-2 px-1" style={{ color: 'var(--text-muted)' }}>
          Simulate operational states. Effects are visible across all panes.
        </div>
        {groups.map((group) => (
          <div key={group} className="mb-2">
            <div className="text-[10px] font-semibold mb-1 px-1 uppercase tracking-wider" style={{ color: 'var(--text-secondary)' }}>
              {group}
            </div>
            <div className="grid grid-cols-2 gap-1">
              {scenarios.filter(s => s.group === group).map((s) => (
                <button
                  key={s.name}
                  onClick={() => applyScenario(s.name)}
                  className={`text-[11px] text-left px-2 py-1.5 rounded border cursor-pointer transition-colors ${
                    activeScenario === s.name ? 'font-semibold' : ''
                  }`}
                  style={{
                    background: activeScenario === s.name ? 'var(--badge-blue-bg)' : 'var(--bg-secondary)',
                    borderColor: activeScenario === s.name ? 'var(--accent-blue)' : 'var(--border-muted)',
                    color: activeScenario === s.name ? 'var(--accent-blue)' : 'var(--text-secondary)',
                  }}
                >
                  {s.label}
                </button>
              ))}
            </div>
          </div>
        ))}
      </div>
    </PanelShell>
  );
}
