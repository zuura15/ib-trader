/**
 * Audit Feed Pane — live-updating event feed for the trader bot view.
 *
 * Three event types share the feed:
 *   - BAR_EVAL      (per 3-min bar evaluation)
 *   - ORDER_PLACED  (entry / exit order submitted to IB)
 *   - TRADE_CLOSED  (round-trip closed)
 *
 * Each row is collapsed by default. Click to expand for structured
 * detail (one line per fact). The 📋 icon opens a modal with the
 * raw JSON dump for copy/paste.
 *
 * Live updates: subscribes to /api/audit/stream (Server-Sent Events).
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import {
  getAuditFeed, getApiBase, getBots,
  type AuditRow, type BotResponse,
} from '../../api/client';

type FilterValue = 'all' | string;

/** Format ISO timestamp explicitly in Pacific Time. */
function _fmtPT(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleTimeString('en-US', {
    timeZone: 'America/Los_Angeles', hour12: false,
    hour: '2-digit', minute: '2-digit', second: '2-digit',
  });
}

function _fmtPrice(s: string | null | number | undefined): string {
  if (s === null || s === undefined || s === '') return '—';
  const n = typeof s === 'number' ? s : Number(s);
  if (!Number.isFinite(n)) return String(s);
  return n.toFixed(2);
}

function _fmtMoney(s: string | null): string {
  if (s === null || s === undefined || s === '') return '';
  const n = Number(s);
  if (!Number.isFinite(n)) return s;
  const sign = n >= 0 ? '+' : '';
  return `${sign}$${n.toFixed(2)}`;
}

function _fmtDuration(seconds: number | null | undefined): string {
  if (!seconds || !Number.isFinite(seconds)) return '';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return `${m}m${s.toString().padStart(2, '0')}s`;
}

function Chip({ text, fg, bg, title }: {
  text: string; fg: string; bg: string; title?: string;
}) {
  return (
    <span title={title} style={{
      padding: '1px 6px',
      borderRadius: 3,
      background: bg,
      color: fg,
      fontSize: 10,
      fontWeight: 500,
      whiteSpace: 'nowrap',
    }}>
      {text}
    </span>
  );
}

interface TouchingLine {
  kind?: string;
  touches?: number;
  slope_per_bar?: number;
  intercept?: number;
  from_idx?: number;
  anchor_b_idx?: number;
  anchor_q_time?: string | null;
  anchor_b_time?: string | null;
  anchor_q_close?: number;
  anchor_b_close?: number;
}

interface AuditPayload {
  audit?: {
    pivot?: 'low' | 'high' | null;
    touch?: {
      line_kind?: string;
      direction?: string;
      touches?: number;
      slope_per_bar?: number;
      intercept?: number;
      anchor_b_time?: string | null;
      anchor_q_time?: string | null;
      anchor_b_price?: number;
      count?: number;
      lines?: TouchingLine[];
    } | null;
    filter_name?: string | null;
    filter_detail?: string | null;
    outcome?: 'B' | 'S' | 'exit' | '—' | string;
    prior_bar_close?: number | null;
    eval_ts_utc?: string | null;
    eval_bar_close?: number | null;
  };
  signal?: { entry_line?: Record<string, unknown> };
  skip?: Record<string, unknown>;
  exit?: Record<string, unknown>;
  bar?: Record<string, unknown>;
  fired?: { side?: string; qty?: string; order_type?: string };
  duration_seconds?: number;
}

function BarEvalRow({ r }: { r: AuditRow }) {
  const a = (r.payload as AuditPayload | null)?.audit ?? {};
  // Pivot chip — gray when none, blue/red for low/high.
  const pivot = a.pivot;
  const pivotChip = pivot === 'low'
    ? <Chip text="PIVOT·L" fg="#2563eb" bg="rgba(37,99,235,0.14)" title="pivot LOW at last_idx-1" />
    : pivot === 'high'
    ? <Chip text="PIVOT·H" fg="#dc2626" bg="rgba(220,38,38,0.14)" title="pivot HIGH at last_idx-1" />
    : <Chip text="NO_PIVOT" fg="#94a3b8" bg="rgba(148,163,184,0.10)" />;

  // Touch chip — "TOUCH·N" where N = how many current-session
  // trendlines this pivot landed on. NO_TOUCH = no lines at all.
  const lineCount = a.touch?.count ?? 0;
  const touchChip = lineCount > 0
    ? <Chip text={`TOUCH·${lineCount}`} fg="#9333ea" bg="rgba(168,85,247,0.16)"
            title={`pivot lies on ${lineCount} current-session trendline(s)`} />
    : <Chip text="NO_TOUCH" fg="#94a3b8" bg="rgba(148,163,184,0.10)" />;

  // Filter chip — three states:
  //   FILTER·<name>  amber, a filter rejected the entry
  //   PASSED         green, this bar had an order-trigger candidate
  //                  (pivot landed on at least one 3+touch line) and
  //                  every filter let it through.
  //   N/A            muted, no order-trigger candidate existed —
  //                  no pivot, no lines touched, OR every touched
  //                  line was a tentative 2-touch (3 is the minimum
  //                  the bot accepts for entry, so 2-touch lines
  //                  never trigger filter evaluation).
  const filt = a.filter_name;
  // An order-trigger candidate requires at least one line with >= 3
  // touches at this pivot. The strategy emits only 3+touch lines into
  // ``pivot_touching_lines``, but check defensively in case older
  // rows (or future code paths) carry weaker lines.
  const has3TouchLine = (a.touch?.lines ?? []).some(
    (ln) => (ln.touches ?? 0) >= 3,
  );
  const hasOrderCandidate = has3TouchLine;
  const filterChip = filt
    ? <Chip text={`FILTER·${filt}`} fg="#b45309" bg="rgba(245,158,11,0.18)"
            title={a.filter_detail || filt} />
    : hasOrderCandidate
    ? <Chip text="PASSED" fg="#16a34a" bg="rgba(34,197,94,0.12)"
            title="all filters passed; entry triggered (or would have)" />
    : <Chip text="N/A" fg="#94a3b8" bg="rgba(148,163,184,0.08)"
            title="no order-trigger candidate this bar; filters didn't run" />;

  // Outcome chip — B/S/exit/— with strong color.
  const outcome = a.outcome ?? '—';
  const outcomeChip = outcome === 'B'
    ? <Chip text="B" fg="#16a34a" bg="rgba(34,197,94,0.22)" title="BUY" />
    : outcome === 'S'
    ? <Chip text="S" fg="#dc2626" bg="rgba(220,38,38,0.22)" title="SELL" />
    : outcome === 'exit'
    ? <Chip text="EXIT" fg="#ea580c" bg="rgba(249,115,22,0.20)" />
    : <Chip text="—" fg="#94a3b8" bg="rgba(148,163,184,0.08)" />;

  return (
    <div style={{
      display: 'flex', gap: 8, alignItems: 'center',
      fontFamily: 'ui-monospace, monospace', fontSize: 11,
      lineHeight: 1.4, flexWrap: 'wrap',
    }}>
      <span style={{ color: 'var(--text-muted)' }}>{_fmtPT(r.event_ts_utc)}</span>
      <span style={{ color: 'var(--text-secondary)', minWidth: 50 }}>{r.symbol}</span>
      <span style={{ color: 'var(--text-primary)', minWidth: 56 }}>
        {_fmtPrice(r.bar_close)}
      </span>
      {pivotChip}
      {touchChip}
      {filterChip}
      {outcomeChip}
    </div>
  );
}

function OrderRow({ r }: { r: AuditRow }) {
  const tone = r.decision.includes('·BUY')
    ? { bg: 'rgba(34,197,94,0.18)', fg: '#16a34a' }
    : { bg: 'rgba(220,38,38,0.16)', fg: '#dc2626' };
  return (
    <div style={{
      display: 'flex', gap: 8, alignItems: 'center',
      fontFamily: 'ui-monospace, monospace', fontSize: 11,
      lineHeight: 1.4, flexWrap: 'wrap',
    }}>
      <span style={{ color: 'var(--text-muted)' }}>{_fmtPT(r.event_ts_utc)}</span>
      <span style={{ color: 'var(--text-secondary)', minWidth: 50 }}>{r.symbol}</span>
      <Chip text={r.decision} fg={tone.fg} bg={tone.bg} />
    </div>
  );
}

function TradeClosedRow({ r }: { r: AuditRow }) {
  const tone = { bg: 'rgba(168,85,247,0.16)', fg: '#9333ea' };
  const pnl = r.pnl_net !== null ? Number(r.pnl_net) : null;
  const pnlColor = pnl === null
    ? 'var(--text-secondary)'
    : pnl > 0 ? '#16a34a' : pnl < 0 ? '#dc2626' : 'var(--text-secondary)';
  const star = pnl !== null ? (pnl > 0 ? '★' : '✗') : '·';
  const duration = (r.payload as AuditPayload | null)?.duration_seconds;
  const direction = r.decision.split('·')[1] || '';
  return (
    <div style={{
      display: 'flex', gap: 8, alignItems: 'center',
      fontFamily: 'ui-monospace, monospace', fontSize: 11,
      lineHeight: 1.4, flexWrap: 'wrap',
      background: tone.bg, padding: '4px 8px', borderRadius: 4,
    }}>
      <span style={{ color: 'var(--text-muted)' }}>{_fmtPT(r.event_ts_utc)}</span>
      <span style={{ color: 'var(--text-secondary)', minWidth: 50 }}>{r.symbol}</span>
      <span style={{ color: tone.fg, fontWeight: 600 }}>{direction}</span>
      <span style={{ color: pnlColor, fontWeight: 600 }}>
        {star} {_fmtMoney(r.pnl_net)}
      </span>
      {duration && (
        <span style={{ color: 'var(--text-secondary)' }}>{_fmtDuration(duration)}</span>
      )}
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────
// Expanded detail panel — one line per fact
// ───────────────────────────────────────────────────────────────────

function DetailLine({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div style={{
      display: 'flex', gap: 8, padding: '2px 0',
      fontFamily: 'ui-monospace, monospace', fontSize: 10,
      lineHeight: 1.5,
    }}>
      <span style={{ color: 'var(--text-muted)', minWidth: 110 }}>{label}</span>
      <span style={{ color: 'var(--text-primary)', wordBreak: 'break-word' }}>
        {value}
      </span>
    </div>
  );
}

/** Fetch the next 3-min bar's close from /api/history. Returns null
 *  if it hasn't closed yet (the bar timestamp is in the future). */
async function fetchNextBarClose(
  symbol: string, currentBarTs: string,
): Promise<number | null> {
  try {
    const base = getApiBase();
    const params = new URLSearchParams({ symbol, sec_type: 'FUT', hours: '1',
                                          bar_size: '3 mins' });
    const resp = await fetch(`${base}/history?${params}`);
    if (!resp.ok) return null;
    const bars: Array<{ ts: string; close: number }> = await resp.json();
    const curTs = new Date(currentBarTs).getTime();
    const next = bars.find((b) => new Date(b.ts).getTime() > curTs);
    return next?.close ?? null;
  } catch {
    return null;
  }
}

function ExpandedBarEval({ row, onShowRaw }: {
  row: AuditRow; onShowRaw: () => void;
}) {
  const a = (row.payload as AuditPayload | null)?.audit ?? {};
  const [nextClose, setNextClose] = useState<number | null | 'pending'>('pending');
  useEffect(() => {
    let cancelled = false;
    if (row.event_ts_utc) {
      fetchNextBarClose(row.symbol, row.event_ts_utc).then((v) => {
        if (!cancelled) setNextClose(v);
      });
    }
    return () => { cancelled = true; };
  }, [row.event_ts_utc, row.symbol]);

  // List one line per trendline this pivot touched. Each line shows
  // its identity (kind · N-touch · slope) and vertices (Q anchor /
  // P anchor with timestamps and prices). When N=0, show "—".
  const touchedLines = a.touch?.lines ?? [];
  const lineEntries = touchedLines.length === 0 ? [
    <DetailLine key="none" label="lines" value="—" />
  ] : touchedLines.map((ln, i) => {
    const summary = `${ln.kind ?? '?'} · ${ln.touches ?? '?'}-touch · `
      + `slope/bar=${(ln.slope_per_bar ?? 0).toFixed(4)} · `
      + `Q@${_fmtPT(ln.anchor_q_time ?? null)} (${_fmtPrice(ln.anchor_q_close)}) · `
      + `P@${_fmtPT(ln.anchor_b_time ?? null)} (${_fmtPrice(ln.anchor_b_close)})`;
    return (
      <DetailLine key={i} label={`line ${i + 1}`} value={summary} />
    );
  });

  // Eval bar close from the BAR payload directly — the chart-tick
  // 3 min after the pivot bar's tick.
  const evalClose = a.eval_bar_close ?? null;
  const evalAt = _fmtPT(a.eval_ts_utc ?? null);
  return (
    <div style={{
      marginTop: 6, marginLeft: 8,
      padding: '8px 10px',
      background: 'var(--panel-bg-alt, rgba(0,0,0,0.04))',
      borderLeft: '2px solid var(--border-default)',
    }}>
      <DetailLine label="prior close" value={_fmtPrice(a.prior_bar_close ?? null)} />
      <DetailLine
        label="next close"
        value={evalClose !== null
          ? `${_fmtPrice(evalClose)}  (eval @ ${evalAt})`
          : nextClose === 'pending' ? '…'
          : nextClose === null ? '— (bar not closed yet)'
          : _fmtPrice(nextClose)}
      />
      {lineEntries}
      <DetailLine
        label="filter"
        value={a.filter_name
          ? `${a.filter_name} — ${a.filter_detail ?? ''}`
          : ((a.touch?.lines ?? []).some((ln) => (ln.touches ?? 0) >= 3)
              ? '— (passed all filters)'
              : 'N/A — no 3+touch order-trigger candidate this bar')}
      />
      <DetailLine label="outcome" value={
        a.outcome === 'B' ? 'BUY · entry order placed'
        : a.outcome === 'S' ? 'SELL · entry order placed'
        : a.outcome === 'exit' ? 'EXIT · trailing stop or counter-line fired'
        : a.outcome ?? '—'
      } />
      <div style={{ marginTop: 6, display: 'flex', justifyContent: 'flex-end' }}>
        <button
          onClick={(e) => { e.stopPropagation(); onShowRaw(); }}
          title="show raw JSON dump for this row"
          style={{
            background: 'transparent',
            border: '1px solid var(--border-default)',
            color: 'var(--text-secondary)',
            cursor: 'pointer',
            fontSize: 10, padding: '2px 6px', borderRadius: 3,
          }}
        >
          📋 raw JSON
        </button>
      </div>
    </div>
  );
}

function ExpandedTradeClosed({ row, onShowRaw }: {
  row: AuditRow; onShowRaw: () => void;
}) {
  const p = row.payload as Record<string, unknown> | null;
  const direction = row.decision.split('·')[1] || '';
  const reason = row.decision.split('·').slice(2).join('·') || '—';
  const get = (k: string) => (p?.[k] as string | number | undefined);
  return (
    <div style={{
      marginTop: 6, marginLeft: 8,
      padding: '8px 10px',
      background: 'var(--panel-bg-alt, rgba(0,0,0,0.04))',
      borderLeft: '2px solid var(--border-default)',
    }}>
      <DetailLine label="direction" value={direction} />
      <DetailLine label="entry price" value={_fmtPrice(get('entry_price') ?? null)} />
      <DetailLine label="exit price"  value={_fmtPrice(get('exit_price') ?? null)} />
      <DetailLine label="entry time"  value={_fmtPT(String(get('entry_time') ?? '')) || '—'} />
      <DetailLine label="exit time"   value={_fmtPT(String(get('exit_time') ?? '')) || '—'} />
      <DetailLine label="duration"    value={_fmtDuration(get('duration_seconds') as number | undefined)} />
      <DetailLine label="realized PnL" value={_fmtMoney(String(get('realized_pnl') ?? '')) || '—'} />
      <DetailLine label="commission"  value={_fmtMoney(String(get('commission') ?? '')) || '—'} />
      <DetailLine label="exit reason" value={reason} />
      <div style={{ marginTop: 6, display: 'flex', justifyContent: 'flex-end' }}>
        <button
          onClick={(e) => { e.stopPropagation(); onShowRaw(); }}
          style={{
            background: 'transparent',
            border: '1px solid var(--border-default)',
            color: 'var(--text-secondary)',
            cursor: 'pointer', fontSize: 10, padding: '2px 6px', borderRadius: 3,
          }}
        >
          📋 raw JSON
        </button>
      </div>
    </div>
  );
}

function ExpandedOrder({ row, onShowRaw }: {
  row: AuditRow; onShowRaw: () => void;
}) {
  const p = row.payload as Record<string, unknown> | null;
  const get = (k: string) => (p?.[k] as string | undefined);
  const ec = (p?.exit_context as Record<string, unknown> | undefined) ?? {};
  return (
    <div style={{
      marginTop: 6, marginLeft: 8,
      padding: '8px 10px',
      background: 'var(--panel-bg-alt, rgba(0,0,0,0.04))',
      borderLeft: '2px solid var(--border-default)',
    }}>
      <DetailLine label="side"       value={get('side') || '—'} />
      <DetailLine label="qty"        value={get('qty') || '—'} />
      <DetailLine label="order type" value={get('order_type') || '—'} />
      <DetailLine label="origin"     value={get('origin') || '—'} />
      {Object.keys(ec).length > 0 && (
        <DetailLine label="exit context"
          value={JSON.stringify(ec).slice(0, 200)} />
      )}
      <div style={{ marginTop: 6, display: 'flex', justifyContent: 'flex-end' }}>
        <button
          onClick={(e) => { e.stopPropagation(); onShowRaw(); }}
          style={{
            background: 'transparent',
            border: '1px solid var(--border-default)',
            color: 'var(--text-secondary)',
            cursor: 'pointer', fontSize: 10, padding: '2px 6px', borderRadius: 3,
          }}
        >
          📋 raw JSON
        </button>
      </div>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────
// Raw JSON modal
// ───────────────────────────────────────────────────────────────────

function RawJsonModal({ row, onClose }: {
  row: AuditRow; onClose: () => void;
}) {
  const json = JSON.stringify(row, null, 2);
  const copy = async () => {
    try { await navigator.clipboard.writeText(json); } catch { /* ignore */ }
  };
  return (
    <div
      onClick={onClose}
      style={{
        position: 'fixed', inset: 0, zIndex: 9999,
        background: 'rgba(0,0,0,0.45)',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          width: 'min(720px, 90vw)', maxHeight: '80vh',
          background: 'var(--panel-bg, #fff)',
          color: 'var(--text-primary)',
          border: '1px solid var(--border-default)',
          borderRadius: 4, overflow: 'hidden',
          display: 'flex', flexDirection: 'column',
        }}
      >
        <div style={{
          display: 'flex', alignItems: 'center', gap: 8,
          padding: '6px 10px',
          borderBottom: '1px solid var(--border-default)',
          fontSize: 12, fontWeight: 600,
        }}>
          Raw JSON — id {row.id}
          <button
            onClick={copy}
            style={{
              marginLeft: 'auto',
              background: 'transparent',
              border: '1px solid var(--border-default)',
              color: 'var(--text-secondary)',
              cursor: 'pointer', fontSize: 11, padding: '2px 8px',
              borderRadius: 3,
            }}
          >Copy</button>
          <button
            onClick={onClose}
            style={{
              background: 'transparent',
              border: '1px solid var(--border-default)',
              color: 'var(--text-secondary)',
              cursor: 'pointer', fontSize: 11, padding: '2px 8px',
              borderRadius: 3,
            }}
          >Close</button>
        </div>
        <pre style={{
          margin: 0, padding: '8px 12px', overflow: 'auto',
          flex: 1, fontFamily: 'ui-monospace, monospace',
          fontSize: 10, lineHeight: 1.4,
          background: 'var(--panel-bg, #fff)',
        }}>{json}</pre>
      </div>
    </div>
  );
}

// ───────────────────────────────────────────────────────────────────
// Row + Pane
// ───────────────────────────────────────────────────────────────────

function FeedRow({ row, onShowRaw }: {
  row: AuditRow; onShowRaw: (r: AuditRow) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  return (
    <div
      onClick={() => setExpanded((v) => !v)}
      style={{
        padding: '5px 8px',
        borderBottom: '1px solid var(--border-default)',
        cursor: 'pointer',
        userSelect: 'none',
      }}
    >
      {row.event_type === 'BAR_EVAL' && <BarEvalRow r={row} />}
      {row.event_type === 'ORDER_PLACED' && <OrderRow r={row} />}
      {row.event_type === 'TRADE_CLOSED' && <TradeClosedRow r={row} />}
      {expanded && row.event_type === 'BAR_EVAL'
        && <ExpandedBarEval row={row} onShowRaw={() => onShowRaw(row)} />}
      {expanded && row.event_type === 'TRADE_CLOSED'
        && <ExpandedTradeClosed row={row} onShowRaw={() => onShowRaw(row)} />}
      {expanded && row.event_type === 'ORDER_PLACED'
        && <ExpandedOrder row={row} onShowRaw={() => onShowRaw(row)} />}
    </div>
  );
}

export function AuditFeedPane() {
  const [bots, setBots] = useState<BotResponse[]>([]);
  const [filter, setFilter] = useState<FilterValue>('all');
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [rawRow, setRawRow] = useState<AuditRow | null>(null);
  const esRef = useRef<EventSource | null>(null);
  const seenIds = useRef<Set<number>>(new Set());

  useEffect(() => {
    let cancelled = false;
    const botId = filter === 'all' ? undefined : filter;
    getAuditFeed({ botId, limit: 100 })
      .then((data) => {
        if (cancelled) return;
        setRows(data);
        seenIds.current = new Set(data.map((r) => r.id));
      })
      .catch((e) => { if (!cancelled) setError(String(e)); });
    return () => { cancelled = true; };
  }, [filter]);

  useEffect(() => {
    if (esRef.current) { esRef.current.close(); esRef.current = null; }
    const base = getApiBase();
    const url = filter === 'all'
      ? `${base}/audit/stream`
      : `${base}/audit/stream?bot_id=${encodeURIComponent(filter)}`;
    const es = new EventSource(url);
    es.onmessage = (ev) => {
      try {
        const row = JSON.parse(ev.data) as AuditRow;
        if (seenIds.current.has(row.id)) return;
        seenIds.current.add(row.id);
        setRows((prev) => [row, ...prev].slice(0, 500));
      } catch { /* ignore parse errors */ }
    };
    esRef.current = es;
    return () => { es.close(); };
  }, [filter]);

  useEffect(() => {
    getBots().then((bs) => setBots(bs)).catch(() => { /* ignore */ });
  }, []);

  const filterOptions = useMemo(() => {
    // The audit feed is primarily for chart_signal bots — they're
    // the ones that emit BAR_EVAL rows. Use the ``chart-bot-*`` id
    // prefix as the discriminator since the API's ``strategy`` field
    // collapses every bot to the generic "strategy_bot" runner type.
    const chartBots = bots.filter((b) => b.id.startsWith('chart-bot-'));
    return [{ id: 'all', label: 'All bots' } as const].concat(
      chartBots.map((b) => ({ id: b.id as 'all', label: b.name })),
    );
  }, [bots]);

  return (
    <div style={{
      display: 'flex', flexDirection: 'column',
      height: '100%', background: 'var(--panel-bg)',
      color: 'var(--text-primary)', fontSize: 11,
    }}>
      <div style={{
        display: 'flex', gap: 8, alignItems: 'center',
        padding: '6px 8px',
        borderBottom: '1px solid var(--border-default)',
      }}>
        <span style={{ fontWeight: 600, fontSize: 12 }}>Audit Feed</span>
        <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>times in PT</span>
        <select
          value={filter}
          onChange={(e) => setFilter(e.target.value)}
          style={{
            fontSize: 11, padding: '2px 6px',
            background: 'transparent', color: 'var(--text-primary)',
            border: '1px solid var(--border-default)', borderRadius: 3,
          }}
        >
          {filterOptions.map((opt) => (
            <option key={opt.id} value={opt.id}>{opt.label}</option>
          ))}
        </select>
        <span style={{ marginLeft: 'auto', color: 'var(--text-muted)' }}>
          {rows.length} events
        </span>
      </div>

      <div style={{ flex: 1, overflowY: 'auto' }}>
        {error && (
          <div style={{ padding: 8, color: 'var(--accent-red, #dc2626)' }}>
            Error: {error}
          </div>
        )}
        {!error && rows.length === 0 && (
          <div style={{ padding: 12, color: 'var(--text-muted)' }}>No events yet.</div>
        )}
        {rows.map((r) => (
          <FeedRow key={r.id} row={r} onShowRaw={setRawRow} />
        ))}
      </div>

      {rawRow && (
        <RawJsonModal row={rawRow} onClose={() => setRawRow(null)} />
      )}
    </div>
  );
}
