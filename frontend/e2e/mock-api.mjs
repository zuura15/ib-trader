/**
 * Mock API server for Playwright E2E tests.
 *
 * Returns predictable fake data for all endpoints so tests can
 * assert UI behavior without a live broker. Also serves a mock
 * WebSocket at /ws.
 */
import http from 'node:http';
import { WebSocketServer } from 'ws';

const PORT = 5198;

// ── Mock Data ──

let commandIdCounter = 1000;
const pendingCommands = new Map();

const mockPositions = [
  { account_id: 'U1234567', symbol: 'AAPL', sec_type: 'STK', quantity: '100', avg_cost: '182.50', broker: 'ib', updated_at: new Date().toISOString() },
  { account_id: 'U1234567', symbol: 'MSFT', sec_type: 'STK', quantity: '-50', avg_cost: '415.30', broker: 'ib', updated_at: new Date().toISOString() },
  { account_id: 'U1234567', symbol: 'SPY', sec_type: 'STK', quantity: '200', avg_cost: '512.18', broker: 'ib', updated_at: new Date().toISOString() },
];

const mockTrades = [
  { id: 't1', serial_number: 10, symbol: 'AAPL', direction: 'LONG', status: 'OPEN', realized_pnl: null, total_commission: null, opened_at: '2026-03-17T10:00:00', closed_at: null },
  { id: 't2', serial_number: 9, symbol: 'QQQ', direction: 'LONG', status: 'CLOSED', realized_pnl: '0.04000000', total_commission: '0E-8', opened_at: '2026-03-17T09:00:00', closed_at: '2026-03-17T09:05:00' },
  { id: 't3', serial_number: 8, symbol: 'MSFT', direction: 'SHORT', status: 'CLOSED', realized_pnl: '-0.55000000', total_commission: '1.00000000', opened_at: '2026-03-16T14:00:00', closed_at: '2026-03-16T15:00:00' },
];

const mockOrders = [
  { id: 'o1', trade_id: 't1', serial_number: 10, ib_order_id: '201', leg_type: 'ENTRY', symbol: 'AAPL', side: 'BUY', security_type: 'STK', qty_requested: '100', qty_filled: '100', order_type: 'MID', status: 'FILLED', price_placed: '182.50', avg_fill_price: '182.45', placed_at: '2026-03-17T10:00:00' },
];

const mockAlerts = [
  { id: 'a1', severity: 'WARNING', trigger: 'RECONCILIATION', message: 'Test alert — order mismatch', created_at: '2026-03-17T10:00:00', resolved_at: null },
];

const mockHeartbeats = [
  { process: 'ENGINE', last_seen_at: new Date().toISOString(), pid: 12345, alive: true, age_seconds: 5 },
  { process: 'API', last_seen_at: new Date().toISOString(), pid: 12346, alive: true, age_seconds: 3 },
  { process: 'DAEMON', last_seen_at: new Date().toISOString(), pid: 12347, alive: true, age_seconds: 10 },
];

const mockStatus = {
  heartbeats: mockHeartbeats,
  alerts: mockAlerts,
  connection_status: 'connected',
  account_mode: 'paper',
  service_health: { engine: true, daemon: true, api: true, bot_runner: false },
  realized_pnl: -0.51,
  engine_uptime_seconds: 3600,
  alert_count: 1,
};

const mockTemplates = [
  { id: 'tpl1', label: 'SPY dip buy', symbol: 'SPY', side: 'BUY', quantity: '100', order_type: 'LMT', price: '510.00', broker: 'ib' },
];

const mockLogs = [
  { timestamp: new Date().toISOString(), level: 'INFO', event: 'ORDER_FILLED', message: 'Filled: BUY 100 AAPL @ 182.45' },
  { timestamp: new Date(Date.now() - 5000).toISOString(), level: 'WARNING', event: 'RECONCILIATION', message: 'Order mismatch detected' },
  { timestamp: new Date(Date.now() - 10000).toISOString(), level: 'ERROR', event: 'COMMAND_FAILED', message: 'No market data for GME' },
];

// ── HTTP Server ──

function parseBody(req) {
  return new Promise((resolve) => {
    let body = '';
    req.on('data', (chunk) => body += chunk);
    req.on('end', () => {
      try { resolve(JSON.parse(body)); }
      catch { resolve({}); }
    });
  });
}

function json(res, data, status = 200) {
  res.writeHead(status, {
    'Content-Type': 'application/json',
    'Access-Control-Allow-Origin': '*',
    'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
    'Access-Control-Allow-Headers': 'Content-Type, Authorization',
  });
  res.end(JSON.stringify(data));
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const path = url.pathname;
  const method = req.method;

  // CORS preflight
  if (method === 'OPTIONS') {
    res.writeHead(204, {
      'Access-Control-Allow-Origin': '*',
      'Access-Control-Allow-Methods': 'GET, POST, DELETE, OPTIONS',
      'Access-Control-Allow-Headers': 'Content-Type, Authorization',
    });
    res.end();
    return;
  }

  // ── Routes ──

  if (path === '/api/status' && method === 'GET') {
    // Update heartbeat timestamps to now
    mockStatus.heartbeats.forEach(h => h.last_seen_at = new Date().toISOString());
    return json(res, mockStatus);
  }

  if (path === '/api/positions' && method === 'GET') {
    return json(res, mockPositions);
  }

  if (path === '/api/trades' && method === 'GET') {
    return json(res, mockTrades);
  }

  if (path === '/api/orders' && method === 'GET') {
    return json(res, mockOrders.filter(o => o.status !== 'FILLED'));
  }

  if (path === '/api/alerts' && method === 'GET') {
    return json(res, mockAlerts.filter(a => !a.resolved_at));
  }

  if (path.match(/^\/api\/alerts\/[^/]+\/resolve$/) && method === 'POST') {
    const alertId = path.split('/')[3];
    const alert = mockAlerts.find(a => a.id === alertId);
    if (alert) alert.resolved_at = new Date().toISOString();
    res.writeHead(204, { 'Access-Control-Allow-Origin': '*' });
    res.end();
    return;
  }

  if (path === '/api/commands' && method === 'POST') {
    const body = await parseBody(req);
    const cmdId = `cmd-${commandIdCounter++}`;
    const command = body.command || '';

    // Simulate async completion after 500ms
    pendingCommands.set(cmdId, {
      command_id: cmdId,
      status: 'RUNNING',
      command_text: command,
      source: 'api',
      output: null,
      error: null,
      submitted_at: new Date().toISOString(),
      started_at: new Date().toISOString(),
      completed_at: null,
    });

    setTimeout(() => {
      const cmd = pendingCommands.get(cmdId);
      if (!cmd) return;

      if (command.includes('fail') || command.includes('INVALID')) {
        cmd.status = 'FAILURE';
        cmd.error = `Command failed: ${command} — simulated error for testing`;
        cmd.completed_at = new Date().toISOString();
      } else if (command === 'status' || command === 'stats') {
        cmd.status = 'SUCCESS';
        cmd.output = 'Positions:  3 open\nOrders:     0 open\nTrades:     3 total (2 closed)\nRealized:   -$0.51\nCommission: $1.00';
        cmd.completed_at = new Date().toISOString();
      } else if (command === 'help') {
        cmd.status = 'SUCCESS';
        cmd.output = 'Available commands:\n  buy SYMBOL QTY STRATEGY\n  sell SYMBOL QTY STRATEGY\n  close SERIAL [STRATEGY]\n  status / stats / orders / help';
        cmd.completed_at = new Date().toISOString();
      } else {
        cmd.status = 'SUCCESS';
        cmd.output = `Order #99 — ${command}\n[10:30:15] Placed @ $182.50 (bid: $182.45 ask: $182.55)\n✓ FILLED: 1 shares @ $182.48 avg\n  Commission: $0.35\n  Serial: #99`;
        cmd.completed_at = new Date().toISOString();
      }
    }, 500);

    return json(res, { command_id: cmdId, status: 'pending' }, 202);
  }

  if (path.match(/^\/api\/commands\/[^/]+$/) && method === 'GET') {
    const cmdId = path.split('/')[3];
    const cmd = pendingCommands.get(cmdId);
    if (!cmd) return json(res, { detail: 'Command not found' }, 404);
    return json(res, cmd);
  }

  if (path === '/api/templates' && method === 'GET') {
    return json(res, mockTemplates);
  }

  if (path === '/api/templates' && method === 'POST') {
    const body = await parseBody(req);
    const tpl = { id: `tpl-${Date.now()}`, ...body };
    mockTemplates.push(tpl);
    return json(res, tpl, 201);
  }

  if (path.match(/^\/api\/templates\/[^/]+$/) && method === 'DELETE') {
    const tplId = path.split('/')[3];
    const idx = mockTemplates.findIndex(t => t.id === tplId);
    if (idx >= 0) mockTemplates.splice(idx, 1);
    res.writeHead(204, { 'Access-Control-Allow-Origin': '*' });
    res.end();
    return;
  }

  if (path === '/api/logs' && method === 'GET') {
    return json(res, mockLogs);
  }

  if (path === '/api/bots' && method === 'GET') {
    return json(res, []);
  }

  // 404
  json(res, { detail: 'Not Found' }, 404);
});

// ── WebSocket ──

const wss = new WebSocketServer({ server, path: '/ws' });

wss.on('connection', (ws) => {
  ws.on('message', (data) => {
    try {
      const msg = JSON.parse(data);
      if (msg.type === 'subscribe') {
        ws.send(JSON.stringify({
          type: 'snapshot',
          data: {
            trades: mockTrades,
            orders: mockOrders,
            alerts: mockAlerts,
            commands: [],
            heartbeats: mockHeartbeats,
          },
        }));
      } else if (msg.type === 'ping') {
        ws.send(JSON.stringify({ type: 'pong' }));
      }
    } catch {}
  });
});

server.listen(PORT, () => {
  console.log(`Mock API server running on http://localhost:${PORT}`);
});
