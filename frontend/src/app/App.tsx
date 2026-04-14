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
      padding: '2rem',
    }}>
      <div style={{
        background: 'var(--bg-surface, #1a1a2e)',
        border: '2px solid var(--accent-red, #ef4444)',
        borderRadius: '12px',
        padding: '2rem 2.5rem',
        maxWidth: '520px',
        width: '100%',
        textAlign: 'center',
      }}>
        <div style={{
          fontSize: '2.5rem',
          marginBottom: '0.75rem',
        }}>
          &#9888;
        </div>
        <h2 style={{
          color: 'var(--accent-red, #ef4444)',
          fontSize: '1.25rem',
          fontWeight: 700,
          marginBottom: '1rem',
          letterSpacing: '0.02em',
        }}>
          SYSTEM ALERT
        </h2>
        {active.map((a) => (
          <div key={a.id} style={{ marginBottom: '1rem' }}>
            <div style={{
              color: 'var(--text-primary, #e2e8f0)',
              fontSize: '0.95rem',
              fontWeight: 600,
              marginBottom: '0.35rem',
            }}>
              {a.title}
            </div>
            <div style={{
              color: 'var(--text-muted, #94a3b8)',
              fontSize: '0.85rem',
              lineHeight: 1.5,
            }}>
              {a.message}
            </div>
          </div>
        ))}
        <button
          onClick={() => active.forEach((a) => dismissAlert(a.id))}
          style={{
            marginTop: '1rem',
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
          Acknowledge
        </button>
      </div>
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
  const isMobile = useIsMobile();

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
        <MobileLayout />
      </>
    );
  }

  return (
    <div className="flex flex-col h-screen w-screen overflow-hidden" style={{ background: 'var(--bg-root)' }}>
      <CatastrophicOverlay />
      <GlobalHeader />
      <WorkstationLayout />
    </div>
  );
}
