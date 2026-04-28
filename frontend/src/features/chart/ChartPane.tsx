import { useEffect, useRef, useState } from 'react';
import { PanelShell } from '../../components/PanelShell';
import { useStore } from '../../data/store';
import { SymbolChart, type SymbolChartHandle } from './SymbolChart';
import { VISIBLE_MINUTES } from './chartUtils';

export function ChartPane() {
  const target = useStore((s) => s.selectedChartTarget);
  const chartRef = useRef<SymbolChartHandle>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);

  // Esc closes fullscreen.
  useEffect(() => {
    if (!fullscreen) return;
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setFullscreen(false); };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [fullscreen]);

  const headerLabel = target ? `${target.symbol} · ${target.secType}` : 'Chart';
  const right = (
    <div className="flex items-center gap-2" style={{ fontSize: 10, color: 'var(--text-muted)' }}>
      {loading && <span>loading…</span>}
      {target && (
        <>
          <button
            onClick={() => chartRef.current?.resetZoom()}
            title={`Reset to last ${VISIBLE_MINUTES}m`}
            style={{
              background: 'transparent', border: '1px solid var(--border-default)',
              color: 'var(--text-secondary)', padding: '1px 6px', borderRadius: 3, cursor: 'pointer',
            }}
          >
            {VISIBLE_MINUTES}m
          </button>
          <button
            onClick={() => setFullscreen((v) => !v)}
            title={fullscreen ? 'Exit fullscreen (Esc)' : 'Open fullscreen'}
            style={{
              background: 'transparent', border: '1px solid var(--border-default)',
              color: 'var(--text-secondary)', padding: '1px 6px', borderRadius: 3, cursor: 'pointer',
            }}
          >
            {fullscreen ? '×' : '⛶'}
          </button>
        </>
      )}
    </div>
  );

  const body = (
    <div className="flex flex-col h-full" style={{ minHeight: 0 }}>
      {error && (
        <div className="text-xs px-2 py-1" style={{ color: 'var(--accent-red)' }}>
          {error}
        </div>
      )}
      <div className="flex-1" style={{ minHeight: 0 }}>
        <SymbolChart
          ref={chartRef}
          target={target}
          onLoadingChange={setLoading}
          onError={setError}
        />
      </div>
    </div>
  );

  if (fullscreen) {
    return (
      <>
        <PanelShell title={headerLabel} right={right}>
          <div
            className="flex-1 flex items-center justify-center text-xs"
            style={{ color: 'var(--text-muted)' }}
          >
            Chart open in fullscreen — press × or Esc to return.
          </div>
        </PanelShell>
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 9999,
            background: 'var(--panel-bg, #fff)',
            display: 'flex', flexDirection: 'column',
          }}
        >
          <div
            style={{
              padding: '8px 12px', borderBottom: '1px solid var(--border-default)',
              display: 'flex', alignItems: 'center', justifyContent: 'space-between',
            }}
          >
            <span style={{ fontSize: 13, fontWeight: 600, color: 'var(--text-primary)' }}>
              {headerLabel}
            </span>
            {right}
          </div>
          {body}
        </div>
      </>
    );
  }

  return (
    <PanelShell title={headerLabel} right={right}>
      {body}
    </PanelShell>
  );
}
