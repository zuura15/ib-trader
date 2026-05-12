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

export function PositionStrip({ state, fsmState, botId }: Props) {
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
  // Falls back to the projected line for legacy state docs.
  const stopNum: number | null =
    state.active_stop != null && state.active_stop !== ''
      ? Number(state.active_stop)
      : (lineNow ?? null);
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

  // PL-at-stop = pnl × (stop − entry) / (last − entry).
  // multiplier × qty × sign cancels out, so the derivation works
  // without knowing the contract multiplier. Falls back to '—' early
  // in a trade when last == entry (denominator is zero / pnl is 0).
  let pnlAtStop: number | null = null;
  if (
    Number.isFinite(pnl) && Number.isFinite(entryNum)
    && Number.isFinite(lastNum) && stopNum != null
    && Number.isFinite(stopNum) && lastNum !== entryNum
  ) {
    pnlAtStop = pnl * (stopNum - entryNum) / (lastNum - entryNum);
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
  const cells: { label: string; value: string; tone?: 'pos' | 'neg' }[] = [
    { label: '@', value: fmtTime(state.entry_time) },
    { label: 'Entry', value: entryWithQty },
    { label: 'Last', value: fmt(state.last_price) },
    { label: 'Stop', value: stopValue },
    { label: 'P/L', value: pnlValue, tone: pnlTone },
  ];

  return (
    <div
      style={{
        flexShrink: 0,
        height: 28,
        display: 'flex',
        alignItems: 'center',
        gap: 14,
        padding: '0 10px',
        background: inPosition
          ? 'rgba(59,130,246,0.10)'
          : 'var(--bg-primary)',
        borderTop: '1px solid var(--border-default)',
        fontSize: 11,
        fontVariantNumeric: 'tabular-nums',
        color: 'var(--text-secondary)',
      }}
    >
      {cells.map(({ label, value, tone }) => {
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
      })}
    </div>
  );
}
