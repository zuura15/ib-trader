import { useState } from 'react';
import { ChartBotPane } from '../chart-bot/ChartBotPane';
import { useChartBotSymbols } from '../../data/useChartBotSymbols';
import { useStore } from '../../data/store';

/**
 * Graphs tab (mobile): every chart pane from the desktop layout as a
 * collapsible panel, stacked vertically. Expanded panels mount the same
 * ``ChartBotPane`` the desktop uses (B/S/C, qty, price strip, order
 * lines, fills, recommendation badge…), so nothing is mobile-only.
 * Collapsed panels unmount their chart and show the position + live
 * P&L from the store in the header instead.
 */

/** Desktop order: gold pair, Nasdaq pair, Dow, crude. */
const SLOT_ORDER = [1, 7, 8, 9, 10, 5];
const OPEN_KEY = 'ib-mobile-open-charts';

function loadOpen(): number[] {
  try {
    const v = JSON.parse(localStorage.getItem(OPEN_KEY) ?? 'null');
    return Array.isArray(v) ? v.filter((n) => Number.isInteger(n)) : [SLOT_ORDER[0]];
  } catch { return [SLOT_ORDER[0]]; }
}

function saveOpen(v: number[]) {
  try { localStorage.setItem(OPEN_KEY, JSON.stringify(v)); } catch { /* private mode */ }
}

export function ChartAccordion() {
  const sym = useChartBotSymbols();
  const chartPnl = useStore((s) => s.chartPnl);
  const [open, setOpenRaw] = useState<number[]>(loadOpen);
  const setOpen = (v: number[]) => { setOpenRaw(v); saveOpen(v); };

  // Known desktop slots first, then any other chart-bot the API reports.
  const slots = [
    ...SLOT_ORDER.filter((s) => sym[s] || Object.keys(sym).length === 0),
    ...Object.keys(sym).map(Number).filter((s) => !SLOT_ORDER.includes(s)).sort((a, b) => a - b),
  ];
  const allOpen = slots.every((s) => open.includes(s));

  return (
    <div style={{ height: '100%', overflowY: 'auto', WebkitOverflowScrolling: 'touch' }}>
      <div style={{
        display: 'flex', justifyContent: 'flex-end', padding: '6px 10px 2px',
      }}>
        <button
          onClick={() => setOpen(allOpen ? [] : slots)}
          style={{
            background: 'transparent', border: '1px solid var(--border-default)',
            color: 'var(--text-secondary)', borderRadius: 4, fontSize: 12,
            padding: '4px 10px', minHeight: 30,
          }}
        >
          {allOpen ? 'Collapse all' : 'Expand all'}
        </button>
      </div>
      {slots.map((slot) => {
        const symbol = sym[slot];
        const isOpen = open.includes(slot);
        const pnl = symbol ? chartPnl[symbol] : undefined;
        return (
          <section
            key={slot}
            data-testid={`mobile-chart-${slot}`}
            style={{
              margin: '6px 8px', border: '1px solid var(--border-default)',
              borderRadius: 6, overflow: 'hidden', background: 'var(--panel-bg)',
            }}
          >
            <button
              onClick={() => setOpen(isOpen ? open.filter((s) => s !== slot) : [...open, slot])}
              aria-expanded={isOpen}
              style={{
                width: '100%', display: 'flex', alignItems: 'center', gap: 10,
                padding: '0 12px', minHeight: 46, border: 'none', cursor: 'pointer',
                background: 'var(--bg-primary)', color: 'var(--text-primary)',
                borderBottom: isOpen ? '1px solid var(--border-default)' : 'none',
              }}
            >
              <span style={{
                fontSize: 12, color: 'var(--text-muted)', width: 12,
                transform: isOpen ? 'rotate(90deg)' : 'none', transition: 'transform 120ms',
              }}>▶</span>
              <span style={{ fontSize: 15, fontWeight: 700, fontFamily: 'ui-monospace, monospace' }}>
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
            {isOpen && (
              // Fixed height: lightweight-charts needs a sized box, and a
              // ~60% viewport chart leaves the next panel's header visible.
              <div style={{ height: 'min(62dvh, 560px)', display: 'flex', flexDirection: 'column' }}>
                <ChartBotPane slot={slot} compact />
              </div>
            )}
          </section>
        );
      })}
      <div style={{ height: 16 }} />
    </div>
  );
}
