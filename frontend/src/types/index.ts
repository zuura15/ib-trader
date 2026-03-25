export type ConnectionStatus = 'connected' | 'disconnected' | 'reconnecting';
export type AccountMode = 'paper' | 'live';
export type Severity = 'info' | 'warning' | 'error' | 'success' | 'debug';
export type OrderStatus = 'submitted' | 'pending' | 'partial' | 'filled' | 'cancelled' | 'rejected' | 'error';
export type OrderSource = 'manual' | 'bot' | 'system' | 'external';
export type CommandStatus = 'queued' | 'running' | 'success' | 'failure';
export type BotStatus = 'running' | 'stopped' | 'error' | 'paused';
export type AlertSeverity = 'catastrophic' | 'warning';
export type LayoutVariant = 'A' | 'B' | 'C' | 'D';
export type ThemeMode = 'dark' | 'charcoal' | 'navy' | 'mocha' | 'light';

export interface WatchlistItem {
  symbol: string;
  last: string | null;
  change: string | null;
  change_pct: string | null;
  volume: string | null;
  avg_volume: string | null;
  high: string | null;
  low: string | null;
  high_52w: string | null;
  low_52w: string | null;
  error: string | null;
}

export interface OrderTemplate {
  id: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  orderType: 'LMT' | 'MKT' | 'STP' | 'MOC';
  price?: number;
  label: string;
}

export interface GlobalState {
  connectionStatus: ConnectionStatus;
  accountMode: AccountMode;
  serviceHealth: Record<string, boolean>;
  staleData: boolean;
  dailyPnl: number;
  unrealizedPnl: number;
  realizedPnl: number;
  sessionUptime: number;
}

export interface LogEntry {
  id: string;
  timestamp: Date;
  level: Severity;
  event: string;
  message: string;
  details?: Record<string, unknown>;
}

export interface Order {
  id: string;
  symbol: string;
  side: 'BUY' | 'SELL';
  quantity: number;
  filledQty: number;
  orderType: 'LMT' | 'MKT' | 'STP' | 'STP_LMT' | 'MOC';
  limitPrice?: number;
  stopPrice?: number;
  status: OrderStatus;
  source: OrderSource;
  submittedAt: Date;
  lastUpdate: Date;
  avgFillPrice?: number;
  commission?: number;
  rejectReason?: string;
}

export interface Position {
  symbol: string;
  quantity: number;
  avgCost: number;
  markPrice: number;
  unrealizedPnl: number;
  realizedPnl: number;
  dailyPnl: number;
  lastUpdate: Date;
}

export interface Alert {
  id: string;
  severity: AlertSeverity;
  title: string;
  message: string;
  timestamp: Date;
  dismissed: boolean;
  source: string;
  details?: string;
}

export interface Bot {
  id: string;
  name: string;
  strategy: string;
  status: BotStatus;
  lastHeartbeat: Date;
  lastSignal?: string;
  lastAction?: string;
  lastActionTime?: Date;
  errorMessage?: string;
  tradesTotal: number;
  tradesToday: number;
  pnlToday: number;
  symbols: string[];
  uptime: number;
}

export interface TradeGroup {
  id: string;
  serialNumber: number;
  symbol: string;
  direction: string;
  status: string;
  realizedPnl: string | null;
  totalCommission: string | null;
  openedAt: string;
  closedAt: string | null;
}

export interface CommandEntry {
  id: string;
  command: string;
  status: CommandStatus;
  output?: string;
  startedAt: Date;
  completedAt?: Date;
}

export type ScenarioName =
  | 'healthy'
  | 'ib_disconnected'
  | 'reconnecting'
  | 'paper_mode'
  | 'live_warning'
  | 'command_running'
  | 'command_failure'
  | 'partial_fill'
  | 'order_rejection'
  | 'broker_burst'
  | 'stale_data'
  | 'bot_heartbeat_missing'
  | 'reconciliation_mismatch';
