import { useCallback, useEffect, useSyncExternalStore } from 'react';
import { GlobalHeader } from '../features/header/GlobalHeader';
import { WorkstationLayout } from '../layout/WorkstationLayout';
import { MobileLayout } from '../layout/MobileLayout';
import { useStore } from '../data/store';

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
  const isMobile = useIsMobile();

  useEffect(() => {
    if (dataMode === 'live') {
      initWebSocket();
    } else {
      const interval = setInterval(tickSimulation, 2000);
      return () => clearInterval(interval);
    }
  }, [dataMode, tickSimulation, initWebSocket]);

  if (isMobile) {
    return <MobileLayout />;
  }

  return (
    <div className="flex flex-col h-screen w-screen overflow-hidden" style={{ background: 'var(--bg-root)' }}>
      <GlobalHeader />
      <WorkstationLayout />
    </div>
  );
}
