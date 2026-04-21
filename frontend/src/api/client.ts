/**
 * HTTP client for the IB Trader API.
 *
 * All API calls go through this module. Base URL is configurable
 * via environment variable or defaults to the Vite proxy.
 */

const BASE_URL = import.meta.env.VITE_API_URL || '/api';

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const url = `${BASE_URL}${path}`;
  const res = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options?.headers,
    },
  });

  if (!res.ok) {
    const body = await res.text();
    throw new Error(`API ${res.status}: ${body}`);
  }

  // 204 No Content
  if (res.status === 204) return undefined as T;

  return res.json();
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
  created_at: string;
}

export function getBotTrades(botId?: string, limit: number = 500) {
  const params = new URLSearchParams();
  if (botId) params.set("bot_id", botId);
  params.set("limit", String(limit));
  return request<BotTradeResponse[]>(`/bot-trades?${params}`);
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
