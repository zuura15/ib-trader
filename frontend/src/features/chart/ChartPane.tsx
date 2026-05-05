import { useEffect, useRef, useState } from 'react';
import { PanelShell } from '../../components/PanelShell';
import { useStore } from '../../data/store';
import { SymbolChart, type SymbolChartHandle } from './SymbolChart';
import { BAR_SECONDS, VISIBLE_MINUTES } from './chartUtils';

/** Mini countdown chip showing time until the next 3-min bar closes
 *  in M:SS format. Updates every second. Used in the ChartPane
 *  header next to the toolbar buttons. */
function BarCloseCountdown() {
  const [text, setText] = useState<string>('');
  useEffect(() => {
    const tick = () => {
      const nowSec = Math.floor(Date.now() / 1000);
      const remaining = BAR_SECONDS - (nowSec % BAR_SECONDS);
      const mm = Math.floor(remaining / 60);
      const ss = remaining % 60;
      setText(`${mm}:${String(ss).padStart(2, '0')}`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, []);
  return (
    <span
      style={{
        fontVariantNumeric: 'tabular-nums',
        color: 'var(--text-muted)',
        padding: '1px 6px',
        border: '1px solid var(--border-default)',
        borderRadius: 3,
        userSelect: 'none',
      }}
      title="Time until the current 3-min bar closes"
    >
      {text}
    </span>
  );
}

export function ChartPane() {
  const target = useStore((s) => s.selectedChartTarget);
  const chartRef = useRef<SymbolChartHandle>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [fullscreen, setFullscreen] = useState(false);
  // Hide broken/archived S/R lines by default — they pile up at
  // zoom-out and obscure the active structure. Toggle to surface
  // recently-broken lines when needed.
  const [showBrokenSr, setShowBrokenSr] = useState(false);

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
            onClick={() => chartRef.current?.clearSupportResistance()}
            title="Clear auto support/resistance lines (re-detects on symbol change)"
            style={{
              background: 'transparent', border: '1px solid var(--border-default)',
              color: 'var(--text-secondary)', padding: '1px 6px', borderRadius: 3, cursor: 'pointer',
            }}
          >
            Clear S/R
          </button>
          <button
            onClick={() => setShowBrokenSr((v) => !v)}
            title={showBrokenSr
              ? 'Hide recently-broken S/R lines (amber dashed)'
              : 'Show recently-broken S/R lines (amber dashed)'}
            style={{
              background: showBrokenSr ? 'var(--accent-yellow, #f7bd5c)' : 'transparent',
              border: '1px solid var(--border-default)',
              color: showBrokenSr ? '#000' : 'var(--text-secondary)',
              padding: '1px 6px', borderRadius: 3, cursor: 'pointer',
            }}
          >
            {showBrokenSr ? 'Broken: on' : 'Broken: off'}
          </button>
          <BarCloseCountdown />
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
          showBrokenSr={showBrokenSr}
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
