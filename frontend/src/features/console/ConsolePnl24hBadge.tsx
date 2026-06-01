import { useEffect, useState } from 'react';
import { getConsolePnl24h } from '../../api/client';
import { useStore } from '../../data/store';

const REFRESH_MS = 30_000;

function formatPnl(pnl: string): { text: string; positive: boolean | null } {
  const n = Number(pnl);
  if (!Number.isFinite(n)) return { text: '—', positive: null };
  if (n === 0) return { text: '$0.00', positive: null };
  const sign = n >= 0 ? '+' : '-';
  // Always 2 decimals — operator wants cents resolution at all sizes,
  // including the running total once it crosses $100. Thousand
  // separator (toLocaleString) keeps large totals scannable.
  const formatted = Math.abs(n).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
  return { text: `${sign}$${formatted}`, positive: n >= 0 };
}

/**
 * Console panel header badge showing 24h realized P&L from console-initiated
 * closes only — explicit ``close`` verbs and console buy/sell orders that
 * reduce an opposite-side position. Bot-driven closes are excluded.
 *
 * Refreshes every 30 s, and immediately whenever a console command reaches
 * a terminal state (so a close that just printed P&L is reflected without
 * waiting for the timer).
 */
export function ConsolePnl24hBadge() {
  const [pnl, setPnl] = useState<string>('0');
  const [count, setCount] = useState<number>(0);
  const [loaded, setLoaded] = useState(false);
  const commands = useStore(s => s.commands);

  const terminalCount = commands.filter(
    c => c.status === 'success' || c.status === 'failure',
  ).length;

  useEffect(() => {
    let cancelled = false;
    const fetchNow = async () => {
      try {
        const r = await getConsolePnl24h();
        if (cancelled) return;
        setPnl(r.pnl);
        setCount(r.count);
        setLoaded(true);
      } catch {
        // Swallow — leave stale value visible; banner stays muted until
        // the next successful refresh.
      }
    };
    fetchNow();
    const t = setInterval(fetchNow, REFRESH_MS);
    return () => {
      cancelled = true;
      clearInterval(t);
    };
  }, [terminalCount]);

  if (!loaded) {
    return (
      <span
        style={{ fontSize: 11, color: 'var(--text-muted)', marginLeft: 10 }}
        title="24h console-close P&L"
      >
        24h: —
      </span>
    );
  }

  const { text, positive } = formatPnl(pnl);
  const color = positive === null
    ? 'var(--text-muted)'
    : positive
      ? 'var(--accent-green)'
      : 'var(--accent-red)';

  return (
    <span
      style={{
        fontSize: 11,
        marginLeft: 10,
        display: 'inline-flex',
        alignItems: 'baseline',
        gap: 4,
      }}
      title={`Last 24h: ${count} console close${count === 1 ? '' : 's'}`}
    >
      <span style={{ color: 'var(--text-muted)' }}>24h</span>
      <span style={{ color, fontWeight: 600 }}>{text}</span>
    </span>
  );
}
