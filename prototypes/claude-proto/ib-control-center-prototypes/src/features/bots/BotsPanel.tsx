import { useStore } from '../../data/store';
import { formatAge, formatCurrency, formatDuration, pnlClass } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';
import type { BotStatus } from '../../types';

const statusConfig: Record<BotStatus, { var: string; label: string; dot: string }> = {
  running: { var: 'var(--accent-green)', label: 'RUNNING', dot: '●' },
  stopped: { var: 'var(--text-muted)', label: 'STOPPED', dot: '○' },
  error: { var: 'var(--accent-red)', label: 'ERROR', dot: '✗' },
  paused: { var: 'var(--accent-yellow)', label: 'PAUSED', dot: '◑' },
};

export function BotsPanel({ large = false }: { large?: boolean }) {
  const bots = useStore((s) => s.bots);

  if (large) {
    return (
      <PanelShell title="Bots" accent="purple" right={
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{bots.length} registered</span>
      }>
        <div className="h-full overflow-auto p-2 grid grid-cols-1 gap-2">
          {bots.map((bot) => {
            const cfg = statusConfig[bot.status];
            return (
              <div key={bot.id} className="rounded border p-3"
                style={{ background: 'var(--bg-secondary)', borderColor: 'var(--border-default)' }}>
                <div className="flex items-center justify-between mb-2">
                  <div className="flex items-center gap-2">
                    <span style={{ color: cfg.var }} className="text-sm">{cfg.dot}</span>
                    <span className="font-semibold text-sm" style={{ color: 'var(--text-primary)' }}>{bot.name}</span>
                    <span className={`badge ${bot.status === 'running' ? 'badge-green' : bot.status === 'error' ? 'badge-red' : bot.status === 'paused' ? 'badge-yellow' : 'badge-gray'}`}>
                      {cfg.label}
                    </span>
                  </div>
                  <span className="text-[10px]" style={{ color: 'var(--text-muted)' }}>
                    {bot.strategy}
                  </span>
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
            </tr>
          </thead>
          <tbody>
            {bots.map((bot) => {
              const cfg = statusConfig[bot.status];
              return (
                <tr key={bot.id}>
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
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </PanelShell>
  );
}
