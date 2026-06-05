/**
 * WebSocket manager for real-time data updates.
 *
 * Handles connection, reconnection with exponential backoff,
 * subscription, snapshot delivery, and diff dispatching.
 */

export type Channel =
  | 'trades' | 'orders' | 'alerts' | 'commands' | 'heartbeats'
  | 'bots' | 'status';

export interface WSDiff {
  type: 'diff';
  channel: Channel;
  added: Record<string, unknown>[];
  updated: Record<string, unknown>[];
  removed: Record<string, unknown>[];
}

export interface WSSnapshot {
  type: 'snapshot';
  data: Record<Channel, Record<string, unknown>[]>;
}

export interface WSCommandOutput {
  type: 'command_output';
  cmd_id: string;
  data: {
    type?: 'line' | 'done';
    message?: string;
    severity?: string;
    status?: string;
    error?: string;
  };
}

export type WSMessage =
  | WSDiff
  | WSSnapshot
  | WSCommandOutput
  | { type: 'pong' };

type DiffHandler = (channel: Channel, diff: WSDiff) => void;
type SnapshotHandler = (data: WSSnapshot['data']) => void;
type StatusHandler = (connected: boolean) => void;
type CommandOutputHandler = (msg: WSCommandOutput) => void;

// Use wss:// when the page is served over HTTPS (e.g. LAN access via basic-ssl).
// Browsers block mixed-content ws:// from an https:// page.
const wsProto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
const WS_BASE = import.meta.env.VITE_WS_URL || `${wsProto}//${window.location.host}/ws`;
const WS_TOKEN = import.meta.env.VITE_API_TOKEN || '';
const WS_URL = WS_TOKEN ? `${WS_BASE}?token=${WS_TOKEN}` : WS_BASE;
const CHANNELS: Channel[] = [
  'trades', 'orders', 'alerts', 'commands', 'heartbeats', 'bots', 'status',
];

const MIN_RECONNECT_MS = 1000;
const MAX_RECONNECT_MS = 30000;
// Message-arrival watchdog. Even with healthy ``readyState``, OS-level
// TCP can die silently (laptop standby, NAT timeout, network blip) and
// browsers may report ``OPEN`` for minutes before the next OS-level
// probe surfaces it. The server pushes a ``status``/``heartbeats`` diff
// every few seconds in the worst case, so 30 s of silence means the
// pipe is dead — force-reconnect rather than wait for ``onclose``.
const WS_STALE_THRESHOLD_MS = 30_000;
const WS_WATCHDOG_INTERVAL_MS = 10_000;

export class WSManager {
  private ws: WebSocket | null = null;
  private reconnectMs = MIN_RECONNECT_MS;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private pingTimer: ReturnType<typeof setInterval> | null = null;
  private watchdogTimer: ReturnType<typeof setInterval> | null = null;
  // Wall-clock of the most recent inbound message (snapshot / diff /
  // command_output / pong — anything). Watchdog reads this to detect
  // silent-dead-pipe; zombie WS where browser reports OPEN but no
  // bytes flow. Updated in onmessage; reset on connect attempt.
  private lastMessageAt = 0;
  private onDiff: DiffHandler | null = null;
  private onSnapshot: SnapshotHandler | null = null;
  private onStatus: StatusHandler | null = null;
  private destroyed = false;
  private cmdOutputHandlers = new Map<string, CommandOutputHandler>();
  // Commands subscribed before the WS opened — flushed on connect.
  private pendingCmdSubscriptions = new Set<string>();
  private visibilityHandler: (() => void) | null = null;

  /**
   * Subscribe to live output for a single in-flight command.
   *
   * The server XREADs the ``cmd:{cmdId}:output`` Redis stream from the
   * beginning and pushes each line plus a final ``done`` marker. Handler
   * is auto-unregistered on the ``done`` message.
   */
  subscribeCommandOutput(cmdId: string, handler: CommandOutputHandler): () => void {
    this.cmdOutputHandlers.set(cmdId, handler);
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ type: 'subscribe_command_output', cmd_id: cmdId }));
    } else {
      this.pendingCmdSubscriptions.add(cmdId);
    }
    return () => {
      this.cmdOutputHandlers.delete(cmdId);
      this.pendingCmdSubscriptions.delete(cmdId);
    };
  }

  /**
   * Register handlers and connect.
   */
  start(handlers: {
    onDiff: DiffHandler;
    onSnapshot: SnapshotHandler;
    onStatus?: StatusHandler;
  }): void {
    this.onDiff = handlers.onDiff;
    this.onSnapshot = handlers.onSnapshot;
    this.onStatus = handlers.onStatus || null;
    this.attachVisibilityWake();
    this.connect();
  }

  /**
   * Browsers throttle / discard background tabs. When the tab returns
   * to ``visible`` the OS-level TCP may have died (laptop standby,
   * network blip) but ``ws.readyState`` still reports ``OPEN`` for
   * minutes — Chrome doesn't surface the underlying close until its
   * own TCP keepalive expires. **Always force-reconnect on wake**
   * rather than trusting ``readyState`` — the cost is one snapshot
   * refetch (small), the alternative is sitting on stale quotes for
   * hours (the 2026-06-05 zombie-socket bug). ``forceReconnect``
   * already does the right teardown + reconnect for both healthy and
   * zombie sockets, so delegate.
   */
  wakeUp(): void {
    this.forceReconnect();
  }

  private attachVisibilityWake(): void {
    if (this.visibilityHandler) return;
    this.visibilityHandler = () => {
      if (document.visibilityState === 'visible') this.wakeUp();
    };
    document.addEventListener('visibilitychange', this.visibilityHandler);
  }

  /**
   * Unconditionally tear down and reopen the WebSocket.
   *
   * Unlike ``wakeUp`` (which short-circuits when the socket looks
   * healthy because the server is presumed to be pushing diffs), this
   * is the operator-driven "force everything" path used by the
   * top-header Resync button. We don't get to peek at server state
   * from here, so we just close and let ``connect`` rebuild —
   * subscribe frames + a fresh snapshot follow naturally.
   */
  forceReconnect(): void {
    if (this.destroyed) return;
    this.reconnectMs = MIN_RECONNECT_MS;
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    if (this.watchdogTimer) {
      clearInterval(this.watchdogTimer);
      this.watchdogTimer = null;
    }
    if (this.ws) {
      this.ws.onclose = null;
      try { this.ws.close(); } catch { /* already closed */ }
      this.ws = null;
    }
    this.connect();
  }

  /**
   * Disconnect and stop reconnecting.
   */
  stop(): void {
    this.destroyed = true;
    if (this.reconnectTimer) clearTimeout(this.reconnectTimer);
    if (this.pingTimer) clearInterval(this.pingTimer);
    if (this.watchdogTimer) clearInterval(this.watchdogTimer);
    if (this.visibilityHandler) {
      document.removeEventListener('visibilitychange', this.visibilityHandler);
      this.visibilityHandler = null;
    }
    if (this.ws) {
      this.ws.onclose = null; // Prevent reconnect
      this.ws.close();
      this.ws = null;
    }
  }

  private connect(): void {
    if (this.destroyed) return;

    // Stamp now so the watchdog doesn't fire immediately during the
    // TCP handshake / WS upgrade window before the server's snapshot
    // arrives. The first real message bumps this on its own.
    this.lastMessageAt = Date.now();

    try {
      this.ws = new WebSocket(WS_URL);
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.ws.onopen = () => {
      this.reconnectMs = MIN_RECONNECT_MS;
      this.onStatus?.(true);

      // Subscribe to all channels (guard against race)
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.send(JSON.stringify({
          type: 'subscribe',
          channels: CHANNELS,
        }));
        // Flush any command-output subscriptions queued before connect.
        for (const cmdId of this.pendingCmdSubscriptions) {
          this.ws.send(JSON.stringify({ type: 'subscribe_command_output', cmd_id: cmdId }));
        }
        this.pendingCmdSubscriptions.clear();
      }

      // Start ping keepalive every 25s
      this.pingTimer = setInterval(() => {
        if (this.ws?.readyState === WebSocket.OPEN) {
          this.ws.send(JSON.stringify({ type: 'ping' }));
        }
      }, 25000);

      // Message-arrival watchdog. The server's status/heartbeats diffs
      // arrive every few seconds in the worst case, so silence beyond
      // ``WS_STALE_THRESHOLD_MS`` while the tab is visible means the
      // pipe is dead even though ``readyState`` may still report OPEN
      // (zombie socket — see ``wakeUp`` docstring). Don't fire while
      // the tab is hidden: backgrounded tabs are throttled by the
      // browser and ``setInterval`` cadence is unreliable; visibility
      // wake will handle the reconnect when the tab returns.
      this.watchdogTimer = setInterval(() => {
        if (this.destroyed) return;
        if (document.visibilityState !== 'visible') return;
        if (Date.now() - this.lastMessageAt > WS_STALE_THRESHOLD_MS) {
          this.forceReconnect();
        }
      }, WS_WATCHDOG_INTERVAL_MS);
    };

    this.ws.onmessage = (event) => {
      this.lastMessageAt = Date.now();
      try {
        const msg: WSMessage = JSON.parse(event.data);
        if (msg.type === 'snapshot') {
          this.onSnapshot?.(msg.data);
        } else if (msg.type === 'diff') {
          this.onDiff?.(msg.channel, msg as WSDiff);
        } else if (msg.type === 'command_output') {
          const handler = this.cmdOutputHandlers.get(msg.cmd_id);
          handler?.(msg);
          if (msg.data?.type === 'done') {
            this.cmdOutputHandlers.delete(msg.cmd_id);
          }
        }
        // pong is silently ignored
      } catch {
        // Malformed message — ignore
      }
    };

    this.ws.onclose = () => {
      this.onStatus?.(false);
      if (this.pingTimer) clearInterval(this.pingTimer);
      if (this.watchdogTimer) clearInterval(this.watchdogTimer);
      this.scheduleReconnect();
    };

    this.ws.onerror = () => {
      // onclose will fire after onerror
    };
  }

  private scheduleReconnect(): void {
    if (this.destroyed) return;
    this.reconnectTimer = setTimeout(() => {
      this.connect();
    }, this.reconnectMs);
    this.reconnectMs = Math.min(this.reconnectMs * 2, MAX_RECONNECT_MS);
  }
}

/** Singleton instance */
export const wsManager = new WSManager();
