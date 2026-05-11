import type { BotPositionState } from '../../data/useBotState';

interface Props {
  state: BotPositionState;
  fsmState: string | undefined;
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
export function PositionStrip({ state, fsmState }: Props) {
  const lineNow = computeLineNow(state.entry_line ?? null);
  const inPosition = fsmState === 'AWAITING_EXIT_TRIGGER'
    || fsmState === 'EXIT_ORDER_PLACED';

  const cells: { label: string; value: string; tone?: 'pos' | 'neg' }[] = [
    {
      label: 'Entry',
      value: state.entry_price ? fmt(state.entry_price) : '—',
    },
    { label: '@', value: fmtTime(state.entry_time) },
    {
      label: 'Stop line',
      value: lineNow == null ? '—' : fmt(lineNow),
    },
    { label: 'Last', value: fmt(state.last_price) },
    { label: 'Qty', value: state.qty ?? '—' },
  ];

  const pnl = state.unrealized_pnl ? Number(state.unrealized_pnl) : NaN;
  if (Number.isFinite(pnl)) {
    cells.push({
      label: 'P/L',
      value: (pnl >= 0 ? '+' : '') + pnl.toFixed(2),
      tone: pnl >= 0 ? 'pos' : 'neg',
    });
  } else {
    cells.push({ label: 'P/L', value: '—' });
  }

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
          ? 'var(--bg-secondary, rgba(80, 200, 120, 0.06))'
          : 'var(--bg-primary)',
        borderTop: '1px solid var(--border-default)',
        fontSize: 11,
        fontVariantNumeric: 'tabular-nums',
        color: 'var(--text-secondary)',
      }}
    >
      {cells.map(({ label, value, tone }) => (
        <span key={label} style={{ display: 'inline-flex', gap: 4 }}>
          <span style={{ color: 'var(--text-muted)' }}>{label}</span>
          <span style={{
            color: tone === 'pos' ? 'var(--accent-green)'
                 : tone === 'neg' ? 'var(--accent-red)'
                 : 'var(--text-primary)',
            fontWeight: 600,
          }}>{value}</span>
        </span>
      ))}
    </div>
  );
}
