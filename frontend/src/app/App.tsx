import { useCallback, useEffect, useSyncExternalStore } from 'react';
import { GlobalHeader } from '../features/header/GlobalHeader';
import { WorkstationLayout } from '../layout/WorkstationLayout';
import { MobileLayout } from '../layout/MobileLayout';
import { useStore } from '../data/store';

// Full-screen blocking overlay for CATASTROPHIC alerts (IB Gateway down, etc.)
// Renders on top of everything so the user can't miss it. The dismiss button
// closes the overlay but the alert stays in the Alerts pane for reference.
function CatastrophicOverlay() {
  const alerts = useStore((s) => s.alerts);
  const dismissAlert = useStore((s) => s.dismissAlert);

  const active = alerts.filter(
    (a) => a.severity === 'catastrophic' && !a.dismissed,
  );

  if (active.length === 0) return null;

  return (
    <div style={{
      position: 'fixed', inset: 0, zIndex: 99999,
      background: 'rgba(0, 0, 0, 0.85)',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      padding: '1rem',
    }}>
      {/* Constrained card with a pinned footer — the Acknowledge button is
          ALWAYS visible regardless of how many active alerts or how long the
          messages are. Previous version centered everything in a single
          flow, so a long message could push the button below the fold and
          leave the page unusable (the watchdog regression on 2026-05-28). */}
      <div style={{
        background: 'var(--bg-surface, #1a1a2e)',
        border: '2px solid var(--accent-red, #ef4444)',
        borderRadius: '12px',
        width: '100%',
        maxWidth: '560px',
        maxHeight: 'calc(100vh - 2rem)',
        display: 'flex',
        flexDirection: 'column',
      }}>
        <div style={{
          padding: '1.5rem 2rem 0.75rem',
          textAlign: 'center',
          borderBottom: '1px solid rgba(239, 68, 68, 0.3)',
        }}>
          <div style={{ fontSize: '2rem', marginBottom: '0.5rem' }}>&#9888;</div>
          <h2 style={{
            color: 'var(--accent-red, #ef4444)',
            fontSize: '1.1rem',
            fontWeight: 700,
            letterSpacing: '0.02em',
            margin: 0,
            paddingBottom: '0.75rem',
          }}>
            SYSTEM ALERT{active.length > 1 ? ` · ${active.length} active` : ''}
          </h2>
        </div>
        <div style={{
          flex: 1,
          minHeight: 0,
          overflowY: 'auto',
          padding: '1rem 2rem',
        }}>
          {active.map((a) => (
            <div key={a.id} style={{
              marginBottom: '0.75rem',
              paddingBottom: '0.75rem',
              borderBottom: '1px solid rgba(255,255,255,0.05)',
            }}>
              <div style={{
                color: 'var(--text-primary, #e2e8f0)',
                fontSize: '0.9rem',
                fontWeight: 600,
                marginBottom: '0.25rem',
              }}>
                {a.title}
              </div>
              <div style={{
                color: 'var(--text-muted, #94a3b8)',
                fontSize: '0.8rem',
                lineHeight: 1.5,
              }}>
                {a.message}
              </div>
            </div>
          ))}
        </div>
        <div style={{
          padding: '0.75rem 2rem 1.25rem',
          borderTop: '1px solid rgba(239, 68, 68, 0.3)',
          display: 'flex',
          justifyContent: 'center',
          gap: '0.5rem',
        }}>
          <button
            onClick={() => active.forEach((a) => dismissAlert(a.id))}
            style={{
              padding: '0.5rem 1.5rem',
              background: 'var(--accent-red, #ef4444)',
              color: '#fff',
              border: 'none',
              borderRadius: '6px',
              fontSize: '0.85rem',
              fontWeight: 600,
              cursor: 'pointer',
            }}
          >
            Acknowledge {active.length > 1 ? `all (${active.length})` : ''}
          </button>
        </div>
      </div>
    </div>
  );
}

// Non-blocking amber banner shown while the engine is reconnecting to
// IB Gateway. Triggered by a WARNING alert with trigger
// IB_GATEWAY_RECONNECTING, fired by ``_raise_ib_disconnect_alert`` in
// the engine. After 5 min of failed retries the engine escalates to a
// CATASTROPHIC alert, which the modal overlay above takes over.
function ReconnectingBanner() {
  const alerts = useStore((s) => s.alerts);
  const active = alerts.find(
    (a) =>
      a.severity === 'warning'
      && !a.dismissed
      && a.title === 'IB_GATEWAY_RECONNECTING',
  );
  if (!active) return null;
  return (
    <div
      role="status"
      style={{
        flexShrink: 0,
        background: 'var(--accent-yellow, #f7bd5c)',
        color: '#000',
        padding: '4px 12px',
        fontSize: 12,
        fontWeight: 500,
        display: 'flex', alignItems: 'center', gap: 8,
        borderBottom: '1px solid rgba(0,0,0,0.15)',
      }}
    >
      <span
        style={{
          display: 'inline-block',
          width: 8, height: 8, borderRadius: '50%',
          background: 'var(--accent-red, #ef4444)',
        }}
      />
      <span style={{ flex: 1 }}>
        IB Gateway disconnected — reconnecting with exponential backoff.
        Trading paused. Will escalate after 5 min if not restored.
      </span>
    </div>
  );
}

const MOBILE_QUERY = '(max-width: 767px)';

// Create the MediaQueryList once at module scope. This is safe because
// the query string is a compile-time constant. Avoids re-creating the
// MQL on every render, which would cause useSyncExternalStore to
// unsubscribe/re-subscribe each cycle.
const mobileMql: MediaQueryList | null =
  typeof window !== 'undefined' ? window.matchMedia(MOBILE_QUERY) : null;

function useIsMobile(): boolean {
  const subscribe = useCallback((callback: () => void) => {
    mobileMql?.addEventListener('change', callback);
    return () => mobileMql?.removeEventListener('change', callback);
  }, []);

  const getSnapshot = useCallback(() => mobileMql?.matches ?? false, []);

  return useSyncExternalStore(subscribe, getSnapshot);
}

export function App() {
  const dataMode = useStore((s) => s.dataMode);
  const tickSimulation = useStore((s) => s.tickSimulation);
  const initWebSocket = useStore((s) => s.initWebSocket);
  const initWatchlist = useStore((s) => s.initWatchlist);
  const layoutOverride = useStore((s) => s.layoutOverride);
  const viewportIsMobile = useIsMobile();
  // Manual override beats viewport detection so users can force a layout
  // when the browser misreports its viewport (Chrome Android's "Desktop
  // site" / UA-Client-Hints stickiness is the recurring case).
  const isMobile =
    layoutOverride === 'mobile' ? true
    : layoutOverride === 'desktop' ? false
    : viewportIsMobile;

  useEffect(() => {
    if (dataMode === 'live') {
      initWebSocket();
      initWatchlist();
    } else {
      const interval = setInterval(tickSimulation, 2000);
      return () => clearInterval(interval);
    }
  }, [dataMode, tickSimulation, initWebSocket, initWatchlist]);

  if (isMobile) {
    return (
      <>
        <CatastrophicOverlay />
        <ReconnectingBanner />
        <MobileLayout />
      </>
    );
  }

  return (
    <div className="flex flex-col h-screen w-screen overflow-hidden" style={{ background: 'var(--bg-root)' }}>
      <CatastrophicOverlay />
      <ReconnectingBanner />
      <GlobalHeader />
      <WorkstationLayout />
    </div>
  );
}
