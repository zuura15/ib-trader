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

export type ChartTarget = {
  symbol: string;        // display label + underlying for OPT
  secType: 'STK' | 'FUT' | 'OPT';
  conId: number | null;  // preferred identifier; null for watchlist
};

interface AppStore {
  // Mode
  dataMode: DataMode;
  wsConnected: boolean;

  // Chart selection (cross-pane)
  selectedChartTarget: ChartTarget | null;
  setSelectedChartTarget: (t: ChartTarget | null) => void;

  // Layout
  activeVariant: LayoutVariant;
  setVariant: (v: LayoutVariant) => void;

  // Theme
  theme: ThemeMode;
  setTheme: (t: ThemeMode) => void;

  // Layout override — manual escape from Chrome's "Desktop site" /
  // UA-Client-Hints viewport trickery. ``auto`` (default) defers to the
  // matchMedia(max-width: 767px) check in App.useIsMobile; ``mobile``
  // and ``desktop`` force the corresponding layout regardless of the
  // browser's reported viewport.
  layoutOverride: 'auto' | 'mobile' | 'desktop';
  setLayoutOverride: (v: 'auto' | 'mobile' | 'desktop') => void;

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

  // Resync token — bumped by the top-header Resync button. Components
  // that hold long-lived subscriptions (chart panes' raw WebSocket,
  // 30s history loop, 15s SR loop, positions push, log stream)
  // include this in their useEffect dep array so the effect tears
  // down and re-runs, dropping any silently-half-dead connection and
  // re-fetching fresh data. ``resyncStatus`` drives the button's
  // own indicator. See triggerResync / triggerDeepResync below.
  resyncToken: number;
  resyncStatus: 'idle' | 'running' | 'done' | 'failed';
  resyncMessage: string | null;
  triggerResync: () => Promise<void>;
  triggerDeepResync: () => Promise<void>;

  // Positions
  positions: Position[];
  updatePosition: (symbol: string, partial: Partial<Position>) => void;
  setPositions: (positions: Position[]) => void;

  // Per-contract P&L published by PositionsPanel for the chart panes.
  // Keyed by exact futures localSymbol (``NQM6``) or stock ticker.
  // PositionsPanel computes these with its own ``computePnl`` on every
  // positions WS push (server-capped ~2 Hz) and clears the map when it
  // unmounts — chart panes treat "no entry" as "no P&L / Close
  // disabled". Deliberately a tiny DERIVED map (qty + pnl only), not
  // raw position rows: single calculation lives in PositionsPanel,
  // consumers do a dumb key lookup. See 2026-06-11 chart-P&L design.
  chartPnl: Record<string, { qty: number; pnl: number | null }>;
  setChartPnl: (map: Record<string, { qty: number; pnl: number | null }>) => void;


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

  // Console draft — a command staged into the console INPUT (not
  // transmitted) by another pane, e.g. the chart click-to-close price
  // strip. The operator reviews and hits Enter to send; the console is
  // the single arm/fire point by design. ``nonce`` bumps on every set
  // so consumers react even when the same text is drafted twice.
  consoleDraft: { text: string; nonce: number } | null;
  setConsoleDraft: (text: string) => void;

  // Templates
  templates: OrderTemplate[];
  addTemplate: (t: OrderTemplate) => void;
  removeTemplate: (id: string) => void;
  loadTemplatesFromAPI: () => void;

  // Watchlist
  watchlist: WatchlistItem[];
  watchlistGeneratedAt: string | null;
  setWatchlist: (items: WatchlistItem[], generatedAt: string | null) => void;
  updateWatchlistQuote: (symbol: string, data: Record<string, unknown>) => void;
  initWatchlist: () => void;

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

/**
 * Operator-driven "resync everything" routine. Bumps ``resyncToken``
 * (so chart panes, positions panel, log stream tear down their
 * subscriptions and re-establish), forces wsManager to reconnect,
 * and fires the engine HTTP endpoints that re-establish backend
 * state (watchlist subscriptions, positions cache). The ``deep``
 * flavor additionally triggers ``prophylactic-resub`` which is
 * heavier (cycles every IB market-data subscription).
 *
 * Wrapped in a top-level helper so both ``triggerResync`` and
 * ``triggerDeepResync`` share the orchestration code.
 */
async function _runResync(
  set: (partial: Partial<AppStore> | ((s: AppStore) => Partial<AppStore>)) => void,
  _get: () => AppStore,
  opts: { deep: boolean },
): Promise<void> {
  const t0 = performance.now();
  set({ resyncStatus: 'running', resyncMessage: opts.deep ? 'Deep resync…' : 'Resyncing…' });

  // Bump the token immediately so every subscribed component starts
  // its tear-down + re-subscribe in parallel with the HTTP calls.
  set((s) => ({ resyncToken: s.resyncToken + 1 }));

  // Kick the central wsManager (watchlist / positions panel snapshot).
  try { wsManager.forceReconnect(); } catch { /* no-op */ }

  // Engine HTTP — best-effort, log + carry on if individual calls fail.
  const fails: string[] = [];
  try {
    const r = await fetch('/api/system/resync', {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ deep: opts.deep }),
    });
    if (!r.ok) fails.push(`engine:${r.status}`);
  } catch (e) {
    fails.push(`engine:${(e as Error).message}`);
  }

  const elapsed = ((performance.now() - t0) / 1000).toFixed(1);
  if (fails.length === 0) {
    set({
      resyncStatus: 'done',
      resyncMessage: `${opts.deep ? 'Deep r' : 'R'}esynced in ${elapsed}s`,
    });
  } else {
    set({
      resyncStatus: 'failed',
      resyncMessage: `Resync issues: ${fails.join(', ')}`,
    });
  }

  // Drop back to idle after a few seconds so the button stops
  // shouting the last result.
  window.setTimeout(() => {
    set((s) => (
      s.resyncStatus === 'idle'
        ? {}
        : { resyncStatus: 'idle', resyncMessage: null }
    ));
  }, 3500);
}

export const useStore = create<AppStore>((set, get) => ({
  dataMode: DATA_MODE,
  wsConnected: false,

  selectedChartTarget: (() => {
    // Restore the last chart selection across browser reloads. Saved
    // by setSelectedChartTarget below.
    try {
      const raw = localStorage.getItem('ib-chart-target-v1');
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (parsed && typeof parsed.symbol === 'string'
          && (parsed.secType === 'STK' || parsed.secType === 'FUT' || parsed.secType === 'OPT')) {
        return parsed as ChartTarget;
      }
    } catch { /* corrupt entry — ignore */ }
    return null;
  })(),
  setSelectedChartTarget: (t) => {
    let next: ChartTarget | null;
    if (t && t.secType === 'OPT') {
      // Chart the underlying stock — option premium over 24h is dominated
      // by theta/gamma and not useful for eyeballing. Header still says
      // "STK" so the user isn't misled about what's being shown.
      next = { symbol: t.symbol, secType: 'STK', conId: null };
    } else {
      next = t;
    }
    try {
      if (next) localStorage.setItem('ib-chart-target-v1', JSON.stringify(next));
      else localStorage.removeItem('ib-chart-target-v1');
    } catch { /* quota — ignore */ }
    set({ selectedChartTarget: next });
  },

  activeVariant: ((): LayoutVariant => {
    // URL override: lets the user deep-link to a specific layout.
    //   /bots.aspx           → Trader (variant T)
    //   ?variant=T (or #…)   → Trader (escape hatch for A/B/C/D too)
    // Default boot path (root / unknown path) reads from localStorage
    // or falls back to 'A' as before. Any URL override is persisted so
    // a reload without the URL still lands on the same layout until
    // the user switches via the header.
    const valid: LayoutVariant[] = ['A', 'B', 'C', 'D', 'T'];
    const pathMap: Record<string, LayoutVariant> = {
      '/bots.aspx': 'T',
    };
    try {
      const url = new URL(window.location.href);
      const fromPath = pathMap[url.pathname.toLowerCase()];
      const fromQuery = url.searchParams.get('variant') ?? '';
      const fromHash = url.hash.replace(/^#/, '')
        .split('&')
        .map((p) => p.split('='))
        .find(([k]) => k === 'variant')?.[1] ?? '';
      const candidate = (fromPath || fromQuery || fromHash).toString().toUpperCase();
      if (valid.includes(candidate as LayoutVariant)) {
        localStorage.setItem('ib-layout-variant', candidate);
        return candidate as LayoutVariant;
      }
    } catch { /* malformed URL — fall through */ }
    const saved = localStorage.getItem('ib-layout-variant') as LayoutVariant;
    return valid.includes(saved) ? saved : 'A';
  })(),
  setVariant: (v) => {
    localStorage.setItem('ib-layout-variant', v);
    set({ activeVariant: v });
  },

  theme: (localStorage.getItem('ib-theme') as ThemeMode) || 'light',
  setTheme: (next: ThemeMode) => {
    document.documentElement.setAttribute('data-theme', next);
    localStorage.setItem('ib-theme', next);
    set({ theme: next });
  },

  layoutOverride: (() => {
    const v = localStorage.getItem('ib-layout-override');
    return v === 'mobile' || v === 'desktop' ? v : 'auto';
  })(),
  setLayoutOverride: (next) => {
    if (next === 'auto') {
      localStorage.removeItem('ib-layout-override');
    } else {
      localStorage.setItem('ib-layout-override', next);
    }
    set({ layoutOverride: next });
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
    engineStartedAt: null,
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

  resyncToken: 0,
  resyncStatus: 'idle',
  resyncMessage: null,
  triggerResync: async () => {
    await _runResync(set, get, { deep: false });
  },
  triggerDeepResync: async () => {
    await _runResync(set, get, { deep: true });
  },

  positions: DATA_MODE === 'mock' ? mockPositions : [],
  updatePosition: (symbol, partial) => set((s) => ({
    positions: s.positions.map((p) => p.symbol === symbol ? { ...p, ...partial } : p),
  })),
  setPositions: (positions) => set({ positions }),
  chartPnl: {},
  setChartPnl: (chartPnl) => set({ chartPnl }),

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
      // Subscribe to the live command-output Redis stream *before* the POST
      // so we don't race the first XADD from the engine. Every line gets
      // appended to the command bubble as it arrives; the final `done`
      // marker unregisters the handler.
      let liveOutput = '';
      const unsubscribe = wsManager.subscribeCommandOutput(id, (msg) => {
        const data = msg.data;
        if (data.type === 'line' && data.message) {
          liveOutput = liveOutput ? `${liveOutput}\n${data.message}` : data.message;
          get().updateCommand(id, {
            status: 'running' as CommandStatus,
            output: liveOutput,
          });
        } else if (data.type === 'done') {
          const isFailure = data.status === 'FAILURE';
          get().updateCommand(id, {
            status: (isFailure ? 'failure' : 'success') as CommandStatus,
            output: liveOutput || data.error || undefined,
            completedAt: new Date(),
          });
        }
      });

      // Submit to API, then poll for completion
      submitCommand(cmd, id).then(async (resp) => {
        const serverId = resp.command_id;
        console.log(`[store] Command submitted: ${cmd} → server id=${serverId} status=${resp.status}`);

        // Synchronous response: command already done (read-only commands or
        // immediate-fail orders). Skip polling.
        if (resp.status === 'completed' || (resp as any).output) {
          // If the live stream already delivered terminal output, keep it;
          // otherwise fall back to the HTTP-response text.
          const current = get().commands.find((c) => c.id === id);
          const alreadyTerminal = current?.status === 'success' || current?.status === 'failure';
          if (!alreadyTerminal) {
            store.updateCommand(id, {
              id: serverId,
              status: 'success' as CommandStatus,
              output: (resp as any).output,
              completedAt: new Date(),
            });
          }
          unsubscribe();
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
        unsubscribe();
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

  consoleDraft: null,
  setConsoleDraft: (text) => set((s) => ({
    consoleDraft: { text, nonce: (s.consoleDraft?.nonce ?? 0) + 1 },
  })),

  watchlist: [],
  watchlistGeneratedAt: null,
  setWatchlist: (items, generatedAt) => set({ watchlist: items, watchlistGeneratedAt: generatedAt }),
  updateWatchlistQuote: (symbol, data) => set((s) => {
    const pick = (k: string) => {
      const v = (data as Record<string, unknown>)[k];
      return v === undefined || v === null ? undefined : String(v);
    };
    return {
      watchlist: s.watchlist.map(item => item.symbol === symbol ? {
        ...item,
        last: pick('last') ?? item.last,
        change: pick('change') ?? item.change,
        change_pct: pick('change_pct') ?? item.change_pct,
        volume: pick('volume') ?? item.volume,
        avg_volume: pick('avg_volume') ?? item.avg_volume,
        high: pick('high') ?? item.high,
        low: pick('low') ?? item.low,
        high_52w: pick('high_52w') ?? item.high_52w,
        low_52w: pick('low_52w') ?? item.low_52w,
      } : item),
    };
  }),
  initWatchlist: () => {
    if (DATA_MODE !== 'live') return;

    // One-shot snapshot → then per-symbol quote subscriptions on a
    // dedicated WS connection. Every IB tick updates the matching
    // watchlist entry in place; no poll, no refresh cadence.
    fetch(`/api/watchlist?_t=${Date.now()}`, { cache: 'no-store' })
      .then(r => r.ok ? r.json() : { items: [], generated_at: null })
      .then((data: { items: WatchlistItem[]; generated_at: string | null }) => {
        const items = data.items || [];
        get().setWatchlist(items, data.generated_at || null);

        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
        const url = `${proto}//${window.location.host}/ws`;
        let ws: WebSocket | null = null;
        let retry: ReturnType<typeof setTimeout> | null = null;
        let closed = false;

        const open = () => {
          if (closed) return;
          ws = new WebSocket(url);
          ws.onopen = () => {
            for (const item of items) {
              ws?.send(JSON.stringify({ type: 'subscribe_quote', symbol: item.symbol }));
            }
          };
          ws.onmessage = (ev) => {
            try {
              const msg = JSON.parse(ev.data);
              if (msg.type === 'quote' && typeof msg.symbol === 'string' && msg.data) {
                get().updateWatchlistQuote(msg.symbol, msg.data);
              }
            } catch { /* ignore malformed frames */ }
          };
          ws.onclose = () => {
            if (!closed) retry = setTimeout(open, 2000);
          };
          ws.onerror = () => { /* onclose handles reconnect */ };
        };
        open();

        // Background tabs are throttled / discarded by the browser; the
        // 2 s reconnect timer may be mid-wait or the socket may be in a
        // half-closed state when the user returns. On visible, drop the
        // stale socket and force an immediate reopen so the live ticker
        // resumes without waiting for the next retry tick.
        const onVisible = () => {
          if (document.visibilityState !== 'visible' || closed) return;
          if (retry) { clearTimeout(retry); retry = null; }
          if (ws) {
            ws.onclose = null;
            try { ws.close(); } catch { /* already closed */ }
            ws = null;
          }
          open();
        };
        document.addEventListener('visibilitychange', onVisible);

        // Close the WS on page unload so we don't leak open subscriptions.
        window.addEventListener('beforeunload', () => {
          closed = true;
          document.removeEventListener('visibilitychange', onVisible);
          if (retry) clearTimeout(retry);
          if (ws) { ws.onclose = null; ws.close(); }
        });
      })
      .catch(() => {});
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
          entryQty: t.entry_qty ?? null,
          entryPrice: t.entry_price ?? null,
          exitQty: t.exit_qty ?? null,
          exitPrice: t.exit_price ?? null,
          orderType: t.order_type ?? null,
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
      // Preserve the operator's originally-typed command text for any
      // command we already have locally. The server stores the EXPANDED
      // form ("buy MGCQ6 1 mid --sec-type FUT --exchange CME") on
      // pending_commands for audit clarity, but the operator typed only
      // "buy MGCQ6 1 mid" — the arrow-up history would otherwise replay
      // the verbose form, which is annoying to edit and re-submit.
      const localById = new Map(get().commands.map(c => [c.id, c.command]));
      const parsed = (data.commands as unknown[]).map((c: any) => ({
        id: c.id,
        command: localById.get(c.id) || c.command_text,
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
    if (data.status) {
      applyStatusPayload(get().updateGlobal, data.status as any[]);
    }
    if (data.bots) {
      set({ bots: (data.bots as any[]).map(mapApiBotData) });
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
    } else if (channel === 'status') {
      // Status ships as a single-row channel; any touch (added or updated)
      // carries the whole latest payload.
      const row = (diff.updated?.[0] || diff.added?.[0]) as any;
      if (row) applyStatusPayload(get().updateGlobal, [row]);
    } else if (channel === 'bots') {
      // Apply per-row deltas rather than refetching.
      set((s) => {
        let next = s.bots;
        const removedIds = new Set((diff.removed as any[]).map((r) => r.id));
        if (removedIds.size) next = next.filter((b) => !removedIds.has(b.id));
        const addOrUpdate = [
          ...(diff.added as any[] | undefined ?? []),
          ...(diff.updated as any[] | undefined ?? []),
        ].map(mapApiBotData);
        const byId = new Map(next.map((b) => [b.id, b] as const));
        for (const b of addOrUpdate) byId.set(b.id, b);
        return { bots: Array.from(byId.values()) };
      });
    }
  },
}));

function applyStatusPayload(
  updateGlobal: (partial: Partial<GlobalState>) => void,
  rows: any[],
): void {
  const status = rows[0];
  if (!status) return;
  updateGlobal({
    connectionStatus: status.connection_status || 'disconnected',
    accountMode: status.account_mode || 'unknown',
    accountId: status.account_id || '',
    serviceHealth: status.service_health || {},
    staleData: false,
    realizedPnl: status.realized_pnl || 0,
    sessionUptime: status.engine_uptime_seconds || 0,
    engineStartedAt: status.engine_started_at || null,
  });
}

function mapApiBotData(b: any): Bot {
  // Mirror features/bots/BotsPanel.tsx:mapApiBot. Keep these two in sync —
  // the WS channel and the REST endpoint both feed this shape.
  return {
    id: b.id,
    name: b.name,
    strategy: b.strategy,
    status: (b.status || 'stopped').toLowerCase(),
    lastHeartbeat: b.last_heartbeat ? new Date(b.last_heartbeat) : new Date(),
    lastSignal: b.last_signal || undefined,
    lastAction: b.last_action || undefined,
    lastActionTime: b.last_action_at ? new Date(b.last_action_at) : undefined,
    errorMessage: b.error_message || undefined,
    tradesTotal: b.trades_total || 0,
    tradesToday: b.trades_today || 0,
    pnlToday: parseFloat(b.pnl_today) || 0,
    symbols: b.symbols_json ? JSON.parse(b.symbols_json) : [],
    refId: b.ref_id,
    uptime: 0,
  } as Bot;
}
