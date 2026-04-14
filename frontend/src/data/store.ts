import { create } from 'zustand';
import { v4 as uuid } from 'uuid';
import type {
  GlobalState, LogEntry, Order, Position, Bot, Alert,
  CommandEntry, LayoutVariant, ScenarioName, CommandStatus, ThemeMode,
  OrderTemplate, TradeGroup, WatchlistItem,
} from '../types';
import {
  mockPositions, mockOrders, mockBots, mockAlerts, mockCommands,
  generateInitialLogs, generateLogEntry,
} from '../mock/data';
import { submitCommand, getCommandStatus, getTemplates, createTemplate, deleteTemplate } from '../api/client';
import { parseUTC } from '../utils/format';
import { wsManager, type Channel, type WSDiff } from '../api/ws';

/**
 * Data source mode:
 *   'mock'  — prototype mode with simulated data (default, no API needed)
 *   'live'  — connected to the API server via REST + WebSocket
 */
type DataMode = 'mock' | 'live';

// Detect mode from env or default to mock
const DATA_MODE: DataMode = (import.meta.env.VITE_DATA_MODE === 'live') ? 'live' : 'mock';

interface AppStore {
  // Mode
  dataMode: DataMode;
  wsConnected: boolean;

  // Layout
  activeVariant: LayoutVariant;
  setVariant: (v: LayoutVariant) => void;

  // Theme
  theme: ThemeMode;
  setTheme: (t: ThemeMode) => void;

  // Global state
  global: GlobalState;
  updateGlobal: (partial: Partial<GlobalState>) => void;

  // Logs
  logs: LogEntry[];
  addLog: (entry: LogEntry) => void;
  addLogMessage: (level: LogEntry['level'], event: string, message: string) => void;

  // Orders
  orders: Order[];
  updateOrder: (id: string, partial: Partial<Order>) => void;
  setOrders: (orders: Order[]) => void;

  // Trade Groups (from API /api/trades)
  tradeGroups: TradeGroup[];
  setTradeGroups: (trades: TradeGroup[]) => void;

  // Position refresh trigger — bumped after each command completes
  positionRefreshTick: number;

  // Positions
  positions: Position[];
  updatePosition: (symbol: string, partial: Partial<Position>) => void;
  setPositions: (positions: Position[]) => void;


  // Bots
  bots: Bot[];
  updateBot: (id: string, partial: Partial<Bot>) => void;
  setBots: (bots: Bot[]) => void;

  // Alerts
  alerts: Alert[];
  addAlert: (alert: Alert) => void;
  dismissAlert: (id: string) => void;
  setAlerts: (alerts: Alert[]) => void;

  // Commands
  commands: CommandEntry[];
  addCommand: (cmd: string) => string;
  updateCommand: (id: string, partial: Partial<CommandEntry>) => void;

  // Templates
  templates: OrderTemplate[];
  addTemplate: (t: OrderTemplate) => void;
  removeTemplate: (id: string) => void;
  loadTemplatesFromAPI: () => void;

  // Watchlist
  watchlist: WatchlistItem[];
  watchlistGeneratedAt: string | null;
  setWatchlist: (items: WatchlistItem[], generatedAt: string | null) => void;
  initWatchlistPolling: () => void;

  // Scenarios (mock mode only)
  activeScenario: ScenarioName;
  applyScenario: (name: ScenarioName) => void;

  // Simulation (mock mode only)
  tickSimulation: () => void;

  // WebSocket
  initWebSocket: () => void;
  handleSnapshot: (data: Record<string, unknown[]>) => void;
  handleDiff: (channel: Channel, diff: WSDiff) => void;
}

export const useStore = create<AppStore>((set, get) => ({
  dataMode: DATA_MODE,
  wsConnected: false,

  activeVariant: (localStorage.getItem('ib-layout-variant') as LayoutVariant) || 'A',
  setVariant: (v) => {
    localStorage.setItem('ib-layout-variant', v);
    set({ activeVariant: v });
  },

  theme: (localStorage.getItem('ib-theme') as ThemeMode) || 'dark',
  setTheme: (next: ThemeMode) => {
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('ib-theme', next);
    set({ theme: next });
  },

  global: {
    connectionStatus: 'connected',
    accountMode: 'paper',
    accountId: '',
    serviceHealth: { ib_gateway: true, market_data: true, order_router: true, reconciler: true },
    staleData: false,
    dailyPnl: 1667.00,
    unrealizedPnl: 6232.50,
    realizedPnl: 6260.25,
    sessionUptime: 15780,
  },
  updateGlobal: (partial) => set((s) => ({ global: { ...s.global, ...partial } })),

  logs: DATA_MODE === 'mock' ? generateInitialLogs(40) : [],
  addLog: (entry) => set((s) => ({ logs: [...s.logs.slice(-200), entry] })),
  addLogMessage: (level, event, message) => set((s) => ({
    logs: [...s.logs.slice(-200), { id: uuid(), timestamp: new Date(), level, event, message }],
  })),

  orders: DATA_MODE === 'mock' ? mockOrders : [],
  updateOrder: (id, partial) => set((s) => ({
    orders: s.orders.map((o) => o.id === id ? { ...o, ...partial } : o),
  })),
  setOrders: (orders) => set({ orders }),

  tradeGroups: [],
  setTradeGroups: (tradeGroups) => set({ tradeGroups }),

  positionRefreshTick: 0,

  positions: DATA_MODE === 'mock' ? mockPositions : [],
  updatePosition: (symbol, partial) => set((s) => ({
    positions: s.positions.map((p) => p.symbol === symbol ? { ...p, ...partial } : p),
  })),
  setPositions: (positions) => set({ positions }),

  bots: DATA_MODE === 'mock' ? mockBots : [],
  updateBot: (id, partial) => set((s) => ({
    bots: s.bots.map((b) => b.id === id ? { ...b, ...partial } : b),
  })),
  setBots: (bots) => set({ bots }),

  alerts: DATA_MODE === 'mock' ? mockAlerts : [],
  _dismissedAlertIds: new Set<string>(),
  addAlert: (alert) => set((s) => ({ alerts: [alert, ...s.alerts] })),
  dismissAlert: (id) => {
    // Track dismissed ID so WebSocket snapshots don't bring it back
    const store = get();
    (store as any)._dismissedAlertIds.add(id);
    set((s) => ({
      alerts: s.alerts.map((a) => a.id === id ? { ...a, dismissed: true } : a),
    }));
    // Also resolve server-side alert via API (fire and forget)
    if (DATA_MODE === 'live') {
      fetch(`/api/alerts/${id}/resolve`, { method: 'POST' }).catch(() => {});
    }
  },
  setAlerts: (alerts) => set({ alerts }),

  templates: [],
  addTemplate: (t) => {
    if (DATA_MODE === 'live') {
      // In live mode, don't update local state optimistically — wait for API
      createTemplate({
        label: t.label, symbol: t.symbol, side: t.side,
        quantity: String(t.quantity), order_type: t.orderType,
        price: t.price ? String(t.price) : undefined,
      }).then(() => get().loadTemplatesFromAPI())
        .catch((err) => console.error('Failed to create template:', err));
    } else {
      set((s) => ({ templates: [...s.templates, t] }));
    }
  },
  removeTemplate: (id) => {
    if (DATA_MODE === 'live') {
      // In live mode, don't remove optimistically — wait for API
      deleteTemplate(id).then(() => get().loadTemplatesFromAPI())
        .catch((err) => console.error('Failed to delete template:', err));
    } else {
      set((s) => ({ templates: s.templates.filter(t => t.id !== id) }));
    }
  },
  loadTemplatesFromAPI: () => {
    if (DATA_MODE !== 'live') return;
    getTemplates().then((templates) => {
      set({
        templates: templates.map(t => ({
          id: t.id,
          label: t.label,
          symbol: t.symbol,
          side: t.side as 'BUY' | 'SELL',
          quantity: Number(t.quantity),
          orderType: t.order_type as 'LMT' | 'MKT' | 'STP' | 'MOC',
          price: t.price ? Number(t.price) : undefined,
        })),
      });
    }).catch(() => { /* ignore fetch errors */ });
  },

  commands: DATA_MODE === 'mock' ? mockCommands : [],
  addCommand: (cmd) => {
    const id = uuid();
    const store = get();

    // Add optimistic pending entry to the UI immediately
    set((s) => ({
      commands: [...s.commands, {
        id, command: cmd, status: 'queued' as CommandStatus, startedAt: new Date(),
      }],
    }));

    if (DATA_MODE === 'live') {
      // Submit to API, then poll for completion
      submitCommand(cmd).then(async (resp) => {
        const serverId = resp.command_id;
        console.log(`[store] Command submitted: ${cmd} → server id=${serverId} status=${resp.status}`);

        // Synchronous response: command already done (read-only commands or
        // immediate-fail orders). Skip polling.
        if (resp.status === 'completed' || (resp as any).output) {
          store.updateCommand(id, {
            id: serverId,
            status: 'success' as CommandStatus,
            output: (resp as any).output,
            completedAt: new Date(),
          });
          if ((resp as any).output) {
            store.addLogMessage('info', 'command.success',
              `${cmd}: ${(resp as any).output}`);
          }
          set((s) => ({ positionRefreshTick: (s as any).positionRefreshTick + 1 || 1 }));
          return;
        }

        // Update local entry to use server ID so future updates match
        set((s) => ({
          commands: s.commands.map(c =>
            c.id === id ? { ...c, id: serverId, status: 'running' as CommandStatus } : c
          ),
        }));

        // Poll for completion (engine processes the command asynchronously)
        const pollInterval = 500; // ms
        const maxPolls = 240; // 2 minutes max
        for (let i = 0; i < maxPolls; i++) {
          await new Promise(r => setTimeout(r, pollInterval));
          try {
            const status = await getCommandStatus(serverId);
            console.log(`[store] Poll ${serverId}: status=${status.status} output=${status.output} error=${status.error}`);
            if (status.status === 'SUCCESS' || status.status === 'FAILURE') {
              const isFailure = status.status === 'FAILURE';
              const outputText = status.output || status.error || (isFailure ? 'Command failed' : undefined);
              console.log(`[store] Command ${serverId} resolved: ${status.status} → "${outputText}"`);
              store.updateCommand(serverId, {
                status: (isFailure ? 'failure' : 'success') as CommandStatus,
                output: outputText,
                completedAt: status.completed_at ? parseUTC(status.completed_at) : new Date(),
              });
              // Surface failures as alerts + log entries
              if (isFailure) {
                const errorMsg = status.error || status.output || 'unknown error';
                // Add to Alerts panel
                store.addAlert({
                  id: uuid(),
                  severity: 'catastrophic',
                  title: `Order Failed: ${cmd}`,
                  message: errorMsg,
                  timestamp: new Date(),
                  dismissed: false,
                  source: 'engine',
                });
                // Add to Logs panel
                store.addLogMessage('error', 'command.failed',
                  `Command failed: ${cmd} — ${errorMsg}`);
              } else if (status.output) {
                store.addLogMessage('info', 'command.success',
                  `Command completed: ${cmd}`);
              }
              // Trigger a position refresh after command completes.
              // Bump a counter so the PositionsPanel re-fetches.
              set((s) => ({ positionRefreshTick: (s as any).positionRefreshTick + 1 || 1 }));
              return;
            }
            // Still running — update status if changed
            if (status.status === 'RUNNING') {
              store.updateCommand(serverId, { status: 'running' as CommandStatus });
            }
          } catch {
            // API unreachable — keep polling
          }
        }
        // Timed out waiting
        store.updateCommand(serverId, {
          status: 'failure' as CommandStatus,
          output: 'Timed out waiting for engine response (2 minutes)',
          completedAt: new Date(),
        });
      }).catch((err) => {
        store.updateCommand(id, {
          status: 'failure' as CommandStatus,
          output: `API error: ${err.message}`,
          completedAt: new Date(),
        });
      });

      return id;
    }

    // Mock mode — simulate execution
    setTimeout(() => {
      store.updateCommand(id, { status: 'running' });
      store.addLogMessage('info', 'command.started', `Executing: ${cmd}`);
    }, 200);
    setTimeout(() => {
      const success = Math.random() > 0.15;
      store.updateCommand(id, {
        status: success ? 'success' : 'failure',
        output: success ? `Command "${cmd}" completed successfully` : `Command "${cmd}" failed: simulated error`,
        completedAt: new Date(),
      });
      store.addLogMessage(
        success ? 'info' : 'error',
        success ? 'command.success' : 'command.failure',
        success ? `Completed: ${cmd}` : `Failed: ${cmd}`,
      );
    }, 800 + Math.random() * 1500);
    return id;
  },
  updateCommand: (id, partial) => set((s) => ({
    commands: s.commands.map((c) => c.id === id ? { ...c, ...partial } : c),
  })),

  watchlist: [],
  watchlistGeneratedAt: null,
  setWatchlist: (items, generatedAt) => set({ watchlist: items, watchlistGeneratedAt: generatedAt }),
  initWatchlistPolling: () => {
    if (DATA_MODE !== 'live') return;
    const poll = () => {
      fetch(`/api/watchlist?_t=${Date.now()}`, { cache: 'no-store' })
        .then(r => r.ok ? r.json() : { items: [], generated_at: null })
        .then((data: { items: WatchlistItem[]; generated_at: string | null }) => {
          get().setWatchlist(data.items || [], data.generated_at || null);
        })
        .catch(() => {});
    };
    poll();
    let timer = setInterval(poll, 5000);
    document.addEventListener('visibilitychange', () => {
      clearInterval(timer);
      if (!document.hidden) {
        poll();
        timer = setInterval(poll, 5000);
      }
    });
  },

  activeScenario: 'healthy',
  applyScenario: (name) => {
    // Scenarios only work in mock mode
    if (DATA_MODE === 'live') return;
    const store = get();
    store.updateGlobal({
      connectionStatus: 'connected',
      accountMode: 'paper',
      accountId: '',
      staleData: false,
      serviceHealth: { ib_gateway: true, market_data: true, order_router: true, reconciler: true },
    });
    set((s) => ({ alerts: s.alerts.filter(a => a.dismissed) }));
    // Scenario logic unchanged from mock implementation
    set({ activeScenario: name });
  },

  tickSimulation: () => {
    // Only in mock mode
    if (DATA_MODE === 'live') return;
    const state = get();

    if (Math.random() > 0.6) {
      state.addLog(generateLogEntry());
    }

    state.positions.forEach((p) => {
      const change = (Math.random() - 0.5) * 0.2;
      const newMark = +(p.markPrice + change).toFixed(2);
      const newUnrealized = +((newMark - p.avgCost) * p.quantity).toFixed(2);
      state.updatePosition(p.symbol, {
        markPrice: newMark,
        unrealizedPnl: newUnrealized,
        lastUpdate: new Date(),
      });
    });

    state.bots.forEach((b) => {
      if (b.status === 'running') {
        state.updateBot(b.id, { lastHeartbeat: new Date() });
      }
    });

    state.updateGlobal({ sessionUptime: state.global.sessionUptime + 2 });

    const totalUnrealized = state.positions.reduce((s, p) => s + p.unrealizedPnl, 0);
    state.updateGlobal({ unrealizedPnl: +totalUnrealized.toFixed(2) });
  },

  // --- WebSocket integration (live mode) ---

  initWebSocket: () => {
    if (DATA_MODE !== 'live') return;

    const store = get();
    wsManager.start({
      onSnapshot: (data) => store.handleSnapshot(data),
      onDiff: (channel, diff) => store.handleDiff(channel, diff),
      onStatus: (connected) => set({ wsConnected: connected }),
    });

    // Load templates from API on init
    store.loadTemplatesFromAPI();

    // Poll system status every 10 seconds to keep header live
    const pollStatus = () => {
      fetch('/api/status')
        .then(r => r.ok ? r.json() : null)
        .then(status => {
          if (!status) return;
          store.updateGlobal({
            connectionStatus: status.connection_status || 'disconnected',
            accountMode: status.account_mode || 'unknown',
            accountId: status.account_id || '',
            serviceHealth: status.service_health || {},
            staleData: false,
            realizedPnl: status.realized_pnl || 0,
            sessionUptime: status.engine_uptime_seconds || 0,
          });
        })
        .catch(() => {
          store.updateGlobal({ connectionStatus: 'disconnected' });
        });
    };
    pollStatus();
    setInterval(pollStatus, 10000);
  },

  handleSnapshot: (data) => {
    // Hydrate store from full WebSocket snapshot
    if (data.trades) {
      set({
        tradeGroups: (data.trades as unknown[]).map((t: any) => ({
          id: t.id,
          serialNumber: t.serial_number,
          symbol: t.symbol,
          direction: t.direction,
          status: t.status,
          realizedPnl: t.realized_pnl,
          totalCommission: t.total_commission,
          openedAt: t.opened_at,
          closedAt: t.closed_at,
        })),
      });
    }
    if (data.orders) {
      set({
        orders: (data.orders as unknown[]).map((o: any) => ({
          id: o.id,
          symbol: o.symbol,
          side: o.side,
          quantity: Number(o.qty_requested),
          filledQty: Number(o.qty_filled),
          orderType: o.order_type,
          status: o.status?.toLowerCase(),
          source: 'system' as const,
          submittedAt: parseUTC(o.placed_at),
          lastUpdate: parseUTC(o.placed_at),
          limitPrice: o.price_placed ? Number(o.price_placed) : undefined,
          avgFillPrice: o.avg_fill_price ? Number(o.avg_fill_price) : undefined,
        })),
      });
    }
    if (data.alerts) {
      const dismissedIds = (get() as any)._dismissedAlertIds as Set<string>;
      const serverAlerts = (data.alerts as unknown[])
        .map((a: any) => ({
          id: a.id,
          severity: a.severity?.toLowerCase(),
          title: a.trigger,
          message: a.message,
          timestamp: parseUTC(a.created_at),
          dismissed: !!a.resolved_at || dismissedIds.has(a.id),
          source: 'system',
        }));
      // Merge: keep client-side alerts (from command failures) + server alerts
      set((s) => {
        const clientAlerts = s.alerts.filter(a => a.source === 'engine');
        return { alerts: [...clientAlerts, ...serverAlerts] };
      });
    }
    if (data.commands) {
      const parsed = (data.commands as unknown[]).map((c: any) => ({
        id: c.id,
        command: c.command_text,
        status: c.status?.toLowerCase(),
        output: c.output,
        startedAt: parseUTC(c.submitted_at),
        completedAt: c.completed_at ? parseUTC(c.completed_at) : undefined,
      }));
      // API returns newest first — reverse to chronological (oldest first)
      // so the console scrolls naturally with newest at bottom
      parsed.reverse();
      set({ commands: parsed });
    }
    if (data.heartbeats) {
      // Update connection status from heartbeats
      const hbs = data.heartbeats as any[];
      const engineHb = hbs.find((h: any) => h.process === 'ENGINE');
      if (engineHb) {
        const age = Date.now() - parseUTC(engineHb.last_seen_at).getTime();
        get().updateGlobal({
          connectionStatus: age < 60000 ? 'connected' : 'disconnected',
        });
      }
    }
  },

  handleDiff: (channel, diff) => {
    // Apply incremental updates from WebSocket diffs
    // This is called for each channel that has changes
    if (channel === 'commands' && diff.updated) {
      for (const cmd of diff.updated as any[]) {
        get().updateCommand(cmd.id, {
          status: cmd.status?.toLowerCase(),
          output: cmd.output,
          completedAt: cmd.completed_at ? new Date(cmd.completed_at) : undefined,
        });
      }
    }
  },
}));
