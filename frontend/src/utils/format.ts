/**
 * Parse a UTC timestamp string from the API into a proper Date object.
 *
 * The API returns naive UTC timestamps (no Z suffix). Without appending Z,
 * JavaScript's Date constructor treats the string as local time, causing
 * all times to display wrong. This helper ensures UTC parsing so the
 * browser then converts to the user's local timezone for display.
 */
export function parseUTC(ts: string | Date | null | undefined): Date {
  if (!ts) return new Date();
  if (ts instanceof Date) return ts;
  // Append Z if the string has no timezone indicator
  const s = ts.endsWith('Z') || ts.includes('+') || ts.includes('-', 10) ? ts : ts + 'Z';
  return new Date(s);
}

export function formatCurrency(value: number): string {
  const abs = Math.abs(value);
  const sign = value >= 0 ? '+' : '-';
  return `${sign}$${abs.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

export function formatPrice(value: number): string {
  return value.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

/** Format a date/timestamp as local time HH:MM:SS */
export function formatTime(date: Date | string): string {
  const d = parseUTC(date);
  return d.toLocaleTimeString('en-US', { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

/** Format a date/timestamp as local short datetime: Mar 16, 9:12 PM */
export function formatDateTime(date: Date | string | null): string {
  if (!date) return '—';
  const d = parseUTC(date);
  return d.toLocaleString('en-US', {
    month: 'short', day: 'numeric',
    hour: 'numeric', minute: '2-digit', second: '2-digit',
    hour12: true,
  });
}

export function formatTimestamp(date: Date | string): string {
  const d = parseUTC(date);
  return `${d.toLocaleDateString('en-US', { month: '2-digit', day: '2-digit' })} ${formatTime(d)}`;
}

export function formatDuration(ms: number): string {
  const s = Math.floor(ms / 1000);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${s % 60}s`;
  return `${s}s`;
}

export function formatAge(date: Date | string): string {
  const d = parseUTC(date);
  const ms = Date.now() - d.getTime();
  if (ms < 0) return '0s';
  if (ms < 60000) return `${Math.floor(ms / 1000)}s`;
  if (ms < 3600000) return `${Math.floor(ms / 60000)}m`;
  return `${Math.floor(ms / 3600000)}h ${Math.floor((ms % 3600000) / 60000)}m`;
}

export function pnlClass(value: number): string {
  if (value > 0) return 'value-positive';
  if (value < 0) return 'value-negative';
  return 'value-neutral';
}

// ------------------------------------------------------------
// Instrument display (Epic 1 Phase 4)
// ------------------------------------------------------------

const MONTH_CODES: Record<number, string> = {
  1: 'F', 2: 'G', 3: 'H', 4: 'J', 5: 'K', 6: 'M',
  7: 'N', 8: 'Q', 9: 'U', 10: 'V', 11: 'X', 12: 'Z',
};

export interface Displayable {
  symbol?: string;
  sec_type?: string | null;
  expiry?: string | null;
  display_symbol?: string | null;
}

/**
 * Normalized instrument display string for every pane (positions, orders,
 * watchlist, trades). FUT rows with an expiry render as ``ES Z26``; STK
 * rows pass through. The backend already supplies ``display_symbol`` for
 * most row shapes — this helper is the fallback when it is absent.
 */
export function formatInstrument(row: Displayable): string {
  if (row.display_symbol) return row.display_symbol;
  const sym = row.symbol || '';
  const sec = (row.sec_type || 'STK').toUpperCase();
  if (sec !== 'FUT' || !row.expiry) return sym;
  const e = String(row.expiry);
  if (!/^\d{6,8}$/.test(e)) return sym;
  const year = Number(e.slice(0, 4));
  const month = Number(e.slice(4, 6));
  const code = MONTH_CODES[month];
  if (!code) return sym;
  return `${sym} ${code}${String(year % 100).padStart(2, '0')}`;
}

/**
 * Clipboard-friendly, IB TWS-compatible symbol shorthand for a futures
 * contract. ``ESZ6``-style. STK passes through.
 */
export function formatIbPasteSymbol(row: Displayable): string {
  const sym = row.symbol || '';
  const sec = (row.sec_type || 'STK').toUpperCase();
  if (sec !== 'FUT' || !row.expiry) return sym;
  const e = String(row.expiry);
  if (!/^\d{6,8}$/.test(e)) return sym;
  const year = Number(e.slice(0, 4));
  const month = Number(e.slice(4, 6));
  const code = MONTH_CODES[month];
  if (!code) return sym;
  return `${sym}${code}${year % 10}`;
}

export type SecType = 'STK' | 'ETF' | 'FUT' | 'OPT';

export function allSecTypes(): SecType[] {
  return ['STK', 'ETF', 'FUT', 'OPT'];
}

/** Filter a list of rows by a set of sec-types (client-side per Epic 1 D11). */
export function filterBySecType<T extends { sec_type?: string | null }>(
  rows: T[],
  allowed: Set<SecType>,
): T[] {
  if (allowed.size === 0 || allowed.size === allSecTypes().length) return rows;
  return rows.filter((r) => allowed.has(((r.sec_type || 'STK').toUpperCase() as SecType)));
}
