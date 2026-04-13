import { useEffect, useRef, useState } from 'react';
import { useStore } from '../../data/store';
import { formatTime } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';

const typeColor: Record<string, string> = {
  BAR: 'var(--text-muted)',
  EVAL: 'var(--text-secondary)',
  SKIP: 'var(--accent-yellow)',
  SIGNAL: 'var(--accent-green)',
  ORDER: 'var(--accent-blue)',
  FILL: 'var(--accent-green)',
  STATE: 'var(--text-secondary)',
  EXIT_CHECK: 'var(--text-muted)',
  CLOSED: 'var(--accent-cyan, var(--accent-blue))',
  RISK: 'var(--accent-red)',
  ERROR: 'var(--accent-red)',
  STARTED: 'var(--accent-green)',
  STOPPED: 'var(--text-muted)',
  ACTION: 'var(--accent-blue)',
  HEARTBEAT: 'var(--text-muted)',
};

const typeLabel: Record<string, string> = {
  BAR: 'BAR',
  EVAL: 'EVAL',
  SKIP: 'SKIP',
  SIGNAL: 'SIG',
  ORDER: 'ORD',
  FILL: 'FILL',
  STATE: 'STA',
  EXIT_CHECK: 'EXIT',
  CLOSED: 'CLSD',
  RISK: 'RISK',
  ERROR: 'ERR',
  STARTED: 'START',
  STOPPED: 'STOP',
  ACTION: 'ACT',
  HEARTBEAT: 'HB',
};

interface BotLogEntry {
  id: number;
  bot_id?: string;
  bot_name?: string;
  event_type: string;
  message: string;
  payload: string | null;
  trade_serial: number | null;
  recorded_at: string;
}

// Short color per bot for the badge
const botColors = [
  'var(--accent-cyan, #06b6d4)',
  'var(--accent-purple, #a78bfa)',
  'var(--accent-yellow)',
  'var(--accent-green)',
  'var(--accent-blue)',
];

export function BotLogStream({ maxLines = 300 }: { maxLines?: number }) {
  const bots = useStore((s) => s.bots);
  const [logs, setLogs] = useState<BotLogEntry[]>([]);
  const [selectedBotId, setSelectedBotId] = useState<string>('all');
  const [filterType, setFilterType] = useState<string>('');
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastIdRef = useRef<number>(0);

  // Build bot name/color lookup
  const botNameMap = Object.fromEntries(bots.map((b) => [b.id, b.name]));
  const botColorMap = Object.fromEntries(bots.map((b, i) => [b.id, botColors[i % botColors.length]]));

  // Short bot label (e.g. "sawtooth-meta" → "META", "sawtooth-qqq" → "QQQ")
  const botLabel = (botId: string) => {
    const name = botNameMap[botId] || botId.slice(0, 6);
    // Try to extract symbol from name (e.g. "sawtooth-meta" → "META")
    const parts = name.split('-');
    return parts.length > 1 ? parts[parts.length - 1].toUpperCase() : name.toUpperCase();
  };

  // Poll bot events — always fetch latest and replace
  useEffect(() => {
    if (bots.length === 0) return;

    const fetchEvents = () => {
      if (selectedBotId === 'all') {
        // Fetch from all bots and merge
        const perBot = Math.ceil(maxLines / Math.max(bots.length, 1));
        const promises = bots.map((b) => {
          const params = new URLSearchParams({ limit: String(perBot) });
          if (filterType) params.set('event_type', filterType);
          return fetch(`/api/bots/${b.id}/events?${params}`)
            .then((r) => r.ok ? r.json() : [])
            .then((entries: any[]) =>
              entries.map((e) => ({ ...e, bot_id: b.id, bot_name: b.name }))
            )
            .catch(() => [] as BotLogEntry[]);
        });
        Promise.all(promises).then((results) => {
          const merged = results.flat();
          // Sort by recorded_at descending (newest first)
          merged.sort((a, b) => (b.recorded_at || '').localeCompare(a.recorded_at || ''));
          setLogs(merged.slice(0, maxLines));
        });
      } else {
        const params = new URLSearchParams({ limit: String(maxLines) });
        if (filterType) params.set('event_type', filterType);
        fetch(`/api/bots/${selectedBotId}/events?${params}`)
          .then((r) => r.ok ? r.json() : [])
          .then((entries: BotLogEntry[]) => {
            const sorted = [...entries].reverse();
            setLogs(sorted.map((e) => ({ ...e, bot_id: selectedBotId, bot_name: botNameMap[selectedBotId] })));
          })
          .catch(() => {});
      }
    };

    fetchEvents();
    const interval = setInterval(fetchEvents, 2000);
    return () => clearInterval(interval);
  }, [selectedBotId, filterType, maxLines, bots]);

  // Scroll to top (newest first)
  const lastLogId = logs.length > 0 ? logs[0]?.id : 0;
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [lastLogId]);

  const filterButtons = ['', 'SIGNAL', 'SKIP', 'ORDER', 'FILL', 'EXIT_CHECK', 'CLOSED', 'ERROR'];

  return (
    <PanelShell title="Bot Log" accent="cyan" right={
      <div className="flex items-center gap-2">
        <select
          value={selectedBotId}
          onChange={(e) => setSelectedBotId(e.target.value)}
          className="text-[10px] bg-transparent border rounded px-1"
          style={{ borderColor: 'var(--border-default)', color: 'var(--text-secondary)' }}
        >
          <option value="all">All Bots</option>
          {bots.map((b) => (
            <option key={b.id} value={b.id}>{b.name}</option>
          ))}
        </select>
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{logs.length} events</span>
      </div>
    }>
      <div className="flex flex-col h-full">
        {/* Sticky error banner — always visible if there are recent errors */}
        {(() => {
          const recentErrors = logs.filter((l) => l.event_type === 'ERROR');
          const lastError = recentErrors.length > 0 ? recentErrors[recentErrors.length - 1] : null;
          if (!lastError) return null;
          const errorBot = lastError.bot_id ? botLabel(lastError.bot_id) : '';
          return (
            <div className="shrink-0 px-2 py-1.5 text-[11px] font-mono border-b flex items-center gap-2"
              style={{ background: 'rgba(239,68,68,0.1)', borderColor: 'var(--accent-red)', color: 'var(--accent-red)' }}>
              <span className="font-bold">ERROR</span>
              {errorBot && <span className="opacity-70">[{errorBot}]</span>}
              <span>{lastError.message}</span>
              <span className="opacity-50 ml-auto">{lastError.recorded_at ? formatTime(lastError.recorded_at) : ''}</span>
            </div>
          );
        })()}
        {/* Filter bar */}
        <div className="flex gap-1 px-2 py-1 shrink-0 border-b" style={{ borderColor: 'var(--border-default)' }}>
          {filterButtons.map((f) => (
            <button
              key={f || 'all'}
              onClick={() => setFilterType(f)}
              className="text-[10px] px-1.5 py-0.5 rounded"
              style={{
                background: filterType === f ? 'var(--bg-accent, var(--accent-blue))' : 'transparent',
                color: filterType === f ? 'var(--text-primary)' : 'var(--text-muted)',
                border: `1px solid ${filterType === f ? 'var(--accent-blue)' : 'var(--border-default)'}`,
              }}
            >
              {f || 'ALL'}
            </button>
          ))}
        </div>

        {/* Log entries */}
        <div ref={scrollRef} className="flex-1 overflow-y-auto p-1 font-mono text-xs leading-[1.7]">
          {logs.length === 0 ? (
            <div style={{ color: 'var(--text-muted)', padding: 20, textAlign: 'center' }}>
              {selectedBotId ? 'No events yet. Start the bot to see output.' : 'No bots registered.'}
            </div>
          ) : logs.map((log) => {
            const color = typeColor[log.event_type] || 'var(--text-secondary)';
            const label = typeLabel[log.event_type] || log.event_type.substring(0, 4);
            const ts = log.recorded_at ? formatTime(log.recorded_at) : '';

            return (
              <div key={log.id} className="flex gap-1.5 px-1" style={{ borderRadius: 2 }}
                onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--row-hover)')}
                onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}>
                <span style={{ color: 'var(--text-muted)' }} className="shrink-0 w-[55px]">{ts}</span>
                {log.bot_id && (
                  <span
                    className="shrink-0 w-[36px] text-center rounded text-[9px] font-bold"
                    style={{
                      color: botColorMap[log.bot_id] || 'var(--text-muted)',
                    }}
                  >{botLabel(log.bot_id)}</span>
                )}
                <span style={{ color, fontWeight: 600 }} className="shrink-0 w-[36px]">{label}</span>
                <span style={{
                  color: log.event_type === 'ERROR' || log.event_type === 'RISK'
                    ? 'var(--accent-red)'
                    : log.event_type === 'SIGNAL' || log.event_type === 'FILL'
                    ? 'var(--accent-green)'
                    : log.event_type === 'SKIP'
                    ? 'var(--accent-yellow)'
                    : 'var(--text-primary)',
                }}>
                  {log.message}
                </span>
                {log.trade_serial && (
                  <span style={{ color: 'var(--text-muted)' }} className="shrink-0">#{log.trade_serial}</span>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </PanelShell>
  );
}
