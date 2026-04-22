import { useEffect, useRef, useState } from 'react';
import { useStore } from '../../data/store';
import { formatTime } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';

// Event types surfaced in the Bot Activity feed. Anything the engine or
// bot writes to bot_events with one of these types shows up here.
const ACTIVITY_TYPES = [
  'SIGNAL', 'ORDER', 'FILL', 'CLOSED', 'STARTED', 'STOPPED',
  'ERROR', 'RISK', 'REPRICE', 'EXIT_CHECK', 'CANCELLED',
];

const typeStyle: Record<string, { color: string; icon: string }> = {
  SIGNAL: { color: 'var(--accent-green)', icon: '▲' },
  ORDER: { color: 'var(--accent-blue)', icon: '→' },
  REPRICE: { color: 'var(--accent-yellow)', icon: '↻' },
  FILL: { color: 'var(--accent-green)', icon: '✓' },
  CANCELLED: { color: 'var(--text-muted)', icon: '⊘' },
  EXIT_CHECK: { color: 'var(--accent-cyan, var(--accent-blue))', icon: '↘' },
  CLOSED: { color: 'var(--accent-cyan, var(--accent-blue))', icon: '◼' },
  STARTED: { color: 'var(--accent-green)', icon: '●' },
  STOPPED: { color: 'var(--text-muted)', icon: '○' },
  ERROR: { color: 'var(--accent-red)', icon: '✗' },
  RISK: { color: 'var(--accent-red)', icon: '⚠' },
};

// Bot colors for "all" view
const botColors = [
  'var(--accent-cyan, #06b6d4)',
  'var(--accent-purple, #a78bfa)',
  'var(--accent-yellow)',
  'var(--accent-green)',
  'var(--accent-blue)',
];

interface ActivityEntry {
  id: number;
  bot_id?: string;
  bot_name?: string;
  event_type: string;
  message: string;
  payload: string | null;
  trade_serial: number | null;
  recorded_at: string;
}

export function BotActivity({ maxLines = 200 }: { maxLines?: number }) {
  const bots = useStore((s) => s.bots);
  const [entries, setEntries] = useState<ActivityEntry[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);

  const botNameMap = Object.fromEntries(bots.map((b) => [b.id, b.name]));
  const botColorMap = Object.fromEntries(bots.map((b, i) => [b.id, botColors[i % botColors.length]]));

  const botLabel = (botId: string) => {
    const name = botNameMap[botId] || botId.slice(0, 6);
    const parts = name.split('-');
    return parts.length > 1 ? parts[parts.length - 1].toUpperCase() : name.toUpperCase();
  };

  useEffect(() => {
    if (bots.length === 0) return;

    const fetchActivity = () => {
      const perBot = Math.ceil(maxLines / Math.max(bots.length, 1));
      const promises = bots.map((b) =>
        Promise.all(
          ACTIVITY_TYPES.map((t) =>
            fetch(`/api/bots/${b.id}/events?event_type=${t}&limit=${Math.ceil(perBot / ACTIVITY_TYPES.length)}`)
              .then((r) => r.ok ? r.json() : [])
              .then((events: any[]) =>
                events.map((e) => ({ ...e, bot_id: b.id, bot_name: b.name }))
              )
              .catch(() => [] as ActivityEntry[])
          )
        ).then((results) => results.flat())
      );

      Promise.all(promises).then((results) => {
        const merged = results.flat();
        // Deduplicate by id
        const seen = new Set<number>();
        const unique = merged.filter((e) => {
          if (seen.has(e.id)) return false;
          seen.add(e.id);
          return true;
        });
        unique.sort((a, b) => (b.recorded_at || '').localeCompare(a.recorded_at || ''));
        setEntries(unique.slice(0, maxLines));
      });
    };

    fetchActivity();
    const interval = setInterval(fetchActivity, 5000);
    return () => clearInterval(interval);
  }, [bots, maxLines]);

  // Scroll to top (newest first)
  const lastEntryId = entries.length > 0 ? entries[0]?.id : 0;
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [lastEntryId]);

  return (
    <PanelShell title="Bot Activity" accent="green" right={
      <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>{entries.length} events</span>
    }>
      <div ref={scrollRef} className="h-full overflow-y-auto p-1" style={{ fontSize: 15 }}>
        {entries.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', padding: 20, textAlign: 'center' }}>
            No activity yet
          </div>
        ) : entries.map((entry) => {
          const style = typeStyle[entry.event_type] || { color: 'var(--text-secondary)', icon: '·' };
          const ts = entry.recorded_at ? formatTime(entry.recorded_at) : '';

          // Parse P&L from CLOSED events
          let pnlStr = '';
          if (entry.event_type === 'CLOSED' && entry.payload) {
            try {
              const p = typeof entry.payload === 'string' ? JSON.parse(entry.payload) : entry.payload;
              if (p.pnl) {
                const pnl = parseFloat(p.pnl);
                pnlStr = pnl >= 0 ? ` +$${pnl.toFixed(2)}` : ` -$${Math.abs(pnl).toFixed(2)}`;
              }
            } catch {}
          }

          return (
            <div key={entry.id} className="flex items-start gap-2 px-2 py-1.5 border-b"
              style={{ borderColor: 'var(--border-default)' }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--row-hover)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}>

              {/* Icon */}
              <span style={{ color: style.color, fontSize: 15, lineHeight: '20px' }} className="shrink-0">
                {style.icon}
              </span>

              {/* Content */}
              <div className="flex-1 min-w-0">
                <div className="flex items-center gap-2">
                  <span className="font-mono" style={{ fontSize: 13, color: 'var(--text-muted)' }}>{ts}</span>
                  {entry.bot_id && (
                    <span className="font-bold" style={{ fontSize: 11, color: botColorMap[entry.bot_id] || 'var(--text-muted)' }}>
                      {botLabel(entry.bot_id)}
                    </span>
                  )}
                  <span className="font-semibold" style={{ fontSize: 13, color: style.color }}>
                    {entry.event_type}
                  </span>
                  {pnlStr && (
                    <span className="font-mono font-semibold" style={{
                      color: pnlStr.startsWith(' +') ? 'var(--accent-green)' : 'var(--accent-red)',
                    }}>
                      {pnlStr}
                    </span>
                  )}
                </div>
                <div className="mt-0.5 truncate" style={{ color: 'var(--text-primary)' }}>
                  {entry.message}
                </div>
              </div>

              {/* Trade serial badge */}
              {entry.trade_serial != null && entry.trade_serial > 0 && (
                <span className="shrink-0 font-mono px-1 rounded"
                  style={{ fontSize: 11, background: 'var(--bg-root)', color: 'var(--text-muted)', border: '1px solid var(--border-default)' }}>
                  #{entry.trade_serial}
                </span>
              )}
            </div>
          );
        })}
      </div>
    </PanelShell>
  );
}
