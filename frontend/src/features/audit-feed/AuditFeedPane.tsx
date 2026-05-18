/**
 * Audit Feed Pane — live-updating event feed for the trader bot view.
 *
 * Event types in the feed:
 *   - BAR_EVAL      (per 3-min bar evaluation)
 *   - ORDER_PLACED  (entry / exit order submitted to IB)
 *   - TRADE_CLOSED  (round-trip summary on exit fill, with net P&L)
 *
 * TRADE_CLOSED rows were briefly suppressed (commit 37bc409) because
 * the server-side pnl_net was always $0.00 — the strategy read
 * entry_price from ctx.state AFTER the runtime had wiped it on
 * exit. The row is now emitted from runtime._handle_record_trade_closed
 * where realized_pnl AND the transactions-seeded commission are both
 * in scope, so the row carries a correct net P&L. Suppression
 * reverted.
 *
 * Each row is collapsed by default. Click to expand for structured
 * detail (one line per fact). The clipboard icon opens a modal with
 * the raw JSON dump for copy/paste.
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

// Reformat a filter SKIP message for the detail pane. The backend
// emits ``"<name> filter (<DIR>) — <description>"``; combined with
// the filter_name prefix the original render read
// ``"<name> — <name> filter (<DIR>) — <description>"`` which buries
// the meaningful part. Lead with the description, then suffix the
// direction (filter_name is already on the chip above).
function _fmtFilter(name: string, detail: string): string {
  if (!detail) return name;
  const parts = detail.split(' — ');
  if (parts.length < 2) return detail;
  const description = parts.slice(1).join(' — ');
  const dirMatch = parts[0].match(/\(([^)]+)\)/);
  const dir = dirMatch ? ` — ${name} (${dirMatch[1]})` : ` — ${name}`;
  return `${description}${dir}`;
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
  signal?: { entry_line?: {
    entry_path?: 'touch' | 'accel' | string;
    touches?: number;
    marginal?: boolean;
    marginal_filters?: string[];
    [k: string]: unknown;
  } };
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

  // Touch chip — three states:
  //   TOUCH·N        pivot landed on a line with N strict touches
  //                  (purple). N ≥ 3 by definition since 2-touch
  //                  lines are below the entry-eligibility floor.
  //   ACCEL·N        pivot is an acceleration entry — overshot the
  //                  line beyond near_tol AND implied slope ≥
  //                  min_slope_ratio × line slope. N is the
  //                  underlying line's touch count (orange).
  //   NO_3RD_TOUCH   pivot didn't land on any 3+touch line (gray).
  //                  2-touch lines may exist on the chart at this
  //                  pivot — they're surfaced visually but don't
  //                  qualify as an entry candidate yet.
  // Accel detection: the SIGNAL's entry_line carries entry_path;
  // when "accel", the pivot doesn't strictly touch (TOUCH·N is 0)
  // but the trade still fired — flag it explicitly so the operator
  // doesn't read NO_3RD_TOUCH next to a B/S outcome and wonder how
  // the entry happened.
  const bestTouches = (a.touch?.lines ?? []).reduce(
    (m, ln) => Math.max(m, ln.touches ?? 0), 0,
  );
  const signalEntryLine = (r.payload as AuditPayload | null)?.signal?.entry_line;
  const isAccel = signalEntryLine?.entry_path === 'accel';
  const accelTouches = typeof signalEntryLine?.touches === 'number'
    ? signalEntryLine.touches : null;
  const touchChip = isAccel && accelTouches !== null
    ? <Chip text={`ACCEL·${accelTouches}`} fg="#ea580c" bg="rgba(249,115,22,0.20)"
            title={`acceleration entry on a ${accelTouches}-touch line — pivot overshot the line, implied slope ≥ min_slope_ratio × line slope`} />
    : bestTouches > 0
    ? <Chip text={`TOUCH·${bestTouches}`} fg="#9333ea" bg="rgba(168,85,247,0.16)"
            title={`pivot is the ${bestTouches}th touch on its strongest line`} />
    : <Chip text="NO_3RD_TOUCH" fg="#94a3b8" bg="rgba(148,163,184,0.10)"
            title="no 3+ touch line at this pivot (2-touch lines may exist on the chart but aren't yet entry-eligible)" />;

  // Filter chip — three states, with N/A taking precedence over
  // FILTER·<name> when there's no real order-trigger candidate.
  //
  //   N/A            muted, no current-session 3+touch line at this
  //                  pivot — filters that happen to "fire" on a
  //                  stale-session 3+touch line aren't meaningful
  //                  for entry, so we hide them. The bot's
  //                  ``q_session`` filter would have rejected them
  //                  anyway; surfacing min_target / shoulder /
  //                  far_from_pivot on those bars just confuses
  //                  the operator's read.
  //   FILTER·<name>  amber, a filter rejected an entry that DID
  //                  have a current-session 3+touch candidate.
  //   PASSED         green, current-session 3+touch candidate AND
  //                  every filter let it through.
  const filt = a.filter_name;
  // Only consider this a real order-trigger candidate if at least one
  // entry in ``pivot_touching_lines`` has >= 3 touches. The strategy
  // already gates that list on (a) 3+ touches and (b) current PT
  // session, so an empty/2-touch-only list = no candidate.
  // Accel entries also count as order candidates: their pivot didn't
  // strictly touch a line, but the SIGNAL still chose an underlying
  // multi-touch line and fired — the filter chain ran on it and the
  // operator should see PASSED/FILTER·*, not N/A.
  const hasOrderCandidate = isAccel || (a.touch?.lines ?? []).some(
    (ln) => (ln.touches ?? 0) >= 3,
  );
  const filterChip = !hasOrderCandidate
    ? <Chip text="N/A" fg="#94a3b8" bg="rgba(148,163,184,0.08)"
            title="no current-session 3+touch candidate; filters not relevant" />
    : filt
    ? <Chip text={`FILTER·${filt}`} fg="#b45309" bg="rgba(245,158,11,0.18)"
            title={a.filter_detail || filt} />
    : <Chip text="PASSED" fg="#16a34a" bg="rgba(34,197,94,0.12)"
            title="all filters passed; entry triggered (or would have)" />;

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
  const p = r.payload as Record<string, unknown> | null;
  const duration = (p?.duration_seconds as number | undefined)
    ?? (r.payload as AuditPayload | null)?.duration_seconds;
  // Decision = ``CLOSED·DIR·exit_reason·entry_tag`` post 2026-05-18.
  // Legacy rows (4-element decision missing) gracefully degrade.
  const parts = r.decision.split('·');
  const direction = parts[1] || '';
  const reason = parts[2] || '';
  // entry_tag is "clean" | "accel" | <first_marginal_filter>. Prefer
  // payload over decision parse so the chip is always present even
  // on legacy rows that have entry_tag in payload but not in the
  // decision string.
  const entryTag = (p?.entry_tag as string | undefined)
    ?? parts[3]
    ?? null;
  const entryPx = p?.entry_price as string | number | undefined;
  const exitPx = p?.exit_price as string | number | undefined;
  // Entry-tag color: green = clean, orange = accel, amber = marginal
  // (anything else implies a marginal-bypassed filter name).
  const entryTagStyle = (() => {
    if (entryTag === 'clean') return { fg: '#16a34a', bg: 'rgba(34,197,94,0.15)' };
    if (entryTag === 'accel') return { fg: '#ea580c', bg: 'rgba(249,115,22,0.18)' };
    if (entryTag) return { fg: '#b45309', bg: 'rgba(245,158,11,0.18)' };
    return null;
  })();
  return (
    <div style={{
      display: 'flex', gap: 8, alignItems: 'center',
      fontFamily: 'ui-monospace, monospace', fontSize: 11,
      lineHeight: 1.4, flexWrap: 'wrap',
      background: tone.bg, padding: '4px 8px', borderRadius: 4,
    }}>
      <span style={{ color: 'var(--text-muted)' }}>{_fmtPT(r.event_ts_utc)}</span>
      <span style={{ color: 'var(--text-secondary)', minWidth: 50, fontWeight: 600 }}>{r.symbol}</span>
      <span style={{ color: tone.fg, fontWeight: 600 }}>{direction}</span>
      {reason && (
        <span style={{ color: tone.fg, fontSize: 10 }}>· {reason}</span>
      )}
      {entryTagStyle && entryTag && (
        <Chip
          text={entryTag}
          fg={entryTagStyle.fg}
          bg={entryTagStyle.bg}
          title={
            entryTag === 'clean' ? 'no entry filter bypassed' :
            entryTag === 'accel' ? 'acceleration-continuation entry' :
            `marginal entry bypassed: ${entryTag} (see expanded for full list)`
          }
        />
      )}
      <span style={{ color: pnlColor, fontWeight: 600 }}>
        {star} {_fmtMoney(r.pnl_net)}
      </span>
      {entryPx !== undefined && exitPx !== undefined && (
        <span style={{ color: 'var(--text-secondary)' }}>
          {_fmtPrice(entryPx)} → {_fmtPrice(exitPx)}
        </span>
      )}
      {duration && (
        <span style={{ color: 'var(--text-secondary)' }}>({_fmtDuration(duration)})</span>
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

/** Copy the rendered text content of ``ref`` to the clipboard.
 *  Uses ``innerText`` (preserves line breaks from block layout)
 *  rather than ``textContent`` (jams everything on one line). */
async function copyDetailsToClipboard(
  ref: React.RefObject<HTMLDivElement | null>,
  setCopied: (v: boolean) => void,
) {
  const el = ref.current;
  if (!el) return;
  const text = el.innerText.trim();
  try {
    await navigator.clipboard.writeText(text);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  } catch {
    // Older browsers / non-secure contexts: fall back to a manual
    // textarea + execCommand. Best-effort; modern Chrome on https
    // takes the path above.
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try { document.execCommand('copy'); } catch { /* swallow */ }
    document.body.removeChild(ta);
    setCopied(true);
    window.setTimeout(() => setCopied(false), 1200);
  }
}

function DetailFooter({ onCopy, onShowRaw, copied }: {
  onCopy: () => void;
  onShowRaw: () => void;
  copied: boolean;
}) {
  const btn = {
    background: 'transparent',
    border: '1px solid var(--border-default)',
    color: 'var(--text-secondary)',
    cursor: 'pointer',
    fontSize: 10, padding: '2px 6px', borderRadius: 3,
  };
  return (
    <div style={{
      marginTop: 6, display: 'flex',
      justifyContent: 'flex-end', gap: 6,
    }}>
      <button
        onClick={(e) => { e.stopPropagation(); onCopy(); }}
        title="copy details as text"
        style={btn}
      >
        {copied ? '✓ copied' : '📋 copy'}
      </button>
      <button
        onClick={(e) => { e.stopPropagation(); onShowRaw(); }}
        title="show raw JSON dump for this row"
        style={btn}
      >
        {'{ }'} raw JSON
      </button>
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
  const signalEntryLine = (row.payload as AuditPayload | null)
    ?.signal?.entry_line;
  const isAccel = signalEntryLine?.entry_path === 'accel';
  const [nextClose, setNextClose] = useState<number | null | 'pending'>('pending');
  const detailsRef = useRef<HTMLDivElement | null>(null);
  const [copied, setCopied] = useState(false);
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
  // P anchor with timestamps and prices). When the pivot didn't
  // touch anything but an ACCEL signal fired, surface the accel
  // line instead (the bot's entry was based on it, even though the
  // pivot is past the line, not on it).
  const touchedLines = a.touch?.lines ?? [];
  let lineEntries: React.ReactNode[];
  if (touchedLines.length > 0) {
    lineEntries = touchedLines.map((ln, i) => {
      const summary = `${ln.kind ?? '?'} · ${ln.touches ?? '?'}-touch · `
        + `slope/bar=${(ln.slope_per_bar ?? 0).toFixed(4)} · `
        + `Q@${_fmtPT(ln.anchor_q_time ?? null)} (${_fmtPrice(ln.anchor_q_close)}) · `
        + `P@${_fmtPT(ln.anchor_b_time ?? null)} (${_fmtPrice(ln.anchor_b_close)})`;
      return (
        <DetailLine key={i} label={`line ${i + 1}`} value={summary} />
      );
    });
  } else if (isAccel && signalEntryLine) {
    const sel = signalEntryLine as Record<string, unknown>;
    const kind = typeof sel.kind === 'string' ? sel.kind : '?';
    const touches = typeof sel.touches === 'number' ? sel.touches : '?';
    const slope = typeof sel.slope_per_bar === 'number'
      ? sel.slope_per_bar : 0;
    const fromTime = typeof sel.from_time === 'string'
      ? sel.from_time : null;
    const anchorTime = typeof sel.anchor_time === 'string'
      ? sel.anchor_time : null;
    const anchorPrice = typeof sel.anchor_price === 'number'
      ? sel.anchor_price : null;
    const lineValAtEntry = typeof sel.line_value_at_entry === 'number'
      ? sel.line_value_at_entry : null;
    const summary = `${kind} · ${touches}-touch · `
      + `slope/bar=${slope.toFixed(4)} · `
      + `Q@${_fmtPT(fromTime)} · `
      + `P@${_fmtPT(anchorTime)} (${_fmtPrice(anchorPrice)}) · `
      + `line@entry=${_fmtPrice(lineValAtEntry)}`;
    lineEntries = [
      <DetailLine key="accel" label="accel line" value={summary} />,
    ];
  } else {
    lineEntries = [<DetailLine key="none" label="lines" value="—" />];
  }

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
      <div ref={detailsRef}>
        <DetailLine label="prior close" value={_fmtPrice(a.prior_bar_close ?? null)} />
        <DetailLine
          label="next close"
          value={evalClose !== null
            ? `${_fmtPrice(evalClose)}  (eval @ ${evalAt})`
            : nextClose === 'pending' ? '…'
            : nextClose === null ? '— (bar not closed yet)'
            : _fmtPrice(nextClose)}
        />
        <DetailLine
          label="filter"
          value={
            isAccel
              ? '— (acceleration entry — line overshot, far_from_pivot exempt)'
              : !(a.touch?.lines ?? []).some((ln) => (ln.touches ?? 0) >= 3)
              ? 'N/A — no current-session 3+touch candidate'
              : a.filter_name
              ? _fmtFilter(a.filter_name, a.filter_detail ?? '')
              : '— (passed all filters)'
          }
        />
        {lineEntries}
        <DetailLine label="outcome" value={
          a.outcome === 'B' ? 'BUY · entry order placed'
          : a.outcome === 'S' ? 'SELL · entry order placed'
          : a.outcome === 'exit' ? 'EXIT · trailing stop or counter-line fired'
          : a.outcome ?? '—'
        } />
      </div>
      <DetailFooter
        onCopy={() => copyDetailsToClipboard(detailsRef, setCopied)}
        onShowRaw={onShowRaw}
        copied={copied}
      />
    </div>
  );
}

function ExpandedTradeClosed({ row, onShowRaw }: {
  row: AuditRow; onShowRaw: () => void;
}) {
  // Mirrors the BotTradesPanel detail layout — Entry / Exit / P&L
  // breakdown / Trail resets / Bot / Serials. Operator ask
  // 2026-05-18: "details can be replica of the info we put in the
  // bot-trades pane".
  const p = row.payload as Record<string, unknown> | null;
  const direction = row.decision.split('·')[1] || '';
  const reason = row.decision.split('·').slice(2).join('·') || '—';
  const detailsRef = useRef<HTMLDivElement | null>(null);
  const [copied, setCopied] = useState(false);
  const get = (k: string) => p?.[k] as string | number | undefined;
  const getStr = (k: string) => {
    const v = p?.[k];
    return v === undefined || v === null ? null : String(v);
  };
  const fmtPnlPart = (v: string | null) => {
    if (v === null) return { text: '—', color: 'var(--text-secondary)' };
    const n = Number(v);
    if (!Number.isFinite(n)) return { text: '—', color: 'var(--text-secondary)' };
    const sign = n >= 0 ? '+' : '-';
    return {
      text: `${sign}$${Math.abs(n).toFixed(2)}`,
      color: n >= 0 ? '#16a34a' : '#dc2626',
    };
  };
  const fmtPriceQty = (px: string | null, qty: string | null) => {
    if (px === null) return '—';
    const pxF = Number(px);
    const qF = qty ? Number(qty) : NaN;
    const pxStr = Number.isFinite(pxF) ? `$${pxF.toFixed(2)}` : px;
    if (Number.isFinite(qF)) {
      const qStr = Number.isInteger(qF) ? String(qF) : qF.toFixed(4);
      return `${pxStr} × ${qStr}`;
    }
    return pxStr;
  };
  const grossStr = getStr('gross_pnl') ?? getStr('realized_pnl');
  const commissionStr = getStr('commission');
  const netStr = getStr('net_pnl');
  const commSource = getStr('commission_source');
  const gross = fmtPnlPart(grossStr);
  const commission = commissionStr ? Number(commissionStr) : 0;
  const net = fmtPnlPart(netStr);

  const labelStyle: React.CSSProperties = {
    color: 'var(--text-muted)', fontSize: 10,
    textTransform: 'uppercase', letterSpacing: '0.05em',
    marginBottom: 3,
  };
  const valueStyle: React.CSSProperties = {
    fontSize: 13, color: 'var(--text-primary)',
    fontWeight: 600,
    fontFamily: 'var(--font-mono, ui-monospace, monospace)',
  };
  const metaStyle: React.CSSProperties = {
    fontSize: 10, color: 'var(--text-muted)',
    fontFamily: 'var(--font-mono, ui-monospace, monospace)',
    marginTop: 2,
  };

  return (
    <div style={{
      marginTop: 6, marginLeft: 8,
      padding: '12px 14px',
      background: 'var(--panel-bg-alt, rgba(0,0,0,0.04))',
      borderLeft: '2px solid var(--border-default)',
    }}>
      <div ref={detailsRef} style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: '10px 18px',
      }}>
        <div>
          <div style={labelStyle}>Entry</div>
          <div style={valueStyle}>
            {fmtPriceQty(getStr('entry_price'), getStr('entry_qty'))}
          </div>
          <div style={metaStyle}>{_fmtPT(String(get('entry_time') ?? '')) || '—'}</div>
        </div>
        <div>
          <div style={labelStyle}>Exit</div>
          <div style={valueStyle}>
            {fmtPriceQty(getStr('exit_price'), getStr('exit_qty'))}
          </div>
          <div style={metaStyle}>{_fmtPT(String(get('exit_time') ?? '')) || '—'}</div>
        </div>
        <div>
          <div style={labelStyle}>P&amp;L breakdown</div>
          <div style={{ ...valueStyle, color: gross.color }}>
            Gross {gross.text}
          </div>
          <div style={metaStyle}>
            − ${commission.toFixed(2)} commission
            {commSource === 'fallback_standard' && (
              <span style={{ color: '#b45309', marginLeft: 4 }}>
                (est. — backfill pending)
              </span>
            )}
          </div>
          <div style={{ ...valueStyle, color: net.color, marginTop: 2 }}>
            Net {net.text}
          </div>
        </div>
        <div>
          <div style={labelStyle}>Direction · Reason</div>
          <div style={valueStyle}>{direction}</div>
          <div style={metaStyle}>{reason}</div>
        </div>
        <div>
          <div style={labelStyle}>Duration · Trail resets</div>
          <div style={valueStyle}>
            {_fmtDuration(get('duration_seconds') as number | undefined)}
          </div>
          <div style={metaStyle}>
            {String(get('trail_reset_count') ?? 0)} resets
          </div>
        </div>
        {(() => {
          // Entry classification block — entry_path + full filter list.
          // Always rendered for new rows (always have entry_path);
          // omitted on legacy rows that pre-date this payload extension.
          const ep = getStr('entry_path');
          if (!ep) return null;
          const filters = (p?.marginal_filters as string[] | undefined) ?? [];
          let label = 'Entry';
          let primary: string;
          let secondary: string | null = null;
          if (filters.length > 0) {
            primary = `marginal · ${ep}`;
            secondary = `bypassed: ${filters.join(', ')}`;
          } else if (ep === 'accel') {
            primary = 'accel';
            secondary = 'acceleration-continuation';
          } else {
            primary = 'clean';
            secondary = 'no filters bypassed';
          }
          return (
            <div>
              <div style={labelStyle}>{label}</div>
              <div style={valueStyle}>{primary}</div>
              {secondary && <div style={metaStyle}>{secondary}</div>}
            </div>
          );
        })()}
        {getStr('bot_name') && (
          <div>
            <div style={labelStyle}>Bot</div>
            <div style={valueStyle}>{getStr('bot_name')}</div>
          </div>
        )}
        {(getStr('entry_serial') || getStr('exit_serial')) && (
          <div>
            <div style={labelStyle}>Serials</div>
            <div style={valueStyle}>
              #{getStr('entry_serial') ?? '—'} → #{getStr('exit_serial') ?? '—'}
            </div>
          </div>
        )}
      </div>
      <DetailFooter
        onCopy={() => copyDetailsToClipboard(detailsRef, setCopied)}
        onShowRaw={onShowRaw}
        copied={copied}
      />
    </div>
  );
}

function ExpandedOrder({ row, onShowRaw }: {
  row: AuditRow; onShowRaw: () => void;
}) {
  const p = row.payload as Record<string, unknown> | null;
  const get = (k: string) => (p?.[k] as string | undefined);
  const ec = (p?.exit_context as Record<string, unknown> | undefined) ?? {};
  const detailsRef = useRef<HTMLDivElement | null>(null);
  const [copied, setCopied] = useState(false);
  return (
    <div style={{
      marginTop: 6, marginLeft: 8,
      padding: '8px 10px',
      background: 'var(--panel-bg-alt, rgba(0,0,0,0.04))',
      borderLeft: '2px solid var(--border-default)',
    }}>
      <div ref={detailsRef}>
        <DetailLine label="side"       value={get('side') || '—'} />
        <DetailLine label="qty"        value={get('qty') || '—'} />
        <DetailLine label="order type" value={get('order_type') || '—'} />
        <DetailLine label="origin"     value={get('origin') || '—'} />
        {Object.keys(ec).length > 0 && (
          <DetailLine label="exit context"
            value={JSON.stringify(ec).slice(0, 200)} />
        )}
      </div>
      <DetailFooter
        onCopy={() => copyDetailsToClipboard(detailsRef, setCopied)}
        onShowRaw={onShowRaw}
        copied={copied}
      />
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
  // Click + userSelect:none only on the header so it toggles cleanly
  // without swallowing text selection. The expanded body sits as a
  // sibling and is freely selectable (and stops click propagation so
  // dragging-to-select doesn't collapse the row).
  return (
    <div
      style={{
        borderBottom: '1px solid var(--border-default)',
      }}
    >
      <div
        onClick={() => setExpanded((v) => !v)}
        style={{
          padding: '5px 8px',
          cursor: 'pointer',
          userSelect: 'none',
        }}
      >
        {row.event_type === 'BAR_EVAL' && <BarEvalRow r={row} />}
        {row.event_type === 'ORDER_PLACED' && <OrderRow r={row} />}
        {row.event_type === 'TRADE_CLOSED' && <TradeClosedRow r={row} />}
      </div>
      {expanded && (
        <div
          onClick={(e) => e.stopPropagation()}
          onMouseDown={(e) => e.stopPropagation()}
          style={{
            padding: '0 8px 5px',
            userSelect: 'text',
          }}
        >
          {row.event_type === 'BAR_EVAL'
            && <ExpandedBarEval row={row} onShowRaw={() => onShowRaw(row)} />}
          {row.event_type === 'TRADE_CLOSED'
            && <ExpandedTradeClosed row={row} onShowRaw={() => onShowRaw(row)} />}
          {row.event_type === 'ORDER_PLACED'
            && <ExpandedOrder row={row} onShowRaw={() => onShowRaw(row)} />}
        </div>
      )}
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
