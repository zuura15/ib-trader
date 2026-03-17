import { v4 as uuid } from 'uuid';
import type { Order, Position, Bot, LogEntry, Alert, CommandEntry } from '../types';

const now = () => new Date();
const ago = (ms: number) => new Date(Date.now() - ms);

export const mockPositions: Position[] = [
  { symbol: 'AAPL', quantity: 500, avgCost: 178.42, markPrice: 182.15, unrealizedPnl: 1865.00, realizedPnl: 420.50, dailyPnl: 312.00, lastUpdate: ago(5000) },
  { symbol: 'MSFT', quantity: -200, avgCost: 415.30, markPrice: 412.80, unrealizedPnl: 500.00, realizedPnl: -180.25, dailyPnl: -95.00, lastUpdate: ago(8000) },
  { symbol: 'SPY', quantity: 1000, avgCost: 512.18, markPrice: 514.22, unrealizedPnl: 2040.00, realizedPnl: 1250.00, dailyPnl: 680.00, lastUpdate: ago(3000) },
  { symbol: 'TSLA', quantity: -100, avgCost: 245.60, markPrice: 248.90, unrealizedPnl: -330.00, realizedPnl: 0, dailyPnl: -180.00, lastUpdate: ago(12000) },
  { symbol: 'NVDA', quantity: 300, avgCost: 875.40, markPrice: 882.10, unrealizedPnl: 2010.00, realizedPnl: 3400.00, dailyPnl: 450.00, lastUpdate: ago(7000) },
  { symbol: 'QQQ', quantity: 400, avgCost: 438.75, markPrice: 440.20, unrealizedPnl: 580.00, realizedPnl: 890.00, dailyPnl: 220.00, lastUpdate: ago(4000) },
  { symbol: 'META', quantity: -150, avgCost: 520.10, markPrice: 518.45, unrealizedPnl: 247.50, realizedPnl: -60.00, dailyPnl: 165.00, lastUpdate: ago(9000) },
  { symbol: 'AMZN', quantity: 200, avgCost: 185.20, markPrice: 186.80, unrealizedPnl: 320.00, realizedPnl: 540.00, dailyPnl: 110.00, lastUpdate: ago(6000) },
];

export const mockOrders: Order[] = [
  { id: uuid(), symbol: 'AAPL', side: 'BUY', quantity: 100, filledQty: 0, orderType: 'LMT', limitPrice: 180.50, status: 'submitted', source: 'manual', submittedAt: ago(120000), lastUpdate: ago(120000) },
  { id: uuid(), symbol: 'SPY', side: 'SELL', quantity: 200, filledQty: 150, orderType: 'LMT', limitPrice: 515.00, status: 'partial', source: 'bot', submittedAt: ago(300000), lastUpdate: ago(45000), avgFillPrice: 514.88, commission: 1.50 },
  { id: uuid(), symbol: 'MSFT', side: 'BUY', quantity: 50, filledQty: 50, orderType: 'MKT', status: 'filled', source: 'manual', submittedAt: ago(600000), lastUpdate: ago(580000), avgFillPrice: 413.22, commission: 0.75 },
  { id: uuid(), symbol: 'NVDA', side: 'BUY', quantity: 25, filledQty: 0, orderType: 'STP', stopPrice: 870.00, status: 'submitted', source: 'bot', submittedAt: ago(900000), lastUpdate: ago(900000) },
  { id: uuid(), symbol: 'TSLA', side: 'SELL', quantity: 100, filledQty: 100, orderType: 'LMT', limitPrice: 250.00, status: 'filled', source: 'system', submittedAt: ago(1800000), lastUpdate: ago(1750000), avgFillPrice: 250.10, commission: 1.00 },
  { id: uuid(), symbol: 'META', side: 'BUY', quantity: 75, filledQty: 0, orderType: 'LMT', limitPrice: 515.00, status: 'submitted', source: 'external', submittedAt: ago(60000), lastUpdate: ago(60000) },
  { id: uuid(), symbol: 'QQQ', side: 'SELL', quantity: 150, filledQty: 0, orderType: 'MOC', status: 'pending', source: 'bot', submittedAt: ago(30000), lastUpdate: ago(30000) },
];

export const mockBots: Bot[] = [
  { id: uuid(), name: 'MeanRevert-SPY', strategy: 'Mean Reversion', status: 'running', lastHeartbeat: ago(2000), lastSignal: 'BUY signal at 512.40', lastAction: 'Placed LMT BUY 200 SPY @ 512.18', lastActionTime: ago(180000), tradesTotal: 847, tradesToday: 12, pnlToday: 680.00, symbols: ['SPY', 'QQQ'], uptime: 86400000 },
  { id: uuid(), name: 'MomentumAlpha', strategy: 'Momentum Breakout', status: 'running', lastHeartbeat: ago(5000), lastSignal: 'Watching NVDA breakout at 885', lastAction: 'No action - below threshold', lastActionTime: ago(300000), tradesTotal: 234, tradesToday: 3, pnlToday: 450.00, symbols: ['NVDA', 'AAPL', 'MSFT'], uptime: 172800000 },
  { id: uuid(), name: 'PairsTrade-Tech', strategy: 'Pairs Trading', status: 'paused', lastHeartbeat: ago(1000), lastSignal: 'Spread normalized', lastAction: 'Closed MSFT/META pair', lastActionTime: ago(3600000), tradesTotal: 156, tradesToday: 1, pnlToday: 165.00, symbols: ['MSFT', 'META'], uptime: 43200000 },
  { id: uuid(), name: 'VWAPScalper', strategy: 'VWAP Scalping', status: 'running', lastHeartbeat: ago(3000), lastSignal: 'AMZN above VWAP', lastAction: 'Placed LMT BUY 50 AMZN @ 186.50', lastActionTime: ago(120000), tradesTotal: 1523, tradesToday: 28, pnlToday: 110.00, symbols: ['AMZN', 'AAPL'], uptime: 28800000 },
  { id: uuid(), name: 'OvernightHedge', strategy: 'Overnight Hedge', status: 'stopped', lastHeartbeat: ago(14400000), lastSignal: 'Market closed', lastAction: 'Hedged portfolio with SPY puts', lastActionTime: ago(14400000), tradesTotal: 45, tradesToday: 0, pnlToday: 0, symbols: ['SPY'], uptime: 0 },
];

const logMessages: Array<{ level: LogEntry['level']; event: string; message: string }> = [
  { level: 'info', event: 'order.submitted', message: 'Order submitted: LMT BUY 100 AAPL @ 180.50' },
  { level: 'info', event: 'order.filled', message: 'Order filled: MKT BUY 50 MSFT @ 413.22' },
  { level: 'info', event: 'order.partial', message: 'Partial fill: 150/200 SPY @ 514.88' },
  { level: 'warning', event: 'connection.latency', message: 'IB API latency elevated: 340ms (threshold: 200ms)' },
  { level: 'info', event: 'bot.heartbeat', message: 'MeanRevert-SPY heartbeat OK — 12 trades today' },
  { level: 'info', event: 'bot.signal', message: 'MomentumAlpha: Watching NVDA breakout at 885' },
  { level: 'debug', event: 'throttle.wait', message: 'Rate limiter: waited 100ms before reqOpenOrders()' },
  { level: 'info', event: 'position.update', message: 'Position mark update: NVDA 882.10 (+0.76%)' },
  { level: 'warning', event: 'reconciliation.mismatch', message: 'Position mismatch: TSLA local=-100, IB=-100 (OK)' },
  { level: 'info', event: 'command.executed', message: 'Command completed: status — 8 positions, 3 open orders' },
  { level: 'info', event: 'session.heartbeat', message: 'Session heartbeat: uptime 4h 23m, 47 orders today' },
  { level: 'info', event: 'pnl.update', message: 'Daily P&L update: +$1,667.00 unrealized, +$6,260.25 realized' },
  { level: 'warning', event: 'order.age', message: 'Order aging warning: AAPL LMT BUY open for 15m' },
  { level: 'info', event: 'market.data', message: 'Market data snapshot refreshed — 8 symbols' },
  { level: 'info', event: 'bot.action', message: 'VWAPScalper: Placed LMT BUY 50 AMZN @ 186.50' },
];

export function generateLogEntry(): LogEntry {
  const template = logMessages[Math.floor(Math.random() * logMessages.length)];
  return {
    id: uuid(),
    timestamp: now(),
    level: template.level,
    event: template.event,
    message: template.message,
  };
}

export function generateInitialLogs(count: number): LogEntry[] {
  const logs: LogEntry[] = [];
  for (let i = 0; i < count; i++) {
    const template = logMessages[Math.floor(Math.random() * logMessages.length)];
    logs.push({
      id: uuid(),
      timestamp: ago((count - i) * 3000),
      level: template.level,
      event: template.event,
      message: template.message,
    });
  }
  return logs;
}

export const mockAlerts: Alert[] = [
  { id: uuid(), severity: 'warning', title: 'Elevated API Latency', message: 'IB API response time averaging 340ms over last 5 minutes', timestamp: ago(300000), dismissed: false, source: 'connection_monitor' },
  { id: uuid(), severity: 'warning', title: 'Order Aging', message: 'AAPL LMT BUY has been open for 15 minutes without fill', timestamp: ago(180000), dismissed: false, source: 'order_monitor', details: 'Order ID: abc123\nSymbol: AAPL\nLimit: 180.50\nCurrent ask: 182.15' },
];

export const mockCommands: CommandEntry[] = [
  { id: uuid(), command: 'status', status: 'success', output: '8 positions, 3 open orders, 5 bots (3 running)', startedAt: ago(600000), completedAt: ago(599000) },
  { id: uuid(), command: 'buy AAPL 100 @ 180.50', status: 'success', output: 'Order submitted: LMT BUY 100 AAPL @ 180.50', startedAt: ago(120000), completedAt: ago(119500) },
  { id: uuid(), command: 'positions', status: 'success', output: 'Showing 8 positions. Net unrealized: +$6,232.50', startedAt: ago(60000), completedAt: ago(59500) },
];

export const commandSuggestions = [
  'status', 'positions', 'orders', 'bots', 'alerts',
  'buy <symbol> <qty> [@ price]', 'sell <symbol> <qty> [@ price]',
  'cancel <order_id>', 'cancel all',
  'bot start <name>', 'bot stop <name>', 'bot status <name>',
  'reconcile', 'health', 'pnl', 'risk',
];
