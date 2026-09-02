import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ChartBotPane } from './ChartBotPane';
import { CommandConsole } from '../console/CommandConsole';
import { PositionsPanel } from '../positions/PositionsPanel';
import { useChartBotSymbols } from '../../data/useChartBotSymbols';
import { useStore } from '../../data/store';

/**
 * Mobile primary view — two chart rows stacked vertically, each its own
 * little tab group:
 *
 *   Top row    : Gold (chart-bot-1) · Console · Positions
 *   Bottom row : Nasdaq (chart-bot-4) · Micro NQ (chart-bot-3)
 *
 * Charts reuse the desktop ``ChartBotPane`` in ``compact`` mode (B/S/C
 * buttons), so order entry / live P&L / smart_market close are identical
 * to desktop. Every sub-tab stays MOUNTED and is toggled with
 * ``display`` rather than unmounted — that keeps each chart's quote
 * subscription alive across tab switches, and (critically) keeps
 * ``PositionsPanel`` mounted so it keeps publishing the per-contract
 * live-P&L the charts read, even while it sits behind the Gold tab.
 */

type TopTab = 'gold' | 'mgc' | 'console' | 'positions';
type BottomTab = 'nasdaq' | 'micro' | 'wti';

function SubTabBar<T extends string>({
  tabs, active, onSelect,
}: {
  tabs: { id: T; label: string }[];
  active: T;
  onSelect: (id: T) => void;
}) {
  return (
    <div
      style={{
        flexShrink: 0,
        display: 'flex',
        gap: 4,
        padding: 4,
        background: 'var(--bg-primary)',
        borderBottom: '1px solid var(--border-default)',
      }}
      role="tablist"
    >
      {tabs.map(({ id, label }) => {
        const on = active === id;
        return (
          <button
            key={id}
            role="tab"
            aria-selected={on}
            onClick={() => onSelect(id)}
            style={{
              flex: 1,
              minHeight: 34,
              border: 'none',
              borderRadius: 4,
              fontSize: 13,
              fontWeight: on ? 700 : 500,
              background: on ? 'var(--accent-blue)' : 'var(--bg-secondary)',
              color: on ? '#fff' : 'var(--text-secondary)',
              cursor: 'pointer',
            }}
          >
            {label}
          </button>
        );
      })}
    </div>
  );
}

/**
 * Mounted-always pane, kept at FULL SIZE even when inactive — we toggle
 * ``visibility``, not ``display``. ``display:none`` zeroes the chart's
 * container, and the 0→real resize on reveal makes lightweight-charts
 * re-fit and throw away the user's pan/zoom (the "Micro NQ starts at a
 * long horizon every time" bug). With ``visibility`` the chart keeps its
 * size and view across tab switches; the inactive pane is non-interactive
 * and sits underneath.
 */
function Pane({ visible, children }: { visible: boolean; children: ReactNode }) {
  return (
    <div
      aria-hidden={!visible}
      style={{
        position: 'absolute',
        inset: 0,
        display: 'flex',
        flexDirection: 'column',
        minHeight: 0,
        visibility: visible ? 'visible' : 'hidden',
        pointerEvents: visible ? 'auto' : 'none',
        zIndex: visible ? 1 : 0,
      }}
    >
      {children}
    </div>
  );
}

export function MobileTradeView() {
  const [top, setTop] = useState<TopTab>('gold');
  const [bottom, setBottom] = useState<BottomTab>('nasdaq');
  // Live slot → symbol so each chart tab shows its actual contract and
  // follows rolls; falls back to a plain label until the map loads.
  const sym = useChartBotSymbols();

  // A chart-strip click drafted a command into the console input —
  // flip the top row to the Console tab so the draft is immediately
  // visible for review + Enter. Nonce ref skips any draft that
  // predates this mount.
  const consoleDraft = useStore((s) => s.consoleDraft);
  const seenDraftNonceRef = useRef(consoleDraft?.nonce ?? 0);
  useEffect(() => {
    if (!consoleDraft || consoleDraft.nonce === seenDraftNonceRef.current) return;
    seenDraftNonceRef.current = consoleDraft.nonce;
    setTop('console');
  }, [consoleDraft]);

  return (
    <div className="flex flex-col h-full" style={{ minHeight: 0 }}>
      {/* Top row — Gold chart, with Console + Positions behind it. */}
      <div className="flex flex-col" style={{ flex: 1, minHeight: 0 }}>
        <SubTabBar<TopTab>
          tabs={[
            { id: 'gold', label: sym[1] ?? 'Gold' },
            { id: 'mgc', label: sym[7] ?? 'Micro GC' },
            { id: 'console', label: 'Console' },
            { id: 'positions', label: 'Positions' },
          ]}
          active={top}
          onSelect={setTop}
        />
        <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
          <Pane visible={top === 'gold'}>
            <ChartBotPane slot={1} compact />
          </Pane>
          <Pane visible={top === 'mgc'}>
            <ChartBotPane slot={7} compact />
          </Pane>
          <Pane visible={top === 'console'}>
            <CommandConsole compact />
          </Pane>
          <Pane visible={top === 'positions'}>
            <PositionsPanel />
          </Pane>
        </div>
      </div>

      <div style={{ height: 1, background: 'var(--border-default)', flexShrink: 0 }} />

      {/* Bottom row — Nasdaq + Micro NQ + WTI Crude. */}
      <div className="flex flex-col" style={{ flex: 1, minHeight: 0 }}>
        <SubTabBar<BottomTab>
          tabs={[
            { id: 'nasdaq', label: sym[4] ?? 'Nasdaq' },
            { id: 'micro', label: sym[3] ?? 'Micro NQ' },
            { id: 'wti', label: sym[5] ?? 'WTI' },
          ]}
          active={bottom}
          onSelect={setBottom}
        />
        <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
          <Pane visible={bottom === 'nasdaq'}>
            <ChartBotPane slot={4} compact />
          </Pane>
          <Pane visible={bottom === 'micro'}>
            <ChartBotPane slot={3} compact />
          </Pane>
          <Pane visible={bottom === 'wti'}>
            <ChartBotPane slot={5} compact />
          </Pane>
        </div>
      </div>
    </div>
  );
}
