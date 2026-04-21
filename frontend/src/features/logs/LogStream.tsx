import { useEffect, useRef, useState } from 'react';
import { useStore } from '../../data/store';
import { formatTime } from '../../utils/format';
import { PanelShell } from '../../components/PanelShell';

const levelColor: Record<string, string> = {
  debug: 'var(--text-muted)',
  info: 'var(--text-secondary)',
  success: 'var(--accent-green)',
  warning: 'var(--accent-yellow)',
  error: 'var(--accent-red)',
  DEBUG: 'var(--text-muted)',
  INFO: 'var(--text-secondary)',
  WARNING: 'var(--accent-yellow)',
  ERROR: 'var(--accent-red)',
};

const levelLabel: Record<string, string> = {
  debug: 'DBG', DEBUG: 'DBG',
  info: 'INF', INFO: 'INF',
  success: 'OK ', SUCCESS: 'OK',
  warning: 'WRN', WARNING: 'WRN',
  error: 'ERR', ERROR: 'ERR',
};

export interface LogEntry {
  id: string;
  timestamp: string;
  level: string;
  event: string;
  message: string;
  fields?: Record<string, unknown>;
  exc_info?: string;
}

export interface LogStreamProps {
  /** Max rows to retain in memory / render. */
  maxLines?: number;
  /** Panel title (default "Logs"). */
  title?: string;
  /** Panel accent color for the header dot. */
  accent?: 'amber' | 'red' | 'cyan' | 'green' | 'blue' | 'purple';
  /** Only show rows whose level matches one of these. */
  levelFilter?: string[];
  /** Empty-state message override. */
  emptyHint?: string;
}

/**
 * Streaming log viewer rendered as free-flowing log lines:
 *   HH:MM:SS  LVL  [event]  message  key=value key=value
 *
 * Structured fields (trigger, bot_id, symbol, alert_id, etc.) that the
 * backend forwards in ``fields`` are rendered inline after the message
 * so entries like PAGER_ALERT_RAISED — whose "message" field is empty
 * but whose structured payload carries all the useful info — are
 * readable instead of appearing as bare event names.
 *
 * Used by the Logs tab (all levels) and the Errors tab (ERROR only) via
 * the ``levelFilter`` prop.
 */
export function LogStream({
  maxLines = 200,
  title = 'Logs',
  accent = 'amber',
  levelFilter,
  emptyHint,
}: LogStreamProps) {
  const dataMode = useStore((s) => s.dataMode);
  const mockLogs = useStore((s) => s.logs);
  const [liveLogs, setLiveLogs] = useState<LogEntry[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const lastTimestamp = useRef<string>('');
  const idCounter = useRef(0);

  // Live mode: stream logs over the WS (backend tails the structured JSON
  // log file). Entries arrive as {type: "log_batch", data: [...]} and are
  // appended as they're written — no poll cadence, no ?after= cursor.
  useEffect(() => {
    if (dataMode !== 'live') return;

    const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const base = import.meta.env.VITE_WS_URL || `${proto}//${window.location.host}/ws`;
    const token = import.meta.env.VITE_API_TOKEN || '';
    const url = token ? `${base}?token=${token}` : base;

    let ws: WebSocket | null = null;
    let retry: ReturnType<typeof setTimeout> | null = null;
    let closed = false;

    const appendEntries = (
      raw: Array<Partial<LogEntry> & { timestamp: string; level: string; event: string; message: string }>,
    ) => {
      if (!raw || raw.length === 0) return;
      const newEntries: LogEntry[] = raw.map((e) => ({
        id: `log-${idCounter.current++}`,
        timestamp: e.timestamp,
        level: e.level,
        event: e.event,
        message: e.message,
        fields: e.fields || undefined,
        exc_info: e.exc_info || undefined,
      }));
      const last = raw[raw.length - 1]?.timestamp;
      if (last) lastTimestamp.current = last;
      setLiveLogs(prev => {
        const combined = [...prev, ...newEntries];
        return combined.slice(-maxLines);
      });
    };

    const open = () => {
      if (closed) return;
      ws = new WebSocket(url);
      ws.onopen = () => {
        ws?.send(JSON.stringify({ type: 'subscribe_logs', backlog: maxLines }));
      };
      ws.onmessage = (ev) => {
        try {
          const msg = JSON.parse(ev.data);
          if (msg.type === 'log_batch' && Array.isArray(msg.data)) {
            appendEntries(msg.data);
          }
        } catch { /* ignore malformed frames */ }
      };
      ws.onclose = () => {
        if (!closed) retry = setTimeout(open, 2000);
      };
      ws.onerror = () => { /* onclose handles reconnect */ };
    };

    open();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      if (ws) { ws.onclose = null; ws.close(); }
    };
  }, [dataMode, maxLines]);

  const logsRaw: LogEntry[] = dataMode === 'live' ? liveLogs : mockLogs.map(l => ({
    id: l.id,
    timestamp: l.timestamp instanceof Date ? l.timestamp.toISOString() : String(l.timestamp),
    level: l.level,
    event: l.event,
    message: l.message,
  }));

  // Apply level filter before reversing for display.
  const filterSet = levelFilter
    ? new Set(levelFilter.flatMap((l) => [l, l.toLowerCase(), l.toUpperCase()]))
    : null;
  const filtered = filterSet
    ? logsRaw.filter((l) => filterSet.has(l.level || 'info'))
    : logsRaw;

  // Newest first — reverse so latest entry is at the top
  const logs = [...filtered].reverse();

  // Always scroll to top (newest) when new entries arrive
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [logs.length]);

  const defaultEmpty = dataMode === 'live' ? 'Loading logs...' : 'No log entries';

  return (
    <PanelShell title={title} accent={accent} right={
      <span style={{ fontSize: 13, color: 'var(--text-muted)' }}>{logs.length} entries</span>
    }>
      <div ref={scrollRef} className="h-full overflow-y-auto p-1 font-mono text-base leading-[1.7]">
        {logs.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', padding: 20, textAlign: 'center', fontSize: 14 }}>
            {emptyHint ?? defaultEmpty}
          </div>
        ) : logs.map((log) => {
          const lvl = log.level || 'info';
          const color = levelColor[lvl] || 'var(--text-secondary)';
          const label = levelLabel[lvl] || lvl.substring(0, 3).toUpperCase();
          const ts = log.timestamp ? formatTime(log.timestamp) : '';
          const msgColor =
            lvl === 'error' || lvl === 'ERROR' ? 'var(--accent-red)'
            : lvl === 'warning' || lvl === 'WARNING' ? 'var(--accent-yellow)'
            : 'var(--text-primary)';
          const fieldEntries = log.fields
            ? Object.entries(log.fields).filter(([, v]) =>
                v !== null && v !== undefined && v !== '' && typeof v !== 'object')
            : [];
          return (
            <div
              key={log.id}
              className="flex flex-wrap gap-2 px-1"
              style={{ borderRadius: 2 }}
              onMouseEnter={(e) => (e.currentTarget.style.background = 'var(--row-hover)')}
              onMouseLeave={(e) => (e.currentTarget.style.background = 'transparent')}
              data-testid="log-entry"
              data-level={lvl}
              data-event={log.event || 'log'}
            >
              <span style={{ color: 'var(--text-muted)' }} className="shrink-0">{ts}</span>
              <span style={{ color }} className="shrink-0 font-semibold">{label}</span>
              <span style={{ color: 'var(--text-muted)' }} className="shrink-0">[{log.event || 'log'}]</span>
              {log.message ? (
                <span style={{ color: msgColor }}>{log.message}</span>
              ) : null}
              {fieldEntries.map(([k, v]) => (
                <span key={k} style={{ color: 'var(--text-secondary)' }}>
                  <span style={{ color: 'var(--text-muted)' }}>{k}</span>=<span>{String(v)}</span>
                </span>
              ))}
              {log.exc_info ? (
                <pre className="w-full mt-1 whitespace-pre-wrap text-[13px]"
                  style={{ color: 'var(--accent-red)', opacity: 0.85 }}>
                  {log.exc_info}
                </pre>
              ) : null}
            </div>
          );
        })}
      </div>
    </PanelShell>
  );
}

/**
 * Convenience wrapper: the Errors tab is just LogStream filtered to ERROR
 * level with a red accent and `exc_info` rendered inline so stack traces
 * (e.g. SMART_MARKET_AMEND_FAILED) are visible without digging into the
 * on-disk log file.
 */
export function ErrorStream({ maxLines = 200 }: { maxLines?: number }) {
  return (
    <LogStream
      maxLines={maxLines}
      title="Errors"
      accent="red"
      levelFilter={['error', 'ERROR']}
      emptyHint="No errors — everything nominal."
    />
  );
}
