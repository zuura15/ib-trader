/**
 * HTTP client for the IB Trader API.
 *
 * All API calls go through this module. Base URL is configurable
 * via environment variable or defaults to the Vite proxy.
 */

const BASE_URL = import.meta.env.VITE_API_URL || '/api';

/** Exposed for components that need to build raw URLs (e.g. EventSource
 *  for SSE streams) — same base the ``request`` helper uses. */
export function getApiBase(): string {
  return BASE_URL;
}

/** Default request timeout. A ``fetch`` with no abort signal hangs
 *  indefinitely (browser default is minutes) if the server stalls.
 *  In a live-updating UI a single hung request can freeze a refresh
 *  loop — notably the chart's 30 s history refresh, where a stalled
 *  ``/engine/history`` used to trap the chart for 3-5 min while
 *  ``historicalLoadedRef`` stayed false and live ticks were dropped
 *  (2026-06-09 chart-freeze investigation). Bounding every request
 *  to a sane ceiling turns "hang forever" into "fail fast + retry on
 *  the next interval". */
const DEFAULT_TIMEOUT_MS = 12_000;

type RequestOptions = RequestInit & {
  /** Override the abort timeout for this call (ms). Heavier endpoints
   *  (history / SR over a long lookback) pass a larger value; it must
   *  stay below the caller's refresh interval so aborted calls don't
   *  pile up behind each other. */
  timeoutMs?: number;
};

async function request<T>(path: string, options?: RequestOptions): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const { timeoutMs = DEFAULT_TIMEOUT_MS, ...fetchOpts } = options ?? {};

  // Compose the caller's signal (if any) with our timeout signal so
  // both an external abort and the timeout cancel the fetch.
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  if (fetchOpts.signal) {
    const ext = fetchOpts.signal;
    if (ext.aborted) controller.abort();
    else ext.addEventListener('abort', () => controller.abort(), { once: true });
  }

  try {
    const res = await fetch(url, {
      ...fetchOpts,
      signal: controller.signal,
      headers: {
        'Content-Type': 'application/json',
        ...fetchOpts.headers,
      },
    });

    if (!res.ok) {
      const body = await res.text();
      throw new Error(`API ${res.status}: ${body}`);
    }

    // 204 No Content
    if (res.status === 204) return undefined as T;

    return res.json();
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') {
      // Surface as a transient-looking error so callers with retry
      // logic (e.g. the chart history loader) treat it like a 5xx and
      // try again on the next cycle rather than giving up.
      throw new Error(`API timeout after ${timeoutMs}ms: ${path}`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

// --- Commands ---

export interface CommandSubmitResponse {
  command_id: string;
  status: string;
}

export interface CommandStatusResponse {
  command_id: string;
  status: string;
  command_text: string;
  source: string;
  output: string | null;
  error: string | null;
  submitted_at: string;
  started_at: string | null;
  completed_at: string | null;
}

export function submitCommand(command: string, commandId?: string, broker = 'ib') {
  return request<CommandSubmitResponse>('/commands', {
    method: 'POST',
    body: JSON.stringify({ command, broker, command_id: commandId }),
  });
}

export function getCommandStatus(cmdId: string) {
  return request<CommandStatusResponse>(`/commands/${cmdId}`);
}

// --- Trades ---

export interface TradeResponse {
  id: string;
  serial_number: number;
  symbol: string;
  direction: string;
  status: string;
  realized_pnl: string | null;
  total_commission: string | null;
  opened_at: string;
  closed_at: string | null;
  // Augmented fill detail from the backend (entry/exit leg fills).
  entry_qty: string | null;
  entry_price: string | null;
  exit_qty: string | null;
  exit_price: string | null;
  order_type: string | null;
}

export function getTrades(status?: string) {
  const qs = status ? `?status=${status}` : '';
  return request<TradeResponse[]>(`/trades${qs}`);
}

// --- Bot Trades ---

export interface BotTradeResponse {
  id: string;
  bot_id: string;
  bot_name: string | null;
  symbol: string;
  direction: string;
  entry_price: string;
  entry_qty: string;
  entry_time: string;
  exit_price: string | null;
  exit_qty: string | null;
  exit_time: string | null;
  realized_pnl: string | null;
  commission: string | null;
  trail_reset_count: number;
  duration_seconds: number | null;
  entry_serial: number | null;
  exit_serial: number | null;
  entry_path: string | null;   // touch | accel | force | <marginal-filter>
  exit_reason: string | null;  // trail_stop | line_breach | counter_line | force_quit | ...
  created_at: string;
}

export function getBotTrades(botId?: string, limit: number = 500) {
  const params = new URLSearchParams();
  if (botId) params.set("bot_id", botId);
  params.set("limit", String(limit));
  return request<BotTradeResponse[]>(`/bot-trades?${params}`);
}

// --- Audit Feed ---

export interface AuditRow {
  id: number;
  bot_id: string;
  symbol: string;
  event_ts_utc: string | null;
  event_type: 'BAR_EVAL' | 'ORDER_PLACED' | 'TRADE_CLOSED';
  pivot_status: string | null;
  line_status: string | null;
  decision: string;
  bar_close: string | null;
  pnl_net: string | null;
  payload: Record<string, unknown> | null;
}

export function getAuditFeed(opts: {
  botId?: string;
  since?: string;
  before?: string;
  limit?: number;
}) {
  const params = new URLSearchParams();
  if (opts.botId) params.set('bot_id', opts.botId);
  if (opts.since) params.set('since', opts.since);
  if (opts.before) params.set('before', opts.before);
  params.set('limit', String(opts.limit ?? 100));
  return request<AuditRow[]>(`/audit?${params}`);
}

// --- Orders ---

export interface OrderResponse {
  id: string;
  trade_id: string;
  serial_number: number | null;
  ib_order_id: string | null;
  leg_type: string;
  symbol: string;
  side: string;
  qty_requested: string;
  qty_filled: string;
  order_type: string;
  status: string;
  price_placed: string | null;
  avg_fill_price: string | null;
  placed_at: string | null;
}

export function getOrders() {
  return request<OrderResponse[]>('/orders');
}

// --- Alerts ---

export interface AlertResponse {
  id: string;
  severity: string;
  trigger: string;
  message: string;
  created_at: string;
  resolved_at: string | null;
}

export function getAlerts() {
  return request<AlertResponse[]>('/alerts');
}

export function resolveAlert(alertId: string) {
  return request<void>(`/alerts/${alertId}/resolve`, { method: 'POST' });
}

// --- System ---

export interface HeartbeatResponse {
  process: string;
  last_seen_at: string;
  pid: number | null;
}

export interface SystemStatusResponse {
  heartbeats: HeartbeatResponse[];
  alerts: AlertResponse[];
}

export function getStatus() {
  return request<SystemStatusResponse>('/status');
}

// --- Bots ---

export interface BotResponse {
  id: string;
  name: string;
  strategy: string;
  broker: string;
  status: string;
  tick_interval_seconds: number;
  last_heartbeat: string | null;
  last_signal: string | null;
  last_action: string | null;
  last_action_at: string | null;
  error_message: string | null;
  trades_total: number;
  trades_today: number;
  pnl_today: string;
  symbols_json: string;
}

export function getBots() {
  return request<BotResponse[]>('/bots');
}

export function startBot(botId: string) {
  return request<{ bot_id: string; status: string }>(`/bots/${botId}/start`, { method: 'POST' });
}

export function stopBot(botId: string) {
  return request<{ bot_id: string; status: string }>(`/bots/${botId}/stop`, { method: 'POST' });
}

export function resetBot(botId: string) {
  return request<{ bot_id: string; state: string; message?: string }>(
    `/bots/${botId}/reset`, { method: 'POST' },
  );
}

// --- Templates ---

export interface TemplateResponse {
  id: string;
  label: string;
  symbol: string;
  side: string;
  quantity: string;
  order_type: string;
  price: string | null;
  broker: string;
}

export function getTemplates() {
  return request<TemplateResponse[]>('/templates');
}

export function createTemplate(data: {
  label: string; symbol: string; side: string;
  quantity: string; order_type: string; price?: string; broker?: string;
}) {
  return request<TemplateResponse>('/templates', {
    method: 'POST',
    body: JSON.stringify(data),
  });
}

export function deleteTemplate(templateId: string) {
  return request<void>(`/templates/${templateId}`, { method: 'DELETE' });
}

// --- Instruments (futures discovery, Epic 1 Phase 1) ---

export interface FutureExpiryCandidate {
  con_id: number;
  root: string;
  expiry: string;          // YYYYMMDD
  trading_class: string;
  exchange: string;
  multiplier: string;      // Decimal-encoded
  tick_size: string;       // Decimal-encoded
  display_symbol: string;  // e.g. "ES Z26"
}

export function listFutureExpiries(
  root: string,
  exchange: string = 'CME',
  tradingClass?: string,
) {
  const qs = new URLSearchParams({ root, exchange });
  if (tradingClass) qs.set('trading_class', tradingClass);
  return request<FutureExpiryCandidate[]>(`/instruments/expiries?${qs.toString()}`);
}

// --- History (chart pane) ---

export interface HistoryBar {
  ts: string;     // ISO timestamp
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
}

export function getHistory(opts: {
  conId?: number | null;
  symbol?: string;
  secType?: string;
  hours?: number;
  barSize?: string;
}) {
  const qs = new URLSearchParams();
  if (opts.conId != null) qs.set('con_id', String(opts.conId));
  if (opts.symbol) qs.set('symbol', opts.symbol);
  if (opts.secType) qs.set('sec_type', opts.secType);
  if (opts.hours != null) qs.set('hours', String(opts.hours));
  if (opts.barSize) qs.set('bar_size', opts.barSize);
  // 25 s: history can legitimately take several seconds (long lookback,
  // cold engine cache, IB round-trip), but must abort well before the
  // chart's 30 s refresh interval so a stalled engine endpoint can't
  // pile up hung fetches and trap the live-tick gate. See the
  // DEFAULT_TIMEOUT_MS comment + the chart history-load effect.
  return request<HistoryBar[]>(`/history?${qs.toString()}`, { timeoutMs: 25_000 });
}

// --- Regime (ADX/ATR/Donchian — chart top-left badge) ---

export interface VState {
  detected: boolean;
  direction: 'v_up' | 'v_down' | null;
  impulse_extreme_idx?: number;
  impulse_extreme_time?: string;
  impulse_extreme_price?: number;
  impulse_magnitude?: number;
  impulse_atr_mult?: number;
  trigger_a_fired: boolean;
  trigger_b_fired: boolean;
  bos_confirmed: boolean;
  retrace_pct?: number;
  bars_since_extreme?: number;
}

export interface RegimeReading {
  regime: 'up' | 'down' | 'flat' | 'uncertain' | 'insufficient';
  n_bars: number;
  sufficient_bars?: boolean;
  last_ts?: string | null;
  adx?: number;
  dmp?: number;  // +DI
  dmn?: number;  // -DI
  atr?: number;
  dcu?: number;  // Donchian upper
  dcl?: number;  // Donchian lower
  v_state?: VState;
}

export function getRegime(opts: {
  conId?: number | null;
  symbol?: string;
  secType?: string;
  hours?: number;
  barSize?: string;
}) {
  const qs = new URLSearchParams();
  if (opts.conId != null) qs.set('con_id', String(opts.conId));
  if (opts.symbol) qs.set('symbol', opts.symbol);
  if (opts.secType) qs.set('sec_type', opts.secType);
  if (opts.hours != null) qs.set('hours', String(opts.hours));
  if (opts.barSize) qs.set('bar_size', opts.barSize);
  return request<RegimeReading>(`/regime?${qs.toString()}`);
}

// --- Console P&L ---

export interface ConsolePnl24h {
  pnl: string;
  count: number;
  since_ms: number;
  until_ms: number;
  window_ms: number;
  error?: string;
}

export function getConsolePnl24h() {
  return request<ConsolePnl24h>('/console/pnl/24h');
}
