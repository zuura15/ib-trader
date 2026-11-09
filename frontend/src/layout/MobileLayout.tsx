import { useEffect, useRef, useState, type ReactNode } from 'react';
import { MobileHeader } from '../features/header/MobileHeader';
import { PositionsPanel } from '../features/positions/PositionsPanel';
import { OrdersPanel } from '../features/orders/OrdersPanel';
import { WatchlistPanel } from '../features/watchlist/WatchlistPanel';
import { CommandConsole } from '../features/console/CommandConsole';
import { TradesPanel } from '../features/trades/TradesPanel';
import { AlertsPanel } from '../features/alerts/AlertsPanel';
import { LogStream } from '../features/logs/LogStream';
import { BotTradesPanel } from '../features/bots/BotTradesPanel';
import { SettingsModal } from '../features/settings/SettingsModal';
import { SignalRulesPanel } from '../features/signals/SignalRulesPanel';
import { ChartAccordion } from '../features/mobile/ChartAccordion';
import { useStore } from '../data/store';

/**
 * Portrait mobile layout: three tabs on a bottom bar — Pos, Graphs,
 * Watch — with everything flowing vertically inside the phone's width
 * (a wide phone like the Fold's inner screen just gets wider panels).
 *
 * Every tab stays MOUNTED and is hidden with ``visibility`` (not
 * ``display:none``): PositionsPanel publishes the live per-contract P&L
 * the charts read, and open charts keep their size and zoom across tab
 * switches. Secondary surfaces (trades, logs, alerts, bot trades,
 * settings, signal rules) open as full-screen sheets from the header menu.
 */

const TABS = [
  { id: 'pos', label: 'Pos', icon: '▤' },
  { id: 'graphs', label: 'Graphs', icon: '📈' },
  { id: 'watch', label: 'Watch', icon: '☰' },
] as const;
type Tab = (typeof TABS)[number]['id'];

type Sheet = 'trades' | 'bot-trades' | 'alerts' | 'logs' | null;

export function MobileLayout() {
  const [tab, setTabRaw] = useState<Tab>(() => {
    const saved = localStorage.getItem('ib-mobile-tab3');
    return TABS.some((t) => t.id === saved) ? (saved as Tab) : 'graphs';
  });
  const setTab = (t: Tab) => {
    try { localStorage.setItem('ib-mobile-tab3', t); } catch { /* private mode */ }
    setTabRaw(t);
  };
  const [sheet, setSheet] = useState<Sheet>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [rulesOpen, setRulesOpen] = useState(false);

  // A chart's price strip drafts a command into the console — show it.
  const consoleDraft = useStore((s) => s.consoleDraft);
  const seenDraftRef = useRef(consoleDraft?.nonce ?? 0);
  useEffect(() => {
    if (!consoleDraft || consoleDraft.nonce === seenDraftRef.current) return;
    seenDraftRef.current = consoleDraft.nonce;
    setTab('pos');
  }, [consoleDraft]);

  return (
    <div
      className="flex flex-col h-dvh w-screen overflow-hidden"
      style={{ background: 'var(--bg-root)' }}
    >
      <MobileHeader
        onOpenSheet={(s) => setSheet(s)}
        onOpenSettings={() => setSettingsOpen(true)}
        onOpenRules={() => setRulesOpen(true)}
      />

      {/* .mobile-layout-shell: DiagOverlay's white-screen watchdog looks
          for it to confirm the mobile layout rendered. */}
      <div className="mobile-layout-shell" style={{ flex: 1, minHeight: 0, position: 'relative' }}>
        <TabPane visible={tab === 'pos'}><PosTab /></TabPane>
        <TabPane visible={tab === 'graphs'}><ChartAccordion /></TabPane>
        <TabPane visible={tab === 'watch'}><WatchlistPanel /></TabPane>
      </div>

      <nav
        role="tablist"
        style={{
          display: 'flex', flexShrink: 0, borderTop: '1px solid var(--border-default)',
          background: 'var(--bg-primary)', paddingBottom: 'env(safe-area-inset-bottom)',
        }}
      >
        {TABS.map((t) => {
          const on = tab === t.id;
          return (
            <button
              key={t.id}
              role="tab"
              aria-selected={on}
              data-testid={`mobile-tab-${t.id}`}
              onClick={() => setTab(t.id)}
              style={{
                flex: 1, minHeight: 54, border: 'none', cursor: 'pointer',
                background: 'transparent', display: 'flex', flexDirection: 'column',
                alignItems: 'center', justifyContent: 'center', gap: 2,
                color: on ? 'var(--accent-blue)' : 'var(--text-muted)',
                borderTop: on ? '2px solid var(--accent-blue)' : '2px solid transparent',
              }}
            >
              <span style={{ fontSize: 18, lineHeight: 1 }}>{t.icon}</span>
              <span style={{ fontSize: 12, fontWeight: on ? 700 : 500 }}>{t.label}</span>
            </button>
          );
        })}
      </nav>

      {sheet && (
        <MobileSheet
          title={{
            trades: 'Trades', 'bot-trades': 'Bot trades', alerts: 'Alerts', logs: 'Logs',
          }[sheet]}
          onClose={() => setSheet(null)}
        >
          {sheet === 'trades' && <TradesPanel compact />}
          {sheet === 'bot-trades' && <BotTradesPanel compact />}
          {sheet === 'alerts' && <AlertsPanel />}
          {sheet === 'logs' && <LogStream maxLines={200} />}
        </MobileSheet>
      )}
      <SettingsModal open={settingsOpen} onClose={() => setSettingsOpen(false)} />
      <SignalRulesPanel open={rulesOpen} onClose={() => setRulesOpen(false)} />
    </div>
  );
}

/** Pos tab: positions, working orders and the console, stacked. */
function PosTab() {
  return (
    <div style={{ height: '100%', overflowY: 'auto', WebkitOverflowScrolling: 'touch' }}>
      <Section title="Positions" height="min(46dvh, 460px)" testId="mobile-pos-positions">
        <PositionsPanel compact />
      </Section>
      <Section title="Working orders" height="min(28dvh, 300px)" testId="mobile-pos-orders">
        <OrdersPanel compact />
      </Section>
      <Section title="Console" height="min(42dvh, 420px)" testId="mobile-pos-console">
        <CommandConsole compact />
      </Section>
      <div style={{ height: 16 }} />
    </div>
  );
}

/** Collapsible, fixed-height section. Kept mounted when collapsed
 *  (hidden) so PositionsPanel keeps publishing live P&L. */
function Section({
  title, height, testId, children,
}: { title: string; height: string; testId: string; children: ReactNode }) {
  const key = `ib-mobile-sec-${testId}`;
  const [open, setOpen] = useState(() => localStorage.getItem(key) !== '0');
  return (
    <section
      data-testid={testId}
      style={{
        margin: '6px 8px', border: '1px solid var(--border-default)',
        borderRadius: 6, overflow: 'hidden', background: 'var(--panel-bg)',
      }}
    >
      <button
        onClick={() => {
          try { localStorage.setItem(key, open ? '0' : '1'); } catch { /* private mode */ }
          setOpen(!open);
        }}
        aria-expanded={open}
        style={{
          width: '100%', display: 'flex', alignItems: 'center', gap: 10,
          padding: '0 12px', minHeight: 42, border: 'none', cursor: 'pointer',
          background: 'var(--bg-primary)', color: 'var(--text-primary)',
          fontSize: 14, fontWeight: 700,
          borderBottom: open ? '1px solid var(--border-default)' : 'none',
        }}
      >
        <span style={{
          fontSize: 12, color: 'var(--text-muted)', width: 12,
          transform: open ? 'rotate(90deg)' : 'none', transition: 'transform 120ms',
        }}>▶</span>
        {title}
      </button>
      <div style={{ height: open ? height : 0, overflow: 'hidden', display: 'flex', flexDirection: 'column' }}>
        {children}
      </div>
    </section>
  );
}

function TabPane({ visible, children }: { visible: boolean; children: ReactNode }) {
  return (
    <div
      aria-hidden={!visible}
      style={{
        position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
        minHeight: 0, visibility: visible ? 'visible' : 'hidden',
        pointerEvents: visible ? 'auto' : 'none', zIndex: visible ? 1 : 0,
      }}
    >
      {children}
    </div>
  );
}

function MobileSheet({
  title, onClose, children,
}: { title: string; onClose: () => void; children: ReactNode }) {
  return (
    <div
      style={{
        position: 'fixed', inset: 0, zIndex: 8000, display: 'flex', flexDirection: 'column',
        background: 'var(--bg-root)',
      }}
    >
      <div style={{
        display: 'flex', alignItems: 'center', minHeight: 48, padding: '0 6px 0 14px',
        borderBottom: '1px solid var(--border-default)', background: 'var(--bg-primary)',
      }}>
        <span style={{ flex: 1, fontSize: 15, fontWeight: 700, color: 'var(--text-primary)' }}>{title}</span>
        <button
          onClick={onClose}
          aria-label="Close"
          style={{
            minWidth: 44, minHeight: 44, border: 'none', background: 'transparent',
            fontSize: 22, color: 'var(--text-muted)', cursor: 'pointer',
          }}
        >
          ×
        </button>
      </div>
      <div style={{ flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column' }}>{children}</div>
    </div>
  );
}
