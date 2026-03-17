export type VariantId = "A" | "B" | "C" | "D";

export type Severity = "info" | "warning" | "error" | "success";
export type ConnectionState = "connected" | "disconnected" | "reconnecting";
export type CommandState = "idle" | "queued" | "running" | "success" | "failure";
export type AccountMode = "paper" | "live";
export type OrderStatus =
  | "PendingSubmit"
  | "Submitted"
  | "PartialFill"
  | "Filled"
  | "Rejected"
  | "Cancelled";

export interface HeaderStatus {
  ibConnection: ConnectionState;
  sessionConnectivity: ConnectionState;
  accountMode: AccountMode;
  serviceHealth: "healthy" | "degraded" | "critical";
  staleData: boolean;
  warningState: boolean;
  pnl: number;
}

export interface CommandEntry {
  id: string;
  command: string;
  state: CommandState;
  startedAt: string;
  finishedAt?: string;
  response: string;
}

export interface LogEntry {
  id: string;
  timestamp: string;
  severity: Severity;
  source: string;
  message: string;
}

export interface Order {
  id: string;
  symbol: string;
  side: "BUY" | "SELL";
  qty: number;
  filledQty: number;
  limitPrice: number;
  status: OrderStatus;
  source: "system" | "bot" | "manual" | "external";
  ageSec: number;
  route: string;
}

export interface Position {
  symbol: string;
  qty: number;
  avgCost: number;
  mark: number;
  unrealizedPnl: number;
  realizedPnl: number;
}

export interface AlertItem {
  id: string;
  title: string;
  detail: string;
  severity: Severity;
  state: "active" | "dismissed";
  timestamp: string;
}

export interface BotItem {
  id: string;
  name: string;
  strategy: string;
  heartbeatSec: number;
  lastSignal: string;
  lastAction: string;
  errorState: boolean;
  mode: "active" | "degraded" | "halted";
  pnl: number;
}

export interface ScenarioDefinition {
  id: string;
  label: string;
  description: string;
}

export interface WorkstationState {
  header: HeaderStatus;
  commands: CommandEntry[];
  logs: LogEntry[];
  orders: Order[];
  positions: Position[];
  alerts: AlertItem[];
  bots: BotItem[];
  selectedOrderId: string;
  selectedAlertId: string;
  activeScenario: string;
}
