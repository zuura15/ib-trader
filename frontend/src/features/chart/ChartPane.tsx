import { useEffect, useRef, useState } from 'react';
import { PanelShell } from '../../components/PanelShell';
import { useStore } from '../../data/store';
import { SymbolChart, type SymbolChartHandle } from './SymbolChart';
import { BAR_SECONDS, VISIBLE_MINUTES } from './chartUtils';
import { setUserSetting, useUserSetting } from '../../data/userSettings';

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
    let id: ReturnType<typeof setInterval> | null = null;
    const start = () => { if (id == null) { tick(); id = setInterval(tick, 1000); } };
    const stop = () => { if (id != null) { clearInterval(id); id = null; } };
    if (!document.hidden) start();
    const onVis = () => { document.hidden ? stop() : start(); };
    document.addEventListener('visibilitychange', onVis);
    return () => {
      stop();
      document.removeEventListener('visibilitychange', onVis);
    };
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
  // SR-line filters. All three booleans are hidden by default to keep
  // the active "with-trend" structure (uptrending supports +
  // downtrending resistances) front-and-centre; toggle to surface
  // counter-trend or recently-broken lines when needed.
  // ``brokenMinutes`` lives in the global user-settings store so the
  // popover spinner here and the Settings modal stay in sync.
  const [showBrokenSr, setShowBrokenSr] = useState(false);
  const brokenMinutes = useUserSetting('brokenLookbackMinutes');
  const setBrokenMinutes = (n: number) => {
    setUserSetting(
      'brokenLookbackMinutes',
      Math.max(3, Math.min(720, Math.round(n))),
    );
  };
  const [showCounterSupport, setShowCounterSupport] = useState(false);
  const [showCounterResistance, setShowCounterResistance] = useState(false);
  const [filtersOpen, setFiltersOpen] = useState(false);

  // Esc closes fullscreen and the filter popover.
  useEffect(() => {
    if (!fullscreen && !filtersOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (filtersOpen) setFiltersOpen(false);
      else if (fullscreen) setFullscreen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [fullscreen, filtersOpen]);

  // Click-outside closes the filter popover.
  const filtersWrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!filtersOpen) return;
    const onClick = (e: MouseEvent) => {
      if (filtersWrapRef.current && !filtersWrapRef.current.contains(e.target as Node)) {
        setFiltersOpen(false);
      }
    };
    // Defer registration so the click that opened the popover doesn't
    // immediately close it.
    const id = setTimeout(() => {
      document.addEventListener('mousedown', onClick);
    }, 0);
    return () => {
      clearTimeout(id);
      document.removeEventListener('mousedown', onClick);
    };
  }, [filtersOpen]);

  const activeFilterCount = (showBrokenSr ? 1 : 0)
    + (showCounterSupport ? 1 : 0)
    + (showCounterResistance ? 1 : 0);

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
          <div ref={filtersWrapRef} style={{ position: 'relative' }}>
            <button
              onClick={() => setFiltersOpen((v) => !v)}
              title="S/R line filters"
              style={{
                background: activeFilterCount > 0
                  ? 'var(--accent-yellow, #f7bd5c)' : 'transparent',
                border: '1px solid var(--border-default)',
                color: activeFilterCount > 0 ? '#000' : 'var(--text-secondary)',
                padding: '1px 6px', borderRadius: 3, cursor: 'pointer',
              }}
            >
              SR filters{activeFilterCount > 0 ? ` (${activeFilterCount})` : ''}
            </button>
            {filtersOpen && (
              <div
                style={{
                  position: 'absolute', top: 'calc(100% + 4px)', right: 0,
                  zIndex: 1000, minWidth: 220,
                  background: 'var(--panel-bg, #fff)',
                  border: '1px solid var(--border-default)',
                  borderRadius: 4, padding: '6px 8px',
                  boxShadow: '0 4px 12px rgba(0,0,0,0.08)',
                  fontSize: 11, color: 'var(--text-primary)',
                }}
              >
                <div style={{ fontSize: 10, color: 'var(--text-muted)', marginBottom: 4 }}>
                  Show extra S/R lines:
                </div>
                <label
                  style={{
                    display: 'flex', alignItems: 'center', gap: 6,
                    padding: '3px 0', cursor: 'pointer',
                  }}
                >
                  <input
                    type="checkbox"
                    checked={showBrokenSr}
                    onChange={(e) => setShowBrokenSr(e.target.checked)}
                    style={{ cursor: 'pointer' }}
                  />
                  <span style={{ flex: 1 }}>Broken (amber dashed)</span>
                  <input
                    type="number"
                    min={3}
                    max={720}
                    step={3}
                    value={brokenMinutes}
                    disabled={!showBrokenSr}
                    onChange={(e) => {
                      const n = parseInt(e.target.value, 10);
                      if (Number.isFinite(n)) setBrokenMinutes(n);
                    }}
                    onClick={(e) => e.stopPropagation()}
                    title="Minutes back to keep broken lines visible (saved globally)"
                    style={{
                      width: 56, fontSize: 11,
                      padding: '1px 3px',
                      border: '1px solid var(--border-default)',
                      borderRadius: 3,
                      background: showBrokenSr ? 'var(--panel-bg, #fff)' : 'transparent',
                      color: showBrokenSr ? 'var(--text-primary)' : 'var(--text-muted)',
                      fontVariantNumeric: 'tabular-nums',
                    }}
                  />
                  <span
                    style={{
                      fontSize: 10,
                      color: showBrokenSr ? 'var(--text-secondary)' : 'var(--text-muted)',
                    }}
                  >
                    min
                  </span>
                </label>
                {([
                  ['Counter-trend support (down-sloping)', showCounterSupport, setShowCounterSupport],
                  ['Counter-trend resistance (up-sloping)', showCounterResistance, setShowCounterResistance],
                ] as const).map(([label, val, set]) => (
                  <label
                    key={label}
                    style={{
                      display: 'flex', alignItems: 'center', gap: 6,
                      padding: '3px 0', cursor: 'pointer',
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={val}
                      onChange={(e) => set(e.target.checked)}
                      style={{ cursor: 'pointer' }}
                    />
                    <span>{label}</span>
                  </label>
                ))}
              </div>
            )}
          </div>
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
          brokenMinutes={brokenMinutes}
          showCounterSupport={showCounterSupport}
          showCounterResistance={showCounterResistance}
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
