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
