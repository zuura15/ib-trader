import { useEffect, useState } from 'react';
import type { BotPositionState } from '../../data/useBotState';
import { getBotTrades } from '../../api/client';

interface Props {
  state: BotPositionState;
  fsmState: string | undefined;
  /** Bot id used to fetch the 24h closed-trade P/L on P/L hover.
   *  Optional so the strip stays usable in contexts without a bot
   *  bound (e.g. demo / placeholder panes). */
  botId?: string;
  /** Symbol the bot trades. Used to look up the contract multiplier
   *  for the PL-at-stop calc (stops are price-based; converting to
   *  dollars needs the $/point multiplier). */
  symbol?: string | null;
}

/** $-per-point multiplier for the futures contracts this app trades.
 *  Stock and unknown symbols fall back to 1. Keep in lockstep with
 *  ``config/bots/chart-bot-*.yaml:contract_multiplier``. */
const SYMBOL_MULTIPLIERS: Record<string, number> = {
  MGC: 10,   // micro gold,    10 oz/contract
  MCL: 100,  // micro crude,  100 bbl/contract
  MES: 5,    // micro S&P 500, $5/index point
  MNQ: 2,    // micro Nasdaq,  $2/index point
  GC: 100,   // full gold
  CL: 1000,  // full crude
  ES: 50,    // full S&P
  NQ: 20,    // full Nasdaq
};

function contractMultiplier(symbol: string | undefined | null): number {
  if (!symbol) return 1;
  const root = symbol.toUpperCase().slice(0, 3);
  return SYMBOL_MULTIPLIERS[root] ?? 1;
}

function fmt(n: string | number | undefined | null, digits = 2): string {
  if (n == null || n === '') return '—';
  const v = typeof n === 'string' ? Number(n) : n;
  if (!Number.isFinite(v)) return '—';
  return v.toFixed(digits);
}

function fmtTime(iso: string | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (!Number.isFinite(d.getTime())) return '—';
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

function computeLineNow(line: BotPositionState['entry_line']): number | null {
  if (!line || typeof line.anchor_price !== 'number'
      || typeof line.slope_per_sec !== 'number'
      || !line.anchor_time) return null;
  const anchor = new Date(line.anchor_time).getTime() / 1000;
  if (!Number.isFinite(anchor)) return null;
  const now = Date.now() / 1000;
  return line.anchor_price + line.slope_per_sec * (now - anchor);
}

/**
 * Bottom 28px strip on a chart-bot pane. Mirrors the bot's Redis FSM
 * doc one-to-one — entry price, line value at current wall-clock,
 * qty, last price, unrealized P/L. Uses ``"—"`` placeholders per
 * CLAUDE.md when a field is absent.
 */
/** Window for the 24h P/L badge next to the live P/L cell. */
const PNL_WINDOW_MS = 24 * 60 * 60 * 1000;
/** Poll cadence for the 24h roll-up. Closed trades only land on
 *  round-trip — re-checking every 30s is plenty. */
const PNL_POLL_MS = 30_000;

export function PositionStrip({ state, fsmState, botId, symbol }: Props) {
  // Rolling 24h realized P/L (sum of bot's closed trades whose
  // exit_time is within the window). Null while the first fetch is
  // pending; number once we have data.
  const [pnl24h, setPnl24h] = useState<number | null>(null);
  const [pnl24hCount, setPnl24hCount] = useState<number>(0);

  useEffect(() => {
    if (!botId) return;
    let cancelled = false;
    const tick = async () => {
      try {
        const trades = await getBotTrades(botId, 500);
        if (cancelled) return;
        const cutoff = Date.now() - PNL_WINDOW_MS;
        let sum = 0;
        let n = 0;
        for (const t of trades) {
          if (!t.exit_time || t.realized_pnl == null) continue;
          const exitMs = new Date(t.exit_time).getTime();
          if (!Number.isFinite(exitMs) || exitMs < cutoff) continue;
          const v = Number(t.realized_pnl);
          if (!Number.isFinite(v)) continue;
          sum += v;
          n += 1;
        }
        setPnl24h(sum);
        setPnl24hCount(n);
      } catch {
        // Keep the last good value visible on a transient error.
      }
    };
    tick();
    const id = window.setInterval(tick, PNL_POLL_MS);
    return () => { cancelled = true; window.clearInterval(id); };
  }, [botId]);

  const lineNow = computeLineNow(state.entry_line ?? null);
  const inPosition = fsmState === 'AWAITING_EXIT_TRIGGER'
    || fsmState === 'EXIT_ORDER_PLACED';

  // Stop value the bot would actually act on. ``active_stop`` is
  // bot-authoritative = max/min of line and trail per direction.
  // We deliberately do NOT fall back to ``lineNow`` here — that's
  // a live projection of the entry line at the current wallclock,
  // which drifts up/down on every tick. Between entry fill and the
  // first EXIT_CHECK (next bar close, up to ~3 min away) the bot
  // has not yet locked an ``active_stop``; showing the moving
  // projection caused pnl_at_stop to drift in the first bar before
  // settling. ``—`` here is honest: the bot's stop is genuinely
  // pending the first exit eval.
  const stopNum: number | null =
    state.active_stop != null && state.active_stop !== ''
      ? Number(state.active_stop)
      : null;
  const stopValue: string = stopNum == null || !Number.isFinite(stopNum)
    ? '—' : stopNum.toFixed(2);

  // Truthy guard dropped numeric ``0`` and string ``""`` to ``NaN`` and
  // rendered "—" instead of "+0.00" — explicitly null-check so a flat
  // round-trip shows P/L correctly.
  const pnl = state.unrealized_pnl != null && state.unrealized_pnl !== ''
    ? Number(state.unrealized_pnl)
    : NaN;
  const entryNum = state.entry_price != null && state.entry_price !== ''
    ? Number(state.entry_price) : NaN;
  const lastNum = state.last_price != null && state.last_price !== ''
    ? Number(state.last_price) : NaN;

  // PL-at-stop = (stop − entry) × sign × qty × multiplier.
  // Direct calc: doesn't depend on the live ``pnl`` ratio, so a
  // fixed stop price gives a fixed dollar outcome regardless of
  // how far the last price has drifted toward (or away from) it.
  // The earlier ratio-based derivation drifted with every tick.
  const qtyNum = Number(state.qty ?? 1) || 1;
  const dirSign =
    String(state.position_direction || '').toUpperCase() === 'SHORT'
      ? -1
      : 1;
  const mult = contractMultiplier(symbol);
  let pnlAtStop: number | null = null;
  if (
    Number.isFinite(entryNum) && stopNum != null
    && Number.isFinite(stopNum) && Number.isFinite(pnl)
  ) {
    pnlAtStop = (stopNum - entryNum) * dirSign * qtyNum * mult;
  }

  const pnlValue = Number.isFinite(pnl)
    ? (pnl >= 0 ? '+$' : '-$') + Math.abs(pnl).toFixed(2)
    : '—';
  const pnlTone: 'pos' | 'neg' | undefined = Number.isFinite(pnl)
    ? (pnl >= 0 ? 'pos' : 'neg') : undefined;

  const entryValue = state.entry_price ? fmt(state.entry_price) : '—';
  const qtyStr = state.qty != null && state.qty !== '' ? String(state.qty) : null;
  const entryWithQty = qtyStr != null
    ? `${entryValue} (${qtyStr})`
    : entryValue;
  type Cell = { label: string; value: string; tone?: 'pos' | 'neg' };
  // Direction-aware water-mark label: HWM for longs, LWM for shorts.
  // ``state.high_water_mark`` is set on long, ``low_water_mark`` on
  // short — the previous round-trip's stale values are wiped via
  // ``clear_position_fields`` so this reads cleanly between trades.
  const dirUpper = String(state.position_direction || '').toUpperCase();
  const wmLabel = dirUpper === 'SHORT' ? 'LWM' : 'HWM';
  const wmRaw = dirUpper === 'SHORT'
    ? state.low_water_mark
    : state.high_water_mark;
  const wmValue = wmRaw == null || wmRaw === '' ? '—' : fmt(wmRaw);

  // Row 1: identity + price snapshot + risk levels (Stop, HWM).
  // Row 2: P/L only — carries the @stop and 24 h brackets.
  const row1: Cell[] = [
    { label: '@', value: fmtTime(state.entry_time) },
    { label: 'Entry', value: entryWithQty },
    { label: 'Last', value: fmt(state.last_price) },
    { label: 'Stop', value: stopValue },
    { label: wmLabel, value: wmValue },
  ];
  const row2: Cell[] = [
    { label: 'P/L', value: pnlValue, tone: pnlTone },
  ];

  const renderCell = ({ label, value, tone }: Cell) => {
    const isPnl = label === 'P/L';
    return (
      <span key={label} style={{ display: 'inline-flex', gap: 4 }}>
        <span style={{ color: 'var(--text-muted)' }}>{label}</span>
        <span style={{
          color: tone === 'pos' ? 'var(--accent-green)'
               : tone === 'neg' ? 'var(--accent-red)'
               : 'var(--text-primary)',
          fontWeight: 600,
        }}>{value}</span>
        {isPnl && pnlAtStop != null && (
          <span style={{ color: 'var(--text-muted)' }}>
            (
            <span style={{
              color: pnlAtStop >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
              fontWeight: 600,
            }}>
              {pnlAtStop >= 0 ? '+$' : '-$'}{Math.abs(pnlAtStop).toFixed(2)}
            </span>
            <span>&nbsp;@stop</span>)
          </span>
        )}
        {isPnl && botId && pnl24h != null && (
          <span style={{ color: 'var(--text-muted)' }}>
            (24h:&nbsp;
            <span style={{
              color: pnl24h >= 0 ? 'var(--accent-green)' : 'var(--accent-red)',
              fontWeight: 600,
            }}>
              {pnl24h >= 0 ? '+$' : '-$'}{Math.abs(pnl24h).toFixed(2)}
            </span>
            <span>&nbsp;/ {pnl24hCount}</span>)
          </span>
        )}
      </span>
    );
  };

  return (
    <div
      style={{
        flexShrink: 0,
        // Just-enough height for two rows of 14 px text with a bit
        // of padding — the strip stops eating into the chart canvas
        // while still giving the operator a legible two-line panel.
        height: 52,
        display: 'flex',
        flexDirection: 'column',
        justifyContent: 'center',
        gap: 4,
        padding: '4px 10px',
        // Slightly darker than the chart pane so the metrics strip
        // reads as a distinct shelf below the chart. Holds the same
        // hue when the bot is in position (blue) vs neutral (grey)
        // so the active-bot signal still pops.
        background: inPosition
          ? 'rgba(59,130,246,0.18)'
          : 'rgba(0,0,0,0.05)',
        borderTop: '1px solid var(--border-default)',
        // 14 px — bigger than the rest of the UI's small chrome on
        // purpose (operator wants the live metrics legible at a
        // glance) but still proportional to the two-row strip.
        fontSize: 14,
        fontVariantNumeric: 'tabular-nums',
        color: 'var(--text-secondary)',
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        {row1.map(renderCell)}
      </div>
      <div style={{ display: 'flex', alignItems: 'center', gap: 14 }}>
        {row2.map(renderCell)}
      </div>
    </div>
  );
}
