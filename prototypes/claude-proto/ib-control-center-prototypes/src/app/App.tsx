import { useEffect } from 'react';
import { GlobalHeader } from '../features/header/GlobalHeader';
import { WorkstationLayout } from '../layout/WorkstationLayout';
import { useStore } from '../data/store';

export function App() {
  const dataMode = useStore((s) => s.dataMode);
  const tickSimulation = useStore((s) => s.tickSimulation);
  const initWebSocket = useStore((s) => s.initWebSocket);

  useEffect(() => {
    if (dataMode === 'live') {
      // Connect to API WebSocket for real-time data
      initWebSocket();
    } else {
      // Mock mode — run simulation tick every 2s
      const interval = setInterval(tickSimulation, 2000);
      return () => clearInterval(interval);
    }
  }, [dataMode, tickSimulation, initWebSocket]);

  return (
    <div className="flex flex-col h-screen w-screen overflow-hidden" style={{ background: 'var(--bg-root)' }}>
      <GlobalHeader />
      <WorkstationLayout />
    </div>
  );
}
