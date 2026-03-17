import { useStore } from '../../data/store';
import { PanelShell } from '../../components/PanelShell';
import type { OrderTemplate } from '../../types';

function buildCommand(t: OrderTemplate): string {
  const base = `${t.side.toLowerCase()} ${t.symbol} ${t.quantity}`;
  if (t.orderType === 'MKT') return base;
  if (t.orderType === 'MOC') return `${base} MOC`;
  if (t.orderType === 'STP') return `${base} STP @ ${t.price!.toFixed(2)}`;
  return `${base} @ ${t.price!.toFixed(2)}`;
}

export function OrderTemplatesPanel() {
  const templates = useStore((s) => s.templates);
  const addCommand = useStore((s) => s.addCommand);
  const removeTemplate = useStore((s) => s.removeTemplate);

  return (
    <PanelShell title="Quick Orders" accent="green" right={
      <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{templates.length} templates</span>
    }>
      <div className="h-full overflow-auto">
        <table className="data-table">
          <thead>
            <tr>
              <th>Label</th>
              <th>Symbol</th>
              <th>Side</th>
              <th>Type</th>
              <th>Qty</th>
              <th>Price</th>
              <th></th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {templates.length === 0 ? (
              <tr>
                <td colSpan={8} style={{ color: 'var(--text-muted)', textAlign: 'center', padding: 16 }}>
                  No templates. Use: template add "label" BUY AAPL 100 LMT @ 180.00
                </td>
              </tr>
            ) : (
              templates.map((t) => (
                <tr key={t.id}>
                  <td style={{ color: 'var(--text-secondary)', fontSize: 11 }}>{t.label}</td>
                  <td className="font-semibold" style={{ color: 'var(--text-primary)' }}>{t.symbol}</td>
                  <td>
                    <span style={{ color: t.side === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)' }}>
                      {t.side}
                    </span>
                  </td>
                  <td style={{ color: 'var(--text-secondary)' }}>{t.orderType}</td>
                  <td className="font-mono">{t.quantity}</td>
                  <td className="font-mono">
                    {t.price ? t.price.toFixed(2) : <span style={{ color: 'var(--text-muted)' }}>MKT</span>}
                  </td>
                  <td>
                    <button
                      onClick={() => addCommand(buildCommand(t))}
                      className="rounded px-2 py-0.5 text-[11px] font-semibold cursor-pointer transition-colors"
                      style={{
                        background: t.side === 'BUY' ? 'var(--badge-green-bg)' : 'var(--badge-red-bg)',
                        color: t.side === 'BUY' ? 'var(--accent-green)' : 'var(--accent-red)',
                        border: 'none',
                      }}
                    >
                      Fire
                    </button>
                  </td>
                  <td>
                    <button
                      onClick={() => removeTemplate(t.id)}
                      className="rounded px-1.5 py-0.5 text-[10px] cursor-pointer transition-colors"
                      style={{
                        background: 'transparent',
                        color: 'var(--text-muted)',
                        border: '1px solid var(--border-default)',
                      }}
                      title="Remove template"
                    >
                      ✕
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </PanelShell>
  );
}
