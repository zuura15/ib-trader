import type {
  AlertItem,
  BotItem,
  CommandEntry,
  HeaderStatus,
  LogEntry,
  Order,
  Position,
  WorkstationState,
} from "../types/models";

const now = () => new Date().toISOString();

export const initialHeader: HeaderStatus = {
  ibConnection: "connected",
  sessionConnectivity: "connected",
  accountMode: "paper",
  serviceHealth: "healthy",
  staleData: false,
  warningState: false,
  pnl: 12843.12,
};

export const initialCommands: CommandEntry[] = [
  {
    id: "cmd-1",
    command: "orders sync --scope day",
    state: "success",
    startedAt: now(),
    finishedAt: now(),
    response: "Synchronized 14 open orders from local engine cache.",
  },
  {
    id: "cmd-2",
    command: "bot restart meanrev-eq",
    state: "queued",
    startedAt: now(),
    response: "Queued for execution on supervisor channel.",
  },
];

export const initialLogs: LogEntry[] = [
  { id: "log-1", timestamp: now(), severity: "info", source: "gateway", message: "IB gateway heartbeat normal." },
  { id: "log-2", timestamp: now(), severity: "warning", source: "risk", message: "Exposure nearing overnight band on NVDA." },
  { id: "log-3", timestamp: now(), severity: "info", source: "engine", message: "Background reconciliation completed." },
  { id: "log-4", timestamp: now(), severity: "info", source: "bot.alpha", message: "Signal generated for ES mean reversion basket." },
];

export const initialOrders: Order[] = [
  { id: "O-24013", symbol: "AAPL", side: "BUY", qty: 300, filledQty: 300, limitPrice: 184.21, status: "Filled", source: "manual", ageSec: 58, route: "SMART" },
  { id: "O-24014", symbol: "NVDA", side: "SELL", qty: 120, filledQty: 60, limitPrice: 907.5, status: "PartialFill", source: "bot", ageSec: 131, route: "ISLAND" },
  { id: "O-24015", symbol: "MSFT", side: "BUY", qty: 200, filledQty: 0, limitPrice: 417.1, status: "Submitted", source: "system", ageSec: 22, route: "SMART" },
  { id: "O-24016", symbol: "AMD", side: "SELL", qty: 150, filledQty: 0, limitPrice: 168.3, status: "PendingSubmit", source: "external", ageSec: 9, route: "ARCA" },
];

export const initialPositions: Position[] = [
  { symbol: "AAPL", qty: 800, avgCost: 181.2, mark: 184.19, unrealizedPnl: 2392, realizedPnl: 1840 },
  { symbol: "MSFT", qty: -200, avgCost: 420.7, mark: 417.08, unrealizedPnl: 724, realizedPnl: -240 },
  { symbol: "ESM6", qty: 2, avgCost: 5298.5, mark: 5302.25, unrealizedPnl: 375, realizedPnl: 1480 },
  { symbol: "NVDA", qty: 120, avgCost: 894.0, mark: 908.4, unrealizedPnl: 1728, realizedPnl: 620 },
];

export const initialAlerts: AlertItem[] = [
  {
    id: "A-100",
    title: "Live routing disabled",
    detail: "Paper mode is enabled. Manual enablement required before live routing becomes available.",
    severity: "warning",
    state: "active",
    timestamp: now(),
  },
  {
    id: "A-101",
    title: "Latency spike resolved",
    detail: "Order acknowledgement latency exceeded 650ms for 14 seconds before returning to baseline.",
    severity: "info",
    state: "dismissed",
    timestamp: now(),
  },
];

export const initialBots: BotItem[] = [
  { id: "B-1", name: "meanrev-eq", strategy: "US equities mean reversion", heartbeatSec: 2, lastSignal: "SELL NVDA", lastAction: "Updated working order O-24014", errorState: false, mode: "active", pnl: 4260 },
  { id: "B-2", name: "index-arb", strategy: "Index future hedge arb", heartbeatSec: 4, lastSignal: "BUY ESM6", lastAction: "No-op, spread inside threshold", errorState: false, mode: "active", pnl: 1970 },
  { id: "B-3", name: "close-auction", strategy: "Closing imbalance capture", heartbeatSec: 11, lastSignal: "Awaiting imbalance feed", lastAction: "Staged watchlist", errorState: false, mode: "degraded", pnl: -180 },
];

export const createInitialState = (): WorkstationState => ({
  header: { ...initialHeader },
  commands: [...initialCommands],
  logs: [...initialLogs],
  orders: [...initialOrders],
  positions: [...initialPositions],
  alerts: [...initialAlerts],
  bots: [...initialBots],
  selectedOrderId: "O-24014",
  selectedAlertId: "A-100",
  activeScenario: "healthy",
});
