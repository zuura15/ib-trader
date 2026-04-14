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

interface LogEntry {
  id: string;
  timestamp: string;
  level: string;
  event: string;
  message: string;
}

export function LogStream({ maxLines = 200 }: { maxLines?: number }) {
  const dataMode = useStore((s) => s.dataMode);
  const mockLogs = useStore((s) => s.logs);
  const [liveLogs, setLiveLogs] = useState<LogEntry[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const autoScroll = useRef(true);
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
      raw: Array<{ timestamp: string; level: string; event: string; message: string }>,
    ) => {
      if (!raw || raw.length === 0) return;
      const newEntries: LogEntry[] = raw.map((e) => ({
        id: `log-${idCounter.current++}`,
        timestamp: e.timestamp,
        level: e.level,
        event: e.event,
        message: e.message,
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

  const logsRaw = dataMode === 'live' ? liveLogs : mockLogs.map(l => ({
    id: l.id,
    timestamp: l.timestamp instanceof Date ? l.timestamp.toISOString() : String(l.timestamp),
    level: l.level,
    event: l.event,
    message: l.message,
  }));

  // Newest first — reverse so latest entry is at the top
  const logs = [...logsRaw].reverse();

  // Always scroll to top (newest) when new entries arrive
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = 0;
    }
  }, [logs.length]);

  return (
    <PanelShell title="Logs" accent="amber" right={
      <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>{logs.length} entries</span>
    }>
      <div ref={scrollRef} className="h-full overflow-y-auto p-1 font-mono text-sm leading-[1.6]">
        {logs.length === 0 ? (
          <div style={{ color: 'var(--text-muted)', padding: 20, textAlign: 'center' }}>
            {dataMode === 'live' ? 'Loading logs...' : 'No log entries'}
          </div>
        ) : logs.map((log) => {
          const lvl = log.level || 'info';
          const color = levelColor[lvl] || 'var(--text-secondary)';
          const label = levelLabel[lvl] || lvl.substring(0, 3).toUpperCase();
          const ts = log.timestamp ? formatTime(log.timestamp) : '';

          return (
            <div
              key={log.id}
              className="flex gap-2 px-1"
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
              <span style={{
                color: lvl === 'error' || lvl === 'ERROR' ? 'var(--accent-red)'
                  : lvl === 'warning' || lvl === 'WARNING' ? 'var(--accent-yellow)'
                  : 'var(--text-primary)',
              }}>
                {log.message}
              </span>
            </div>
          );
        })}
      </div>
    </PanelShell>
  );
}
