import { ChartBotPane } from '../chart-bot/ChartBotPane';
import { useStore } from '../../data/store';

/**
 * Graphs tab (mobile): every chart pane from the desktop layout as a
 * collapsible panel, stacked vertically. Expanded panels mount the same
 * ``ChartBotPane`` the desktop uses (B/S/C, qty, price strip, order
 * lines, fills, recommendation badge…), so nothing is mobile-only. An
 * open panel has no separate title bar — the collapse chevron sits in
 * the chart's own header row. Collapsed panels unmount their chart and
 * show the position + live P&L from the store instead.
 */

const chevron = (isOpen: boolean) => (
  <span style={{
    display: 'inline-block', fontSize: 11, color: 'var(--text-muted)', width: 12,
    transform: isOpen ? 'rotate(90deg)' : 'none', transition: 'transform 120ms',
  }}>▶</span>
);

export function ChartAccordion({
  sym, slots, open, setOpen,
}: {
  sym: Record<number, string>;
  slots: number[];
  open: number[];
  setOpen: (v: number[]) => void;
}) {
  const chartPnl = useStore((s) => s.chartPnl);
  const toggle = (slot: number) =>
    setOpen(open.includes(slot) ? open.filter((s) => s !== slot) : [...open, slot]);

  return (
    <div style={{ height: '100%', overflowY: 'auto', WebkitOverflowScrolling: 'touch' }}>
      {slots.map((slot) => {
        const symbol = sym[slot];
        const isOpen = open.includes(slot);
        const pnl = symbol ? chartPnl[symbol] : undefined;
        return (
          <section
            key={slot}
            data-testid={`mobile-chart-${slot}`}
            style={{
              borderBottom: '1px solid var(--border-default)',
              background: 'var(--panel-bg)',
            }}
          >
            {isOpen ? (
              // Fixed height: lightweight-charts needs a sized box, and a
              // ~62% viewport chart leaves the next panel's header visible.
              <div style={{ height: 'min(62dvh, 560px)', display: 'flex', flexDirection: 'column' }}>
                <ChartBotPane
                  slot={slot}
                  compact
                  headerLeading={(
                    <button
                      onClick={() => toggle(slot)}
                      aria-label={`Collapse ${symbol ?? `chart ${slot}`}`}
                      aria-expanded
                      style={{
                        border: 'none', background: 'transparent', cursor: 'pointer',
                        padding: '6px 4px 6px 0', margin: '-6px 0', display: 'flex',
                      }}
                    >
                      {chevron(true)}
                    </button>
                  )}
                />
              </div>
            ) : (
              <button
                onClick={() => toggle(slot)}
                aria-expanded={false}
                style={{
                  width: '100%', display: 'flex', alignItems: 'center', gap: 10,
                  padding: '0 10px', minHeight: 40, border: 'none', cursor: 'pointer',
                  background: 'var(--bg-primary)', color: 'var(--text-primary)',
                }}
              >
                {chevron(false)}
                <span style={{ fontSize: 14, fontWeight: 700, fontFamily: 'ui-monospace, monospace' }}>
                  {symbol ?? `Chart ${slot}`}
                </span>
                <span style={{ flex: 1 }} />
                {pnl && pnl.qty !== 0 && (
                  <span style={{
                    fontSize: 13, fontFamily: 'ui-monospace, monospace',
                    color: 'var(--text-secondary)',
                  }}>
                    {pnl.qty > 0 ? '+' : ''}{pnl.qty}
                  </span>
                )}
                {pnl && pnl.qty !== 0 && pnl.pnl != null && (
                  <span style={{
                    fontSize: 13, fontWeight: 700, fontFamily: 'ui-monospace, monospace',
                    color: pnl.pnl >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
                  }}>
                    {pnl.pnl >= 0 ? '+' : '−'}${Math.abs(pnl.pnl).toLocaleString(undefined, { maximumFractionDigits: 0 })}
                  </span>
                )}
              </button>
            )}
          </section>
        );
      })}
      <div style={{ height: 12 }} />
    </div>
  );
}
