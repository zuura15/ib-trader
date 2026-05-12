import { useEffect, useRef, useState } from 'react';
import { SymbolChart, type SymbolChartHandle } from '../chart/SymbolChart';
import { BarCloseCountdown } from '../chart/ChartPane';
import { VISIBLE_MINUTES } from '../chart/chartUtils';
import { setUserSetting, useUserSetting } from '../../data/userSettings';
import type { ChartTarget } from '../../data/store';
import { useBotState, type BotPositionState } from '../../data/useBotState';
import { PositionStrip } from './PositionStrip';

interface Props {
  /** Bot identity. Frontend convention: ``chart-bot-<slot>``. */
  botId: string;
  /** Bot's ``ref_id``, falls back to ``botId`` when absent. */
  botRef?: string;
  /** Symbol the bot is configured to trade. Comes from the bot YAML. */
  symbol: string | null;
  /** Security type of the symbol. Drives chart contract resolution. */
  secType: ChartTarget['secType'];
  /** Optional renderer for slot-specific bot controls (Start / Stop /
   *  Re-arm / Force-quit / status chip). The VOC-mirrored toolbar is
   *  built inside BotChart and prepended; ``renderHeader`` runs to the
   *  right of it so the slot label + bot controls don't drift from the
   *  status chip. */
  renderHeader?: (state: BotPositionState) => React.ReactNode;
  /** Optional renderer for the pane's left-hand header label. */
  renderTitle?: (state: BotPositionState) => React.ReactNode;
  /** Layout chrome — set false to drop the panel header entirely
   *  (mobile uses the tab strip as the title). Defaults true. */
  showHeader?: boolean;
}

/**
 * Specialized chart frame for chart_signal bots.
 *
 * Reuses ``SymbolChart`` as the engine and surfaces the same toolbar
 * the view-only ``ChartPane`` exposes — ``90m`` reset, ``Clear S/R``,
 * the ``SR filters`` popover (Broken on/off + minutes, counter-trend
 * toggles), the ``BarCloseCountdown`` chip, and a fullscreen toggle.
 * Bot-specific controls (Start / Stop / Re-arm / Force-quit, status
 * chip) live to the right of the VOC toolbar via ``renderHeader``.
 *
 * The bot-aware overlays are:
 * - When a position is held, the bot's frozen entry support line is
 *   drawn in distinct orange on top of the auto-detected SR overlay.
 * - The B/S badge corresponding to the bot's entry bar gets a green
 *   ring drawn behind it.
 * - The bottom 28px strip shows entry / line / qty / live P/L.
 *
 * Reused by both the desktop ``ChartBotPane`` and the mobile Charts
 * tab.
 */
export function BotChart({
  botId, botRef, symbol, secType,
  renderHeader, renderTitle, showHeader = true,
}: Props) {
  const [state, setState] = useState<BotPositionState>({});
  const chartRef = useRef<SymbolChartHandle>(null);
  // Mirror the chart's SR-hidden state so the toolbar can show the
  // inverse toggle label. The chart's ref holds the source of truth;
  // we bump on click.
  const [srHidden, setSrHidden] = useState(false);

  // SR-line filters — same defaults as ChartPane. ``brokenMinutes``
  // is the global user setting (synced with the Settings modal); the
  // other two are local to this pane.
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
  const [fullscreen, setFullscreen] = useState(false);

  // Esc closes fullscreen and the filter popover (matches ChartPane).
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

  // Click-outside closes the filter popover (matches ChartPane).
  const filtersWrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!filtersOpen) return;
    const onClick = (e: MouseEvent) => {
      if (filtersWrapRef.current
          && !filtersWrapRef.current.contains(e.target as Node)) {
        setFiltersOpen(false);
      }
    };
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

  useBotState(symbol ?? '', botRef ?? botId, {
    subscribeBot: true,
    onBotState: (s) => setState(s),
  });

  const fsmState = (state.state as string | undefined) ?? 'UNKNOWN';
  const target: ChartTarget | null = symbol
    ? { symbol, secType, conId: null }
    : null;

  const entryLine = state.entry_line && state.entry_line.anchor_time
    && typeof state.entry_line.anchor_price === 'number'
    && typeof state.entry_line.slope_per_sec === 'number'
    ? {
        anchorTime: state.entry_line.anchor_time,
        anchorPrice: state.entry_line.anchor_price,
        slopePerSec: state.entry_line.slope_per_sec,
        // Q timestamp — the chart clips the line render to start here
        // instead of projecting backward across the full visible
        // window (which historically looked like a long-running trend
        // the bot never validated).
        fromTime: state.entry_line.from_time,
      }
    : null;

  // Surface the bot's active entry to the chart. We also surface it
  // while the entry order is in flight (``ENTRY_ORDER_PLACED``) so the
  // user sees the badge the moment the bot acts, not after the fill
  // lands — fixes "bot placed an order but the chart shows no B/S"
  // (2026-05-11).
  const activeEntryBarTime = state.entry_bar_time
    && (fsmState === 'ENTRY_ORDER_PLACED'
        || fsmState === 'AWAITING_EXIT_TRIGGER'
        || fsmState === 'EXIT_ORDER_PLACED')
    ? state.entry_bar_time as string
    : null;
  // Direction drives whether the chart shows a B (long) or S (short)
  // when synthetically injecting the badge for a bot-fired signal
  // that the chart's own detection missed. Prefer the runtime-set
  // ``position_direction`` (LONG/SHORT) over ``entry_line.direction``
  // (long/short). Bail to null when neither is present — the prior
  // default-to-long silently rendered green B badges over short
  // entries on MNQ (2026-05-11).
  let entrySide: 'long' | 'short' | null = null;
  if (activeEntryBarTime) {
    if (state.position_direction === 'SHORT') entrySide = 'short';
    else if (state.position_direction === 'LONG') entrySide = 'long';
    else if (state.entry_line?.direction === 'short') entrySide = 'short';
    else if (state.entry_line?.direction === 'long') entrySide = 'long';
    // else leave null — caller skips badge injection
  }

  // VOC-mirrored toolbar fragment. Rendered both in the inline header
  // and in the fullscreen header so the controls follow the chart.
  const vocToolbar = (
    <div className="flex items-center gap-2" style={{ fontSize: 10, color: 'var(--text-muted)' }}>
      <button
        onClick={() => chartRef.current?.resetZoom()}
        title={`Reset to last ${VISIBLE_MINUTES}m`}
        style={{
          background: 'transparent', border: '1px solid var(--border-default)',
          color: 'var(--text-secondary)', padding: '1px 6px',
          borderRadius: 3, cursor: 'pointer',
        }}
      >
        {VISIBLE_MINUTES}m
      </button>
      <button
        onClick={() => {
          if (srHidden) {
            chartRef.current?.showSupportResistance();
            setSrHidden(false);
          } else {
            chartRef.current?.clearSupportResistance();
            setSrHidden(true);
          }
        }}
        title={srHidden
          ? 'Re-enable auto support/resistance overlay'
          : 'Hide auto support/resistance lines for this pane'}
        style={{
          background: srHidden ? 'var(--accent-yellow, #f7bd5c)' : 'transparent',
          border: '1px solid var(--border-default)',
          color: srHidden ? '#000' : 'var(--text-secondary)',
          padding: '1px 6px',
          borderRadius: 3, cursor: 'pointer',
        }}
      >
        {srHidden ? 'Show S/R' : 'Clear S/R'}
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
          color: 'var(--text-secondary)', padding: '1px 6px',
          borderRadius: 3, cursor: 'pointer',
        }}
      >
        {fullscreen ? '×' : '⛶'}
      </button>
    </div>
  );

  const headerRow = (
    <div
      style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '4px 8px',
        borderBottom: '1px solid var(--border-default)',
        background: 'var(--bg-primary)',
        flexShrink: 0,
        minHeight: 28,
      }}
    >
      <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-primary)' }}>
        {renderTitle ? renderTitle(state) : (symbol ?? '—')}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        {vocToolbar}
        {renderHeader && <div>{renderHeader(state)}</div>}
      </div>
    </div>
  );

  const chartBody = (
    <div className="flex flex-col h-full" style={{ minHeight: 0 }}>
      <div className="flex-1" style={{ minHeight: 0 }}>
        <SymbolChart
          ref={chartRef}
          target={target}
          enableSr
          showBrokenSr={showBrokenSr}
          brokenMinutes={brokenMinutes}
          showCounterSupport={showCounterSupport}
          showCounterResistance={showCounterResistance}
          entryLine={entryLine}
          activeEntryBarTime={activeEntryBarTime}
          entrySide={entrySide}
          placeholder={symbol ? null : 'No bot bound to this slot.'}
        />
      </div>
      <PositionStrip state={state} fsmState={fsmState} />
    </div>
  );

  if (fullscreen) {
    // Fullscreen overlay — covers the viewport. Header is the same
    // headerRow so the toolbar follows the chart.
    return (
      <>
        <div className="flex flex-col h-full" style={{ minHeight: 0 }}>
          {showHeader && headerRow}
          <div
            className="flex-1 flex items-center justify-center text-xs"
            style={{ color: 'var(--text-muted)' }}
          >
            Chart open in fullscreen — press × or Esc to return.
          </div>
        </div>
        <div
          style={{
            position: 'fixed', inset: 0, zIndex: 9999,
            background: 'var(--panel-bg, #fff)',
            display: 'flex', flexDirection: 'column',
          }}
        >
          {headerRow}
          {chartBody}
        </div>
      </>
    );
  }

  return (
    <div className="flex flex-col h-full" style={{ minHeight: 0 }}>
      {showHeader && headerRow}
      {chartBody}
    </div>
  );
}

export type { Props as BotChartProps };
